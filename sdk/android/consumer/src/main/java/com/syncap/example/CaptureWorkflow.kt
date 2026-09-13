package com.syncap.example

import com.syncap.sdk.Cancellation
import com.syncap.sdk.CaptureStart
import com.syncap.sdk.DeviceClient
import com.syncap.sdk.ExportReport
import com.syncap.sdk.StorageTarget
import java.io.File

/** Example calls for a host application's explicit capture/export buttons. */
class CaptureWorkflow(private val device: DeviceClient) {
    // Serialize this complete operation with other storage/start/stop actions in the host.
    suspend fun start(name: String, target: StorageTarget): CaptureStart {
        // The shared core rejects a successful HTTP response unless the returned
        // selected target is correct and ready; do not duplicate wire parsing here.
        device.configureStorageJson(target)
        return device.startCapture(name)
    }

    suspend fun stopAndExport(captureId: String, exportRoot: File, cancellation: Cancellation): ExportReport {
        val stopped = device.stopCapture(captureId)
        check(stopped.state == "completed") { "Capture did not finish: ${stopped.state}" }
        // Use an app-owned real directory such as File(context.filesDir, "exports").
        // content:// document-tree URIs need a host-side SAF copy after verified export.
        return device.exportSession(stopped.sessionId, File(exportRoot, stopped.sessionId).absolutePath, cancellation)
    }
}
