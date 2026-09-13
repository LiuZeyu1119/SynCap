import { mkdirSync } from "node:fs";
import { spawnSync } from "node:child_process";

function run(command, args) {
  const result = spawnSync(command, args, { stdio: "inherit", env: process.env });
  if (result.status !== 0) throw new Error(`${command} failed with exit code ${result.status}`);
}

const npx = process.platform === "win32" ? "npx.cmd" : "npx";
if (process.platform === "darwin") {
  run(npx, ["tauri", "build", "--bundles", "app"]);
  run("codesign", [
    "--force", "--deep", "--sign", "-",
    "src-tauri/target/release/bundle/macos/SynCap Studio.app",
  ]);
  mkdirSync("artifacts", { recursive: true });
  run("ditto", [
    "-c", "-k", "--sequesterRsrc", "--keepParent",
    "src-tauri/target/release/bundle/macos/SynCap Studio.app",
    "artifacts/SynCap-Studio-macOS-arm64.zip",
  ]);
} else if (process.platform === "win32") {
  run(npx, ["tauri", "build", "--bundles", "nsis"]);
} else {
  throw new Error("Desktop packaging is configured for macOS and Windows");
}
