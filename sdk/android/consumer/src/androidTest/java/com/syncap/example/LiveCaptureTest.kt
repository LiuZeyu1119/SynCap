package com.syncap.example

import android.app.Activity
import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.PowerManager
import android.os.SystemClock
import android.view.WindowManager
import androidx.test.platform.app.InstrumentationRegistry
import com.syncap.sdk.CaptureStart
import com.syncap.sdk.DeviceClient
import com.syncap.sdk.ExportReport
import com.syncap.sdk.StorageTarget
import java.io.File
import java.security.MessageDigest
import java.util.UUID
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/** Explicit opt-in hardware tests. Never reboot, clear recovery state, or delete recordings. */
class LiveCaptureTest {
    private var visibleActivity: Activity? = null
    private var testWakeLock: PowerManager.WakeLock? = null

    @After fun closeTestActivity() {
        testWakeLock?.let { if (it.isHeld) it.release() }
        visibleActivity?.let { activity ->
            InstrumentationRegistry.getInstrumentation().runOnMainSync { activity.finish() }
        }
    }

    @Test fun queryAfterCoroutineDelay() = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        val endpoint = arguments.getString("syncap.endpoint")
        assumeTrue(endpoint != null)
        DeviceClient(endpoint!!).use { device ->
            emit(JSONObject().put("event", "query-before-delay").put("state", device.currentCapture().state))
            delay(1000)
            emit(JSONObject().put("event", "delay-completed"))
            emit(JSONObject().put("event", "query-after-delay").put("state", device.currentCapture().state))
        }
    }

    @Test fun recoveryStateIsPreserved() = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(arguments.getString("syncap.expectRecovery") == "true")
        val endpoint = requireNotNull(arguments.getString("syncap.endpoint"))
        DeviceClient(endpoint).use { device ->
            val state = device.currentCapture()
            assertEquals("failed", state.state)
            assertTrue(JSONObject(state.rawJson).getBoolean("recoveryRequired"))
            assertTrue(JSONObject(state.rawJson).getBoolean("rebootRequired"))
            emit(JSONObject().put("event", "recovery-preserved").put("capture", JSONObject(state.rawJson)))
        }
    }

    @Test fun captureStopExportAndResume() = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(arguments.getString("syncap.allowCapture") == "true")
        val endpoint = requireNotNull(arguments.getString("syncap.endpoint"))
        val seconds = (arguments.getString("syncap.captureSeconds") ?: "30").toInt()
        require(seconds in 10..120)
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        val keyguard = context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        val locked = keyguard.isDeviceLocked
        if (!locked) {
            val activity = instrumentation.startActivitySync(
                Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            visibleActivity = activity
            instrumentation.runOnMainSync {
                activity.window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            }
        }
        val power = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        testWakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "SynCapSDK:HardwareTest").apply {
            acquire(240000L)
        }
        val testId = "sdk-live-${UUID.randomUUID()}"
        val root = File(context.filesDir, testId).apply { check(mkdir()) }
        val events = JSONArray()
        fun record(event: String, data: JSONObject) {
            val item = JSONObject().put("event", event).put("phoneMonotonicMs", SystemClock.elapsedRealtime())
                .put("data", data)
            events.put(item)
            File(root, "evidence.json").writeText(JSONObject().put("events", events).toString(2))
            emit(item)
        }

        record("host-execution", JSONObject().put("screenLocked", locked).put("boundedCpuWakeLockMs", 240000))

        DeviceClient(endpoint).use { device ->
            var ownCapture: CaptureStart? = null
            var stopAttempted = false
            try {
                val before = device.currentCapture()
                record("before", JSONObject(before.rawJson))
                assertEquals("The test must not interrupt an existing capture or bypass recovery", "idle", before.state)
                val storage = JSONObject(device.configureStorageJson(StorageTarget.REMOVABLE))
                record("storage", storage)
                val selected = storage.getJSONObject("usb")
                assertTrue(selected.getBoolean("canCapture"))
                // Leave the device's reserve plus a conservative budget for this test.
                val minimumFree = storage.optJSONObject("capturePolicy")?.optLong("minimumFreeBytes", 536870912L)
                    ?: 536870912L
                assertTrue("Insufficient free space for the test", selected.getLong("freeBytes") > minimumFree + seconds * 4000000L)

                val started = device.startCapture(testId)
                ownCapture = started
                record("started", JSONObject(started.rawJson))
                assertEquals("recording", started.state)
                var previousElapsed = started.elapsedMs ?: 0uL
                var previousBytes = 0L
                var growthSamples = 0
                val deadline = SystemClock.elapsedRealtime() + seconds * 1000L
                while (SystemClock.elapsedRealtime() < deadline) {
                    delay(5000)
                    record("poll-request", JSONObject())
                    val current = device.currentCapture()
                    val raw = JSONObject(current.rawJson)
                    record("recording", raw)
                    assertEquals(started.captureId, current.captureId)
                    assertEquals("recording", current.state)
                    val elapsed = requireNotNull(current.elapsedMs)
                    assertTrue("Device timer did not advance", elapsed > previousElapsed)
                    val bytes = raw.getLong("sizeBytes")
                    assertTrue("Recorded byte count went backwards", bytes >= previousBytes)
                    if (bytes > previousBytes) growthSamples++
                    previousElapsed = elapsed
                    previousBytes = bytes
                }
                assertTrue("Recording never produced growing nonzero data", previousBytes > 0 && growthSamples >= 2)
                stopAttempted = true
                val stopped = device.stopCapture(started.captureId)
                record("stopped", JSONObject(stopped.rawJson))
                assertEquals("completed", stopped.state)
                assertEquals(started.sessionId, stopped.sessionId)
                val session = device.sessions().first { it.id == started.sessionId }
                // Older firmware omits this optional hint; actual export must still verify its full inventory.
                assertTrue("Device explicitly marked the session unavailable", session.exportAvailable != false)
                assertTrue(requireNotNull(session.sizeBytes) > 0uL)

                val destination = File(root, "export")
                val report = device.exportSession(session.id, destination.absolutePath, null)
                val manifest = verifyExport(destination, report)
                record("export-verified", JSONObject().put("sessionId", session.id).put("path", report.path)
                    .put("bytes", report.bytes.toString()).put("verifiedFiles", report.verifiedFiles.toString()))

                // Simulate an interrupted download in a NEW directory, never truncate originals.
                val files = manifest.getJSONArray("files")
                val first = files.getJSONObject(0)
                val source = File(destination, first.getString("name"))
                val prefixBytes = minOf(131071L, source.length() / 3).toInt()
                assertTrue(prefixBytes > 0)
                val resumeDirectory = File(root, "resume").apply { check(mkdir()) }
                source.inputStream().use { input ->
                    File(resumeDirectory, "${source.name}.part").outputStream().use { output ->
                        val buffer = ByteArray(prefixBytes)
                        var offset = 0
                        while (offset < buffer.size) {
                            val count = input.read(buffer, offset, buffer.size - offset)
                            check(count > 0)
                            offset += count
                        }
                        output.write(buffer)
                    }
                }
                val resumed = device.exportSession(session.id, resumeDirectory.absolutePath, null)
                verifyExport(resumeDirectory, resumed)
                record("resume-verified", JSONObject().put("seedBytes", prefixBytes).put("path", resumed.path)
                    .put("verifiedFiles", resumed.verifiedFiles.toString()).put("simulatedPartial", true))
                record("complete", JSONObject().put("sessionId", session.id).put("path", root.absolutePath))
            } catch (error: Throwable) {
                record("failed", JSONObject().put("type", error.javaClass.name).put("message", error.message))
                throw error
            } finally {
                // Only stop a capture whose returned ID belongs to this test, and send stop once.
                val owned = ownCapture
                if (owned != null && !stopAttempted) {
                    val current = device.currentCapture()
                    record("cleanup-state", JSONObject(current.rawJson))
                    if (current.state == "recording" && current.captureId == owned.captureId) {
                        record("cleanup-stop", JSONObject(device.stopCapture(owned.captureId).rawJson))
                    }
                }
            }
        }
    }

    private fun verifyExport(destination: File, report: ExportReport): JSONObject {
        assertTrue(report.verified)
        val manifest = JSONObject(File(destination, "session.json").readText())
        assertEquals(report.sessionId, manifest.getString("id"))
        val files = manifest.getJSONArray("files")
        assertTrue(files.length() > 0)
        assertEquals(files.length().toULong(), report.verifiedFiles)
        for (index in 0 until files.length()) {
            val item = files.getJSONObject(index)
            val file = File(destination, item.getString("name"))
            assertTrue("Nonpositive recorded file: ${file.name}", file.length() > 0)
            assertEquals(item.getLong("sizeBytes"), file.length())
            val hash = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { input ->
                val buffer = ByteArray(65536)
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    hash.update(buffer, 0, count)
                }
            }
            assertEquals(item.getString("sha256"), hash.digest().joinToString("") { "%02x".format(it) })
        }
        return manifest
    }

    private fun emit(message: JSONObject) {
        InstrumentationRegistry.getInstrumentation().sendStatus(0, Bundle().apply {
            putString("stream", "\n$message\n")
        })
    }
}
