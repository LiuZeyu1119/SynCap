import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const checker = fileURLToPath(new URL('./check-publication.mjs', import.meta.url));
function check(files) {
  const repository = mkdtempSync(join(tmpdir(), 'syncap-publication-test-'));
  try {
    execFileSync('git', ['init', '-q', repository]);
    for (const [path, contents] of files) {
      const object = execFileSync('git', ['hash-object', '-w', '--stdin'], {
        cwd: repository, input: contents,
      }).toString().trim();
      execFileSync('git', ['update-index', '--add', '--cacheinfo', `100644,${object},${path}`], { cwd: repository });
    }
    return spawnSync(process.execPath, [checker], { cwd: repository, encoding: 'utf8' });
  } finally {
    // Only this test's freshly created, uniquely named Git fixture is removed.
    rmSync(repository, { recursive: true });
  }
}

test('requires a nonempty index', () => assert.equal(check([]).status, 1));
test('accepts ordinary source and retained dependency license', () => {
  assert.equal(check([
    ['sdk/core/src/lib.rs', '// source\n'],
    ['sdk/apple-media/Vendor/MobileVLCKit-COPYING.txt', 'license text\n'],
  ]).status, 0);
});
test('reads staged blobs even when no working files exist', () => {
  const result = check([['README.md', '# staged only\n']]);
  assert.equal(result.status, 0);
  assert.match(result.stdout, /1 staged files/);
});
test('rejects firmware, credentials, recordings and APK paths', () => {
  for (const path of ['tina/firmware.img', '.env', 'auto.key', 'take.mcap', 'example.apk', 'artifacts/note.md']) {
    assert.equal(check([[path, 'fixture']]).status, 1, path);
  }
});
test('rejects private photo even if force-staged', () => {
  assert.equal(check([['apps/android/public/assets/syncap/fisheye-lab.png', Buffer.from([0, 1])]]).status, 1);
});
test('rejects oversized source blobs', () => {
  assert.equal(check([['huge.dat', Buffer.alloc(10 * 1024 * 1024 + 1)]]).status, 1);
});
test('rejects a synthetic token without echoing it', () => {
  const fake = ['ghp', '_', 'a'.repeat(36)].join('');
  const result = check([['config.txt', fake]]);
  assert.equal(result.status, 1);
  assert.ok(!result.stderr.includes(fake));
});
