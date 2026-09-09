# WiFi Phone APK

## Build
Install Buildozer and run:

    buildozer android debug

The generated APK will be in the `bin/` directory.

## Important LAN behavior
This app is designed for same-WiFi/LAN calling. The original WebRTC page uses
an empty ICE server list, so it does not configure an external STUN/TURN server.

The current Python file embeds the HTML and adds Android WebView/audio support.
For fully automatic device-to-device calling, a real LAN signaling/discovery
layer must exchange WebRTC offers/answers between devices. UDP discovery alone
cannot replace WebRTC signaling; the next production step is to connect the
discovery layer to that signaling flow.
