import { createHash } from "node:crypto";
import { chmodSync, copyFileSync, existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const version = "1.20.0";
const releases = {
  "darwin-arm64": {
    archive: `mediamtx_v${version}_darwin_arm64.tar.gz`,
    checksum: "5923ea2517543f21d4c182f9ad832809f71393cf63409350556f372b8013f440",
    triple: "aarch64-apple-darwin",
  },
  "darwin-x64": {
    archive: `mediamtx_v${version}_darwin_amd64.tar.gz`,
    checksum: "811c33593a74b11397d60787c95cb0550780aac7f9879501ef10be49bcf0f800",
    triple: "x86_64-apple-darwin",
  },
  "win32-x64": {
    archive: `mediamtx_v${version}_windows_amd64.zip`,
    checksum: "7364e7672e6b4420e986ec4b56e2cc32ec7b4085f69b56ec224d596d0fa8b19f",
    triple: "x86_64-pc-windows-msvc",
  },
};

const platformKey = process.argv[2] ?? `${process.platform}-${process.arch}`;
const release = releases[platformKey];
if (!release) throw new Error(`MediaMTX is not configured for ${platformKey}`);

const binaryDirectory = "src-tauri/binaries";
const executableSuffix = platformKey.startsWith("win32-") ? ".exe" : "";
const destination = join(binaryDirectory, `mediamtx-${release.triple}${executableSuffix}`);
const versionFile = join(binaryDirectory, "mediamtx.version");
if (existsSync(destination) && existsSync(versionFile) && readFileSync(versionFile, "utf8").trim() === version) {
  console.log(`MediaMTX ${version} is ready for ${release.triple}.`);
  process.exit(0);
}

function run(command, args) {
  const result = spawnSync(command, args, { stdio: "inherit", env: process.env });
  if (result.status !== 0) throw new Error(`${command} failed with exit code ${result.status}`);
}

const temporary = mkdtempSync(join(tmpdir(), "syncap-mediamtx-"));
try {
  const archive = join(temporary, release.archive);
  const url = `https://github.com/bluenviron/mediamtx/releases/download/v${version}/${release.archive}`;
  run("curl", ["-fL", "--retry", "3", "-o", archive, url]);
  const actual = createHash("sha256").update(readFileSync(archive)).digest("hex");
  if (actual !== release.checksum) throw new Error("MediaMTX archive checksum does not match the official release");
  run("tar", ["-xf", archive, "-C", temporary]);
  mkdirSync(binaryDirectory, { recursive: true });
  copyFileSync(join(temporary, `mediamtx${executableSuffix}`), destination);
  if (!platformKey.startsWith("win32-")) chmodSync(destination, 0o755);
  if (existsSync(join(temporary, "LICENSE"))) {
    copyFileSync(join(temporary, "LICENSE"), join(binaryDirectory, "MediaMTX-LICENSE"));
  }
  writeFileSync(versionFile, `${version}\n`);
} finally {
  rmSync(temporary, { recursive: true, force: true });
}

console.log(`MediaMTX ${version} is ready for ${release.triple}.`);
