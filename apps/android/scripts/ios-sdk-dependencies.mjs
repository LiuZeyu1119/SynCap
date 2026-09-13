/** Reattach the optional SDK after Capacitor regenerates its Swift package. */
export function attachSynCapMedia(source) {
  const packageLine = '.package(name: "CapacitorApp", path: "../../../node_modules/@capacitor/app")';
  const productLine = '.product(name: "Cordova", package: "capacitor-swift-pm")';
  let result = source;
  if (!result.includes('.package(name: "SynCapMedia",')) {
    if (!result.includes(packageLine)) throw new Error("Capacitor package dependencies changed");
    result = result.replace(packageLine, `${packageLine},\n        .package(name: "SynCapMedia", path: "../../../../../sdk/apple-media")`);
  }
  if (!result.includes('.product(name: "SynCapMedia",')) {
    if (!result.includes(productLine)) throw new Error("Capacitor target dependencies changed");
    result = result.replace(productLine, `${productLine},\n                .product(name: "SynCapMedia", package: "SynCapMedia")`);
  }
  return result;
}
