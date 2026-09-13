"""Deterministic stop regression: a closed boundary waiting behind slow SD writes."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import test_tina_service as service_tests


ROOT = Path(__file__).resolve().parent
HARNESS = r'''
#define main tina_service_main
#define fsync slow_fsync
#define clock_gettime test_clock_gettime
#define nanosleep test_nanosleep
__SERVICE_SOURCE__
#undef main
#undef fsync
#undef clock_gettime
#undef nanosleep

extern int fsync(int);
extern int clock_gettime(clockid_t, struct timespec *);
static uint64_t clock_offset;
static bool delayed;
static bool published;
static bool publish_boundary;
static bool corrupt_boundary;
static const char *fixture_root;

static void publish(void) {
    char from[PATH_MAX], to[PATH_MAX];
    if (published || !publish_boundary) return;
    published = true;
    snprintf(from, sizeof(from), "%s/%s", fixture_root,
             corrupt_boundary ? "failed-status" : "stop-status");
    snprintf(to, sizeof(to), "%s/1970-rec-9999.mcap.status.json", g_cfg.ring_dir);
    if (rename(from, to) != 0) abort();
    snprintf(from, sizeof(from), "%s/later-mcap", fixture_root);
    snprintf(to, sizeof(to), "%s/1970-rec-10000.mcap", g_cfg.ring_dir);
    if (rename(from, to) != 0) abort();
    snprintf(from, sizeof(from), "%s/later-status", fixture_root);
    snprintf(to, sizeof(to), "%s/1970-rec-10000.mcap.status.json", g_cfg.ring_dir);
    if (rename(from, to) != 0) abort();
}

int test_clock_gettime(clockid_t clock, struct timespec *value) {
    int result = clock_gettime(clock, value);
    if (result == 0 && clock == CLOCK_MONOTONIC) value->tv_sec += (time_t)clock_offset;
    return result;
}

int test_nanosleep(const struct timespec *delay, struct timespec *remaining) {
    (void)delay;
    (void)remaining;
    if (g_capture.finalizing) { publish(); ++clock_offset; }
    return 0;
}

int slow_fsync(int fd) {
    if (g_capture.finalizing && !delayed) {
        delayed = true;
        publish();
        clock_offset += 9; /* A single SD write crosses the old camera deadline. */
    }
    return fsync(fd);
}

int main(int argc, char **argv) {
    struct string_buf response = {0};
    int status = 200;
    const char *code = "";
    char message[256] = "";
    char session[PATH_MAX], id[96], from[PATH_MAX], to[PATH_MAX];
    bool ok;
    char *args[] = {argv[0], "--dry-run", "--allow-unmounted-storage",
                    "--min-free-bytes", "1", "--ring", argv[1], "--storage", argv[2]};
    if (argc != 5 || !configure(9, args)) return 2;
    fixture_root = argv[3];
    publish_boundary = strcmp(argv[4], "missing") != 0;
    corrupt_boundary = strcmp(argv[4], "corrupt") == 0;
    if (!begin_capture("backpressure", &response, &status, &code, message, sizeof(message))) return 3;
    safe_copy(session, sizeof(session), g_capture.session_dir);
    safe_copy(id, sizeof(id), g_capture.capture_id);
    const char *names[] = {"1970-rec-9998.mcap", "1970-rec-9998.mcap.status.json", "1970-rec-9999.mcap"};
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        snprintf(from, sizeof(from), "%s/%s", fixture_root, names[i]);
        snprintf(to, sizeof(to), "%s/%s", g_cfg.ring_dir, names[i]);
        if (rename(from, to) != 0) return 4;
    }
    sb_free(&response);
    /* Exercise the production watchdog too, without ever signalling a real
     * recorder: the child is this harness's own short-lived stand-in. */
    safe_copy(g_cfg.qgapp_path, sizeof(g_cfg.qgapp_path), "/fixture-recorder");
    g_qgapp_pid = fork();
    if (g_qgapp_pid < 0) return 5;
    if (g_qgapp_pid == 0) { sleep(10); _exit(0); }
    g_cfg.dry_run = false;
    ok = finalize_capture(id, &response, &status, &code, message, sizeof(message));
    if (g_qgapp_pid > 1) {
        kill(g_qgapp_pid, SIGKILL);
        waitpid(g_qgapp_pid, NULL, 0);
    }
    printf("{\"ok\":%s,\"quarantined\":%s,\"session\":\"%s\",\"message\":\"%s\"}\n",
           ok ? "true" : "false", g_ring_quarantined ? "true" : "false", session, message);
    sb_free(&response);
    return 0;
}
'''


class TinaStopBackpressureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="tina-stop-build-")
        cls.addClassCleanup(cls.build.cleanup)
        cls.binary = Path(cls.build.name) / "stop-test"
        source = (ROOT / "tina_service.c").read_text()
        subprocess.run([
            os.environ.get("CC", "cc"), "-std=c11", "-O2", "-Wall", "-Wextra",
            "-Werror", "-pedantic", "-x", "c", "-", "-o", str(cls.binary),
        ], input=HARNESS.replace("__SERVICE_SOURCE__", source), text=True, check=True)

    def run_case(self, mode):
        with tempfile.TemporaryDirectory(prefix="tina-stop-test-") as temporary:
            root = Path(temporary)
            ring, storage = root / "ring", root / "storage"
            ring.mkdir()
            storage.mkdir()
            for name in ("1970-rec-9998.mcap", "1970-rec-9999.mcap", "later-mcap"):
                (root / name).write_bytes(b"mcap-fixture-payload" + service_tests.MCAP_MAGIC)
            for name, marks in (("1970-rec-9998.mcap.status.json", 62),
                                ("stop-status", 124), ("later-status", 186), ("failed-status", 124)):
                status = service_tests.TinaServiceTest.clean_segment_status(fsync_marks=marks)
                if name == "failed-status":
                    status["drops"]["imu_gaps"] = 1
                (root / name).write_text(json.dumps(status))
            result = subprocess.run([str(self.binary), str(ring), str(storage), str(root), mode],
                                    check=True, capture_output=True, text=True, timeout=12)
            outcome = json.loads(result.stdout)
            manifest = json.loads((Path(outcome["session"]) / "session.json").read_text())
            return outcome, manifest, sorted(path.name for path in ring.iterdir())

    def test_slow_sd_does_not_become_camera_timeout_or_extend_stop_boundary(self):
        result, manifest, ring = self.run_case("closed")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["quarantined"])
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(manifest["fileCount"], 4)
        self.assertIn("1970-rec-10000.mcap", ring)
        self.assertNotIn("1970-rec-9999.mcap", ring)

    def test_camera_that_never_closes_still_fails_and_retains_ring(self):
        result, manifest, ring = self.run_case("missing")
        self.assertFalse(result["ok"])
        self.assertTrue(result["quarantined"])
        self.assertEqual(manifest["state"], "failed")
        self.assertIn("1970-rec-9999.mcap", ring)

    def test_closed_boundary_with_new_imu_gaps_is_not_accepted(self):
        result, manifest, ring = self.run_case("corrupt")
        self.assertFalse(result["ok"])
        self.assertTrue(result["quarantined"])
        self.assertEqual(manifest["state"], "failed")
        self.assertIn("1970-rec-9999.mcap", ring)


if __name__ == "__main__":
    unittest.main()
