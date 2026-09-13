package com.syncap.example

import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.os.SystemClock
import androidx.test.platform.app.InstrumentationRegistry
import com.syncap.sdk.DeviceClient
import com.syncap.studio.LivePreviewActivity
import com.syncap.studio.SynCapMedia
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test

/** Opt-in real rendering check using the published media AAR, not an RTSP/socket probe. */
class LivePreviewTest {
    private var visibleHost: android.app.Activity? = null
    @org.junit.After fun finishHost() {
        visibleHost?.let { host -> InstrumentationRegistry.getInstrumentation().runOnMainSync { host.finish() } }
    }
    @Test fun eachCameraContinuesRenderingAndReleases() = runBlocking {
        val args = InstrumentationRegistry.getArguments()
        assumeTrue(args.getString("syncap.allowPreview") == "true")
        val observationSeconds = (args.getString("syncap.previewSeconds") ?: "12").toInt()
        require(observationSeconds in 12..120)
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        assertFalse("Unlock the phone for the rendering test",
            (context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager).isDeviceLocked)
        visibleHost = instrumentation.startActivitySync(Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        instrumentation.runOnMainSync { visibleHost!!.window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON) }
        val manifest = DeviceClient(requireNotNull(args.getString("syncap.endpoint"))).use {
            assertEquals("idle", it.currentCapture().state)
            JSONObject(it.manifestJson())
        }
        val cameras = manifest.getJSONArray("cameras")
        val urls = (0 until cameras.length()).map { cameras.getJSONObject(it).getJSONObject("preview").getString("url") }
        val labels = (0 until cameras.length()).map { cameras.getJSONObject(it).getString("id") }
        val activity = instrumentation.startActivitySync(SynCapMedia.previewIntent(context, urls, labels)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as LivePreviewActivity
        fun counters(): List<Int> {
            var result = emptyList<Int>()
            instrumentation.runOnMainSync {
                val started = LivePreviewActivity::class.java.getDeclaredField("activityStarted").apply { isAccessible = true }
                assertTrue("Preview test interrupted: the preview left the foreground", started.getBoolean(activity))
                val field = LivePreviewActivity::class.java.getDeclaredField("sessions").apply { isAccessible = true }
                result = (field.get(activity) as List<*>).map { session ->
                    val count = session!!.javaClass.getDeclaredField("lastDisplayedPictures").apply { isAccessible = true }
                    count.getInt(session)
                }
            }
            return result
        }
        fun reportStage(stage: String) {
            val details = JSONArray()
            instrumentation.runOnMainSync {
                val field = LivePreviewActivity::class.java.getDeclaredField("sessions").apply { isAccessible = true }
                (field.get(activity) as List<*>).forEach { session ->
                    val item = JSONObject()
                    for (name in listOf("lastDisplayedPictures", "lastDecodedVideo", "hasRenderedFrame", "live", "retryExhausted")) {
                        item.put(name, session!!.javaClass.getDeclaredField(name).apply { isAccessible = true }.get(session))
                    }
                    val state = session!!.javaClass.getDeclaredField("stateView").apply { isAccessible = true }.get(session) as android.widget.TextView
                    item.put("uiState", state.text.toString())
                    details.put(item)
                }
            }
            instrumentation.sendStatus(0, android.os.Bundle().apply {
                putString("sdkPreviewStage", JSONObject().put("stage", stage).put("cameras", details).toString())
            })
        }
        try {
            val deadline = SystemClock.elapsedRealtime() + 15000
            var initial = counters()
            while ((initial.size != urls.size || initial.any { it < 1 }) && SystemClock.elapsedRealtime() < deadline) {
                Thread.sleep(500)
                initial = counters()
            }
            reportStage("startup")
            assertEquals(urls.size, initial.size)
            assertTrue("A camera never rendered its first frame: $initial", initial.all { it > 0 })
            var previous = initial
            var stagnant = 0
            repeat(observationSeconds) {
                Thread.sleep(1000)
                val current = counters()
                assertEquals(urls.size, current.size)
                stagnant = if (current.indices.any { current[it] <= previous[it] }) stagnant + 1 else 0
                assertTrue("A camera stopped rendering: $current", stagnant < 4)
                previous = current
            }
            assertTrue(previous.indices.all { previous[it] - initial[it] >= 10 })
            instrumentation.sendStatus(0, android.os.Bundle().apply {
                putString("sdkPreview", JSONObject().put("initialDisplayed", JSONArray(initial))
                    .put("finalDisplayed", JSONArray(previous)).put("observationSeconds", observationSeconds).toString())
            })
        } finally {
            instrumentation.runOnMainSync { activity.finish() }
        }
        val gate = requireNotNull(LivePreviewActivity::class.java.getDeclaredField("ENGINE_GATE").apply { isAccessible = true }.get(null))
        val lease = LivePreviewActivity::class.java.getDeclaredField("engineLeased").apply { isAccessible = true }
        val deadline = SystemClock.elapsedRealtime() + 10000
        while (synchronized(gate) { lease.getBoolean(null) } && SystemClock.elapsedRealtime() < deadline) Thread.sleep(100)
        assertFalse("Preview engine did not release after close", synchronized(gate) { lease.getBoolean(null) })
    }
}
