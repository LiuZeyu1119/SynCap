package com.syncap.example

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.syncap.sdk.Cancellation
import com.syncap.sdk.DeviceClient
import com.syncap.sdk.SdkErrorKind
import com.syncap.sdk.SdkException
import com.syncap.sdk.StorageTarget
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.BufferedInputStream
import java.io.File
import java.net.InetAddress
import java.net.ServerSocket
import java.net.SocketException
import java.security.MessageDigest
import java.util.Collections
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Exercises the Android AAR's actual JNA/native library, without the App bridge. */
@RunWith(AndroidJUnit4::class)
class NativeSdkTest {
    private data class Request(
        val method: String,
        val path: String,
        val headers: Map<String, String>,
        val body: String,
    )

    private data class Reply(
        val body: ByteArray,
        val status: Int = 200,
        val headers: Map<String, String> = emptyMap(),
    ) {
        constructor(body: String, status: Int = 200) : this(body.toByteArray(), status)
    }

    private class Fixture(private val handler: (Request) -> Reply?) : AutoCloseable {
        val requests: MutableList<Request> = Collections.synchronizedList(mutableListOf())
        private val server = ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"))
        private val failure = AtomicReference<Throwable?>()
        @Volatile private var running = true
        val endpoint: String = "http://127.0.0.1:${server.localPort}"

        private val worker = Thread({
            try {
                while (running) {
                    server.accept().use { socket ->
                        socket.soTimeout = 3000
                        val input = BufferedInputStream(socket.getInputStream())
                        val first = readLine(input).split(' ')
                        check(first.size == 3) { "Invalid request line: $first" }
                        val headers = mutableMapOf<String, String>()
                        while (true) {
                            val line = readLine(input)
                            if (line.isEmpty()) break
                            val split = line.indexOf(':')
                            check(split > 0) { "Invalid request header" }
                            headers[line.substring(0, split).lowercase()] = line.substring(split + 1).trim()
                        }
                        val length = headers["content-length"]?.toInt() ?: 0
                        check(length in 0..65536) { "Unexpected request body size" }
                        val bytes = ByteArray(length)
                        var offset = 0
                        while (offset < length) {
                            val count = input.read(bytes, offset, length - offset)
                            check(count > 0) { "Incomplete request body" }
                            offset += count
                        }
                        val request = Request(first[0], first[1], headers, bytes.toString(Charsets.UTF_8))
                        requests.add(request)
                        val reply = handler(request) ?: return@use
                        val extraHeaders = reply.headers.entries.joinToString("") { "${it.key}: ${it.value}\r\n" }
                        val header = "HTTP/1.1 ${reply.status} Test\r\nContent-Type: application/json\r\n" +
                            "Content-Length: ${reply.body.size}\r\n${extraHeaders}Connection: close\r\n\r\n"
                        socket.getOutputStream().apply {
                            write(header.toByteArray(Charsets.US_ASCII))
                            write(reply.body)
                            flush()
                        }
                    }
                }
            } catch (error: Throwable) {
                if (running || error !is SocketException) failure.set(error)
            }
        }, "syncap-instrumentation-http").apply {
            isDaemon = true
            start()
        }

        override fun close() {
            running = false
            server.close()
            worker.join(4000)
            check(!worker.isAlive) { "HTTP test worker did not stop" }
            failure.get()?.let { throw AssertionError("HTTP fixture failed", it) }
        }

        private fun readLine(input: BufferedInputStream): String {
            val result = StringBuilder()
            while (true) {
                val byte = input.read()
                check(byte >= 0) { "Unexpected end of request headers" }
                if (byte == 10) return result.toString().removeSuffix("\r")
                result.append(byte.toChar())
                check(result.length <= 8192) { "Request header too large" }
            }
        }
    }

    private fun temporaryDirectory(): File {
        val cache = InstrumentationRegistry.getInstrumentation().targetContext.cacheDir
        return File(cache, "syncap-sdk-test-${UUID.randomUUID()}").apply {
            check(mkdir()) { "Could not create SDK test destination" }
        }
    }

    private suspend fun sdkFailure(operation: suspend () -> Unit): SdkException.Failure {
        try {
            operation()
        } catch (error: SdkException.Failure) {
            return error
        }
        throw AssertionError("Expected a structured SDK error")
    }

    @Test fun typedCurrentCaptureLoadsAndroidNativeLibrary() = runBlocking {
        Fixture {
            Reply("""{"state":"recording","captureId":"cap_test","sessionId":"ses_test","elapsedMs":3095,"startedAtDeviceTimeNs":"9007199254740993001","producerState":"running"}""")
        }.use { server ->
            DeviceClient(server.endpoint).use { client ->
                val capture = client.currentCapture()
                assertEquals("recording", capture.state)
                assertEquals("cap_test", capture.captureId)
                assertEquals(3095uL, capture.elapsedMs)
                assertEquals("9007199254740993001", capture.startedAtDeviceTimeNs)
                assertTrue(capture.rawJson.contains("producerState"))
            }
            assertEquals(listOf("GET /v1/captures/current"), server.requests.map { "${it.method} ${it.path}" })
        }
    }

    @Test fun storageStartAndStopShareTheNativeCore() = runBlocking {
        Fixture { request ->
            when (request.path) {
                "/v1/storage/configure" -> Reply("""{"target":"usb","usb":{"canCapture":true,"mediaType":"sd"}}""")
                "/v1/captures/start" -> Reply("""{"captureId":"cap_test","sessionId":"ses_test","state":"recording","elapsedMs":0}""")
                "/v1/captures/cap_test/stop" -> Reply("""{"captureId":"cap_test","sessionId":"ses_test","state":"completed","session":{"id":"ses_test","sizeBytes":4096,"status":"completed","exportAvailable":true}}""")
                else -> Reply("{}", 404)
            }
        }.use { server ->
            DeviceClient(server.endpoint).use { client ->
                assertTrue(client.configureStorageJson(StorageTarget.REMOVABLE).contains("mediaType"))
                val capture = client.startCapture("Android SDK test")
                assertEquals("recording", capture.state)
                assertEquals(0uL, capture.elapsedMs)
                val stopped = client.stopCapture(capture.captureId)
                assertEquals("completed", stopped.state)
                assertEquals(capture.sessionId, stopped.sessionId)
                assertEquals(4096uL, stopped.session?.sizeBytes)
                assertEquals(true, stopped.session?.exportAvailable)
            }
            assertEquals(listOf("POST /v1/storage/configure", "POST /v1/captures/start", "POST /v1/captures/cap_test/stop"), server.requests.map { "${it.method} ${it.path}" })
            assertTrue(server.requests[0].body.contains("\"target\":\"usb\""))
            assertTrue(server.requests[1].body.contains("Android SDK test"))
        }
    }

    @Test fun structuredDeviceFailureDoesNotRetry() = runBlocking {
        Fixture { Reply("""{"error":"producer.not_ready","message":"camera recovery needed"}""", 503) }.use { server ->
            DeviceClient(server.endpoint).use { client ->
                val failure = sdkFailure { client.startCapture("Android test") }
                assertEquals(SdkErrorKind.DEVICE, failure.kind)
                assertEquals(503u.toUShort(), failure.httpStatus)
                assertEquals("producer.not_ready", failure.deviceCode)
                assertEquals("camera recovery needed", failure.detail)
                assertTrue(failure.outcomeUnknown)
            }
            assertEquals(1, server.requests.size)
        }
    }

    @Test fun invalidEndpointMapsNativeValidationError() = runBlocking {
        val failure = sdkFailure { DeviceClient("http://user:password@localhost").close() }
        assertEquals(SdkErrorKind.INVALID_INPUT, failure.kind)
        assertFalse(failure.outcomeUnknown)
        assertFalse(failure.detail.contains("password"))
    }

    @Test fun explicitPreCancellationDoesNotCreateDataOrContactDevice() = runBlocking {
        Fixture { Reply("{}", 500) }.use { server ->
            val destination = temporaryDirectory()
            try {
                DeviceClient(server.endpoint).use { client ->
                    Cancellation().use { cancellation ->
                        cancellation.cancel()
                        assertTrue(cancellation.isCancelled())
                        val failure = sdkFailure {
                            client.exportSession("ses_test", destination.absolutePath, cancellation)
                        }
                        assertEquals(SdkErrorKind.CANCELLED, failure.kind)
                        assertTrue(server.requests.isEmpty())
                        assertTrue(destination.listFiles()!!.isEmpty())
                    }
                }
            } finally {
                assertTrue(destination.deleteRecursively())
            }
        }
    }

    @Test fun resumableExportVerifiesRangeHashAndOriginalManifest() = runBlocking {
        val payload = "camera-video-payload".toByteArray()
        val sha = MessageDigest.getInstance("SHA-256").digest(payload).joinToString("") { "%02x".format(it) }
        val entry = """{"name":"camera.mcap","sizeBytes":${payload.size},"sha256":"$sha","url":"/v1/sessions/ses_test/files/camera.mcap"}"""
        val originalManifest = """{"id":"ses_test","state":"complete","files":[$entry],"calibration":{"original":true}}"""
        val range = AtomicReference<String?>()
        Fixture { request ->
            when (request.path) {
                "/v1/sessions/ses_test/prepare-export" -> Reply("""{"sessionId":"ses_test","files":[$entry]}""")
                "/v1/sessions/ses_test/manifest" -> Reply(originalManifest)
                "/v1/sessions/ses_test/files/camera.mcap" -> {
                    range.set(request.headers["range"])
                    Reply(payload.copyOfRange(5, payload.size), 206, mapOf("Content-Range" to "bytes 5-${payload.size - 1}/${payload.size}"))
                }
                else -> Reply("{}", 404)
            }
        }.use { server ->
            val destination = temporaryDirectory()
            try {
                destination.resolve("camera.mcap.part").writeBytes(payload.copyOfRange(0, 5))
                DeviceClient(server.endpoint).use { client ->
                    val report = client.exportSession("ses_test", destination.absolutePath, null)
                    assertTrue(report.verified)
                    assertEquals((payload.size + originalManifest.toByteArray().size).toULong(), report.bytes)
                    assertEquals(2uL, report.files)
                    assertEquals(1uL, report.verifiedFiles)
                    assertTrue(report.manifestSaved)
                    assertEquals("bytes=5-", range.get())
                    assertArrayEquals(payload, destination.resolve("camera.mcap").readBytes())
                    assertEquals(originalManifest, destination.resolve("session.json").readText())
                    assertFalse(destination.resolve("camera.mcap.part").exists())
                    // A repeat export must verify and reuse the final file, not download it again.
                    assertTrue(client.exportSession("ses_test", destination.absolutePath, null).verified)
                    assertEquals(1, server.requests.count { it.path.endsWith("/files/camera.mcap") })
                }
            } finally {
                assertTrue(destination.deleteRecursively())
            }
        }
    }

    @Test fun concurrentExportIsRejectedThenCancellationAllowsResume() = runBlocking {
        val payload = "camera-video-payload".toByteArray()
        val sha = MessageDigest.getInstance("SHA-256").digest(payload).joinToString("") { "%02x".format(it) }
        val entry = """{"name":"camera.mcap","sizeBytes":${payload.size},"sha256":"$sha","url":"/v1/sessions/ses_test/files/camera.mcap"}"""
        val manifest = """{"id":"ses_test","state":"complete","files":[$entry]}"""
        val firstBlobReached = CountDownLatch(1)
        val releaseFirstBlob = CountDownLatch(1)
        val resumedRange = AtomicReference<String?>()
        val destination = temporaryDirectory()
        try {
            destination.resolve("camera.mcap.part").writeBytes(payload.copyOfRange(0, 5))
            Fixture { request ->
                when (request.path) {
                    "/v1/sessions/ses_test/prepare-export" -> Reply("""{"sessionId":"ses_test","files":[$entry]}""")
                    "/v1/sessions/ses_test/manifest" -> Reply(manifest)
                    "/v1/sessions/ses_test/files/camera.mcap" -> {
                        // Reaching the download proves the first native export owns the lock.
                        firstBlobReached.countDown()
                        check(releaseFirstBlob.await(10, TimeUnit.SECONDS)) { "First export was not released" }
                        // The cancelled client no longer needs a response; close without writing.
                        null
                    }
                    else -> Reply("{}", 404)
                }
            }.use { firstServer ->
                Fixture { request ->
                    when (request.path) {
                        "/v1/sessions/ses_test/prepare-export" -> Reply("""{"sessionId":"ses_test","files":[$entry]}""")
                        "/v1/sessions/ses_test/manifest" -> Reply(manifest)
                        "/v1/sessions/ses_test/files/camera.mcap" -> {
                            resumedRange.set(request.headers["range"])
                            Reply(payload.copyOfRange(5, payload.size), 206, mapOf("Content-Range" to "bytes 5-${payload.size - 1}/${payload.size}"))
                        }
                        else -> Reply("{}", 404)
                    }
                }.use { secondServer ->
                    DeviceClient(firstServer.endpoint).use { firstClient ->
                        DeviceClient(secondServer.endpoint).use { secondClient ->
                            Cancellation().use { cancellation ->
                                val first = async(Dispatchers.Default) {
                                    sdkFailure { firstClient.exportSession("ses_test", destination.absolutePath, cancellation) }
                                }
                                try {
                                    assertTrue("First export must reach its download", firstBlobReached.await(5, TimeUnit.SECONDS))
                                    val busy = sdkFailure {
                                        secondClient.exportSession("ses_test", destination.absolutePath, null)
                                    }
                                    assertEquals(SdkErrorKind.IO, busy.kind)
                                    assertTrue(busy.detail.contains("Another export"))
                                    assertArrayEquals(payload.copyOfRange(0, 5), destination.resolve("camera.mcap.part").readBytes())
                                    assertFalse(destination.resolve("camera.mcap").exists())
                                    assertTrue(secondServer.requests.none { it.path.endsWith("/files/camera.mcap") })
                                    cancellation.cancel()
                                    assertEquals(SdkErrorKind.CANCELLED, withTimeout(5000) { first.await() }.kind)
                                    releaseFirstBlob.countDown()
                                    // The next export uses a different client and the same real native lock.
                                    val report = secondClient.exportSession("ses_test", destination.absolutePath, null)
                                    assertTrue(report.verified)
                                    assertEquals("bytes=5-", resumedRange.get())
                                    assertArrayEquals(payload, destination.resolve("camera.mcap").readBytes())
                                    assertFalse(destination.resolve("camera.mcap.part").exists())
                                } finally {
                                    cancellation.cancel()
                                    releaseFirstBlob.countDown()
                                    withTimeout(5000) { first.await() }
                                }
                            }
                        }
                    }
                }
            }
        } finally {
            releaseFirstBlob.countDown()
            assertTrue(destination.deleteRecursively())
        }
    }
}
