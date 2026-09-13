@file:Suppress("DEPRECATION")
package com.syncap.sdk.bluetooth

import android.annotation.SuppressLint
import android.bluetooth.*
import android.bluetooth.le.*
import android.content.*
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

enum class BluetoothTransport { AUTO, GATT, RFCOMM }
data class DiscoveredDevice(val address: String, val name: String?, val bonded: Boolean, val rssi: Int?)
data class BluetoothReply(val transport: BluetoothTransport, val rawJson: String) {
    val state: String get() = JSONObject(rawJson).optString("state", "unknown")
}
class ProvisioningException(val outcomeUnknown: Boolean, message: String, cause: Throwable? = null) : IOException(message, cause)

/** One connection lifecycle. The host requests runtime permissions and owns system pairing UI. */
@SuppressLint("MissingPermission")
class ProvisioningClient(context: Context) : AutoCloseable {
    private val context = context.applicationContext
    private val adapter get() = (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
        ?.takeIf { it.isEnabled } ?: throw IllegalStateException("Bluetooth is unavailable or disabled")
    private val handler = Handler(Looper.getMainLooper())
    private val executor = Executors.newSingleThreadExecutor()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val serial = Mutex()
    private val closed = AtomicBoolean(false)

    fun pairedDevices(): List<DiscoveredDevice> {
        check(!closed.get()) { "Client is closed" }
        return adapter.bondedDevices.map { DiscoveredDevice(it.address, it.name, true, null) }
    }

    /** Explicit user action; Android shows/controls confirmation, SDK does not inject a PIN. */
    fun requestPairing(address: String): Boolean {
        check(!closed.get()) { "Client is closed" }
        require(BluetoothAdapter.checkBluetoothAddress(address))
        return adapter.getRemoteDevice(address).let { it.bondState == BluetoothDevice.BOND_BONDED || it.createBond() }
    }

    /** Includes paired devices, SynCap BLE advertisements, and Classic discovery results. */
    suspend fun discover(durationMs: Long = 10000): List<DiscoveredDevice> = owned {
        require(durationMs in 1000..30000)
        serial.withLock {
            val found = java.util.concurrent.ConcurrentHashMap<String, DiscoveredDevice>()
            pairedDevices().forEach { found[it.address] = it }
            val bluetooth = adapter
            val scanner = bluetooth.bluetoothLeScanner
            fun add(device: BluetoothDevice, name: String?, rssi: Int?) {
                found[device.address] = DiscoveredDevice(device.address, name ?: device.name,
                    device.bondState == BluetoothDevice.BOND_BONDED, rssi)
            }
            val scanFailure = java.util.concurrent.atomic.AtomicInteger(0)
            val callback = object : ScanCallback() {
                override fun onScanResult(type: Int, result: ScanResult) {
                    val name = result.scanRecord?.deviceName ?: result.device.name
                    if (name?.startsWith("SynCap", true) == true ||
                        result.scanRecord?.serviceUuids?.any { it.uuid == BluetoothProtocol.service } == true) {
                        add(result.device, name, result.rssi)
                    }
                }
                override fun onScanFailed(errorCode: Int) { scanFailure.set(errorCode) }
            }
            val receiver = object : BroadcastReceiver() {
                override fun onReceive(context: Context, intent: Intent) {
                    val device = intent.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE) ?: return
                    add(device, intent.getStringExtra(BluetoothDevice.EXTRA_NAME),
                        intent.getShortExtra(BluetoothDevice.EXTRA_RSSI, Short.MIN_VALUE).toInt().takeIf { it != Short.MIN_VALUE.toInt() })
                }
            }
            if (Build.VERSION.SDK_INT >= 33) context.registerReceiver(receiver, IntentFilter(BluetoothDevice.ACTION_FOUND), Context.RECEIVER_EXPORTED)
            else context.registerReceiver(receiver, IntentFilter(BluetoothDevice.ACTION_FOUND))
            var leStarted = false
            var classicStarted = false
            try {
                scanner?.startScan(callback)
                leStarted = scanner != null
                classicStarted = bluetooth.startDiscovery()
                delay(durationMs)
                if (!classicStarted && (scanner == null || scanFailure.get() != 0)) throw IOException("Bluetooth discovery did not start")
                found.values.sortedBy { it.name ?: it.address }
            } finally {
                if (leStarted) runCatching { scanner?.stopScan(callback) }
                if (classicStarted) runCatching { bluetooth.cancelDiscovery() }
                context.unregisterReceiver(receiver)
            }
        }
    }

    suspend fun scanWifi(address: String, transport: BluetoothTransport = BluetoothTransport.AUTO): BluetoothReply =
        execute(address, BluetoothProtocol.request("wifi.scan"), transport, 45000)
    suspend fun deviceStatus(address: String, transport: BluetoothTransport = BluetoothTransport.AUTO): BluetoothReply =
        execute(address, BluetoothProtocol.request("device.status"), transport, 25000)
    suspend fun configureWifi(address: String, ssid: String, password: String, security: String = "wpa2-psk",
        transport: BluetoothTransport = BluetoothTransport.AUTO): BluetoothReply =
        execute(address, BluetoothProtocol.wifi(ssid, password, security), transport, 120000)

    suspend fun configureStorage(address: String, target: String): BluetoothReply {
        require(target in setOf("internal", "usb")) { "Invalid storage target" }
        return execute(address, BluetoothProtocol.request("storage.configure", JSONObject().put("target", target)))
    }
    suspend fun ejectStorage(address: String): BluetoothReply = execute(address, BluetoothProtocol.request("storage.eject"))
    suspend fun startCapture(address: String, name: String): BluetoothReply {
        require(name.isNotBlank() && name.toByteArray(Charsets.UTF_8).size <= 160 && !name.contains('\u0000'))
        return execute(address, BluetoothProtocol.request("capture.start", JSONObject().put("name", name)))
    }
    suspend fun stopCapture(address: String, captureId: String): BluetoothReply {
        require(captureId.matches(Regex("[A-Za-z0-9][A-Za-z0-9._-]{0,95}")))
        return execute(address, BluetoothProtocol.request("capture.stop", JSONObject().put("captureId", captureId)))
    }

    private suspend fun <T> owned(block: suspend () -> T): T {
        check(!closed.get()) { "Client is closed" }
        val work = scope.async { block() }
        return try { work.await() } finally { work.cancel() }
    }

    private suspend fun execute(address: String, payload: ByteArray, transport: BluetoothTransport = BluetoothTransport.AUTO,
        timeoutMs: Long = 120000): BluetoothReply = owned {
        require(BluetoothAdapter.checkBluetoothAddress(address)) { "Invalid Bluetooth address" }
        serial.withLock {
            val device = adapter.getRemoteDevice(address)
            check(device.bondState == BluetoothDevice.BOND_BONDED) { "Pair the device in Android before sending commands" }
            val directSerial = device.type == BluetoothDevice.DEVICE_TYPE_CLASSIC ||
                (device.type == BluetoothDevice.DEVICE_TYPE_DUAL && device.uuids?.any { it.uuid == BluetoothProtocol.spp } == true)
            val deadline = SystemClock.elapsedRealtime() + timeoutMs
            if (transport == BluetoothTransport.RFCOMM || (transport == BluetoothTransport.AUTO && directSerial)) {
                rfcomm(device, payload, timeoutMs)
            } else {
                try { gatt(device, payload, timeoutMs) }
                catch (failure: ProvisioningException) {
                    val remaining = deadline - SystemClock.elapsedRealtime()
                    if (transport != BluetoothTransport.AUTO || failure.outcomeUnknown || remaining < 1000) throw failure
                    rfcomm(device, payload, remaining)
                }
            }
        }
    }

    private suspend fun rfcomm(device: BluetoothDevice, payload: ByteArray, timeoutMs: Long): BluetoothReply =
        suspendCancellableCoroutine { continuation ->
            val finished = AtomicBoolean(false)
            val wrote = AtomicBoolean(false)
            val socket = device.createRfcommSocketToServiceRecord(BluetoothProtocol.spp)
            var timer: Runnable? = null
            fun finish(reply: JSONObject?, error: Throwable?) {
                if (!finished.compareAndSet(false, true)) return
                timer?.let { handler.removeCallbacks(it) }
                runCatching { socket.close() }
                if (reply != null) continuation.resume(BluetoothReply(BluetoothTransport.RFCOMM, reply.toString()))
                else continuation.resumeWithException(ProvisioningException(wrote.get(), "Bluetooth command failed; query device state before retrying", error))
            }
            timer = Runnable { finish(null, IOException("Bluetooth operation timed out")) }
            continuation.invokeOnCancellation {
                if (finished.compareAndSet(false, true)) {
                    timer?.let { handler.removeCallbacks(it) }
                    runCatching { socket.close() }
                }
            }
            handler.postDelayed(timer!!, timeoutMs)
            try { executor.execute {
                try {
                    if (finished.get()) return@execute
                    adapter.cancelDiscovery()
                    socket.connect()
                    if (finished.get()) return@execute
                    wrote.set(true) // Once writing begins, no mutating-command retry or transport fallback.
                    BluetoothProtocol.write(socket.outputStream, payload)
                    finish(BluetoothProtocol.read(socket.inputStream), null)
                } catch (error: Exception) { finish(null, error) }
            } } catch (error: Exception) { finish(null, error) }
        }

    private suspend fun gatt(device: BluetoothDevice, payload: ByteArray, timeoutMs: Long): BluetoothReply =
        suspendCancellableCoroutine { continuation ->
            // Serialize all callbacks, cancellation and cleanup on Android's main looper.
            var connection: BluetoothGatt? = null
            var finished = false
            var wrote = false
            var mtu = 23
            var discovered = false
            var config: BluetoothGattCharacteristic? = null
            var status: BluetoothGattCharacteristic? = null
            var chunks = emptyList<ByteArray>()
            var index = 0
            val token = Any()
            fun later(delay: Long = 0, action: () -> Unit) {
                handler.postAtTime({ if (!finished) action() }, token, android.os.SystemClock.uptimeMillis() + delay)
            }
            fun cleanup() {
                finished = true
                handler.removeCallbacksAndMessages(token)
                runCatching { connection?.disconnect() }
                runCatching { connection?.close() }
                connection = null
            }
            fun fail(error: Throwable) {
                if (finished) return
                cleanup()
                continuation.resumeWithException(ProvisioningException(wrote, "BLE command failed; query device state before retrying", error))
            }
            fun read() {
                try { if (connection?.readCharacteristic(status) != true) fail(IOException("BLE status read rejected")) }
                catch (error: Exception) { fail(error) }
            }
            fun write() {
                try {
                    wrote = true
                    config!!.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                    config!!.value = chunks[index]
                    if (connection?.writeCharacteristic(config) != true) fail(IOException("BLE write rejected"))
                } catch (error: Exception) { fail(error) }
            }
            fun discover() {
                if (discovered) return
                discovered = true
                try { if (connection?.discoverServices() != true) fail(IOException("BLE service discovery rejected")) }
                catch (error: Exception) { fail(error) }
            }
            fun response(bytes: ByteArray, result: Int) {
                if (result != BluetoothGatt.GATT_SUCCESS) { fail(IOException("BLE read failed: $result")); return }
                try {
                    val value = BluetoothProtocol.json(bytes)
                    if (value.optString("state") in setOf("working", "connecting")) later(1200) { read() }
                    else { cleanup(); continuation.resume(BluetoothReply(BluetoothTransport.GATT, value.toString())) }
                } catch (error: Exception) { fail(error) }
            }
            val callback = object : BluetoothGattCallback() {
                override fun onConnectionStateChange(g: BluetoothGatt, result: Int, state: Int) = later {
                    if (g !== connection) return@later
                    if (result != BluetoothGatt.GATT_SUCCESS || state == BluetoothProfile.STATE_DISCONNECTED) {
                        fail(IOException("BLE disconnected: $result"))
                    } else if (state == BluetoothProfile.STATE_CONNECTED) {
                        try {
                            if (!g.requestMtu(512)) discover()
                            else later(3000) { if (!discovered) fail(IOException("BLE MTU negotiation timed out")) }
                        } catch (error: Exception) { fail(error) }
                    }
                }
                override fun onMtuChanged(g: BluetoothGatt, value: Int, result: Int) = later {
                    if (g !== connection) return@later
                    if (result == BluetoothGatt.GATT_SUCCESS) mtu = value
                    discover()
                }
                override fun onServicesDiscovered(g: BluetoothGatt, result: Int) = later {
                    if (g !== connection) return@later
                    try {
                        val service = g.getService(BluetoothProtocol.service)
                        config = service?.getCharacteristic(BluetoothProtocol.config)
                        status = service?.getCharacteristic(BluetoothProtocol.status)
                        if (result != BluetoothGatt.GATT_SUCCESS || config == null || status == null) {
                            fail(IOException("SynCap BLE service unavailable")); return@later
                        }
                        chunks = BluetoothProtocol.fragments(payload, mtu, System.nanoTime().toInt())
                        write()
                    } catch (error: Exception) { fail(error) }
                }
                override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, result: Int) = later {
                    if (g !== connection || c.uuid != BluetoothProtocol.config) return@later
                    if (result != BluetoothGatt.GATT_SUCCESS) fail(IOException("BLE write failed: $result"))
                    else if (++index < chunks.size) write() else later(1000) { read() }
                }
                override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray, result: Int) = later {
                    if (g === connection && c.uuid == BluetoothProtocol.status) response(value, result)
                }
                override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, result: Int) {
                    if (Build.VERSION.SDK_INT < 33) onCharacteristicRead(g, c, c.value ?: byteArrayOf(), result)
                }
            }
            continuation.invokeOnCancellation { handler.post { if (!finished) cleanup() } }
            later {
                if (!continuation.isActive) { cleanup(); return@later }
                try {
                    connection = device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
                    if (connection == null) fail(IOException("Unable to create BLE connection"))
                    later(12000) { if (!wrote) fail(IOException("BLE connection setup timed out")) }
                    later(timeoutMs) { fail(IOException("BLE operation timed out")) }
                } catch (error: Exception) { fail(error) }
            }
        }

    override fun close() {
        if (!closed.compareAndSet(false, true)) return
        scope.cancel()
        executor.shutdownNow()
    }
}
