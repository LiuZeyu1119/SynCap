package com.syncap.studio;

import android.Manifest;
import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.BluetoothSocket;
import android.bluetooth.BluetoothStatusCodes;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.location.LocationManager;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelUuid;
import android.os.SystemClock;
import android.system.ErrnoException;
import android.system.Os;
import android.util.Log;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.EOFException;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.ConnectException;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReferenceArray;

@CapacitorPlugin(
    name = "SynCapDevice",
    permissions = {
        @Permission(
            alias = "bluetooth",
            strings = { Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT }
        ),
        @Permission(
            alias = "location",
            strings = { Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.ACCESS_FINE_LOCATION }
        )
    }
)
public class SynCapDevicePlugin extends Plugin {
    private static final String TAG = "SynCapDevice";
    private static final String DEFAULT_DEVICE_HOST = "192.168.1.12";
    static final int CONNECT_TIMEOUT_MS = 1500;
    static final int CONNECT_ATTEMPTS = 2;
    static final int CONTROL_IO_THREADS = 2;
    static final int RTSP_PROBE_THREADS = 4;
    static final int FAST_DEVICE_READ_TIMEOUT_MS = 2500;
    static final int DEVICE_STATUS_READ_TIMEOUT_MS = 8000;
    private static final UUID WIFI_SERVICE_UUID = UUID.fromString("8f7a0001-6c2b-4dd4-9f1a-53f65c9b40d1");
    private static final UUID WIFI_CONFIG_UUID = UUID.fromString("8f7a0002-6c2b-4dd4-9f1a-53f65c9b40d1");
    private static final UUID WIFI_STATUS_UUID = UUID.fromString("8f7a0003-6c2b-4dd4-9f1a-53f65c9b40d1");
    static final UUID SPP_SERVICE_UUID = UUID.fromString("00001101-0000-1000-8000-00805f9b34fb");
    private static final String FIXED_CLAIM_CODE = "123456";
    static final int BLUETOOTH_COMMAND_MAX_BYTES = 4096;
    private static final int BLE_CONNECT_ATTEMPTS = 3;
    private static final int BLE_MAX_FRAGMENTS = 128;
    static final int BLE_OPERATION_TIMEOUT_MS = 120000;
    static final int BLE_WIFI_SCAN_TIMEOUT_MS = 45000;
    static final int BLE_DEVICE_STATUS_TIMEOUT_MS = 25000;
    static final int BLE_MTU_CALLBACK_TIMEOUT_MS = 3000;
    static final int BLE_SERVICE_FALLBACK_INTERVAL_MS = 250;
    static final int BLE_SERVICE_FALLBACK_ATTEMPTS = 20;
    private static final AtomicInteger BLE_OPERATION_SEQUENCE = new AtomicInteger();
    private final IoExecutors ioExecutors = new IoExecutors();
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ActiveOperationRegistry<RfcommOperation> activeRfcommOperations =
        new ActiveOperationRegistry<>();
    private final AtomicBoolean shuttingDown = new AtomicBoolean(false);

    @PluginMethod
    public void probe(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        JSArray requestedPorts = call.getArray("ports");
        JSArray requestedPaths = call.getArray("paths");
        JSArray requestedTransports = call.getArray("transports");
        ArrayList<Integer> ports = new ArrayList<>();
        ArrayList<String> paths = new ArrayList<>();
        ArrayList<String> transports = new ArrayList<>();

        if (requestedPorts != null) {
            for (int index = 0; index < requestedPorts.length(); index++) {
                int port = requestedPorts.optInt(index, -1);
                if (port > 0 && port <= 65535) {
                    String transport = normalizePreviewTransport(
                        requestedTransports == null ? "" : requestedTransports.optString(index, "")
                    );
                    String path = "tcp-hevc".equals(transport)
                        ? ""
                        : requestedPaths == null ? "/PRR"
                            : normalizeRtspPath(requestedPaths.optString(index, "/PRR"));
                    boolean duplicate = false;
                    for (int existing = 0; existing < ports.size(); existing++) {
                        if (ports.get(existing) == port
                            && paths.get(existing).equals(path)
                            && transports.get(existing).equals(transport)) {
                            duplicate = true;
                            break;
                        }
                    }
                    if (!duplicate) {
                        ports.add(port);
                        paths.add(path);
                        transports.add(transport);
                    }
                }
            }
        }

        if (ports.isEmpty()) {
            ports.add(554);
            ports.add(555);
            ports.add(556);
            ports.add(557);
            paths.add("/PRR");
            paths.add("/PRR");
            paths.add("/PRR");
            paths.add("/PRR");
            transports.add("rtsp");
            transports.add("rtsp");
            transports.add("rtsp");
            transports.add("rtsp");
        }

        long startedAt = System.nanoTime();
        AtomicReferenceArray<RtspProbeResult> results = new AtomicReferenceArray<>(ports.size());
        AtomicInteger remaining = new AtomicInteger(ports.size());
        AtomicBoolean completed = new AtomicBoolean(false);
        Log.d(TAG, "probe queued host=" + host + " streams=" + ports.size());

        for (int index = 0; index < ports.size(); index++) {
            final int resultIndex = index;
            final int port = ports.get(index);
            final String path = paths.get(index);
            final String transport = transports.get(index);
            try {
                ioExecutors.rtspProbes.execute(() -> {
                    RtspProbeResult probe;
                    try {
                        probe = "tcp-hevc".equals(transport)
                            ? probeTcp(host, port)
                            : probeRtsp(host, port, path);
                    } catch (RuntimeException error) {
                        Log.w(TAG, "probe failed unexpectedly port=" + port, error);
                        probe = new RtspProbeResult(false, "probe_error", null, 0);
                    }
                    results.set(resultIndex, probe);
                    Log.d(TAG, "probe result port=" + port + " online=" + probe.online
                        + " reason=" + probe.reason + " latencyMs=" + probe.latencyMs);
                    if (remaining.decrementAndGet() == 0 && completed.compareAndSet(false, true)) {
                        resolveProbe(call, host, ports, paths, transports, results, startedAt);
                    }
                });
            } catch (RejectedExecutionException error) {
                if (completed.compareAndSet(false, true)) {
                    Log.w(TAG, "probe rejected because the plugin is shutting down", error);
                    call.reject("Unable to schedule camera probe", error);
                }
                return;
            }
        }
    }

    @PluginMethod
    public void openPreview(PluginCall call) {
        JSArray urlValues = call.getArray("urls");
        JSArray labelValues = call.getArray("labels");
        if (urlValues == null || urlValues.length() == 0) {
            call.reject("At least one RTSP URL is required");
            return;
        }

        ArrayList<String> urls = new ArrayList<>();
        ArrayList<String> labels = new ArrayList<>();
        for (int index = 0; index < urlValues.length(); index++) {
            String url = urlValues.optString(index, "");
            if (isSupportedPreviewUrl(url)) {
                urls.add(url);
                labels.add(labelValues == null ? "CAM " + index : labelValues.optString(index, "CAM " + index));
            }
        }

        if (urls.isEmpty()) {
            call.reject("No valid camera preview URL was provided");
            return;
        }

        getActivity().runOnUiThread(() -> {
            Intent intent = new Intent(getContext(), LivePreviewActivity.class);
            intent.putStringArrayListExtra(LivePreviewActivity.EXTRA_URLS, urls);
            intent.putStringArrayListExtra(LivePreviewActivity.EXTRA_LABELS, labels);
            getActivity().startActivity(intent);
            call.resolve();
        });
    }

    @PluginMethod
    public void openCalibration(PluginCall call) {
        JSArray urlValues = call.getArray("urls");
        JSArray labelValues = call.getArray("labels");
        JSArray cameraIdValues = call.getArray("cameraIds");
        String pair = call.getString("pair", "");
        int squaresLong = call.getInt("squaresLong", 0);
        int squaresWide = call.getInt("squaresWide", 0);
        double squareSizeMm = call.getData().optDouble("squareSizeMm", 0);
        if (urlValues == null || urlValues.length() != 2
            || cameraIdValues == null || cameraIdValues.length() != 2) {
            call.reject("Two calibration cameras are required", "SYNCAP_CALIBRATION_INPUT");
            return;
        }
        if (!("pair14".equals(pair) || "pair23".equals(pair))) {
            call.reject("Unsupported calibration pair", "SYNCAP_CALIBRATION_INPUT");
            return;
        }
        if (squaresLong < 4 || squaresLong > 20 || squaresWide < 4 || squaresWide > 20
            || !Double.isFinite(squareSizeMm) || squareSizeMm < 1 || squareSizeMm > 200) {
            call.reject("Invalid chessboard dimensions", "SYNCAP_CALIBRATION_INPUT");
            return;
        }

        ArrayList<String> urls = new ArrayList<>();
        ArrayList<String> labels = new ArrayList<>();
        ArrayList<String> cameraIds = new ArrayList<>();
        for (int index = 0; index < 2; index++) {
            String url = urlValues.optString(index, "");
            String cameraId = cameraIdValues.optString(index, "").trim();
            if (!url.startsWith("rtsp://") || cameraId.isEmpty()) {
                call.reject("Invalid calibration camera", "SYNCAP_CALIBRATION_INPUT");
                return;
            }
            urls.add(url);
            cameraIds.add(cameraId);
            labels.add(labelValues == null ? cameraId : labelValues.optString(index, cameraId));
        }

        getActivity().runOnUiThread(() -> {
            Intent intent = new Intent(getContext(), CalibrationActivity.class);
            intent.putStringArrayListExtra(CalibrationActivity.EXTRA_URLS, urls);
            intent.putStringArrayListExtra(CalibrationActivity.EXTRA_LABELS, labels);
            intent.putStringArrayListExtra(CalibrationActivity.EXTRA_CAMERA_IDS, cameraIds);
            intent.putExtra(CalibrationActivity.EXTRA_PAIR, pair);
            intent.putExtra(CalibrationActivity.EXTRA_SQUARES_LONG, squaresLong);
            intent.putExtra(CalibrationActivity.EXTRA_SQUARES_WIDE, squaresWide);
            intent.putExtra(CalibrationActivity.EXTRA_SQUARE_SIZE_MM, squareSizeMm);
            getActivity().startActivity(intent);
            call.resolve();
        });
    }

    @PluginMethod
    public void getManifest(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        int port = call.getInt("port", 8080);

        executeJsonTask(
            ioExecutors.control,
            "manifest",
            call,
            "Unable to read device manifest",
            () -> requestJson(host, port, "/v1/manifest", "GET", null)
        );
    }

    @PluginMethod
    public void getStatus(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        int port = call.getInt("port", 8080);

        executeJsonTask(
            ioExecutors.control,
            "status",
            call,
            "Unable to read device status",
            () -> requestJson(host, port, "/v1/status", "GET", null)
        );
    }

    @PluginMethod
    public void getStorage(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        int port = call.getInt("port", 8080);

        executeJsonTask(
            ioExecutors.control,
            "storage.get",
            call,
            "Unable to read device storage",
            () -> requestJson(host, port, "/v1/storage", "GET", null)
        );
    }

    @PluginMethod
    public void configureStorage(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        int port = call.getInt("port", 8080);
        String target = call.getString("target", "");
        if (!validStorageTarget(target)) {
            call.reject("Storage target must be internal or usb", "SYNCAP_INVALID_STORAGE_TARGET");
            return;
        }

        executeJsonTask(
            ioExecutors.control,
            "storage.configure",
            call,
            "Unable to configure device storage",
            () -> {
                JSObject body = new JSObject();
                body.put("target", target);
                body.put("claimCode", FIXED_CLAIM_CODE);
                return requestJson(host, port, "/v1/storage/configure", "POST", body);
            }
        );
    }

    @PluginMethod
    public void getCameraConfiguration(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        int port = call.getInt("port", 8080);
        executeJsonTask(
            ioExecutors.control,
            "camera.configuration.get",
            call,
            "Unable to read camera orientation",
            () -> requestJson(host, port, "/v1/camera/configuration", "GET", null)
        );
    }

    @PluginMethod
    public void configureCameraOrientation(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        int port = call.getInt("port", 8080);
        int rotationDegrees = call.getInt("rotationDegrees", -1);
        if (rotationDegrees != 0 && rotationDegrees != 90
            && rotationDegrees != 180 && rotationDegrees != 270) {
            call.reject("Camera rotation must be 0, 90, 180, or 270", "SYNCAP_INVALID_CAMERA_ROTATION");
            return;
        }
        executeJsonTask(
            ioExecutors.control,
            "camera.configuration.set",
            call,
            "Unable to change camera orientation",
            () -> {
                JSObject body = new JSObject();
                body.put("rotationDegrees", rotationDegrees);
                body.put("claimCode", FIXED_CLAIM_CODE);
                return requestJson(host, port, "/v1/camera/configuration", "POST", body);
            }
        );
    }

    @PluginMethod
    public void startCapture(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        int port = call.getInt("port", 8080);
        String name = call.getString("name", "session");
        executeJsonTask(
            ioExecutors.control,
            "capture.start",
            call,
            "Unable to start capture",
            () -> {
                JSObject body = new JSObject();
                body.put("name", name);
                return requestJson(host, port, "/v1/captures/start", "POST", body);
            }
        );
    }

    @PluginMethod
    public void stopCapture(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        int port = call.getInt("port", 8080);
        String captureId = call.getString("captureId", "");
        if (!validOpaqueIdentifier(captureId)) {
            call.reject("Invalid capture id");
            return;
        }
        executeJsonTask(
            ioExecutors.control,
            "capture.stop",
            call,
            "Unable to stop capture",
            () -> requestJson(host, port, "/v1/captures/" + captureId + "/stop", "POST", new JSObject())
        );
    }

    @PluginMethod
    public void listSessions(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        int port = call.getInt("port", 8080);
        executeJsonTask(
            ioExecutors.transfers,
            "sessions.list",
            call,
            "Unable to list sessions",
            () -> requestJson(host, port, "/v1/sessions", "GET", null)
        );
    }

    @PluginMethod
    public void prepareExport(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        int port = call.getInt("port", 8080);
        String sessionId = call.getString("sessionId", "");
        if (!validOpaqueIdentifier(sessionId)) {
            call.reject("Invalid session id");
            return;
        }
        executeJsonTask(
            ioExecutors.transfers,
            "export.prepare",
            call,
            "Unable to prepare export",
            () -> requestJson(host, port, "/v1/sessions/" + sessionId + "/prepare-export", "POST", new JSObject())
        );
    }

    @PluginMethod
    public void downloadSession(PluginCall call) {
        String host = call.getString("host", DEFAULT_DEVICE_HOST);
        int port = call.getInt("port", 8080);
        String sessionId = call.getString("sessionId", "");
        if (!validOpaqueIdentifier(sessionId)) {
            call.reject("Invalid session id");
            return;
        }
        executeJsonTask(
            ioExecutors.transfers,
            "session.download",
            call,
            "Unable to export session",
            () -> {
                JSObject export = requestJson(
                    host, port, "/v1/sessions/" + sessionId + "/prepare-export", "POST", new JSObject()
                );
                JSONArray files = export.getJSONArray("files");
                File downloadsRoot = getContext().getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
                if (downloadsRoot == null) throw new IOException("External downloads directory is unavailable");
                File destination = new File(new File(downloadsRoot, "SynCap"), sessionId);
                if (!destination.isDirectory() && !destination.mkdirs()) {
                    throw new IOException("Unable to create export directory");
                }

                long downloadedBytes = 0;
                HashSet<String> savedNames = new HashSet<>();
                for (int index = 0; index < files.length(); index++) {
                    JSONObject item = files.getJSONObject(index);
                    String name = item.getString("name");
                    if (!name.matches("[A-Za-z0-9._-]+")
                        || "session.json".equals(name)
                        || !savedNames.add(name)) {
                        throw new IOException("Invalid or duplicate export file name");
                    }
                    long size = item.getLong("sizeBytes");
                    String expectedSha256 = item.getString("sha256");
                    String path = item.getString("url");
                    File output = new File(destination, name);
                    downloadWithResume(host, port, path, output, size, expectedSha256);
                    downloadedBytes += size;
                }

                String manifestPath = normalizeSessionManifestPath(
                    export.optString("manifestUrl", "/v1/sessions/" + sessionId + "/manifest"),
                    host,
                    port,
                    sessionId
                );
                downloadedBytes += downloadSessionManifest(
                    host,
                    port,
                    manifestPath,
                    new File(destination, "session.json"),
                    sessionId
                );

                JSObject result = new JSObject();
                result.put("sessionId", sessionId);
                result.put("path", destination.getAbsolutePath());
                result.put("bytes", downloadedBytes);
                result.put("files", files.length() + 1);
                result.put("verifiedFiles", files.length());
                result.put("manifestSaved", true);
                result.put("manifestFile", "session.json");
                result.put("verified", true);
                return result;
            }
        );
    }

    private void resolveProbe(
        PluginCall call,
        String host,
        List<Integer> ports,
        List<String> paths,
        List<String> transports,
        AtomicReferenceArray<RtspProbeResult> results,
        long startedAt
    ) {
        JSArray streams = new JSArray();
        int onlineCount = 0;
        for (int index = 0; index < ports.size(); index++) {
            int port = ports.get(index);
            String path = paths.get(index);
            String transport = transports.get(index);
            RtspProbeResult probe = results.get(index);
            if (probe == null) probe = new RtspProbeResult(false, "probe_error", null, 0);
            if (probe.online) onlineCount++;

            JSObject stream = new JSObject();
            stream.put("port", port);
            stream.put("path", path);
            stream.put("transport", transport);
            stream.put("online", probe.online);
            stream.put("url", "tcp-hevc".equals(transport)
                ? "tcp://" + host + ":" + port
                : "rtsp://" + host + ":" + port + path);
            stream.put("reason", probe.reason);
            stream.put("latencyMs", probe.latencyMs);
            if (probe.responseCode != null) stream.put("responseCode", probe.responseCode);
            streams.put(stream);
        }

        long durationMs = elapsedMs(startedAt);
        Log.i(TAG, "probe complete host=" + host + " online=" + onlineCount + "/" + ports.size()
            + " durationMs=" + durationMs);
        JSObject result = new JSObject();
        result.put("host", host);
        result.put("reachable", onlineCount > 0);
        result.put("onlineCount", onlineCount);
        result.put("streams", streams);
        call.resolve(result);
    }

    private void executeJsonTask(
        ExecutorService executor,
        String operation,
        PluginCall call,
        String failureMessage,
        JsonTask task
    ) {
        long queuedAt = System.nanoTime();
        try {
            executor.execute(() -> {
                long startedAt = System.nanoTime();
                long queueMs = elapsedMs(queuedAt);
                Log.d(TAG, operation + " started queueMs=" + queueMs);
                try {
                    JSObject result = task.run();
                    Log.i(TAG, operation + " complete queueMs=" + queueMs
                        + " durationMs=" + elapsedMs(startedAt));
                    call.resolve(result);
                } catch (Exception error) {
                    Log.w(TAG, operation + " failed queueMs=" + queueMs
                        + " durationMs=" + elapsedMs(startedAt), error);
                    rejectJsonTask(call, failureMessage, error);
                }
            });
        } catch (RejectedExecutionException error) {
            Log.w(TAG, operation + " rejected because the plugin is shutting down", error);
            call.reject(failureMessage, "SYNCAP_PLUGIN_SHUTDOWN", error);
        }
    }

    private void rejectJsonTask(PluginCall call, String fallbackMessage, Exception error) {
        if (!(error instanceof DeviceHttpException)) {
            call.reject(fallbackMessage, "SYNCAP_IO_ERROR", error);
            return;
        }

        DeviceHttpException httpError = (DeviceHttpException) error;
        JSObject details = new JSObject();
        details.put("httpStatus", httpError.statusCode);
        if (httpError.serverCode != null) details.put("serverCode", httpError.serverCode);
        if (httpError.serverMessage != null) details.put("serverMessage", httpError.serverMessage);
        call.reject(httpError.messageFor(fallbackMessage), httpError.rejectionCode(), error, details);
    }

    static boolean validStorageTarget(String target) {
        return "internal".equals(target) || "usb".equals(target);
    }

    private static long elapsedMs(long startedAt) {
        return (System.nanoTime() - startedAt) / 1_000_000;
    }

    @Override
    protected void handleOnDestroy() {
        Log.d(TAG, "shutting down native IO executors");
        shuttingDown.set(true);
        activeRfcommOperations.cancelAll();
        ioExecutors.shutdownNow();
        super.handleOnDestroy();
    }

    @PluginMethod
    public void scanBle(PluginCall call) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && getPermissionState("bluetooth") != PermissionState.GRANTED) {
            requestPermissionForAlias("bluetooth", call, "bluetoothPermissionCallback");
            return;
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S && getPermissionState("location") != PermissionState.GRANTED) {
            requestPermissionForAlias("location", call, "bluetoothLocationPermissionCallback");
            return;
        }
        performBleScan(call);
    }

    @PluginMethod
    public void scanWifi(PluginCall call) {
        if (getPermissionState("location") != PermissionState.GRANTED) {
            requestPermissionForAlias("location", call, "wifiLocationPermissionCallback");
            return;
        }
        performWifiScan(call);
    }

    @PluginMethod
    public void scanWifiBle(PluginCall call) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && getPermissionState("bluetooth") != PermissionState.GRANTED) {
            requestPermissionForAlias("bluetooth", call, "wifiBlePermissionCallback");
            return;
        }
        performDeviceWifiScan(call);
    }

    @PluginMethod
    public void configureWifiBle(PluginCall call) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && getPermissionState("bluetooth") != PermissionState.GRANTED) {
            requestPermissionForAlias("bluetooth", call, "bluetoothPermissionCallback");
            return;
        }
        performWifiConfiguration(call);
    }

    @PluginMethod
    public void offlineBleCommand(PluginCall call) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && getPermissionState("bluetooth") != PermissionState.GRANTED) {
            if (call.getBoolean("backgroundRecovery", false)) {
                resolveSkippedBackgroundRecovery(call);
                return;
            }
            requestPermissionForAlias("bluetooth", call, "bluetoothPermissionCallback");
            return;
        }
        performOfflineCommand(call);
    }

    @PermissionCallback
    private void bluetoothPermissionCallback(PluginCall call) {
        if (getPermissionState("bluetooth") != PermissionState.GRANTED) {
            call.reject("Bluetooth permission is required");
            return;
        }
        if (call.getString("op") != null) performOfflineCommand(call);
        else if (call.getString("address") != null) performWifiConfiguration(call);
        else performBleScan(call);
    }

    @PermissionCallback
    private void wifiBlePermissionCallback(PluginCall call) {
        if (getPermissionState("bluetooth") != PermissionState.GRANTED) {
            call.reject("Bluetooth permission is required to scan Wi-Fi from the headring");
            return;
        }
        performDeviceWifiScan(call);
    }

    @PermissionCallback
    private void bluetoothLocationPermissionCallback(PluginCall call) {
        if (getPermissionState("location") != PermissionState.GRANTED) {
            call.reject("Location permission is required to discover Bluetooth devices on this Android version");
            return;
        }
        performBleScan(call);
    }

    @PermissionCallback
    private void wifiLocationPermissionCallback(PluginCall call) {
        if (getPermissionState("location") != PermissionState.GRANTED) {
            call.reject("Location permission is required by Android to scan nearby Wi-Fi networks");
            return;
        }
        performWifiScan(call);
    }

    @SuppressLint("MissingPermission")
    private void performBleScan(PluginCall call) {
        BluetoothManager manager = (BluetoothManager) getContext().getSystemService(Context.BLUETOOTH_SERVICE);
        BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
        BluetoothLeScanner scanner = adapter == null ? null : adapter.getBluetoothLeScanner();
        if (adapter == null || !adapter.isEnabled() || scanner == null) {
            call.reject("Bluetooth is unavailable or disabled");
            return;
        }
        int requestedDurationMs = Math.max(1500, Math.min(call.getInt("durationMs", 4500), 10000));
        Map<String, BleDeviceRecord> results = new LinkedHashMap<>();
        addBondedDevices(adapter, results);
        int durationMs = results.isEmpty() ? requestedDurationMs : Math.min(requestedDurationMs, 1800);
        AtomicBoolean completed = new AtomicBoolean(false);
        ScanCallback callback = new ScanCallback() {
            @Override
            public void onScanResult(int callbackType, ScanResult result) {
                BluetoothDevice device = result.getDevice();
                String name = result.getScanRecord() == null ? null : result.getScanRecord().getDeviceName();
                if (name == null) name = device.getName();
                boolean serviceMatch = advertisesSynCapService(result);
                if (!isSynCapDeviceName(name) && !serviceMatch) return;
                boolean bonded = device.getBondState() == BluetoothDevice.BOND_BONDED;
                BleDeviceRecord previous = results.get(device.getAddress());
                String displayName = name == null || name.trim().isEmpty()
                    ? "SynCap device · " + device.getAddress().substring(device.getAddress().length() - 5)
                    : name;
                results.put(
                    device.getAddress(),
                    new BleDeviceRecord(
                        device.getAddress(), displayName, result.getRssi(), bonded || (previous != null && previous.bonded), true
                    )
                );
            }

            @Override
            public void onScanFailed(int errorCode) {
                if (!completed.compareAndSet(false, true)) return;
                if (results.isEmpty()) call.reject("BLE scan failed: " + errorCode);
                else resolveBleScan(call, results, false, errorCode);
            }
        };
        ScanSettings settings = new ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build();
        try {
            scanner.startScan(null, settings, callback);
        } catch (Exception error) {
            if (results.isEmpty()) call.reject("Unable to start BLE scan", error);
            else resolveBleScan(call, results, false, -1);
            return;
        }
        handler.postDelayed(() -> {
            if (!completed.compareAndSet(false, true)) return;
            scanner.stopScan(callback);
            addBondedDevices(adapter, results);
            resolveBleScan(call, results, true, null);
        }, durationMs);
    }

    @SuppressLint({ "MissingPermission", "UnspecifiedRegisterReceiverFlag" })
    private void performWifiScan(PluginCall call) {
        LocationManager locationManager = (LocationManager) getContext().getSystemService(Context.LOCATION_SERVICE);
        boolean locationEnabled;
        if (locationManager == null) {
            locationEnabled = false;
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            locationEnabled = locationManager.isLocationEnabled();
        } else {
            locationEnabled = locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)
                || locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER);
        }
        if (!locationEnabled) {
            call.reject("Android location services must be enabled to scan nearby Wi-Fi networks");
            return;
        }

        WifiManager wifiManager = (WifiManager) getContext().getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        if (wifiManager == null || !wifiManager.isWifiEnabled()) {
            call.reject("Wi-Fi is unavailable or disabled");
            return;
        }

        AtomicBoolean completed = new AtomicBoolean(false);
        BroadcastReceiver receiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                if (!completed.compareAndSet(false, true)) return;
                unregisterReceiver(this);
                boolean fresh = intent.getBooleanExtra(WifiManager.EXTRA_RESULTS_UPDATED, false);
                resolveWifiScan(call, wifiManager, fresh);
            }
        };

        IntentFilter filter = new IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            getContext().registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            getContext().registerReceiver(receiver, filter);
        }

        boolean scanStarted;
        try {
            scanStarted = wifiManager.startScan();
        } catch (Exception error) {
            completed.set(true);
            unregisterReceiver(receiver);
            call.reject("Unable to start Wi-Fi scan", error);
            return;
        }

        if (!scanStarted) {
            if (completed.compareAndSet(false, true)) {
                unregisterReceiver(receiver);
                resolveWifiScan(call, wifiManager, false);
            }
            return;
        }

        int timeoutMs = Math.max(2000, Math.min(call.getInt("timeoutMs", 6500), 10000));
        handler.postDelayed(() -> {
            if (!completed.compareAndSet(false, true)) return;
            unregisterReceiver(receiver);
            resolveWifiScan(call, wifiManager, false);
        }, timeoutMs);
    }

    @SuppressLint("MissingPermission")
    private void addBondedDevices(BluetoothAdapter adapter, Map<String, BleDeviceRecord> results) {
        for (BluetoothDevice device : adapter.getBondedDevices()) {
            String name = device.getName();
            if (!isSynCapDeviceName(name)) continue;
            BleDeviceRecord previous = results.get(device.getAddress());
            results.put(
                device.getAddress(),
                new BleDeviceRecord(
                    device.getAddress(), name, previous == null ? -127 : previous.rssi, true, previous != null && previous.advertising
                )
            );
        }
    }

    private boolean isSynCapDeviceName(String name) {
        if (name == null) return false;
        String normalized = name.trim().toLowerCase(Locale.ROOT).replace(" ", "").replace("_", "").replace("-", "");
        return normalized.startsWith("syncap") || normalized.startsWith("robobaton");
    }

    private boolean advertisesSynCapService(ScanResult result) {
        if (result.getScanRecord() == null) return false;
        List<ParcelUuid> serviceUuids = result.getScanRecord().getServiceUuids();
        if (serviceUuids == null) return false;
        for (ParcelUuid serviceUuid : serviceUuids) {
            if (WIFI_SERVICE_UUID.equals(serviceUuid.getUuid())) return true;
        }
        return false;
    }

    private void resolveBleScan(
        PluginCall call,
        Map<String, BleDeviceRecord> results,
        boolean scanComplete,
        Integer scanError
    ) {
        ArrayList<BleDeviceRecord> sorted = new ArrayList<>(results.values());
        sorted.sort((left, right) -> {
            if (left.bonded != right.bonded) return left.bonded ? -1 : 1;
            return Integer.compare(right.rssi, left.rssi);
        });
        JSArray devices = new JSArray();
        for (BleDeviceRecord record : sorted) {
            JSObject item = new JSObject();
            item.put("id", record.address);
            item.put("address", record.address);
            item.put("name", record.name);
            item.put("rssi", record.rssi);
            item.put("paired", record.bonded);
            item.put("bonded", record.bonded);
            item.put("advertising", record.advertising);
            devices.put(item);
        }
        JSObject response = new JSObject();
        response.put("devices", devices);
        response.put("scanComplete", scanComplete);
        if (scanError != null) response.put("scanError", scanError);
        call.resolve(response);
    }

    @SuppressLint("MissingPermission")
    private void resolveWifiScan(PluginCall call, WifiManager wifiManager, boolean fresh) {
        try {
            String connectedSsid = wifiManager.getConnectionInfo().getSSID();
            if (connectedSsid != null && connectedSsid.length() >= 2
                && connectedSsid.startsWith("\"") && connectedSsid.endsWith("\"")) {
                connectedSsid = connectedSsid.substring(1, connectedSsid.length() - 1);
            }
            Map<String, android.net.wifi.ScanResult> strongest = new LinkedHashMap<>();
            for (android.net.wifi.ScanResult result : wifiManager.getScanResults()) {
                String ssid = result.SSID == null ? "" : result.SSID.trim();
                if (ssid.isEmpty() || ssid.equals("<unknown ssid>")) continue;
                android.net.wifi.ScanResult previous = strongest.get(ssid);
                if (previous == null || result.level > previous.level) strongest.put(ssid, result);
            }
            ArrayList<android.net.wifi.ScanResult> sorted = new ArrayList<>(strongest.values());
            sorted.sort(Comparator.comparingInt((android.net.wifi.ScanResult result) -> result.level).reversed());

            JSArray networks = new JSArray();
            for (android.net.wifi.ScanResult result : sorted) {
                String security = wifiSecurity(result.capabilities);
                JSObject item = new JSObject();
                item.put("ssid", result.SSID.trim());
                item.put("rssi", result.level);
                item.put("signal", WifiManager.calculateSignalLevel(result.level, 5));
                item.put("frequency", result.frequency);
                item.put("security", security);
                item.put("secure", !security.equals("open"));
                item.put("connected", result.SSID.trim().equals(connectedSsid));
                networks.put(item);
            }

            JSObject response = new JSObject();
            response.put("networks", networks);
            response.put("fresh", fresh);
            response.put("scannedAt", System.currentTimeMillis());
            call.resolve(response);
        } catch (SecurityException error) {
            call.reject("Android denied access to nearby Wi-Fi scan results", error);
        }
    }

    private String wifiSecurity(String capabilities) {
        String value = capabilities == null ? "" : capabilities.toUpperCase(Locale.ROOT);
        if (value.contains("SAE") || value.contains("WPA3")) return "wpa3-sae";
        if (value.contains("OWE")) return "owe";
        if (value.contains("EAP") || value.contains("SUITE_B")) return "enterprise";
        if (value.contains("PSK") && value.contains("WPA2")) return "wpa2-psk";
        if (value.contains("PSK")) return "wpa-psk";
        if (value.contains("WEP")) return "wep";
        return "open";
    }

    private void unregisterReceiver(BroadcastReceiver receiver) {
        try {
            getContext().unregisterReceiver(receiver);
        } catch (IllegalArgumentException ignored) {
            // The scan may resolve through its timeout at the same time as the system broadcast.
        }
    }

    private static final class BleDeviceRecord {
        final String address;
        final String name;
        final int rssi;
        final boolean bonded;
        final boolean advertising;

        BleDeviceRecord(String address, String name, int rssi, boolean bonded, boolean advertising) {
            this.address = address;
            this.name = name;
            this.rssi = rssi;
            this.bonded = bonded;
            this.advertising = advertising;
        }
    }

    @SuppressLint("MissingPermission")
    private void performDeviceWifiScan(PluginCall call) {
        String address = call.getString("address", "");
        if (!BluetoothAdapter.checkBluetoothAddress(address)) {
            call.reject("Invalid Bluetooth address");
            return;
        }
        BluetoothManager manager = (BluetoothManager) getContext().getSystemService(Context.BLUETOOTH_SERVICE);
        BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
        if (adapter == null || !adapter.isEnabled()) {
            call.reject("Bluetooth is unavailable or disabled");
            return;
        }
        try {
            new BleGattOperation(
                adapter.getRemoteDevice(address),
                buildWifiScanPayload().getBytes(StandardCharsets.UTF_8),
                call,
                BLE_WIFI_SCAN_TIMEOUT_MS
            ).start();
        } catch (Exception error) {
            call.reject("Unable to scan Wi-Fi from the headring", error);
        }
    }

    static String buildWifiScanPayload() {
        return "{\"op\":\"wifi.scan\",\"claimCode\":\"" + FIXED_CLAIM_CODE + "\"}";
    }

    @SuppressLint("MissingPermission")
    private void performWifiConfiguration(PluginCall call) {
        String address = call.getString("address", "");
        String ssid = call.getString("ssid", "");
        String password = call.getString("password", "");
        String security = call.getString("security", "wpa2-psk");
        if (!BluetoothAdapter.checkBluetoothAddress(address)) {
            call.reject("Invalid Bluetooth address");
            return;
        }
        if (ssid.isEmpty()) {
            call.reject("SSID is required");
            return;
        }
        BluetoothManager manager = (BluetoothManager) getContext().getSystemService(Context.BLUETOOTH_SERVICE);
        BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
        if (adapter == null || !adapter.isEnabled()) {
            call.reject("Bluetooth is unavailable or disabled");
            return;
        }
        try {
            JSObject payload = new JSObject();
            payload.put("op", "wifi.configure");
            payload.put("ssid", ssid);
            payload.put("password", password);
            payload.put("security", security);
            payload.put("claimCode", FIXED_CLAIM_CODE);
            new BleGattOperation(
                adapter.getRemoteDevice(address), payload.toString().getBytes(StandardCharsets.UTF_8), call
            ).start();
        } catch (Exception error) {
            call.reject("Unable to begin BLE provisioning", error);
        }
    }

    @SuppressLint("MissingPermission")
    private void performOfflineCommand(PluginCall call) {
        String address = call.getString("address", "");
        String op = call.getString("op", "");
        boolean backgroundRecovery = call.getBoolean("backgroundRecovery", false);
        if (!BluetoothAdapter.checkBluetoothAddress(address)) {
            call.reject("Invalid Bluetooth address");
            return;
        }
        if (!Arrays.asList(
            "storage.configure", "storage.eject", "capture.start", "capture.stop", "device.status"
        ).contains(op)) {
            call.reject("Unsupported offline BLE operation");
            return;
        }
        if (backgroundRecovery && !op.equals("device.status")) {
            call.reject("Background recovery only supports device.status");
            return;
        }
        String captureId = call.getString("captureId", "");
        if (op.equals("capture.stop") && !validOpaqueIdentifier(captureId)) {
            call.reject("Invalid capture id");
            return;
        }
        BluetoothManager manager = (BluetoothManager) getContext().getSystemService(Context.BLUETOOTH_SERVICE);
        BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
        if (adapter == null || !adapter.isEnabled()) {
            call.reject("Bluetooth is unavailable or disabled");
            return;
        }
        try {
            JSObject payload = new JSObject();
            payload.put("op", op);
            payload.put("claimCode", FIXED_CLAIM_CODE);
            if (op.equals("storage.configure")) payload.put("target", call.getString("target", "usb"));
            if (op.equals("capture.start")) payload.put("name", call.getString("name", "offline_session"));
            if (op.equals("capture.stop")) payload.put("captureId", captureId);
            int timeoutMs = op.equals("device.status") ? BLE_DEVICE_STATUS_TIMEOUT_MS : BLE_OPERATION_TIMEOUT_MS;
            new BleGattOperation(
                adapter.getRemoteDevice(address), payload.toString().getBytes(StandardCharsets.UTF_8), call,
                timeoutMs, backgroundRecovery
            ).start();
        } catch (Exception error) {
            call.reject("Unable to begin offline BLE operation", error);
        }
    }

    private JSObject requestJson(String host, int port, String path, String method, JSObject body) throws Exception {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL("http", host, port, path).openConnection();
            boolean fastDeviceRead = method.equals("GET")
                && (path.equals("/v1/status") || path.equals("/v1/manifest"));
            connection.setConnectTimeout(fastDeviceRead ? 1500 : 3000);
            connection.setReadTimeout(path.equals("/v1/status")
                ? DEVICE_STATUS_READ_TIMEOUT_MS
                : fastDeviceRead ? FAST_DEVICE_READ_TIMEOUT_MS : 60000);
            connection.setRequestMethod(method);
            connection.setRequestProperty("Accept", "application/json");
            if (body != null) {
                byte[] encoded = body.toString().getBytes(StandardCharsets.UTF_8);
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                connection.setFixedLengthStreamingMode(encoded.length);
                connection.getOutputStream().write(encoded);
            }
            int statusCode = connection.getResponseCode();
            InputStream stream = statusCode >= 200 && statusCode < 300
                ? connection.getInputStream()
                : connection.getErrorStream();
            String response = stream == null ? "" : readStream(stream);
            if (statusCode < 200 || statusCode >= 300) {
                throw DeviceHttpException.fromResponse(statusCode, response);
            }
            return new JSObject(response);
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private String readStream(InputStream stream) throws IOException {
        StringBuilder body = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) body.append(line);
        }
        return body.toString();
    }

    static final class DeviceHttpException extends IOException {
        final int statusCode;
        final String serverCode;
        final String serverMessage;

        private DeviceHttpException(int statusCode, String serverCode, String serverMessage, String responseBody) {
            super("HTTP " + statusCode + (responseBody.isEmpty() ? "" : ": " + responseBody));
            this.statusCode = statusCode;
            this.serverCode = serverCode;
            this.serverMessage = serverMessage;
        }

        static DeviceHttpException fromResponse(int statusCode, String responseBody) {
            String body = responseBody == null ? "" : responseBody.trim();
            String serverCode = null;
            String serverMessage = null;
            if (!body.isEmpty()) {
                try {
                    JSONObject payload = new JSONObject(body);
                    serverCode = nonEmpty(payload.optString("error", null));
                    serverMessage = nonEmpty(payload.optString("message", null));
                } catch (Exception ignored) {
                    // Non-JSON error responses retain the operation's stable fallback message.
                }
            }
            return fromParsedResponse(statusCode, serverCode, serverMessage, body);
        }

        static DeviceHttpException fromParsedResponse(
            int statusCode,
            String serverCode,
            String serverMessage,
            String responseBody
        ) {
            return new DeviceHttpException(
                statusCode,
                nonEmpty(serverCode),
                nonEmpty(serverMessage),
                responseBody == null ? "" : responseBody.trim()
            );
        }

        String messageFor(String fallbackMessage) {
            return serverMessage == null ? fallbackMessage : serverMessage;
        }

        String rejectionCode() {
            if (serverCode == null) return "SYNCAP_HTTP_" + statusCode;
            String normalized = serverCode.toUpperCase(Locale.ROOT).replaceAll("[^A-Z0-9]+", "_");
            return "SYNCAP_" + normalized;
        }

        private static String nonEmpty(String value) {
            if (value == null) return null;
            String trimmed = value.trim();
            return trimmed.isEmpty() ? null : trimmed;
        }
    }

    private void downloadWithResume(
        String host,
        int port,
        String path,
        File output,
        long expectedSize,
        String expectedSha256
    ) throws Exception {
        if (output.isFile() && output.length() == expectedSize && sha256(output).equalsIgnoreCase(expectedSha256)) {
            return;
        }
        File partial = new File(output.getParentFile(), output.getName() + ".part");
        if (partial.length() > expectedSize && !partial.delete() && partial.exists()) {
            throw new IOException("Unable to discard oversized partial download");
        }
        long existing = partial.isFile() ? partial.length() : 0;
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL("http", host, port, path).openConnection();
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(30000);
            connection.setRequestMethod("GET");
            if (existing > 0) connection.setRequestProperty("Range", "bytes=" + existing + "-");
            int statusCode = connection.getResponseCode();
            if (statusCode != HttpURLConnection.HTTP_OK && statusCode != HttpURLConnection.HTTP_PARTIAL) {
                throw new IOException("Download returned HTTP " + statusCode);
            }
            boolean append = existing > 0 && statusCode == HttpURLConnection.HTTP_PARTIAL;
            try (RandomAccessFile destination = new RandomAccessFile(partial, "rw");
                 InputStream source = connection.getInputStream()) {
                if (append) destination.seek(existing);
                else destination.setLength(0);
                byte[] buffer = new byte[128 * 1024];
                int count;
                while ((count = source.read(buffer)) != -1) destination.write(buffer, 0, count);
            }
        } finally {
            if (connection != null) connection.disconnect();
        }
        if (partial.length() != expectedSize) {
            throw new IOException("Downloaded size does not match session manifest");
        }
        String actualSha256 = sha256(partial);
        if (!actualSha256.equalsIgnoreCase(expectedSha256)) {
            throw new IOException("SHA-256 verification failed for " + output.getName());
        }
        try {
            Os.rename(partial.getAbsolutePath(), output.getAbsolutePath());
        } catch (ErrnoException error) {
            throw new IOException("Unable to finalize session download", error);
        }
    }

    private long downloadSessionManifest(
        String host,
        int port,
        String path,
        File output,
        String expectedSessionId
    ) throws Exception {
        final long maximumManifestBytes = 8L * 1024L * 1024L;
        File partial = new File(output.getParentFile(), output.getName() + ".part");
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL("http", host, port, path).openConnection();
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(30000);
            connection.setRequestMethod("GET");
            connection.setRequestProperty("Accept", "application/json");
            int statusCode = connection.getResponseCode();
            if (statusCode != HttpURLConnection.HTTP_OK) {
                throw new IOException("Session manifest download returned HTTP " + statusCode);
            }
            long declaredLength = connection.getContentLengthLong();
            if (declaredLength > maximumManifestBytes) {
                throw new IOException("Session manifest is too large");
            }
            try (RandomAccessFile destination = new RandomAccessFile(partial, "rw");
                 InputStream source = connection.getInputStream()) {
                destination.setLength(0);
                byte[] buffer = new byte[32 * 1024];
                long total = 0;
                int count;
                while ((count = source.read(buffer)) != -1) {
                    total += count;
                    if (total > maximumManifestBytes) {
                        throw new IOException("Session manifest is too large");
                    }
                    destination.write(buffer, 0, count);
                }
            }

            byte[] payload = new byte[(int) partial.length()];
            try (InputStream input = new FileInputStream(partial)) {
                int offset = 0;
                while (offset < payload.length) {
                    int count = input.read(payload, offset, payload.length - offset);
                    if (count < 0) throw new IOException("Session manifest download is incomplete");
                    offset += count;
                }
            }
            validateSessionManifest(new String(payload, StandardCharsets.UTF_8), expectedSessionId);
            try {
                Os.rename(partial.getAbsolutePath(), output.getAbsolutePath());
            } catch (ErrnoException error) {
                throw new IOException("Unable to save session manifest", error);
            }
            return payload.length;
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    static void validateSessionManifest(String payload, String expectedSessionId) throws IOException {
        try {
            JSONObject manifest = new JSONObject(payload);
            LinkedHashMap<String, String> declaredIdentities = new LinkedHashMap<>();
            for (String key : Arrays.asList("id", "sessionId", "session_id")) {
                if (manifest.has(key)) declaredIdentities.put(key, manifest.optString(key, null));
            }
            validateDeclaredSessionIdentities(declaredIdentities, expectedSessionId);
        } catch (IOException error) {
            throw error;
        } catch (Exception error) {
            throw new IOException("Session manifest is not valid JSON", error);
        }
    }

    static void validateDeclaredSessionIdentities(
        Map<String, String> declaredIdentities,
        String expectedSessionId
    ) throws IOException {
        for (String value : declaredIdentities.values()) {
            if (!expectedSessionId.equals(value)) {
                throw new IOException("Session manifest identity does not match the export request");
            }
        }
    }

    static String normalizeSessionManifestPath(
        String value,
        String host,
        int port,
        String sessionId
    ) throws IOException {
        String expectedPath = "/v1/sessions/" + sessionId + "/manifest";
        if (expectedPath.equals(value)) return expectedPath;
        try {
            URL absolute = new URL(value);
            int effectivePort = absolute.getPort() >= 0 ? absolute.getPort() : absolute.getDefaultPort();
            if (!"http".equalsIgnoreCase(absolute.getProtocol())
                || !host.equalsIgnoreCase(absolute.getHost())
                || effectivePort != port
                || !expectedPath.equals(absolute.getPath())
                || absolute.getUserInfo() != null
                || absolute.getQuery() != null
                || absolute.getRef() != null) {
                throw new IOException("Export manifest returned an unexpected session manifest URL");
            }
            return expectedPath;
        } catch (IOException error) {
            throw error;
        } catch (Exception error) {
            throw new IOException("Export manifest returned an invalid session manifest URL", error);
        }
    }

    private String sha256(File path) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream input = new FileInputStream(path)) {
            byte[] buffer = new byte[1024 * 1024];
            int count;
            while ((count = input.read(buffer)) != -1) digest.update(buffer, 0, count);
        }
        StringBuilder output = new StringBuilder();
        for (byte value : digest.digest()) output.append(String.format(Locale.ROOT, "%02x", value));
        return output.toString();
    }

    static boolean validOpaqueIdentifier(String value) {
        return value != null
            && value.length() > 0
            && value.length() <= 96
            && !value.equals(".")
            && !value.equals("..")
            && value.matches("[A-Za-z0-9][A-Za-z0-9._-]*");
    }

    static void writeRfcommFrame(OutputStream output, byte[] payload) throws IOException {
        if (output == null) throw new IOException("RFCOMM output stream is unavailable");
        if (payload == null || payload.length == 0 || payload.length > BLUETOOTH_COMMAND_MAX_BYTES) {
            throw new IOException("RFCOMM JSON payload length is invalid");
        }
        byte[] header = ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN).putInt(payload.length).array();
        output.write(header);
        output.write(payload);
        output.flush();
    }

    static byte[] readRfcommFrame(InputStream input) throws IOException {
        if (input == null) throw new IOException("RFCOMM input stream is unavailable");
        byte[] header = new byte[4];
        readRfcommFully(input, header);
        int length = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN).getInt();
        if (length <= 0 || length > BLUETOOTH_COMMAND_MAX_BYTES) {
            throw new IOException("RFCOMM JSON payload length is invalid");
        }
        byte[] payload = new byte[length];
        readRfcommFully(input, payload);
        return payload;
    }

    private static void readRfcommFully(InputStream input, byte[] destination) throws IOException {
        int offset = 0;
        while (offset < destination.length) {
            int count = input.read(destination, offset, destination.length - offset);
            if (count < 0) throw new EOFException("RFCOMM response ended before the declared payload length");
            if (count == 0) throw new IOException("RFCOMM response made no progress");
            offset += count;
        }
    }

    static String validateRfcommJson(byte[] payload) throws IOException {
        if (payload == null || payload.length == 0 || payload.length > BLUETOOTH_COMMAND_MAX_BYTES) {
            throw new IOException("RFCOMM JSON payload length is invalid");
        }
        final String value;
        try {
            value = StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(payload))
                .toString();
        } catch (CharacterCodingException error) {
            throw new IOException("RFCOMM response is not valid UTF-8", error);
        }
        if (!hasSingleJsonObjectEnvelope(value)) {
            throw new IOException("RFCOMM response is not one JSON object");
        }
        try {
            new JSONObject(value);
            return value;
        } catch (Exception error) {
            throw new IOException("RFCOMM response is not valid JSON", error);
        }
    }

    static boolean hasSingleJsonObjectEnvelope(String value) {
        if (value == null) return false;
        int index = 0;
        while (index < value.length() && isJsonWhitespace(value.charAt(index))) index++;
        if (index >= value.length() || value.charAt(index) != '{') return false;
        int depth = 0;
        boolean inString = false;
        boolean escaped = false;
        for (; index < value.length(); index++) {
            char character = value.charAt(index);
            if (inString) {
                if (escaped) escaped = false;
                else if (character == '\\') escaped = true;
                else if (character == '"') inString = false;
                continue;
            }
            if (character == '"') {
                inString = true;
            } else if (character == '{') {
                depth++;
            } else if (character == '}') {
                depth--;
                if (depth < 0) return false;
                if (depth == 0) {
                    index++;
                    while (index < value.length() && isJsonWhitespace(value.charAt(index))) index++;
                    return index == value.length();
                }
            }
        }
        return false;
    }

    private static boolean isJsonWhitespace(char value) {
        return value == ' ' || value == '\t' || value == '\n' || value == '\r';
    }

    static boolean shouldUseRfcommFallback(
        boolean bonded,
        boolean gattWriteStarted,
        long nowMs,
        long deadlineMs
    ) {
        return bonded && !gattWriteStarted && nowMs < deadlineMs;
    }

    static boolean shouldUseDirectRfcomm(boolean bonded, int deviceType, boolean advertisesSpp) {
        return bonded && (deviceType == BluetoothDevice.DEVICE_TYPE_CLASSIC
            || (deviceType == BluetoothDevice.DEVICE_TYPE_DUAL && advertisesSpp));
    }

    static boolean shouldSkipBackgroundRecovery(boolean backgroundRecovery, boolean permissionGranted, int bondState) {
        return backgroundRecovery && (!permissionGranted || bondState != BluetoothDevice.BOND_BONDED);
    }

    static boolean shouldRetryGattAuthentication(boolean backgroundRecovery, int status, int retries) {
        return !backgroundRecovery && (status == 5 || status == 15) && retries < 3;
    }

    private void resolveSkippedBackgroundRecovery(PluginCall call) {
        call.resolve(skippedBackgroundRecoveryResponse());
    }

    private JSObject skippedBackgroundRecoveryResponse() {
        JSObject result = new JSObject();
        result.put("state", "skipped");
        result.put("op", "device.status");
        result.put("error", "background_recovery_requires_pairing_and_permission");
        return result;
    }

    @SuppressLint("MissingPermission")
    private boolean shouldSkipBackgroundRecovery(BluetoothDevice device, boolean backgroundRecovery) {
        if (!backgroundRecovery) return false;
        boolean permissionGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.S
            || getPermissionState("bluetooth") == PermissionState.GRANTED;
        int bondState = BluetoothDevice.BOND_NONE;
        if (permissionGranted) {
            try {
                bondState = device.getBondState();
            } catch (RuntimeException error) {
                Log.w(TAG, "background recovery cannot inspect bond state", error);
            }
        }
        boolean skip = shouldSkipBackgroundRecovery(true, permissionGranted, bondState);
        if (skip) Log.d(TAG, "skip background recovery permission=" + permissionGranted + " bondState=" + bondState);
        return skip;
    }

    enum BluetoothFailureRoute {
        COMPLETE,
        ALREADY_COMPLETED,
        RFCOMM
    }

    static final class GattRfcommHandoff {
        private boolean gattWriteStarted;

        synchronized boolean beginGattWrite(AtomicBoolean completed) {
            if (completed.get()) return false;
            gattWriteStarted = true;
            return true;
        }

        synchronized boolean canUseRfcommFallback(boolean bonded, long nowMs, long deadlineMs) {
            return shouldUseRfcommFallback(bonded, gattWriteStarted, nowMs, deadlineMs);
        }

        synchronized BluetoothFailureRoute routeFailure(
            boolean bonded,
            long nowMs,
            long deadlineMs,
            AtomicBoolean completed
        ) {
            if (!completed.compareAndSet(false, true)) return BluetoothFailureRoute.ALREADY_COMPLETED;
            return canUseRfcommFallback(bonded, nowMs, deadlineMs)
                ? BluetoothFailureRoute.RFCOMM
                : BluetoothFailureRoute.COMPLETE;
        }

        synchronized boolean complete(AtomicBoolean completed) {
            return completed.compareAndSet(false, true);
        }

        synchronized boolean hasGattWriteStarted() {
            return gattWriteStarted;
        }
    }

    @SuppressLint("MissingPermission")
    private final class RfcommOperation implements CancellableOperation {
        private final BluetoothDevice device;
        private final byte[] payload;
        private final PluginCall call;
        private final long deadline;
        private final String logPrefix;
        private final boolean backgroundRecovery;
        private final AtomicBoolean completed = new AtomicBoolean(false);
        private final Object socketLock = new Object();
        private final Runnable timeoutTask = this::timeout;
        private BluetoothSocket socket;

        RfcommOperation(
            BluetoothDevice device,
            byte[] payload,
            PluginCall call,
            long deadline,
            String logPrefix,
            boolean backgroundRecovery
        ) {
            this.device = device;
            this.payload = payload;
            this.call = call;
            this.deadline = deadline;
            this.logPrefix = logPrefix;
            this.backgroundRecovery = backgroundRecovery;
        }

        void start() {
            if (!activeRfcommOperations.register(this)) {
                cancel();
                return;
            }
            long remainingMs = deadline - SystemClock.elapsedRealtime();
            if (remainingMs <= 0) {
                fail("Bluetooth operation timed out", null);
                return;
            }
            handler.postDelayed(timeoutTask, remainingMs);
            try {
                ioExecutors.control.execute(this::run);
            } catch (RejectedExecutionException error) {
                fail("Unable to schedule Bluetooth fallback", error);
            }
        }

        private void run() {
            BluetoothSocket connection = null;
            try {
                if (completed.get() || shuttingDown.get()) return;
                if (shouldSkipBackgroundRecovery(device, backgroundRecovery)) {
                    finish(skippedBackgroundRecoveryResponse());
                    return;
                }
                if (device.getBondState() != BluetoothDevice.BOND_BONDED) {
                    throw new IOException("Bluetooth Classic fallback requires a paired device");
                }
                validateRfcommJson(payload);
                if (SystemClock.elapsedRealtime() >= deadline) throw new SocketTimeoutException();

                BluetoothManager manager = (BluetoothManager) getContext().getSystemService(Context.BLUETOOTH_SERVICE);
                BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
                if (adapter != null) adapter.cancelDiscovery();

                connection = device.createRfcommSocketToServiceRecord(SPP_SERVICE_UUID);
                if (!setActiveSocket(connection)) return;
                log("RFCOMM connecting");
                connection.connect();
                if (SystemClock.elapsedRealtime() >= deadline) throw new SocketTimeoutException();

                writeRfcommFrame(connection.getOutputStream(), payload);
                byte[] responsePayload = readRfcommFrame(connection.getInputStream());
                String responseJson = validateRfcommJson(responsePayload);
                finish(new JSObject(responseJson));
            } catch (SocketTimeoutException error) {
                fail("Bluetooth operation timed out", error);
            } catch (Exception error) {
                fail("Unable to complete Bluetooth provisioning", error);
            } finally {
                clearAndClose(connection);
            }
        }

        private boolean setActiveSocket(BluetoothSocket connection) {
            synchronized (socketLock) {
                if (completed.get()) {
                    closeSocket(connection);
                    return false;
                }
                socket = connection;
                return true;
            }
        }

        private void timeout() {
            fail("Bluetooth operation timed out", null);
        }

        private void finish(JSObject result) {
            if (!completed.compareAndSet(false, true)) return;
            log("RFCOMM finish");
            handler.removeCallbacks(timeoutTask);
            closeActiveSocket();
            activeRfcommOperations.unregister(this);
            if (!shuttingDown.get()) call.resolve(result);
        }

        private void fail(String message, Exception error) {
            if (!completed.compareAndSet(false, true)) return;
            String failureLog = logPrefix + " RFCOMM fail reason=" + message;
            if (error == null) {
                Log.w(TAG, failureLog);
            } else {
                Log.w(TAG, failureLog + " exception=" + error.getClass().getName(), error);
            }
            handler.removeCallbacks(timeoutTask);
            closeActiveSocket();
            activeRfcommOperations.unregister(this);
            if (shuttingDown.get()) return;
            if (error == null) call.reject(message);
            else call.reject(message, error);
        }

        @Override
        public void cancel() {
            if (!completed.compareAndSet(false, true)) return;
            log("RFCOMM cancelled");
            handler.removeCallbacks(timeoutTask);
            closeActiveSocket();
            activeRfcommOperations.unregister(this);
        }

        private void closeActiveSocket() {
            BluetoothSocket connection;
            synchronized (socketLock) {
                connection = socket;
                socket = null;
            }
            closeSocket(connection);
        }

        private void clearAndClose(BluetoothSocket connection) {
            if (connection == null) return;
            boolean shouldClose;
            synchronized (socketLock) {
                shouldClose = socket == connection;
                if (shouldClose) socket = null;
            }
            if (shouldClose) closeSocket(connection);
        }

        private void closeSocket(BluetoothSocket connection) {
            if (connection == null) return;
            try {
                connection.close();
            } catch (IOException error) {
                Log.w(TAG, logPrefix + " RFCOMM close failed", error);
            }
        }

        private void log(String message) {
            Log.d(TAG, logPrefix + " " + message);
        }
    }

    @SuppressLint("MissingPermission")
    private final class BleGattOperation extends BluetoothGattCallback {
        private final BluetoothDevice device;
        private final byte[] payload;
        private final PluginCall call;
        private final int operationId = BLE_OPERATION_SEQUENCE.incrementAndGet();
        private final int timeoutMs;
        private final boolean backgroundRecovery;
        private final long deadline;
        private final String operationName;
        private final AtomicBoolean completed = new AtomicBoolean(false);
        private final GattRfcommHandoff handoff = new GattRfcommHandoff();
        private volatile BluetoothGatt gatt;
        private volatile BluetoothGattCharacteristic configCharacteristic;
        private volatile BluetoothGattCharacteristic statusCharacteristic;
        private volatile boolean discoveryStarted;
        private volatile boolean servicesReady;
        private int serviceFallbackAttempts;
        private int authenticationRetries;
        private int bondWaitAttempts;
        private int negotiatedMtu = 23;
        private ArrayList<byte[]> payloadChunks;
        private int payloadChunkIndex;
        private int connectionAttempts;
        private volatile boolean connected;
        private volatile boolean reconnectScheduled;
        private boolean skipMtuNegotiation;

        BleGattOperation(BluetoothDevice device, byte[] payload, PluginCall call) {
            this(device, payload, call, BLE_OPERATION_TIMEOUT_MS);
        }

        BleGattOperation(BluetoothDevice device, byte[] payload, PluginCall call, int timeoutMs) {
            this(device, payload, call, timeoutMs, false);
        }

        BleGattOperation(BluetoothDevice device, byte[] payload, PluginCall call, int timeoutMs, boolean backgroundRecovery) {
            this.device = device;
            this.payload = payload;
            this.call = call;
            this.timeoutMs = timeoutMs;
            this.backgroundRecovery = backgroundRecovery;
            this.deadline = SystemClock.elapsedRealtime() + timeoutMs;
            this.operationName = operationName(payload);
        }

        void start() {
            if (payload.length > BLUETOOTH_COMMAND_MAX_BYTES) {
                call.reject("BLE command payload is too large");
                return;
            }
            if (skipBackgroundRecoveryIfNeeded()) return;
            boolean bonded = false;
            int deviceType = BluetoothDevice.DEVICE_TYPE_UNKNOWN;
            boolean advertisesSpp = false;
            try {
                bonded = device.getBondState() == BluetoothDevice.BOND_BONDED;
                deviceType = device.getType();
                ParcelUuid[] services = device.getUuids();
                if (services != null) {
                    for (ParcelUuid service : services) {
                        if (service != null && SPP_SERVICE_UUID.equals(service.getUuid())) {
                            advertisesSpp = true;
                            break;
                        }
                    }
                }
            } catch (RuntimeException error) {
                Log.w(TAG, prefix() + " unable to inspect Bluetooth transport", error);
            }
            log("start timeoutMs=" + timeoutMs + " bonded=" + bonded + " deviceType=" + deviceType
                + " advertisesSpp=" + advertisesSpp);
            if (shouldUseDirectRfcomm(bonded, deviceType, advertisesSpp)) {
                if (!completed.compareAndSet(false, true)) return;
                log("paired serial-port device; using RFCOMM directly");
                new RfcommOperation(device, payload, call, deadline, prefix(), backgroundRecovery).start();
                return;
            }
            handler.post(this::connect);
            handler.postDelayed(() -> fail("BLE operation timed out", null), timeoutMs);
        }

        private synchronized void connect() {
            if (completed.get()) return;
            if (skipBackgroundRecoveryIfNeeded()) return;
            reconnectScheduled = false;
            connected = false;
            discoveryStarted = false;
            servicesReady = false;
            serviceFallbackAttempts = 0;
            negotiatedMtu = 23;
            configCharacteristic = null;
            statusCharacteristic = null;
            bondWaitAttempts = 0;
            connectionAttempts++;
            log("connect attempt=" + connectionAttempts);
            try {
                gatt = device.connectGatt(getContext(), false, this, BluetoothDevice.TRANSPORT_LE);
            } catch (Exception error) {
                gatt = null;
                if (canUseRfcommFallback() || !scheduleConnectionRetry(null)) {
                    fail("Unable to create BLE connection", error);
                }
                return;
            }
            BluetoothGatt attemptGatt = gatt;
            if (attemptGatt == null) {
                if (canUseRfcommFallback() || !scheduleConnectionRetry(null)) {
                    fail("Unable to create BLE connection", null);
                }
                return;
            }
            handler.postDelayed(() -> {
                if (completed.get() || connected || gatt != attemptGatt || handoff.hasGattWriteStarted()) return;
                if (canUseRfcommFallback() || !scheduleConnectionRetry(attemptGatt)) {
                    fail("BLE connection timed out", null);
                }
            }, 12000);
        }

        @Override
        public void onConnectionStateChange(BluetoothGatt connection, int status, int newState) {
            if (connection != gatt) {
                closeGatt(connection, "stale callback");
                return;
            }
            if (skipBackgroundRecoveryIfNeeded()) return;
            log("connection state status=" + status + " newState=" + newState);
            if (status != BluetoothGatt.GATT_SUCCESS) {
                if (canUseRfcommFallback() || !scheduleConnectionRetry(connection)) {
                    fail("BLE connection failed: " + status, null);
                }
                return;
            }
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                connected = true;
                if (skipMtuNegotiation) {
                    log("skip MTU negotiation after callback timeout");
                    discover(connection, "MTU fallback reconnect");
                    return;
                }
                boolean mtuRequested;
                try {
                    mtuRequested = connection.requestMtu(512);
                } catch (RuntimeException error) {
                    log("MTU request threw " + error.getClass().getSimpleName());
                    mtuRequested = false;
                }
                log("MTU request accepted=" + mtuRequested);
                if (!mtuRequested) {
                    discover(connection, "MTU request unavailable");
                } else {
                    handler.postDelayed(
                        () -> handleMtuCallbackTimeout(connection),
                        BLE_MTU_CALLBACK_TIMEOUT_MS
                    );
                }
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED && !completed.get()) {
                connected = false;
                if (canUseRfcommFallback() || !scheduleConnectionRetry(connection)) {
                    fail("BLE device disconnected during operation", null);
                }
            }
        }

        @Override
        public void onMtuChanged(BluetoothGatt connection, int mtu, int status) {
            if (connection != gatt) return;
            if (status == BluetoothGatt.GATT_SUCCESS) negotiatedMtu = mtu;
            log("MTU changed status=" + status + " mtu=" + mtu);
            discover(connection, "MTU callback");
        }

        private synchronized void handleMtuCallbackTimeout(BluetoothGatt connection) {
            if (completed.get() || connection != gatt || discoveryStarted) return;
            skipMtuNegotiation = true;
            log("MTU callback timed out");
            if (canUseRfcommFallback() || !scheduleConnectionRetry(connection)) {
                fail("BLE MTU negotiation timed out", null);
            }
        }

        private synchronized void discover(BluetoothGatt connection, String source) {
            if (completed.get() || connection != gatt || discoveryStarted) return;
            discoveryStarted = true;
            log("discover services source=" + source);
            if (!connection.discoverServices()) {
                fail("Unable to discover SynCap BLE service", null);
                return;
            }
            handler.postDelayed(
                () -> pollDiscoveredServices(connection),
                BLE_SERVICE_FALLBACK_INTERVAL_MS
            );
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt connection, int status) {
            if (connection != gatt) return;
            log("services callback status=" + status);
            if (status != BluetoothGatt.GATT_SUCCESS) {
                fail("BLE service discovery failed: " + status, null);
                return;
            }
            if (!advanceAfterServiceDiscovery(connection, "callback")) {
                fail("BLE service discovery did not expose SynCap provisioning", null);
            }
        }

        private void pollDiscoveredServices(BluetoothGatt connection) {
            if (completed.get() || connection != gatt || servicesReady) return;
            serviceFallbackAttempts++;
            if (advanceAfterServiceDiscovery(connection, "fallback-" + serviceFallbackAttempts)) return;
            if (serviceFallbackAttempts >= BLE_SERVICE_FALLBACK_ATTEMPTS) {
                fail("BLE service discovery did not expose SynCap provisioning", null);
                return;
            }
            handler.postDelayed(
                () -> pollDiscoveredServices(connection),
                BLE_SERVICE_FALLBACK_INTERVAL_MS
            );
        }

        private synchronized boolean advanceAfterServiceDiscovery(BluetoothGatt connection, String source) {
            if (completed.get() || connection != gatt) return false;
            if (servicesReady) return true;
            BluetoothGattService service = connection.getService(WIFI_SERVICE_UUID);
            if (service == null) return false;
            BluetoothGattCharacteristic config = service.getCharacteristic(WIFI_CONFIG_UUID);
            BluetoothGattCharacteristic status = service.getCharacteristic(WIFI_STATUS_UUID);
            if (config == null || status == null) return false;
            configCharacteristic = config;
            statusCharacteristic = status;
            int fragmentPayloadSize = Math.max(1, negotiatedMtu - 19);
            int fragmentCount = Math.max(1, (payload.length + fragmentPayloadSize - 1) / fragmentPayloadSize);
            if (fragmentCount > BLE_MAX_FRAGMENTS) {
                fail("BLE command exceeds the negotiated packet limit", null);
                return true;
            }
            payloadChunks = fragmentPayload(payload, fragmentPayloadSize);
            servicesReady = true;
            log("services ready source=" + source + " mtu=" + negotiatedMtu
                + " chunks=" + payloadChunks.size());
            handler.post(() -> waitForBondThenWrite(connection));
            return true;
        }

        private void waitForBondThenWrite(BluetoothGatt connection) {
            if (completed.get() || connection != gatt) return;
            if (skipBackgroundRecoveryIfNeeded()) return;
            int bondState = device.getBondState();
            log("bond state=" + bondState + " waitAttempt=" + bondWaitAttempts);
            if (bondState == BluetoothDevice.BOND_BONDED || bondWaitAttempts >= 20) {
                writeConfiguration(connection);
                return;
            }
            if (!backgroundRecovery && bondState == BluetoothDevice.BOND_NONE) device.createBond();
            bondWaitAttempts++;
            handler.postDelayed(() -> waitForBondThenWrite(connection), 750);
        }

        private void writeConfiguration(BluetoothGatt connection) {
            if (completed.get() || connection != gatt || configCharacteristic == null) return;
            if (skipBackgroundRecoveryIfNeeded()) return;
            if (payloadChunks == null || payloadChunkIndex >= payloadChunks.size()) {
                fail("BLE provisioning payload was not prepared", null);
                return;
            }
            byte[] chunk = payloadChunks.get(payloadChunkIndex);
            if (!handoff.beginGattWrite(completed)) return;
            if (connection != gatt || configCharacteristic == null) return;
            log("write command chunk=" + (payloadChunkIndex + 1) + "/" + payloadChunks.size()
                + " bytes=" + chunk.length);
            boolean accepted;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                accepted = connection.writeCharacteristic(
                    configCharacteristic, chunk, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                ) == BluetoothStatusCodes.SUCCESS;
            } else {
                configCharacteristic.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
                configCharacteristic.setValue(chunk);
                accepted = connection.writeCharacteristic(configCharacteristic);
            }
            log("write command accepted=" + accepted);
            if (!accepted) fail("Unable to send encrypted BLE command", null);
        }

        private synchronized boolean scheduleConnectionRetry(BluetoothGatt connection) {
            if (skipBackgroundRecoveryIfNeeded()) return false;
            if (completed.get() || handoff.hasGattWriteStarted()
                || reconnectScheduled || connectionAttempts >= BLE_CONNECT_ATTEMPTS) {
                return false;
            }
            reconnectScheduled = true;
            connected = false;
            closeGatt(connection, "retry");
            long delayMs = 500L * connectionAttempts;
            log("retry scheduled delayMs=" + delayMs);
            handler.postDelayed(this::connect, delayMs);
            return true;
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt connection, BluetoothGattCharacteristic characteristic, int status) {
            if (connection != gatt) return;
            if (!WIFI_CONFIG_UUID.equals(characteristic.getUuid())) return;
            log("write callback status=" + status + " chunk=" + (payloadChunkIndex + 1));
            if (status == BluetoothGatt.GATT_SUCCESS) {
                payloadChunkIndex++;
                if (payloadChunks != null && payloadChunkIndex < payloadChunks.size()) {
                    handler.post(() -> writeConfiguration(connection));
                } else {
                    handler.postDelayed(() -> readProvisioningStatus(connection), 1000);
                }
            } else if (shouldRetryGattAuthentication(backgroundRecovery, status, authenticationRetries)) {
                authenticationRetries++;
                device.createBond();
                handler.postDelayed(() -> writeConfiguration(connection), 2000);
            } else {
                fail("Encrypted BLE write failed: " + status, null);
            }
        }

        private void readProvisioningStatus(BluetoothGatt connection) {
            if (completed.get() || connection != gatt || statusCharacteristic == null) return;
            if (skipBackgroundRecoveryIfNeeded()) return;
            boolean accepted = connection.readCharacteristic(statusCharacteristic);
            log("status read accepted=" + accepted);
            if (!accepted) {
                fail("Unable to read BLE operation status", null);
            }
        }

        private ArrayList<byte[]> fragmentPayload(byte[] value, int fragmentPayloadSize) {
            int count = Math.max(1, (value.length + fragmentPayloadSize - 1) / fragmentPayloadSize);
            int messageId = (int) System.nanoTime();
            ArrayList<byte[]> chunks = new ArrayList<>();
            for (int index = 0; index < count; index++) {
                int offset = index * fragmentPayloadSize;
                int length = Math.min(fragmentPayloadSize, value.length - offset);
                ByteBuffer buffer = ByteBuffer.allocate(16 + length).order(ByteOrder.BIG_ENDIAN);
                buffer.put((byte) 0x53);
                buffer.put((byte) 0x43);
                buffer.put((byte) 1);
                buffer.put((byte) 1);
                buffer.putInt(messageId);
                buffer.putShort((short) index);
                buffer.putShort((short) count);
                buffer.putShort((short) length);
                buffer.putShort((short) 0);
                buffer.put(value, offset, length);
                chunks.add(buffer.array());
            }
            return chunks;
        }

        @Override
        public void onCharacteristicRead(
            BluetoothGatt connection,
            BluetoothGattCharacteristic characteristic,
            byte[] value,
            int status
        ) {
            if (WIFI_STATUS_UUID.equals(characteristic.getUuid())) handleStatus(connection, value, status);
        }

        @Override
        @SuppressWarnings("deprecation")
        public void onCharacteristicRead(BluetoothGatt connection, BluetoothGattCharacteristic characteristic, int status) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU && WIFI_STATUS_UUID.equals(characteristic.getUuid())) {
                handleStatus(connection, characteristic.getValue(), status);
            }
        }

        private void handleStatus(BluetoothGatt connection, byte[] value, int status) {
            if (connection != gatt) return;
            log("status read callback status=" + status + " bytes=" + (value == null ? 0 : value.length));
            if (status != BluetoothGatt.GATT_SUCCESS) {
                if (shouldRetryGattAuthentication(backgroundRecovery, status, authenticationRetries)) {
                    authenticationRetries++;
                    device.createBond();
                    handler.postDelayed(() -> readProvisioningStatus(connection), 2000);
                    return;
                }
                fail("Encrypted BLE status read failed: " + status, null);
                return;
            }
            try {
                JSObject result = new JSObject(new String(value, StandardCharsets.UTF_8));
                String state = result.getString("state", "unknown");
                log("device state=" + state + " responseOp=" + result.getString("op", "unknown"));
                if (!state.equals("working") && !state.equals("connecting")) {
                    finish(result);
                } else if (SystemClock.elapsedRealtime() >= deadline) {
                    fail("BLE operation timed out", null);
                } else {
                    handler.postDelayed(() -> readProvisioningStatus(connection), 1200);
                }
            } catch (Exception error) {
                fail("Device returned invalid BLE operation status", error);
            }
        }

        private void finish(JSObject result) {
            if (!handoff.complete(completed)) return;
            log("finish");
            closeGatt(gatt, "finish");
            call.resolve(result);
        }

        private void fail(String message, Exception error) {
            if (completed.get() || skipBackgroundRecoveryIfNeeded()) return;
            boolean bonded = false;
            try {
                bonded = device.getBondState() == BluetoothDevice.BOND_BONDED;
            } catch (RuntimeException bondError) {
                Log.w(TAG, prefix() + " unable to read bond state for RFCOMM fallback", bondError);
            }
            BluetoothFailureRoute route = handoff.routeFailure(
                bonded,
                SystemClock.elapsedRealtime(),
                deadline,
                completed
            );
            if (route == BluetoothFailureRoute.ALREADY_COMPLETED) return;
            if (route == BluetoothFailureRoute.RFCOMM) {
                log("GATT unavailable; switching to RFCOMM reason=" + message);
                closeGatt(gatt, "RFCOMM fallback");
                new RfcommOperation(device, payload, call, deadline, prefix(), backgroundRecovery).start();
                return;
            }
            log("fail reason=" + message);
            closeGatt(gatt, "failure");
            if (error == null) call.reject(message);
            else call.reject(message, error);
        }

        private boolean canUseRfcommFallback() {
            try {
                return handoff.canUseRfcommFallback(
                    device.getBondState() == BluetoothDevice.BOND_BONDED,
                    SystemClock.elapsedRealtime(),
                    deadline
                );
            } catch (RuntimeException error) {
                Log.w(TAG, prefix() + " unable to read bond state for RFCOMM fallback", error);
                return false;
            }
        }

        private boolean skipBackgroundRecoveryIfNeeded() {
            if (!shouldSkipBackgroundRecovery(device, backgroundRecovery)) return false;
            if (handoff.complete(completed)) {
                closeGatt(gatt, "background recovery skipped");
                resolveSkippedBackgroundRecovery(call);
            }
            return true;
        }

        private synchronized void closeGatt(BluetoothGatt connection, String reason) {
            if (connection == null) return;
            if (gatt == connection) {
                gatt = null;
                connected = false;
            }
            log("close GATT reason=" + reason);
            try {
                connection.disconnect();
            } catch (RuntimeException error) {
                Log.w(TAG, prefix() + " disconnect failed", error);
            }
            try {
                connection.close();
            } catch (RuntimeException error) {
                Log.w(TAG, prefix() + " close failed", error);
            }
        }

        private String operationName(byte[] value) {
            try {
                return new JSONObject(new String(value, StandardCharsets.UTF_8)).optString("op", "unknown");
            } catch (Exception ignored) {
                return "unknown";
            }
        }

        private String prefix() {
            return "BLE op#" + operationId + " " + operationName;
        }

        private void log(String message) {
            Log.d(TAG, prefix() + " " + message);
        }
    }

    @FunctionalInterface
    private interface JsonTask {
        JSObject run() throws Exception;
    }

    interface CancellableOperation {
        void cancel();
    }

    static final class ActiveOperationRegistry<T extends CancellableOperation> {
        private final HashSet<T> operations = new HashSet<>();
        private boolean closed;

        synchronized boolean register(T operation) {
            if (closed || operation == null) return false;
            return operations.add(operation);
        }

        synchronized void unregister(T operation) {
            operations.remove(operation);
        }

        void cancelAll() {
            ArrayList<T> snapshot;
            synchronized (this) {
                if (closed) return;
                closed = true;
                snapshot = new ArrayList<>(operations);
                operations.clear();
            }
            for (T operation : snapshot) operation.cancel();
        }

        synchronized int size() {
            return operations.size();
        }
    }

    static final class IoExecutors {
        final ExecutorService control = Executors.newFixedThreadPool(
            CONTROL_IO_THREADS, namedThreadFactory("syncap-control")
        );
        final ExecutorService rtspProbes = Executors.newFixedThreadPool(
            RTSP_PROBE_THREADS, namedThreadFactory("syncap-probe")
        );
        final ExecutorService transfers = Executors.newSingleThreadExecutor(
            namedThreadFactory("syncap-transfer")
        );

        void shutdownNow() {
            control.shutdownNow();
            rtspProbes.shutdownNow();
            transfers.shutdownNow();
        }
    }

    private static ThreadFactory namedThreadFactory(String prefix) {
        AtomicInteger sequence = new AtomicInteger();
        return task -> new Thread(task, prefix + "-" + sequence.incrementAndGet());
    }

    static String normalizeRtspPath(String path) {
        if (path == null || !path.matches("/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,200}")) return "/PRR";
        return path;
    }

    static String normalizePreviewTransport(String transport) {
        return "tcp-hevc".equals(transport) ? "tcp-hevc" : "rtsp";
    }

    static boolean isSupportedPreviewUrl(String url) {
        if (url == null || url.indexOf('\r') >= 0 || url.indexOf('\n') >= 0) return false;
        return url.startsWith("rtsp://") || url.startsWith("tcp://");
    }

    static RtspProbeResult probeTcp(String host, int port) {
        /* A raw qgapp HEVC listener may admit only one consumer.  Opening a
         * diagnostic socket can steal the App's preview connection, so raw
         * stream availability must come from device-service telemetry. */
        return new RtspProbeResult(false, "service_telemetry_required", null, 0);
    }

    static RtspProbeResult probeRtsp(String host, int port) {
        return probeRtsp(host, port, "/PRR");
    }

    static RtspProbeResult probeRtsp(String host, int port, String requestedPath) {
        long startedAt = System.nanoTime();
        String lastReason = "unreachable";
        String path = normalizeRtspPath(requestedPath);
        for (int attempt = 0; attempt < CONNECT_ATTEMPTS; attempt++) {
            try (Socket socket = new Socket()) {
                socket.connect(new InetSocketAddress(host, port), CONNECT_TIMEOUT_MS);
                socket.setSoTimeout(CONNECT_TIMEOUT_MS);
                String request = "DESCRIBE rtsp://" + host + ":" + port + path + " RTSP/1.0\r\n"
                    + "CSeq: 1\r\nAccept: application/sdp\r\nConnection: close\r\nUser-Agent: SynCap-Studio\r\n\r\n";
                socket.getOutputStream().write(request.getBytes(StandardCharsets.US_ASCII));
                socket.getOutputStream().flush();
                try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(socket.getInputStream(), StandardCharsets.US_ASCII)
                )) {
                    String statusLine = reader.readLine();
                    Integer responseCode = parseRtspResponseCode(statusLine);
                    long latencyMs = (System.nanoTime() - startedAt) / 1_000_000;
                    if (responseCode == null) return new RtspProbeResult(false, "invalid_response", null, latencyMs);
                    boolean online = responseCode >= 200 && responseCode < 300;
                    return new RtspProbeResult(
                        online, online ? "online" : "rtsp_status_" + responseCode, responseCode, latencyMs
                    );
                }
            } catch (SocketTimeoutException error) {
                lastReason = "timeout";
            } catch (ConnectException error) {
                lastReason = "connection_refused";
            } catch (IOException error) {
                lastReason = "io_error";
            }
            if (attempt + 1 < CONNECT_ATTEMPTS) {
                try {
                    Thread.sleep(180);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    return new RtspProbeResult(
                        false, "interrupted", null, (System.nanoTime() - startedAt) / 1_000_000
                    );
                }
            }
        }
        return new RtspProbeResult(false, lastReason, null, (System.nanoTime() - startedAt) / 1_000_000);
    }

    private static Integer parseRtspResponseCode(String statusLine) {
        if (statusLine == null || !statusLine.startsWith("RTSP/")) return null;
        String[] parts = statusLine.split(" ", 3);
        if (parts.length < 2) return null;
        try {
            return Integer.parseInt(parts[1]);
        } catch (NumberFormatException ignored) {
            return null;
        }
    }

    static final class RtspProbeResult {
        final boolean online;
        final String reason;
        final Integer responseCode;
        final long latencyMs;

        RtspProbeResult(boolean online, String reason, Integer responseCode, long latencyMs) {
            this.online = online;
            this.reason = reason;
            this.responseCode = responseCode;
            this.latencyMs = latencyMs;
        }
    }
}
