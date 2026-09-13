package com.syncap.sdk.bluetooth

import java.io.InputStream
import java.io.OutputStream
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.util.UUID
import org.json.JSONObject
import org.json.JSONTokener

internal object BluetoothProtocol {
    const val MAX_BYTES = 4096
    val service: UUID = UUID.fromString("8f7a0001-6c2b-4dd4-9f1a-53f65c9b40d1")
    val config: UUID = UUID.fromString("8f7a0002-6c2b-4dd4-9f1a-53f65c9b40d1")
    val status: UUID = UUID.fromString("8f7a0003-6c2b-4dd4-9f1a-53f65c9b40d1")
    val spp: UUID = UUID.fromString("00001101-0000-1000-8000-00805f9b34fb")

    fun json(bytes: ByteArray): JSONObject {
        require(bytes.size in 1..MAX_BYTES) { "Invalid Bluetooth payload size" }
        val text = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
        require(singleObject(text)) { "Expected one JSON object without trailing data" }
        val parser = JSONTokener(text)
        val value = parser.nextValue()
        require(value is JSONObject && parser.nextClean() == '\u0000') { "Expected one JSON object" }
        return value
    }

    private fun singleObject(text: String): Boolean {
        val source = text.trim { it == ' ' || it == '\r' || it == '\n' || it == '\t' }
        if (!source.startsWith('{')) return false
        var depth = 0
        var quoted = false
        var escaped = false
        for ((index, char) in source.withIndex()) {
            if (quoted) {
                if (char < ' ') return false
                if (escaped) escaped = false else if (char == '\\') escaped = true else if (char == '"') quoted = false
            } else when (char) {
                '"' -> quoted = true
                '{' -> depth++
                '}' -> { depth--; if (depth == 0) return index == source.lastIndex }
                '\u0000' -> return false
            }
        }
        return false
    }

    fun write(output: OutputStream, bytes: ByteArray) {
        json(bytes)
        output.write(ByteBuffer.allocate(4).putInt(bytes.size).array())
        output.write(bytes)
        output.flush()
    }

    fun read(input: InputStream): JSONObject {
        fun fully(size: Int): ByteArray = ByteArray(size).also { bytes ->
            var offset = 0
            while (offset < size) {
                val count = input.read(bytes, offset, size - offset)
                if (count <= 0) throw IOException("Truncated Bluetooth response")
                offset += count
            }
        }
        val size = ByteBuffer.wrap(fully(4)).int
        require(size in 1..MAX_BYTES) { "Invalid Bluetooth frame size" }
        return json(fully(size))
    }

    fun fragments(bytes: ByteArray, mtu: Int, id: Int): List<ByteArray> {
        json(bytes)
        require(mtu in 23..517)
        val size = mtu - 19
        val count = (bytes.size + size - 1) / size
        require(count in 1..128) { "Command exceeds negotiated BLE packet limit" }
        return (0 until count).map { index ->
            val start = index * size
            val length = minOf(size, bytes.size - start)
            ByteBuffer.allocate(16 + length).put(0x53).put(0x43).put(1).put(1)
                .putInt(id).putShort(index.toShort()).putShort(count.toShort())
                .putShort(length.toShort()).putShort(0).put(bytes, start, length).array()
        }
    }

    fun request(op: String, fields: JSONObject = JSONObject()): ByteArray {
        require(op in setOf("wifi.scan", "wifi.configure", "device.status", "storage.configure",
            "storage.eject", "capture.start", "capture.stop")) { "Unsupported offline operation" }
        // Compatibility field only, never a user-entered code or an authentication secret.
        val value = JSONObject(fields.toString()).put("op", op).put("claimCode", "123456")
        return value.toString().toByteArray(Charsets.UTF_8).also { json(it) }
    }

    fun wifi(ssid: String, password: String, security: String): ByteArray {
        require(ssid.toByteArray(Charsets.UTF_8).size in 1..32 && !ssid.contains('\u0000')) { "Invalid SSID" }
        require(security in setOf("open", "wpa2-psk", "wpa3-sae")) { "Unsupported Wi-Fi security" }
        require(if (security == "open") password.isEmpty() else
            password.toByteArray(Charsets.UTF_8).size in 8..63 && !password.contains('\u0000')) { "Invalid Wi-Fi password" }
        return request("wifi.configure", JSONObject().put("ssid", ssid).put("password", password).put("security", security))
    }
}
