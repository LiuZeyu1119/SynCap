package com.syncap.sdk.bluetooth

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ProvisioningTest {
    @Test fun framingRoundTripAndBounds() {
        val payload = BluetoothProtocol.request("wifi.scan")
        val stream = ByteArrayOutputStream()
        BluetoothProtocol.write(stream, payload)
        assertEquals("wifi.scan", BluetoothProtocol.read(ByteArrayInputStream(stream.toByteArray())).getString("op"))
        assertThrows(Exception::class.java) { BluetoothProtocol.read(ByteArrayInputStream(byteArrayOf(0, 0))) }
        assertThrows(Exception::class.java) { BluetoothProtocol.read(ByteArrayInputStream(ByteBuffer.allocate(4).putInt(4097).array())) }
        assertThrows(Exception::class.java) { BluetoothProtocol.read(ByteArrayInputStream(stream.toByteArray().dropLast(1).toByteArray())) }
    }

    @Test fun rejectsTrailingDataAndMalformedEncoding() {
        for (value in listOf("{}{}", "{}\u0000{}", "[]", "null", "{}garbage", "")) {
            assertThrows(Exception::class.java) { BluetoothProtocol.json(value.toByteArray()) }
        }
        assertThrows(Exception::class.java) { BluetoothProtocol.json(byteArrayOf(123, 0xc0.toByte(), 125)) }
        assertEquals("quoted } brace", BluetoothProtocol.json("{\"value\":\"quoted } brace\"} \n".toByteArray()).getString("value"))
    }

    @Test fun fragmentsReassembleWithoutTruncation() {
        val payload = BluetoothProtocol.wifi("测试 Wi-Fi", "test-only-password", "wpa2-psk")
        for (mtu in listOf(23, 100, 512, 517)) {
            val chunks = BluetoothProtocol.fragments(payload, mtu, 123)
            val output = ByteArrayOutputStream()
            chunks.forEachIndexed { index, value ->
                val buffer = ByteBuffer.wrap(value)
                assertEquals(0x53430101, buffer.int)
                assertEquals(123, buffer.int)
                assertEquals(index, buffer.short.toInt())
                assertEquals(chunks.size, buffer.short.toInt())
                assertEquals(value.size - 16, buffer.short.toInt())
                assertEquals(0, buffer.short.toInt())
                assertTrue(value.size <= mtu - 3)
                output.write(value, 16, value.size - 16)
            }
            assertArrayEquals(payload, output.toByteArray())
        }
    }

    @Test fun wifiValidationAndCompatibilityField() {
        assertThrows(Exception::class.java) { BluetoothProtocol.wifi("中".repeat(11), "password", "wpa2-psk") }
        assertThrows(Exception::class.java) { BluetoothProtocol.wifi("valid", "short", "wpa2-psk") }
        assertThrows(Exception::class.java) { BluetoothProtocol.wifi("valid", "password", "wep") }
        val open = BluetoothProtocol.json(BluetoothProtocol.wifi("Open Wi-Fi", "", "open"))
        assertEquals("123456", open.getString("claimCode"))
        assertThrows(Exception::class.java) { BluetoothProtocol.request("device.reboot") }
    }

    @Test fun closedClientRejectsWork() = runBlocking {
        val client = ProvisioningClient(InstrumentationRegistry.getInstrumentation().targetContext)
        client.close()
        client.close()
        assertThrows(IllegalStateException::class.java) { client.pairedDevices() }
        try { client.deviceStatus("00:11:22:33:44:55"); fail("Closed client accepted work") }
        catch (_: IllegalStateException) { }
    }

    /** Explicit hardware test. Never reconfigures Wi-Fi unless allowProvision=true and credentials are supplied. */
    @Test fun livePairedDevice() = runBlocking {
        val args = InstrumentationRegistry.getArguments()
        assumeTrue(args.getString("syncap.allowBluetooth") == "true")
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        fun granted() = PermissionActivity.requiredPermissions().all {
            context.checkSelfPermission(it) == android.content.pm.PackageManager.PERMISSION_GRANTED
        }
        val host = instrumentation.startActivitySync(android.content.Intent(context, PermissionActivity::class.java)
            .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK))
        val power = context.getSystemService(android.content.Context.POWER_SERVICE) as android.os.PowerManager
        val wake = power.newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, "SynCapSDK:BluetoothTest")
        wake.acquire(240000L)
        if (!granted()) {
            val deadline = android.os.SystemClock.elapsedRealtime() + 60000
            while (!granted() && android.os.SystemClock.elapsedRealtime() < deadline) kotlinx.coroutines.delay(250)
        }
        try {
        assertTrue("Allow nearby devices in the Android permission dialog", granted())
        ProvisioningClient(InstrumentationRegistry.getInstrumentation().targetContext).use { client ->
            val candidates = client.pairedDevices().filter { it.name?.startsWith("SynCap", true) == true }
            val address = args.getString("syncap.bluetoothAddress") ?: candidates.single().address
            val status = client.deviceStatus(address)
            report("status", status.transport.toString(), JSONObject(status.rawJson))
            val scan = client.scanWifi(address)
            val json = JSONObject(scan.rawJson)
            assertEquals("completed", json.getString("state"))
            assertTrue(json.getJSONArray("networks").length() > 0)
            report("wifi.scan", scan.transport.toString(), JSONObject().put("networks", json.getJSONArray("networks").length()))
            if (args.getString("syncap.allowProvision") == "true") {
                val ssid = requireNotNull(args.getString("syncap.ssid"))
                val password = requireNotNull(args.getString("syncap.password"))
                val reply = client.configureWifi(address, ssid, password)
                val configured = JSONObject(reply.rawJson)
                assertEquals("connected", configured.getString("state"))
                assertEquals(ssid, configured.getString("ssid"))
                assertTrue(configured.getString("ipAddress").isNotEmpty())
                report("wifi.configure", reply.transport.toString(), JSONObject().put("state", "connected").put("ipAddress", configured.getString("ipAddress")))
            }
        }
        } finally {
            if (wake.isHeld) wake.release()
            instrumentation.runOnMainSync { host.finish() }
        }
    }

    private fun report(operation: String, transport: String, detail: JSONObject) {
        InstrumentationRegistry.getInstrumentation().sendStatus(0, android.os.Bundle().apply {
            putString("sdkBluetooth", JSONObject().put("operation", operation).put("transport", transport).put("detail", detail).toString())
        })
    }
}
