package com.syncap.sdk

import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import kotlinx.coroutines.runBlocking
import org.junit.Test
import java.net.InetSocketAddress
import java.nio.file.Files
import java.security.MessageDigest
import java.util.Collections
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class BindingTest {
    private class Fixture(private val handler: (HttpExchange) -> Unit) : AutoCloseable {
        val requests: MutableList<String> = Collections.synchronizedList(mutableListOf())
        private val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0).apply {
            createContext("/") { exchange ->
                requests.add("${exchange.requestMethod} ${exchange.requestURI.path}")
                exchange.requestBody.use { it.readBytes() }
                try { handler(exchange) } finally { exchange.close() }
            }
            start()
        }
        val endpoint: String get() = "http://127.0.0.1:${server.address.port}"
        override fun close() = server.stop(0)
    }

    private fun HttpExchange.reply(body: String, status: Int = 200) = reply(body.toByteArray(), status)
    private fun HttpExchange.reply(body: ByteArray, status: Int = 200) {
        responseHeaders.add("Content-Type", "application/json")
        sendResponseHeaders(status, body.size.toLong())
        responseBody.use { it.write(body) }
    }

    @Test fun invalidEndpointMapsStructuredException() {
        val failure = assertFailsWith<SdkException.Failure> { DeviceClient("http://user:password@localhost") }
        assertEquals(SdkErrorKind.INVALID_INPUT, failure.kind)
        assertFalse(failure.outcomeUnknown)
        assertFalse(failure.message.contains("password"))
    }

    @Test fun asyncCallPreservesDeviceTimeAndExtensions() = runBlocking {
        Fixture { it.reply("""{"state":"recording","captureId":"cap_test","sessionId":"ses_test","elapsedMs":3095,"startedAtDeviceTimeNs":"9007199254740993001","producerState":"running"}""") }.use { server ->
            DeviceClient(server.endpoint).use { client ->
                val capture = client.currentCapture()
                assertEquals("recording", capture.state)
                assertEquals(3095uL, capture.elapsedMs)
                assertEquals("9007199254740993001", capture.startedAtDeviceTimeNs)
                assertTrue(capture.rawJson.contains("producerState"))
            }
            assertEquals(listOf("GET /v1/captures/current"), server.requests)
        }
    }

    @Test fun deviceFailureDoesNotRetryStart() = runBlocking {
        Fixture { it.reply("""{"error":"producer.not_ready","message":"camera recovery needed"}""", 503) }.use { server ->
            DeviceClient(server.endpoint).use { client ->
                val failure = assertFailsWith<SdkException.Failure> { client.startCapture("binding-test") }
                assertEquals(SdkErrorKind.DEVICE, failure.kind)
                assertEquals(503u.toUShort(), failure.httpStatus)
                assertEquals("producer.not_ready", failure.deviceCode)
                assertTrue(failure.outcomeUnknown)
            }
            assertEquals(listOf("POST /v1/captures/start"), server.requests)
        }
    }

    @Test fun storageAndEmptySessionsUseGeneratedTypes() = runBlocking {
        Fixture { exchange ->
            when (exchange.requestURI.path) {
                "/v1/storage/configure" -> exchange.reply("""{"target":"usb","usb":{"canCapture":true,"mediaType":"sd"}}""")
                "/v1/sessions" -> exchange.reply("""{"sessions":[]}""")
                else -> exchange.reply("{}", 404)
            }
        }.use { server ->
            DeviceClient(server.endpoint).use { client ->
                assertTrue(client.configureStorageJson(StorageTarget.REMOVABLE).contains("mediaType"))
                assertTrue(client.sessions().isEmpty())
            }
        }
    }

    @Test fun explicitCancellationDoesNotContactDevice() = runBlocking {
        Fixture { it.reply("{}", 500) }.use { server ->
            DeviceClient(server.endpoint).use { client ->
                Cancellation().use { cancellation ->
                    cancellation.cancel()
                    assertTrue(cancellation.isCancelled())
                    val destination = Files.createTempDirectory("syncap-kotlin-cancel-").toFile()
                    try {
                        val failure = assertFailsWith<SdkException.Failure> {
                            client.exportSession("ses_test", destination.absolutePath, cancellation)
                        }
                        assertEquals(SdkErrorKind.CANCELLED, failure.kind)
                        assertTrue(server.requests.isEmpty())
                        assertTrue(destination.listFiles()!!.isEmpty())
                    } finally { destination.deleteRecursively() }
                }
            }
        }
    }

    @Test fun resumableExportTraversesKotlinAndNativeCore() = runBlocking {
        val payload = "camera-video-payload".toByteArray()
        val sha = MessageDigest.getInstance("SHA-256").digest(payload).joinToString("") { "%02x".format(it) }
        val entry = """{"name":"camera.mcap","sizeBytes":${payload.size},"sha256":"$sha","url":"/v1/sessions/ses_test/files/camera.mcap"}"""
        val originalManifest = """{"id":"ses_test","state":"complete","files":[$entry],"calibration":{"original":true}}"""
        var requestedRange: String? = null
        Fixture { exchange ->
            when (exchange.requestURI.path) {
                "/v1/sessions/ses_test/prepare-export" -> exchange.reply("""{"sessionId":"ses_test","files":[$entry]}""")
                "/v1/sessions/ses_test/manifest" -> exchange.reply(originalManifest)
                "/v1/sessions/ses_test/files/camera.mcap" -> {
                    requestedRange = exchange.requestHeaders.getFirst("Range")
                    exchange.responseHeaders.add("Content-Range", "bytes 5-${payload.size - 1}/${payload.size}")
                    exchange.reply(payload.copyOfRange(5, payload.size), 206)
                }
                else -> exchange.reply("{}", 404)
            }
        }.use { server ->
            val destination = Files.createTempDirectory("syncap-kotlin-export-").toFile()
            try {
                destination.resolve("camera.mcap.part").writeBytes(payload.copyOfRange(0, 5))
                DeviceClient(server.endpoint).use { client ->
                    val report = client.exportSession("ses_test", destination.absolutePath, null)
                    assertTrue(report.verified)
                    assertEquals((payload.size + originalManifest.toByteArray().size).toULong(), report.bytes)
                    assertEquals(2uL, report.files)
                    assertEquals(1uL, report.verifiedFiles)
                    assertEquals("bytes=5-", requestedRange)
                    assertContentEquals(payload, destination.resolve("camera.mcap").readBytes())
                    assertEquals(originalManifest, destination.resolve("session.json").readText())
                    assertFalse(destination.resolve("camera.mcap.part").exists())
                }
            } finally { destination.deleteRecursively() }
        }
    }
}
