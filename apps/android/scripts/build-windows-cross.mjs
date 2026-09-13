import { copyFileSync, mkdirSync, readdirSync } from "node:fs";
import { delimiter, join } from "node:path";
import { spawnSync } from "node:child_process";

function run(command, args, env = process.env) {
  const result = spawnSync(command, args, { stdio: "inherit", env });
  if (result.status !== 0) throw new Error(`${command} failed with exit code ${result.status}`);
}

if (process.platform !== "darwin") {
  throw new Error("This command is the macOS cross-build path; use npm run desktop:build on Windows");
}

run(process.execPath, ["scripts/prepare-mediamtx.mjs", "win32-x64"]);
const toolPath = ["/opt/homebrew/opt/llvm/bin", "/opt/homebrew/opt/lld/bin", process.env.PATH]
  .filter(Boolean)
  .join(delimiter);
const npx = process.platform === "win32" ? "npx.cmd" : "npx";
run(
  npx,
  ["tauri", "build", "--runner", "cargo-xwin", "--target", "x86_64-pc-windows-msvc", "--bundles", "nsis"],
  { ...process.env, PATH: toolPath },
);

const bundleDirectory = "src-tauri/target/x86_64-pc-windows-msvc/release/bundle/nsis";
const installer = readdirSync(bundleDirectory).find((name) => name.endsWith("-setup.exe"));
if (!installer) throw new Error("The Windows NSIS installer was not produced");
mkdirSync("artifacts", { recursive: true });
copyFileSync(join(bundleDirectory, installer), "artifacts/SynCap-Studio-Windows-x64-setup.exe");
