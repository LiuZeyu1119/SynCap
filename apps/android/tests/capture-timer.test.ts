import assert from "node:assert/strict";
import test from "node:test";
import { captureTimerElapsedMs, observeCaptureTimer } from "../src/captureTimer.ts";

test("uses the device's elapsed duration before its timestamps", () => {
  const timer = observeCaptureTimer(null, "capture", {
    elapsedMs: 65_000,
    startedAtDeviceTimeNs: "343237623579",
  }, "350237623579", 1_000);
  assert.equal(timer.elapsedMs, 65_000);
});

test("subtracts nanoseconds in the same device domain without losing precision", () => {
  const timer = observeCaptureTimer(null, "capture", {
    startedAtDeviceTimeNs: "1788999999999999999",
  }, "1789000000000999999", 0);
  assert.equal(timer.elapsedMs, 1);
});

test("keeps the start response's device timestamp for later legacy status polls", () => {
  const started = observeCaptureTimer(null, "capture", { startedAtDeviceTimeNs: "343237623579" }, undefined, 100);
  const polled = observeCaptureTimer(started, "capture", {}, "350237623579", 1_000);
  assert.equal(polled.elapsedMs, 7_000);
});

test("accounts for missed UI ticks using a monotonic local clock", () => {
  const timer = observeCaptureTimer(null, "capture", { elapsedMs: 7_000 }, undefined, 1_000);
  assert.equal(captureTimerElapsedMs(timer, 61_000), 67_000);
});

test("repeated or delayed observations do not reset the current capture", () => {
  const timer = observeCaptureTimer(null, "capture", { elapsedMs: 7_000 }, undefined, 1_000);
  const stale = observeCaptureTimer(timer, "capture", { elapsedMs: 7_000 }, undefined, 5_000);
  assert.equal(stale.elapsedMs, 11_000);
});

test("a different capture resets elapsed time and does not inherit clock metadata", () => {
  const first = observeCaptureTimer(null, "first", { elapsedMs: 65_000, startedAtDeviceTimeNs: "123" }, undefined, 0);
  const second = observeCaptureTimer(first, "second", {}, "65000000123", 1_000);
  assert.equal(second.elapsedMs, 0);
  assert.equal(second.startedAtDeviceTimeNs, undefined);
});

for (const timing of [{ elapsedMs: NaN }, { elapsedMs: Infinity }, { elapsedMs: -1 }, { startedAtDeviceTimeNs: "invalid" }, { startedAtDeviceTimeNs: "9999999999999" }]) {
  test(`invalid timing ${JSON.stringify(timing)} falls back to local confirmation`, () => {
    const timer = observeCaptureTimer(null, "capture", timing, "343237623579", 100);
    assert.equal(timer.elapsedMs, 0);
    assert.equal(captureTimerElapsedMs(timer, 1_100), 1_000);
  });
}
