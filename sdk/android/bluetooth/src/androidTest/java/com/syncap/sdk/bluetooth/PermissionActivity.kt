package com.syncap.sdk.bluetooth

import android.app.Activity
import android.os.Bundle

/** Test host only. Permission is granted through Android UI, never by the SDK. */
class PermissionActivity : Activity() {
    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(android.widget.TextView(this).apply { text = "SynCap SDK Bluetooth test: please allow nearby devices." })
        requestPermissions(requiredPermissions(), 1)
    }

    companion object {
        fun requiredPermissions(): Array<String> = if (android.os.Build.VERSION.SDK_INT >= 31)
            arrayOf(android.Manifest.permission.BLUETOOTH_SCAN, android.Manifest.permission.BLUETOOTH_CONNECT)
        else arrayOf(android.Manifest.permission.ACCESS_FINE_LOCATION)
    }
}
