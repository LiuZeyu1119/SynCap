import { copyFileSync, existsSync, mkdtempSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { attachSynCapMedia } from "./ios-sdk-dependencies.mjs";

const vendorDirectory = "../../sdk/apple-media/Vendor";
const framework = join(vendorDirectory, "MobileVLCKit.xcframework");
const license = join(vendorDirectory, "MobileVLCKit-COPYING.txt");
const archiveURL = "https://artifacts.videolan.org/VLCKit/MobileVLCKit/MobileVLCKit-3.7.3-319ed2c0-79128878.tar.xz";

function run(command, args) {
  const result = spawnSync(command, args, { stdio: "inherit", env: process.env });
  if (result.status !== 0) throw new Error(`${command} failed with exit code ${result.status}`);
}

if (!existsSync(framework)) {
  const temporary = mkdtempSync(join(tmpdir(), "syncap-vlckit-"));
  const archive = join(temporary, "MobileVLCKit.tar.xz");
  try {
    run("curl", ["-fL", "--retry", "3", "-o", archive, archiveURL]);
    run("tar", [
      "-xf", archive,
      "-C", temporary,
      "MobileVLCKit-binary/MobileVLCKit.xcframework",
      "MobileVLCKit-binary/COPYING.txt",
    ]);
    mkdirSync(vendorDirectory, { recursive: true });
    renameSync(join(temporary, "MobileVLCKit-binary/MobileVLCKit.xcframework"), framework);
    renameSync(join(temporary, "MobileVLCKit-binary/COPYING.txt"), license);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}

const packageFile = "ios/App/CapApp-SPM/Package.swift";
const packageSource = readFileSync(packageFile, "utf8");
const attached = attachSynCapMedia(packageSource);
if (attached !== packageSource) writeFileSync(packageFile, attached);

if (!existsSync(license)) throw new Error("MobileVLCKit license is missing");
const licenseDirectory = "ios/App/App/public/licenses";
mkdirSync(licenseDirectory, { recursive: true });
copyFileSync(license, join(licenseDirectory, "MobileVLCKit-COPYING.txt"));

console.log("MobileVLCKit 3.7.3 is ready for the iOS build.");
