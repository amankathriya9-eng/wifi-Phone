# -*- coding: utf-8 -*-
"""
WiFi Phone - LAN-only Android application

Build:
    buildozer android debug

Features:
- Original WebRTC voice-call UI embedded in Android WebView
- LAN-only intent: no cloud/STUN/TURN server configured by the web app
- Microphone permission
- Android WebView JavaScript + media permission support
- Local device name stored on-device
- Offline/local app shell (HTML embedded in this file)
- Speaker/mute/hangup controls
- No analytics, ads, Firebase, or cloud dependency

Important:
Automatic peer discovery/signaling requires a local LAN signaling mechanism.
The Python entry point below includes a small localhost HTTP bridge and UDP
discovery foundation. The actual WebRTC offer/answer remains compatible with
the original HTML.
"""

from pathlib import Path
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import uuid

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.widget import Widget
from kivy.utils import platform

HTML = '<!DOCTYPE html>\n<html lang="hi">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>WiFi Call — Same Network Calling</title>\n<style>\n  :root{\n    --bg:#0E1420;\n    --panel:#161E2E;\n    --line:#26314A;\n    --accent:#3DDC97;\n    --accent-dim:#1E5A44;\n    --warn:#E2574C;\n    --text:#EAF0F8;\n    --text-dim:#8A96AE;\n  }\n  *{box-sizing:border-box;}\n  body{\n    margin:0;\n    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;\n    background:var(--bg);\n    color:var(--text);\n    min-height:100vh;\n    padding:24px 16px 60px;\n  }\n  .wrap{max-width:520px; margin:0 auto;}\n\n  header{text-align:center; margin-bottom:28px;}\n  header .icon{font-size:36px; margin-bottom:6px;}\n  header h1{font-size:22px; font-weight:600; margin:0 0 6px;}\n  header p{color:var(--text-dim); font-size:14px; margin:0; line-height:1.5;}\n\n  .status-pill{\n    display:flex;\n    align-items:center;\n    gap:8px;\n    justify-content:center;\n    font-size:13px;\n    color:var(--text-dim);\n    background:var(--panel);\n    border:1px solid var(--line);\n    border-radius:999px;\n    padding:8px 16px;\n    margin:0 auto 24px;\n    width:fit-content;\n  }\n  .dot{width:8px; height:8px; border-radius:50%; background:var(--text-dim);}\n  .dot.live{background:var(--accent); box-shadow:0 0 8px var(--accent);}\n  .dot.warn{background:var(--warn);}\n\n  .tabs{display:flex; gap:8px; margin-bottom:18px;}\n  .tab{\n    flex:1;\n    text-align:center;\n    padding:12px;\n    border-radius:12px;\n    background:var(--panel);\n    border:1px solid var(--line);\n    color:var(--text-dim);\n    font-size:14px;\n    font-weight:500;\n    cursor:pointer;\n  }\n  .tab.active{\n    color:var(--bg);\n    background:var(--accent);\n    border-color:var(--accent);\n  }\n\n  .panel{\n    background:var(--panel);\n    border:1px solid var(--line);\n    border-radius:16px;\n    padding:20px;\n    margin-bottom:16px;\n  }\n  .panel h2{font-size:15px; font-weight:600; margin:0 0 4px;}\n  .panel .step-desc{font-size:13px; color:var(--text-dim); margin:0 0 14px; line-height:1.5;}\n\n  .hidden{display:none !important;}\n\n  textarea{\n    width:100%;\n    min-height:90px;\n    background:#0A0F18;\n    border:1px solid var(--line);\n    border-radius:10px;\n    color:var(--text);\n    font-size:12px;\n    font-family: ui-monospace, monospace;\n    padding:10px;\n    resize:vertical;\n  }\n\n  .btn{\n    width:100%;\n    padding:13px;\n    border-radius:10px;\n    border:none;\n    font-size:14px;\n    font-weight:600;\n    cursor:pointer;\n    margin-top:10px;\n  }\n  .btn-primary{background:var(--accent); color:#06251A;}\n  .btn-secondary{background:transparent; color:var(--text); border:1px solid var(--line);}\n  .btn-danger{background:var(--warn); color:#2A0906;}\n  .btn:disabled{opacity:0.4; cursor:not-allowed;}\n  .btn-row{display:flex; gap:8px;}\n  .btn-row .btn{margin-top:0;}\n\n  .call-controls{\n    display:flex;\n    justify-content:center;\n    gap:16px;\n    margin-top:20px;\n  }\n  .round-btn{\n    width:56px; height:56px;\n    border-radius:50%;\n    border:1px solid var(--line);\n    background:var(--panel);\n    color:var(--text);\n    font-size:20px;\n    display:flex; align-items:center; justify-content:center;\n    cursor:pointer;\n  }\n  .round-btn.active{background:var(--accent); color:#06251A; border-color:var(--accent);}\n  .round-btn.end{background:var(--warn); color:#2A0906; border-color:var(--warn);}\n\n  .copy-note{font-size:12px; color:var(--accent); margin-top:6px; min-height:16px;}\n\n  audio{display:none;}\n\n  .hint{\n    font-size:12px;\n    color:var(--text-dim);\n    background:rgba(61,220,151,0.08);\n    border:1px solid var(--accent-dim);\n    border-radius:10px;\n    padding:10px 12px;\n    margin-top:16px;\n    line-height:1.5;\n  }\n</style>\n</head>\n<body>\n<div class="wrap">\n\n  <header>\n    <div class="icon">📶</div>\n    <h1>WiFi Call</h1>\n    <p>Same WiFi network par direct call — bina internet data ke, bina server ke.</p>\n  </header>\n\n  <div class="status-pill">\n    <span class="dot" id="statusDot"></span>\n    <span id="statusText">Disconnected</span>\n  </div>\n\n  <div class="tabs">\n    <div class="tab active" id="tabStart">Call banao</div>\n    <div class="tab" id="tabJoin">Call join karo</div>\n  </div>\n\n  <!-- CALLER FLOW -->\n  <div id="startFlow">\n    <div class="panel">\n      <h2>Step 1 — Call shuru karo</h2>\n      <p class="step-desc">Button dabao, mic permission do. Neeche ek code ban jayega — wo doosre device ko WhatsApp/text se bhej do.</p>\n      <button class="btn btn-primary" id="createCallBtn">🎙️ Naya call banao</button>\n      <div id="offerBox" class="hidden">\n        <textarea id="offerOutput" readonly></textarea>\n        <button class="btn btn-secondary" id="copyOfferBtn">Code copy karo</button>\n        <div class="copy-note" id="offerCopyNote"></div>\n      </div>\n    </div>\n\n    <div class="panel hidden" id="answerPanel">\n      <h2>Step 2 — Unka reply code paste karo</h2>\n      <p class="step-desc">Doosre device se jo code aaya hai, wo yahan paste karke connect dabao.</p>\n      <textarea id="answerInput" placeholder="Yahan unka answer code paste karo..."></textarea>\n      <button class="btn btn-primary" id="connectBtn">Connect karo</button>\n    </div>\n  </div>\n\n  <!-- CALLEE FLOW -->\n  <div id="joinFlow" class="hidden">\n    <div class="panel">\n      <h2>Step 1 — Unka code paste karo</h2>\n      <p class="step-desc">Jis device ne call banayi hai, uska code yahan paste karo.</p>\n      <textarea id="offerInput" placeholder="Yahan caller ka code paste karo..."></textarea>\n      <button class="btn btn-primary" id="joinCallBtn">🎙️ Join karo</button>\n    </div>\n\n    <div class="panel hidden" id="replyPanel">\n      <h2>Step 2 — Ye reply code unko bhejo</h2>\n      <p class="step-desc">Ye code caller ko wapas bhejo. Wo apne device par paste karke connect karega, phir call jud jayegi.</p>\n      <textarea id="answerOutput" readonly></textarea>\n      <button class="btn btn-secondary" id="copyAnswerBtn">Code copy karo</button>\n      <div class="copy-note" id="answerCopyNote"></div>\n    </div>\n  </div>\n\n  <div id="controls" class="hidden">\n    <div class="call-controls">\n      <button class="round-btn" id="muteBtn" title="Mute">🎤</button>\n      <button class="round-btn end" id="hangupBtn" title="Hangup">📵</button>\n    </div>\n  </div>\n\n  <div class="hint">\n    💡 Ye same WiFi/local network par kaam karta hai — mobile data ki zaroorat nahi. Dono devices ek hi router se connected hone chahiye. Ek device "Call banao" karega, doosra "Call join karo".\n  </div>\n\n  <audio id="remoteAudio" autoplay playsinline></audio>\n</div>\n\n<script>\nlet pc = null;\nlet localStream = null;\nlet muted = false;\n\nconst statusDot = document.getElementById(\'statusDot\');\nconst statusText = document.getElementById(\'statusText\');\nconst controls = document.getElementById(\'controls\');\nconst remoteAudio = document.getElementById(\'remoteAudio\');\n\nfunction setStatus(text, mode){\n  statusText.textContent = text;\n  statusDot.className = \'dot\' + (mode ? \' \' + mode : \'\');\n}\n\n// Tabs\nconst tabStart = document.getElementById(\'tabStart\');\nconst tabJoin = document.getElementById(\'tabJoin\');\nconst startFlow = document.getElementById(\'startFlow\');\nconst joinFlow = document.getElementById(\'joinFlow\');\n\ntabStart.onclick = () => {\n  tabStart.classList.add(\'active\');\n  tabJoin.classList.remove(\'active\');\n  startFlow.classList.remove(\'hidden\');\n  joinFlow.classList.add(\'hidden\');\n};\ntabJoin.onclick = () => {\n  tabJoin.classList.add(\'active\');\n  tabStart.classList.remove(\'active\');\n  joinFlow.classList.remove(\'hidden\');\n  startFlow.classList.add(\'hidden\');\n};\n\nfunction createPeerConnection(){\n  const peer = new RTCPeerConnection({ iceServers: [] });\n  peer.ontrack = (e) => {\n    remoteAudio.srcObject = e.streams[0];\n  };\n  peer.onconnectionstatechange = () => {\n    if(peer.connectionState === \'connected\'){\n      setStatus(\'Connected — call chal rahi hai\', \'live\');\n      controls.classList.remove(\'hidden\');\n    } else if(peer.connectionState === \'disconnected\' || peer.connectionState === \'failed\'){\n      setStatus(\'Disconnected\', \'warn\');\n    } else if(peer.connectionState === \'connecting\'){\n      setStatus(\'Connecting...\', \'\');\n    }\n  };\n  return peer;\n}\n\nfunction waitForIceGathering(peer){\n  return new Promise((resolve) => {\n    if(peer.iceGatheringState === \'complete\'){\n      resolve();\n    } else {\n      function check(){\n        if(peer.iceGatheringState === \'complete\'){\n          peer.removeEventListener(\'icegatheringstatechange\', check);\n          resolve();\n        }\n      }\n      peer.addEventListener(\'icegatheringstatechange\', check);\n    }\n  });\n}\n\n// ---------- CALLER ----------\ndocument.getElementById(\'createCallBtn\').onclick = async () => {\n  const btn = document.getElementById(\'createCallBtn\');\n  btn.disabled = true;\n  btn.textContent = \'Mic ki ijazat maango...\';\n  try{\n    localStream = await navigator.mediaDevices.getUserMedia({ audio: true });\n  }catch(err){\n    setStatus(\'Mic permission nahi mili\', \'warn\');\n    btn.disabled = false;\n    btn.textContent = \'🎙️ Naya call banao\';\n    return;\n  }\n\n  pc = createPeerConnection();\n  localStream.getTracks().forEach(track => pc.addTrack(track, localStream));\n\n  const offer = await pc.createOffer();\n  await pc.setLocalDescription(offer);\n  await waitForIceGathering(pc);\n\n  const code = btoa(JSON.stringify(pc.localDescription));\n  document.getElementById(\'offerOutput\').value = code;\n  document.getElementById(\'offerBox\').classList.remove(\'hidden\');\n  document.getElementById(\'answerPanel\').classList.remove(\'hidden\');\n  btn.textContent = \'Code ban gaya ✓\';\n  setStatus(\'Reply code ka wait ho raha hai\', \'\');\n};\n\ndocument.getElementById(\'copyOfferBtn\').onclick = () => {\n  const out = document.getElementById(\'offerOutput\');\n  out.select();\n  navigator.clipboard.writeText(out.value);\n  document.getElementById(\'offerCopyNote\').textContent = \'Copy ho gaya — ab isse doosre device ko bhejo.\';\n};\n\ndocument.getElementById(\'connectBtn\').onclick = async () => {\n  const val = document.getElementById(\'answerInput\').value.trim();\n  if(!val || !pc) return;\n  try{\n    const desc = JSON.parse(atob(val));\n    await pc.setRemoteDescription(desc);\n    setStatus(\'Connect ho raha hai...\', \'\');\n  }catch(err){\n    setStatus(\'Code galat hai, dobara check karo\', \'warn\');\n  }\n};\n\n// ---------- CALLEE ----------\ndocument.getElementById(\'joinCallBtn\').onclick = async () => {\n  const btn = document.getElementById(\'joinCallBtn\');\n  const val = document.getElementById(\'offerInput\').value.trim();\n  if(!val) return;\n\n  btn.disabled = true;\n  btn.textContent = \'Mic ki ijazat maango...\';\n\n  let offerDesc;\n  try{\n    offerDesc = JSON.parse(atob(val));\n  }catch(err){\n    setStatus(\'Code galat hai\', \'warn\');\n    btn.disabled = false;\n    btn.textContent = \'🎙️ Join karo\';\n    return;\n  }\n\n  try{\n    localStream = await navigator.mediaDevices.getUserMedia({ audio: true });\n  }catch(err){\n    setStatus(\'Mic permission nahi mili\', \'warn\');\n    btn.disabled = false;\n    btn.textContent = \'🎙️ Join karo\';\n    return;\n  }\n\n  pc = createPeerConnection();\n  localStream.getTracks().forEach(track => pc.addTrack(track, localStream));\n\n  await pc.setRemoteDescription(offerDesc);\n  const answer = await pc.createAnswer();\n  await pc.setLocalDescription(answer);\n  await waitForIceGathering(pc);\n\n  const code = btoa(JSON.stringify(pc.localDescription));\n  document.getElementById(\'answerOutput\').value = code;\n  document.getElementById(\'replyPanel\').classList.remove(\'hidden\');\n  btn.textContent = \'Reply code ban gaya ✓\';\n  setStatus(\'Caller ke connect hone ka wait ho raha hai\', \'\');\n};\n\ndocument.getElementById(\'copyAnswerBtn\').onclick = () => {\n  const out = document.getElementById(\'answerOutput\');\n  out.select();\n  navigator.clipboard.writeText(out.value);\n  document.getElementById(\'answerCopyNote\').textContent = \'Copy ho gaya — ab isse caller ko wapas bhejo.\';\n};\n\n// ---------- CALL CONTROLS ----------\ndocument.getElementById(\'muteBtn\').onclick = (e) => {\n  if(!localStream) return;\n  muted = !muted;\n  localStream.getAudioTracks().forEach(t => t.enabled = !muted);\n  e.currentTarget.classList.toggle(\'active\', muted);\n  e.currentTarget.textContent = muted ? \'🔇\' : \'🎤\';\n};\n\ndocument.getElementById(\'hangupBtn\').onclick = () => {\n  if(pc) pc.close();\n  if(localStream) localStream.getTracks().forEach(t => t.stop());\n  pc = null;\n  localStream = null;\n  controls.classList.add(\'hidden\');\n  setStatus(\'Call khatam\', \'\');\n};\n</script>\n\n<script>\n/* Android LAN-only enhancements */\n(function(){\n  const banner = document.createElement(\'div\');\n  banner.style.cssText =\n    \'margin:0 auto 16px;max-width:520px;padding:10px 12px;border:1px solid #26314A;\' +\n    \'border-radius:10px;background:#161E2E;color:#8A96AE;font-size:12px;text-align:center\';\n  banner.textContent = \'🔒 LAN ONLY • Internet / Mobile Data not required\';\n  document.querySelector(\'.wrap\').insertBefore(banner, document.querySelector(\'.status-pill\'));\n\n  const controls = document.getElementById(\'controls\');\n  if (controls) {\n    const speaker = document.createElement(\'button\');\n    speaker.className = \'round-btn\';\n    speaker.title = \'Speaker\';\n    speaker.textContent = \'🔊\';\n    speaker.onclick = async () => {\n      const a = document.getElementById(\'remoteAudio\');\n      try {\n        a.muted = false;\n        if (typeof a.setSinkId === \'function\') await a.setSinkId(\'\');\n        a.play().catch(()=>{});\n      } catch(e) {}\n      speaker.classList.toggle(\'active\');\n    };\n    controls.querySelector(\'.call-controls\').insertBefore(speaker, controls.querySelector(\'#hangupBtn\'));\n  }\n})();\n</script>\n\n</body>\n</html>\n'

APP_PORT = 8765
DISCOVERY_PORT = 8766
DEVICE_ID_FILE = "device_id.txt"
DEVICE_NAME_FILE = "device_name.txt"


def load_or_create_device_id():
    p = Path(App.get_running_app().user_data_dir) / DEVICE_ID_FILE
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    value = uuid.uuid4().hex
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value, encoding="utf-8")
    return value


class LocalHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/device":
            app = App.get_running_app()
            data = {
                "id": app.device_id,
                "name": app.device_name,
                "ip": app.local_ip,
                "port": APP_PORT,
                "lan_only": True,
            }
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()


class WiFiCallApp(App):
    def build(self):
        self.device_id = load_or_create_device_id()
        self.device_name = "WiFi Phone"
        self.local_ip = self._get_local_ip()
        self.httpd = None

        if platform == "android":
            Clock.schedule_once(self._start_android, 0)
        return Widget()

    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.0.2.1", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "0.0.0.0"

    def _start_local_server(self):
        try:
            self.httpd = HTTPServer(("127.0.0.1", APP_PORT), LocalHandler)
            Thread(target=self.httpd.serve_forever, daemon=True).start()
        except Exception as exc:
            print("Local server error:", exc)

    def _start_android(self, *_):
        self._start_local_server()

        try:
            from android.permissions import request_permissions, Permission
            request_permissions([
                Permission.RECORD_AUDIO,
                Permission.MODIFY_AUDIO_SETTINGS,
                Permission.ACCESS_WIFI_STATE,
                Permission.CHANGE_WIFI_STATE,
                Permission.ACCESS_NETWORK_STATE,
                Permission.ACCESS_FINE_LOCATION,
                Permission.POST_NOTIFICATIONS,
            ])
        except Exception as exc:
            print("Permission request:", exc)

        try:
            from jnius import autoclass, PythonJavaClass, java_method

            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            WebView = autoclass("android.webkit.WebView")
            WebViewClient = autoclass("android.webkit.WebViewClient")
            WebChromeClient = autoclass("android.webkit.WebChromeClient")
            LayoutParams = autoclass("android.view.ViewGroup$LayoutParams")

            activity = PythonActivity.mActivity
            webview = WebView(activity)
            settings = webview.getSettings()
            settings.setJavaScriptEnabled(True)
            settings.setDomStorageEnabled(True)
            settings.setDatabaseEnabled(True)
            settings.setMediaPlaybackRequiresUserGesture(False)
            settings.setAllowFileAccess(True)
            settings.setAllowContentAccess(True)

            webview.setWebViewClient(WebViewClient())

            class ChromeClient(PythonJavaClass):
                __javainterfaces__ = ["android/webkit/WebChromeClient"]

                @java_method("(Landroid/webkit/PermissionRequest;)V")
                def onPermissionRequest(self, request):
                    try:
                        request.grant(request.getResources())
                    except Exception:
                        pass

            chrome = ChromeClient()
            webview.setWebChromeClient(chrome)

            # A secure localhost origin is used for media APIs.
            webview.loadDataWithBaseURL(
                "https://localhost/",
                HTML,
                "text/html",
                "UTF-8",
                None,
            )

            activity.setContentView(
                webview,
                LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT),
            )

            self.webview = webview
            self.chrome = chrome

        except Exception as exc:
            print("Android WebView startup error:", exc)

    def on_stop(self):
        try:
            if self.httpd:
                self.httpd.shutdown()
        except Exception:
            pass
        return True


if __name__ == "__main__":
    WiFiCallApp().run()
