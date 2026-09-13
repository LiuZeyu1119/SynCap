import assert from "node:assert/strict";
import { test } from "node:test";
import { attachSynCapMedia } from "../scripts/ios-sdk-dependencies.mjs";

test("adds the SDK package and product once after Capacitor regeneration", () => {
  const generated = `.package(name: "CapacitorApp", path: "../../../node_modules/@capacitor/app")
  .product(name: "Cordova", package: "capacitor-swift-pm")`;
  const result = attachSynCapMedia(generated);
  assert.match(result, /\.package\(name: "SynCapMedia", path: "\.\.\/\.\.\/\.\.\/\.\.\/\.\.\/sdk\/apple-media"\)/);
  assert.match(result, /\.product\(name: "SynCapMedia", package: "SynCapMedia"\)/);
  assert.equal(attachSynCapMedia(result), result);
});

test("does not silently rewrite unknown dependency templates", () => {
  assert.throws(() => attachSynCapMedia("unknown package"), /dependencies changed/);
});
