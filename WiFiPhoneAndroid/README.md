# WiFi Phone

Android Wi-Fi/LAN voice-call APK project.

## Build on GitHub

Upload the complete project to a GitHub repository, preserving folders, then open:

Actions -> Build WiFi Phone APK -> Run workflow

The workflow builds `app/build/outputs/apk/debug/app-debug.apk` and uploads it as the `WiFi-Phone-APK` artifact.

## Current call model

The supplied prototype HTML uses WebRTC with `iceServers: []` and manual SDP Offer/Answer exchange. This Android project packages that prototype in a native Android WebView with microphone permission handling. It does not add automatic LAN discovery/signaling.
