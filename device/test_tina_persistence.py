"""Host regression for FAT's post-rename child-inode writeback requirement.

The harness uses real files but models the separately dirty FAT directory
entry: fsync(parent) cannot substitute for fsync(the renamed file). It is not
a power-loss test; the device's O_DIRECT directory checks cover the real FAT.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
TEXT = b'{"state":"complete"}\n'
HARNESS = r'''
#define main tina_service_main
#define rename persistence_rename
#define fsync persistence_fsync
#define close persistence_close
__SERVICE_SOURCE__
#undef main
#undef rename
#undef fsync
#undef close

extern int rename(const char *, const char *);
extern int fsync(int);
extern int close(int);

static int tracked_fd = -1;
static dev_t tracked_device;
static ino_t tracked_inode;
static bool tracked_closed;
static bool renamed;
static bool target_inode_dirty;
static bool parent_synced;
static bool fail_after_rename;
static char events[256];

static void event(const char *name) {
    size_t used = strlen(events);
    (void)snprintf(events + used, sizeof(events) - used, "%s%s",
                   used ? "," : "", name);
}

int persistence_rename(const char *old_path, const char *new_path) {
    int result = rename(old_path, new_path);
    if (result == 0) {
        event("rename");
        renamed = true;
        target_inode_dirty = true;
        parent_synced = false;
    }
    return result;
}

int persistence_fsync(int fd) {
    struct stat st;
    int result;
    if (fstat(fd, &st) != 0) return -1;
    if (S_ISDIR(st.st_mode)) {
        event("parent_fsync");
        /* A parent's successful fsync does not clean its child's inode. */
        parent_synced = true;
        return 0;
    }
    if (tracked_fd < 0) {
        tracked_fd = fd;
        tracked_device = st.st_dev;
        tracked_inode = st.st_ino;
    }
    if (renamed && fail_after_rename) {
        event("file_fsync_failed");
        errno = EIO;
        return -1;
    }
    event("file_fsync");
    result = fsync(fd);
    if (result == 0 && renamed && fd == tracked_fd && !tracked_closed &&
        st.st_dev == tracked_device && st.st_ino == tracked_inode) {
        target_inode_dirty = false;
    }
    return result;
}

int persistence_close(int fd) {
    if (fd == tracked_fd && !tracked_closed) {
        event("file_close");
        tracked_closed = true;
    }
    return close(fd);
}

int main(int argc, char **argv) {
    static const char content[] = "{\"state\":\"complete\"}\n";
    uint64_t size = 0;
    char sha[65] = "";
    bool ok;
    if (argc != 5) return 2;
    fail_after_rename = strcmp(argv[4], "fail") == 0;
    if (strcmp(argv[1], "copy") == 0) {
        ok = copy_file_atomic(argv[2], argv[3], &size, sha);
    } else {
        ok = write_atomic_text(argv[3], content, sizeof(content) - 1);
    }
    printf("{\"ok\":%s,\"durable\":%s,\"dirty\":%s,"
           "\"source_exists\":%s,\"events\":\"%s\","
           "\"size\":%llu,\"sha\":\"%s\"}\n",
           ok ? "true" : "false",
           renamed && !target_inode_dirty && parent_synced ? "true" : "false",
           target_inode_dirty ? "true" : "false",
           access(argv[2], F_OK) == 0 ? "true" : "false", events,
           (unsigned long long)size, sha);
    return 0;
}
'''


class TinaPersistenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = tempfile.TemporaryDirectory(prefix="syncap-tina-persistence-build-")
        cls.addClassCleanup(cls.build.cleanup)
        source = (ROOT / "tina_service.c").read_text()
        cls.binary = cls.compile_harness(source, "current")

        # Negative control: reproduce the missing post-rename file fsync in
        # memory, without editing the production source or the working tree.
        old_copy = "rename(temporary, destination) != 0 || fsync(dest_fd) != 0 ||"
        old_text = "rename(temporary, path) != 0 || fsync(fd) != 0 ||"
        if source.count(old_copy) != 1 or source.count(old_text) != 1:
            raise AssertionError("Update the negative-control mutation for the new atomic-write implementation")
        legacy = source.replace(old_copy, "rename(temporary, destination) != 0 ||")
        legacy = legacy.replace(old_text, "rename(temporary, path) != 0 ||")
        cls.legacy_binary = cls.compile_harness(legacy, "missing-post-rename-fsync")

    @classmethod
    def compile_harness(cls, source: str, name: str) -> Path:
        binary = Path(cls.build.name) / name
        subprocess.run([
            os.environ.get("CC", "cc"), "-std=c11", "-O2", "-Wall", "-Wextra",
            "-Werror", "-pedantic", "-x", "c", "-", "-o", str(binary),
        ], input=HARNESS.replace("__SERVICE_SOURCE__", source), text=True, check=True)
        return binary

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="syncap-tina-persistence-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "ring.mcap"
        self.destination = self.root / "session.mcap"
        self.payload = bytes(range(256)) * 600
        self.source.write_bytes(self.payload)

    def run_case(self, operation: str, *, fail: bool = False, legacy: bool = False) -> dict:
        completed = subprocess.run([
            str(self.legacy_binary if legacy else self.binary), operation,
            str(self.source), str(self.destination), "fail" if fail else "ok",
        ], capture_output=True, text=True, check=True)
        return json.loads(completed.stdout)

    def assert_success(self, operation: str) -> None:
        result = self.run_case(operation)
        self.assertTrue(result["ok"])
        self.assertTrue(result["durable"])
        self.assertFalse(result["dirty"])
        self.assertEqual(result["events"], "file_fsync,rename,file_fsync,parent_fsync,file_close")
        self.assertEqual(self.destination.read_bytes(), self.payload if operation == "copy" else TEXT)
        self.assertEqual(self.source.read_bytes(), self.payload)
        if operation == "copy":
            self.assertEqual(result["size"], len(self.payload))
            self.assertEqual(result["sha"], hashlib.sha256(self.payload).hexdigest())

    def assert_post_rename_failure(self, operation: str) -> None:
        result = self.run_case(operation, fail=True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["durable"])
        self.assertTrue(result["dirty"])
        self.assertEqual(result["events"], "file_fsync,rename,file_fsync_failed,file_close")
        self.assertTrue(result["source_exists"])
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertEqual(list(self.root.glob("*.part.*")), [])
        self.assertEqual(list(self.root.glob("*.tmp.*")), [])

    def assert_old_order_is_unsafe(self, operation: str) -> None:
        result = self.run_case(operation, legacy=True)
        self.assertTrue(result["ok"], "the old sequence incorrectly reports success")
        self.assertFalse(result["durable"], "parent fsync must not hide an unsynced child inode")
        self.assertTrue(result["dirty"])
        self.assertEqual(result["events"], "file_fsync,rename,parent_fsync,file_close")

    def test_copy_syncs_renamed_file_before_parent(self) -> None:
        self.assert_success("copy")

    def test_text_syncs_renamed_file_before_parent(self) -> None:
        self.assert_success("text")

    def test_copy_preserves_source_after_post_rename_fsync_failure(self) -> None:
        self.assert_post_rename_failure("copy")

    def test_text_reports_post_rename_fsync_failure(self) -> None:
        self.assert_post_rename_failure("text")

    def test_old_copy_order_is_detected_as_not_durable(self) -> None:
        self.assert_old_order_is_unsafe("copy")

    def test_old_text_order_is_detected_as_not_durable(self) -> None:
        self.assert_old_order_is_unsafe("text")


if __name__ == "__main__":
    unittest.main()
