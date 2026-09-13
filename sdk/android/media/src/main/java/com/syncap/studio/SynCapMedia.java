package com.syncap.studio;

import android.content.Context;
import android.content.Intent;
import java.net.URI;
import java.util.ArrayList;
import java.util.List;

/** Optional native screens; no dependency on the SynCap App or its web bridge. */
public final class SynCapMedia {
    private SynCapMedia() {}

    public static Intent previewIntent(Context context, List<String> urls, List<String> labels) {
        if (urls == null || urls.isEmpty() || urls.size() > 8 || labels == null || labels.size() != urls.size()) {
            throw new IllegalArgumentException("Provide one to eight preview URLs and matching labels");
        }
        for (String value : urls) {
            if (value == null) throw new IllegalArgumentException("Missing preview endpoint");
            URI url = URI.create(value);
            if (!("rtsp".equals(url.getScheme()) || "tcp".equals(url.getScheme()))
                || url.getHost() == null || url.getUserInfo() != null || url.getFragment() != null
                || url.getPort() == 0 || url.getPort() > 65535
                || ("tcp".equals(url.getScheme()) && url.getPort() < 1)) {
                throw new IllegalArgumentException("Invalid preview endpoint");
            }
        }
        return new Intent(context, LivePreviewActivity.class)
            .putStringArrayListExtra(LivePreviewActivity.EXTRA_URLS, new ArrayList<>(urls))
            .putStringArrayListExtra(LivePreviewActivity.EXTRA_LABELS, new ArrayList<>(labels));
    }

    /** Requires synchronized raw-NV12 snapshots from the device, never preview screenshots. */
    public static Intent calibrationIntent(
        Context context, List<String> urls, List<String> labels, List<String> cameraIds,
        String pair, int squaresLong, int squaresWide, double squareSizeMm
    ) {
        previewIntent(context, urls, labels);
        if (urls.size() != 2 || cameraIds == null || cameraIds.size() != 2
            || cameraIds.get(0) == null || cameraIds.get(1) == null
            || !cameraIds.get(0).matches("[A-Za-z0-9_-]{1,64}")
            || !cameraIds.get(1).matches("[A-Za-z0-9_-]{1,64}")
            || cameraIds.get(0).equals(cameraIds.get(1))
            || !("pair14".equals(pair) || "pair23".equals(pair))
            || squaresLong < 4 || squaresLong > 20 || squaresWide < 4 || squaresWide > 20
            || !Double.isFinite(squareSizeMm) || squareSizeMm < 1 || squareSizeMm > 200) {
            throw new IllegalArgumentException("Invalid stereo cameras or chessboard dimensions");
        }
        URI left = URI.create(urls.get(0));
        URI right = URI.create(urls.get(1));
        if (!"rtsp".equals(left.getScheme()) || !"rtsp".equals(right.getScheme())
            || !left.getHost().equals(right.getHost())) {
            throw new IllegalArgumentException("Calibration screen requires two RTSP cameras on one raw-snapshot device");
        }
        return new Intent(context, CalibrationActivity.class)
            .putStringArrayListExtra(CalibrationActivity.EXTRA_URLS, new ArrayList<>(urls))
            .putStringArrayListExtra(CalibrationActivity.EXTRA_LABELS, new ArrayList<>(labels))
            .putStringArrayListExtra(CalibrationActivity.EXTRA_CAMERA_IDS, new ArrayList<>(cameraIds))
            .putExtra(CalibrationActivity.EXTRA_PAIR, pair)
            .putExtra(CalibrationActivity.EXTRA_SQUARES_LONG, squaresLong)
            .putExtra(CalibrationActivity.EXTRA_SQUARES_WIDE, squaresWide)
            .putExtra(CalibrationActivity.EXTRA_SQUARE_SIZE_MM, squareSizeMm);
    }
}
