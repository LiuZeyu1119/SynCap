package com.syncap.example

import android.os.Bundle
import androidx.test.platform.app.InstrumentationRegistry
import com.syncap.sdk.DeviceClient
import java.io.File
import java.security.MessageDigest
import java.util.UUID
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeNotNull
import org.junit.Test

/** Opt-in only. Reads a named, existing device session; never starts/stops capture. */
class LiveDeviceSmokeTest {
    @Test fun inspectAndExportExistingSession() = runBlocking {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val arguments = InstrumentationRegistry.getArguments()
        val endpoint = arguments.getString("syncap.endpoint")
        val sessionId = arguments.getString("syncap.sessionId")
        assumeNotNull(endpoint, sessionId)
        val destination = File(instrumentation.targetContext.cacheDir, "sdk-live-${UUID.randomUUID()}")
        DeviceClient(endpoint!!).use { device ->
            val state = device.currentCapture()
            val session = device.sessions().first { it.id == sessionId }
            assertTrue(session.exportAvailable != false)
            val report = device.exportSession(sessionId!!, destination.absolutePath, null)
            assertTrue(report.verified)
            assertEquals(sessionId, report.sessionId)
            val saved = JSONObject(File(destination, "session.json").readText())
            assertEquals(sessionId, saved.getString("id"))
            val files = saved.getJSONArray("files")
            assertEquals(files.length().toULong(), report.verifiedFiles)
            for (index in 0 until files.length()) {
                val item = files.getJSONObject(index)
                val file = File(destination, item.getString("name"))
                assertTrue(file.length() > 0)
                assertEquals(item.getLong("sizeBytes"), file.length())
                val hash = MessageDigest.getInstance("SHA-256")
                file.inputStream().use { stream ->
                    val buffer = ByteArray(65536)
                    while (true) {
                        val count = stream.read(buffer)
                        if (count < 0) break
                        hash.update(buffer, 0, count)
                    }
                }
                assertEquals(item.getString("sha256"), hash.digest().joinToString("") { "%02x".format(it) })
            }
            val summary = JSONObject().put("state", state.state).put("sessionId", sessionId)
                .put("path", destination.absolutePath).put("verifiedFiles", files.length())
                .put("bytes", report.bytes.toString()).put("verified", true)
            instrumentation.sendStatus(0, Bundle().apply { putString("stream", "\n$summary\n") })
        }
    }
}
