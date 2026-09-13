package com.syncap.studio;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.net.ServerSocket;
import java.net.Socket;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.Test;

public class SynCapDevicePluginConcurrencyTest {
    @Test
    public void blockedTransferDoesNotDelayControlRequest() throws Exception {
        SynCapDevicePlugin.IoExecutors io = new SynCapDevicePlugin.IoExecutors();
        CountDownLatch transferStarted = new CountDownLatch(1);
        CountDownLatch releaseTransfer = new CountDownLatch(1);
        try {
            io.transfers.execute(() -> {
                transferStarted.countDown();
                await(releaseTransfer);
            });
            assertTrue("transfer task did not start", transferStarted.await(1, TimeUnit.SECONDS));

            long startedAt = System.nanoTime();
            Future<String> controlResult = io.control.submit(() -> "ready");
            assertEquals("ready", controlResult.get(1, TimeUnit.SECONDS));
            long durationMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedAt);
            assertTrue("control request waited behind transfer for " + durationMs + " ms", durationMs < 1000);
            assertEquals("transfer unexpectedly completed", 1, releaseTransfer.getCount());
        } finally {
            releaseTransfer.countDown();
            io.shutdownNow();
        }
    }

    @Test
    public void fourProbeTasksCanStartTogether() throws Exception {
        SynCapDevicePlugin.IoExecutors io = new SynCapDevicePlugin.IoExecutors();
        CountDownLatch allStarted = new CountDownLatch(4);
        CountDownLatch releaseProbes = new CountDownLatch(1);
        List<Future<?>> tasks = new ArrayList<>();
        try {
            for (int index = 0; index < 4; index++) {
                tasks.add(io.rtspProbes.submit(() -> {
                    allStarted.countDown();
                    await(releaseProbes);
                }));
            }
            assertTrue("four probe workers were not available", allStarted.await(1, TimeUnit.SECONDS));
            releaseProbes.countDown();
            for (Future<?> task : tasks) task.get(1, TimeUnit.SECONDS);
        } finally {
            releaseProbes.countDown();
            io.shutdownNow();
        }
    }

    @Test
    public void rtspProbeKeepsTwoAttemptsAtFifteenHundredMilliseconds() throws Exception {
        assertEquals(1500, SynCapDevicePlugin.CONNECT_TIMEOUT_MS);
        assertEquals(2, SynCapDevicePlugin.CONNECT_ATTEMPTS);

        AtomicInteger acceptedConnections = new AtomicInteger();
        ExecutorService serverWorkers = Executors.newCachedThreadPool();
        try (ServerSocket server = new ServerSocket(0)) {
            Future<?> acceptTask = serverWorkers.submit(() -> {
                for (int attempt = 0; attempt < SynCapDevicePlugin.CONNECT_ATTEMPTS; attempt++) {
                    try {
                        Socket connection = server.accept();
                        acceptedConnections.incrementAndGet();
                        serverWorkers.execute(() -> holdOpen(connection));
                    } catch (Exception error) {
                        throw new RuntimeException(error);
                    }
                }
            });

            SynCapDevicePlugin.RtspProbeResult result = SynCapDevicePlugin.probeRtsp(
                "127.0.0.1", server.getLocalPort()
            );
            acceptTask.get(1, TimeUnit.SECONDS);

            assertFalse(result.online);
            assertEquals("timeout", result.reason);
            assertEquals(2, acceptedConnections.get());
            assertTrue("both 1500 ms attempts were not observed", result.latencyMs >= 2900);
        } finally {
            serverWorkers.shutdownNow();
        }
    }

    @Test
    public void gattWriteAndRfcommFallbackCannotBothWinTheHandoff() throws Exception {
        ExecutorService workers = Executors.newFixedThreadPool(2);
        try {
            for (int iteration = 0; iteration < 500; iteration++) {
                SynCapDevicePlugin.GattRfcommHandoff handoff =
                    new SynCapDevicePlugin.GattRfcommHandoff();
                AtomicBoolean completed = new AtomicBoolean(false);
                CountDownLatch start = new CountDownLatch(1);
                Future<Boolean> gattWrite = workers.submit(() -> {
                    await(start);
                    return handoff.beginGattWrite(completed);
                });
                Future<SynCapDevicePlugin.BluetoothFailureRoute> failure = workers.submit(() -> {
                    await(start);
                    return handoff.routeFailure(true, 999, 1000, completed);
                });

                start.countDown();
                boolean writeWon = gattWrite.get(1, TimeUnit.SECONDS);
                boolean rfcommWon = failure.get(1, TimeUnit.SECONDS)
                    == SynCapDevicePlugin.BluetoothFailureRoute.RFCOMM;
                assertFalse("GATT write and RFCOMM fallback both won", writeWon && rfcommWon);
                assertTrue("Neither GATT write nor RFCOMM fallback won", writeWon || rfcommWon);
            }
        } finally {
            workers.shutdownNow();
        }
    }

    @Test
    public void closingOperationRegistryCancelsCurrentAndRejectsFutureOperations() {
        SynCapDevicePlugin.ActiveOperationRegistry<SynCapDevicePlugin.CancellableOperation> registry =
            new SynCapDevicePlugin.ActiveOperationRegistry<>();
        AtomicInteger cancellations = new AtomicInteger();
        SynCapDevicePlugin.CancellableOperation first = cancellations::incrementAndGet;
        SynCapDevicePlugin.CancellableOperation second = cancellations::incrementAndGet;

        assertTrue(registry.register(first));
        assertTrue(registry.register(second));
        assertEquals(2, registry.size());

        registry.cancelAll();
        assertEquals(2, cancellations.get());
        assertEquals(0, registry.size());
        assertFalse(registry.register(cancellations::incrementAndGet));

        registry.cancelAll();
        assertEquals(2, cancellations.get());
    }

    private static void holdOpen(Socket connection) {
        try (Socket ignored = connection) {
            Thread.sleep(4000);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        } catch (Exception ignored) {
            // The test server only needs to keep the socket silent until the client times out.
        }
    }

    private static void await(CountDownLatch latch) {
        try {
            latch.await();
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }
}
