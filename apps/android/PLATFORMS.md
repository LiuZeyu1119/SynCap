# SynCap Studio platform builds

The legacy `apps/android` directory is now the single SynCap Studio source tree. React and TypeScript provide the shared responsive interface. Capacitor hosts it on iOS and Android, while Tauri hosts the same build on macOS and Windows.

## Build commands

- Web/type check: `npx tsc --noEmit && npm run build`
- UI and connection-truth tests: `npm run test:runtime`
- macOS app: `npm run desktop:build`
- Windows NSIS installer: run `npm run desktop:build` from Windows with Rust, Node.js, WebView2, and Visual Studio C++ Build Tools installed. The installer embeds the offline WebView2 runtime.
- Windows NSIS installer from macOS: `npm run desktop:build:windows-cross` after installing LLVM, LLD, NSIS, and cargo-xwin
- iOS project sync: `npm run ios:sync`
- iOS simulator: `npm run ios:simulator`

An Apple development team and distribution certificate are required to create an installable iPhone archive. The unsigned device build is still compiled in CI/local validation to catch arm64 and Swift errors.

## Native functions

Both native hosts expose the same TypeScript bridge for device probing, four-camera status, capture start/stop, session listing, resumable SHA-256-verified export, Bluetooth discovery, Wi-Fi provisioning, and offline commands. Desktop preview uses the bundled MediaMTX process to expose the device RTSP streams to the shared HLS grid. iOS preview uses the bundled MobileVLCKit framework for four native RTSP players.

All online and camera-ready indicators come from a successful device/API or RTSP probe. Disconnected builds do not inject sample frames, simulated networks, or simulated success states.
