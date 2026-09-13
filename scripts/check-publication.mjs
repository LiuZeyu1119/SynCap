#!/usr/bin/env node
// Audit the exact index that will be committed; never print matching secrets.
import { execFileSync } from 'node:child_process';

const entries = execFileSync('git', ['ls-files', '--stage', '-z']).toString().split('\0').filter(Boolean);
if (!entries.length) {
  console.error('Publication check failed: the index is empty. Stage the intended source files first.');
  process.exit(1);
}
const forbiddenPath = /(^|\/)(artifacts|tina|vendor|dense_results|node_modules|target|build|\.build|\.gradle|DerivedData|xcuserdata)(\/|$)|(^|\/)\.env(?:\.|$)/;
const forbiddenFile = /\.(?:apk|aar|aab|ipa|img|mcap|h264|h265|hevc|nv12|raw|mp4|mov|pcd|ply|key|crt|pem|p12|pfx|jks|keystore|mobileprovision|log|zip|tar|gz|7z|so|dylib|a|bin|exe)$/i;
const secretPatterns = [
  /-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----/,
  /\bgh[pousr]_[A-Za-z0-9]{36,}\b/,
  /\bgithub_pat_[A-Za-z0-9_]{50,}\b/,
  /\bAKIA[A-Z0-9]{16}\b/,
  /\bxox[baprs]-[A-Za-z0-9-]{20,}\b/,
];
let failures = 0;
for (const entry of entries) {
  const [metadata, path] = entry.split('\t');
  const [mode, object, stage] = metadata.split(' ');
  const issues = [];
  // These directories contain intentionally retained third-party license text.
  const licenseOnly = /^(?:sdk\/apple-media|apps\/android\/ios\/App\/CapApp-SPM)\/Vendor\/MobileVLCKit-COPYING\.txt$/.test(path);
  if ((!licenseOnly && forbiddenPath.test(path)) || forbiddenFile.test(path) || path.endsWith('/fisheye-lab.png')) issues.push('excluded artifact or credential path');
  if (mode === '120000' || mode === '160000' || stage !== '0') issues.push('review symlink, submodule or unmerged entry');
  const data = execFileSync('git', ['cat-file', 'blob', object], { maxBuffer: 128 * 1024 * 1024 });
  if (data.length > 10 * 1024 * 1024) issues.push('source blob exceeds 10 MiB; use a reviewed Release asset');
  if (!data.includes(0) && secretPatterns.some(pattern => pattern.test(data.toString()))) issues.push('possible token or private key (value withheld)');
  if (issues.length) {
    failures++;
    console.error(`${path}: ${issues.join('; ')}`);
  }
}
if (failures) {
  console.error(`Publication check failed: ${failures} file(s). Nothing was changed or uploaded.`);
  process.exitCode = 1;
} else {
  console.log(`Publication check passed: ${entries.length} staged files. Manual credential and license review is still required.`);
}
