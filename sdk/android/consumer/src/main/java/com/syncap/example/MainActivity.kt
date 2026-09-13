package com.syncap.example

import android.app.Activity
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.syncap.sdk.DeviceClient
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/** Independent, read-only integration example. No discovery or hardcoded device address. */
class MainActivity : Activity() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val endpoint = EditText(this).apply {
            hint = "Device origin, e.g. http://192.168.1.12:8080"
            setSingleLine()
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI
        }
        val result = TextView(this).apply { setTextIsSelectable(true) }
        val query = Button(this).apply { text = "Read device status" }
        val preview = Button(this).apply { text = "Open native preview" }
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 64, 32, 32)
            addView(endpoint)
            addView(query)
            addView(preview)
            addView(result)
        }
        setContentView(ScrollView(this).apply { addView(content) })
        query.setOnClickListener {
            query.isEnabled = false
            scope.launch {
                try {
                    DeviceClient(endpoint.text.toString().trim()).use { device ->
                        val capture = device.currentCapture()
                        val sessions = device.sessions()
                        result.text = "Capture: ${capture.state}\nElapsed (device): ${capture.elapsedMs ?: "unknown"} ms\nSessions: ${sessions.size}\n\n${capture.rawJson}"
                    }
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (error: Exception) {
                    result.text = error.message ?: error.javaClass.simpleName
                } finally {
                    query.isEnabled = true
                }
            }
        }
        preview.setOnClickListener {
            preview.isEnabled = false
            scope.launch {
                try {
                    DeviceClient(endpoint.text.toString().trim()).use { device ->
                        val cameras = org.json.JSONObject(device.manifestJson()).getJSONArray("cameras")
                        val urls = (0 until cameras.length()).map { cameras.getJSONObject(it).getJSONObject("preview").getString("url") }
                        val labels = (0 until cameras.length()).map { cameras.getJSONObject(it).optString("direction", cameras.getJSONObject(it).getString("id")) }
                        startActivity(com.syncap.studio.SynCapMedia.previewIntent(this@MainActivity, urls, labels))
                    }
                } catch (cancelled: CancellationException) { throw cancelled }
                catch (error: Exception) { result.text = error.message ?: error.javaClass.simpleName }
                finally { preview.isEnabled = true }
            }
        }
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }
}
