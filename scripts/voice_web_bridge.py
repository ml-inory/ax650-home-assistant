#!/usr/bin/env python3
"""
AX650 Voice Web Bridge
=======================
Serves a web page that captures PC microphone audio, sends to AX650 ASR/LLM/TTS
pipeline, plays TTS response in browser, and controls devices via Home Assistant.

Requirements: pip3 install aiohttp aiohttp-cors
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import struct
import sys
import wave
from pathlib import Path

import aiohttp
import ssl
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
_LOGGER = logging.getLogger("voice-bridge")

# ---------------------------------------------------------------------------
# Wyoming protocol helpers (lightweight client)
# ---------------------------------------------------------------------------

WYOMING_EVENT_TYPES = {
    "audio-start": 0x01,
    "audio-chunk": 0x02,
    "audio-stop": 0x03,
    "transcribe": 0x04,
    "transcript": 0x05,
    "synthesize": 0x06,
    "synthesize-start": 0x07,
    "synthesize-chunk": 0x08,
    "synthesize-stop": 0x09,
    "synthesize-stopped": 0x0A,
    "describe": 0x0B,
    "info": 0x0C,
}
WYOMING_TYPE_NAMES = {v: k for k, v in WYOMING_EVENT_TYPES.items()}


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return value, pos


def _write_varint(value: int) -> bytes:
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _decode_wyoming_events(data: bytes) -> list[dict]:
    events = []
    pos = 0
    while pos < len(data):
        if pos + 1 > len(data):
            break
        event_type = data[pos]
        pos += 1
        payload_len, pos = _read_varint(data, pos)
        payload = data[pos : pos + payload_len]
        pos += payload_len
        event = {"type": event_type, "type_name": WYOMING_TYPE_NAMES.get(event_type, f"unknown-{event_type}"), "payload": payload}
        if event_type == WYOMING_EVENT_TYPES["audio-start"] and len(payload) >= 12:
            rate, width, channels = struct.unpack_from("<III", payload)
            event.update(rate=rate, width=width, channels=channels)
        elif event_type == WYOMING_EVENT_TYPES["audio-chunk"] and len(payload) >= 12:
            rate, width, channels = struct.unpack_from("<III", payload, 0)
            audio_data = payload[12:]
            event.update(rate=rate, width=width, channels=channels, audio=audio_data)
        elif event_type == WYOMING_EVENT_TYPES["transcript"]:
            # text_len(4) + text
            if len(payload) >= 4:
                text_len = struct.unpack_from("<I", payload)[0]
                text = payload[4 : 4 + text_len].decode("utf-8", errors="replace") if text_len > 0 else ""
                event["text"] = text
        elif event_type == WYOMING_EVENT_TYPES["synthesize-chunk"] and len(payload) >= 12:
            rate, width, channels = struct.unpack_from("<III", payload, 0)
            audio_data = payload[12:]
            event.update(rate=rate, width=width, channels=channels, audio=audio_data)
        events.append(event)
    return events


def _encode_wyoming_event(event_type: str, payload: bytes = b"") -> bytes:
    type_id = WYOMING_EVENT_TYPES.get(event_type)
    if type_id is None:
        raise ValueError(f"Unknown event type: {event_type}")
    return bytes([type_id]) + _write_varint(len(payload)) + payload


async def wyoming_transcribe(host: str, port: int, pcm_data: bytes, rate: int = 16000, width: int = 2, channels: int = 1) -> str:
    """Send PCM audio to Wyoming ASR and get transcript text."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5.0
        )
    except Exception:
        return ""

    try:
        # send describe first (some servers need it)
        writer.write(_encode_wyoming_event("describe"))
        # consume info response
        resp = await asyncio.wait_for(reader.read(4096), timeout=3.0)

        # send transcribe + audio
        writer.write(_encode_wyoming_event("transcribe"))
        audio_start = struct.pack("<III", rate, width, channels)
        writer.write(_encode_wyoming_event("audio-start", audio_start))

        # chunk audio in 4096-byte pieces
        chunk_size = 4096
        for i in range(0, len(pcm_data), chunk_size):
            chunk = pcm_data[i : i + chunk_size]
            header = struct.pack("<III", rate, width, channels)
            writer.write(_encode_wyoming_event("audio-chunk", header + chunk))

        writer.write(_encode_wyoming_event("audio-stop"))

        # read transcript
        all_data = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=30.0)
                if not chunk:
                    break
                all_data += chunk
            except asyncio.TimeoutError:
                break

        events = _decode_wyoming_events(all_data)
        for evt in events:
            if evt.get("text"):
                return evt["text"]
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    return ""


async def wyoming_synthesize(host: str, port: int, text: str) -> bytes | None:
    """Send text to Wyoming TTS and get WAV audio bytes."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5.0
        )
    except Exception:
        return None

    try:
        # describe
        writer.write(_encode_wyoming_event("describe"))
        await asyncio.wait_for(reader.read(4096), timeout=3.0)

        # synthesize
        text_bytes = text.encode("utf-8")
        synth_payload = struct.pack("<I", len(text_bytes)) + text_bytes
        writer.write(_encode_wyoming_event("synthesize", synth_payload))

        # collect audio chunks
        all_audio = bytearray()
        rate = 24000
        width = 2
        channels = 1

        all_data = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(8192), timeout=60.0)
                if not chunk:
                    break
                all_data += chunk
            except asyncio.TimeoutError:
                break

        events = _decode_wyoming_events(all_data)
        for evt in events:
            if evt.get("type_name") == "synthesize-chunk":
                all_audio.extend(evt.get("audio", b""))
                rate = evt.get("rate", rate)
                width = evt.get("width", width)
                channels = evt.get("channels", channels)

        if all_audio:
            return pcm_to_wav_bytes(bytes(all_audio), rate, width, channels)
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    return None


def pcm_to_wav_bytes(audio: bytes, rate: int, width: int, channels: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(rate)
        wav_file.writeframes(audio)
    return output.getvalue()


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AX650 语音助手</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  background:linear-gradient(135deg,#1a1a2e,#16213e);color:#eee;min-height:100vh;
  display:flex;flex-direction:column;align-items:center;padding:20px}
h1{font-size:1.6em;margin:20px 0 10px;color:#00d4aa}
.subtitle{font-size:0.9em;color:#888;margin-bottom:20px}
.panel{background:rgba(255,255,255,0.06);border-radius:16px;padding:24px;
  max-width:500px;width:100%}
.status-row{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.dot{width:10px;height:10px;border-radius:50%;background:#555}
.dot.on{background:#00d4aa;box-shadow:0 0 8px #00d4aa}
.dot.off{background:#ff4444}
.status-text{font-size:0.85em;color:#aaa}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:14px 28px;border:none;border-radius:50px;font-size:1em;cursor:pointer;
  transition:all 0.2s;font-weight:600;color:#fff}
.btn-record{background:linear-gradient(135deg,#ff4444,#cc0000);width:100%}
.btn-record.listening{background:linear-gradient(135deg,#00d4aa,#00a080);
  animation:pulse 1.5s infinite}
.btn-device{background:rgba(255,255,255,0.1);margin:4px;font-size:0.85em;padding:8px 16px}
.btn-device:hover{background:rgba(255,255,255,0.2)}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(0,212,170,0.4)}
  50%{box-shadow:0 0 0 12px rgba(0,212,170,0)}}
#chat-box{background:rgba(0,0,0,0.3);border-radius:12px;padding:16px;
  max-height:300px;overflow-y:auto;margin-bottom:16px;min-height:80px}
.msg{margin:6px 0;padding:8px 12px;border-radius:10px;max-width:80%;word-wrap:break-word}
.msg.user{background:rgba(0,212,170,0.2);margin-left:auto;text-align:right}
.msg.assistant{background:rgba(255,255,255,0.08)}
.msg.system{background:rgba(255,180,0,0.15);text-align:center;font-size:0.85em}
.devices-bar{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
input[type=text]{width:100%;padding:12px;border-radius:10px;border:1px solid rgba(255,255,255,0.2);
  background:rgba(0,0,0,0.3);color:#eee;font-size:0.95em;margin-bottom:10px}
input[type=text]:focus{outline:none;border-color:#00d4aa}
.spinner{display:none;width:20px;height:20px;border:2px solid #333;
  border-top-color:#00d4aa;border-radius:50%;animation:spin 0.8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<h1>🎙️ AX650 语音助手</h1>
<div class="subtitle">PC 录音 → AX650 AI → 控制设备</div>

<div class="panel">
  <div class="status-row">
    <div class="dot" id="status-dot"></div>
    <span class="status-text" id="status-text">连接中...</span>
    <div class="spinner" id="spinner"></div>
  </div>

  <div id="chat-box"></div>

  <input type="text" id="text-input" placeholder="输入文字指令，如：打开客厅灯" 
    onkeydown="if(event.key==='Enter'){sendText();return false}">

  <button class="btn btn-record" id="record-btn" onclick="toggleRecord()">
    <span id="record-icon">🎤</span> <span id="record-label">按住说话</span>
  </button>

  <div class="devices-bar" id="devices-bar"></div>
</div>

<script>
const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") + location.hostname + ":" + location.port + "/ws";
let ws = null;
let isRecording = false;
let mediaRecorder = null;
let audioChunks = [];
let deviceList = [];

function setStatus(ok, text) {
  document.getElementById('status-dot').className = 'dot ' + (ok ? 'on' : 'off');
  document.getElementById('status-text').textContent = text;
}

function addMsg(type, text) {
  const box = document.getElementById('chat-box');
  const div = document.createElement('div');
  div.className = 'msg ' + type;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

function connect() {
  ws = new WebSocket(WS_URL);
  ws.onopen = () => { setStatus(true, '已连接 AX650'); loadDevices(); };
  ws.onclose = () => { setStatus(false, '断开，2秒后重连...'); setTimeout(connect, 2000); };
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === 'text') {
      addMsg('assistant', data.text);
      if (data.command) addMsg('system', '📟 ' + data.command);
      if (data.devices) { deviceList = data.devices; renderDevices(); }
    } else if (data.type === 'audio') {
      playAudio(data.data, data.format || 'wav');
    } else if (data.type === 'error') {
      addMsg('system', '❌ ' + data.text);
    }
  };
}

function loadDevices() {
  send({type: 'get_devices'});
}

function renderDevices() {
  const bar = document.getElementById('devices-bar');
  bar.innerHTML = '<span style="font-size:0.8em;color:#888">设备:</span>';
  for (const d of deviceList) {
    const btn = document.createElement('button');
    btn.className = 'btn btn-device';
    btn.textContent = (d.state === 'on' ? '💡 ' : '⚫ ') + (d.name || d.entity_id);
    btn.onclick = () => {
      const action = d.state === 'on' ? 'turn_off' : 'turn_on';
      send({type: 'device_cmd', entity_id: d.entity_id, action: action});
    };
    bar.appendChild(btn);
  }
}

// ---- Recording via Web Speech API ----
let recognition = null;
let recognitionActive = false;

function initRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    addMsg('system', '浏览器不支持语音识别，请使用 Chrome');
    return null;
  }
  const rec = new SpeechRecognition();
  rec.lang = 'zh-CN';
  rec.interimResults = false;
  rec.continuous = false;
  rec.maxAlternatives = 1;
  rec.onresult = (e) => {
    const text = e.results[0][0].transcript.trim();
    if (text) {
      addMsg('user', text);
      send({type: 'text', text: text});
      document.getElementById('spinner').style.display = 'inline-block';
    }
    stopRecordingUI();
  };
  rec.onerror = (e) => {
    addMsg('system', '识别失败: ' + e.error);
    stopRecordingUI();
  };
  rec.onend = () => {
    stopRecordingUI();
  };
  return rec;
}

function stopRecordingUI() {
  isRecording = false;
  recognitionActive = false;
  document.getElementById('record-btn').classList.remove('listening');
  document.getElementById('record-label').textContent = '按住说话';
  document.getElementById('record-icon').textContent = '🎤';
}

// Debounce guard
let toggleLock = false;

async function toggleRecord() {
  if (toggleLock) return;
  toggleLock = true;

  if (!recognition) recognition = initRecognition();
  if (!recognition) { toggleLock = false; return; }

  // Abort any previous recognition to avoid 'already started' error
  try { recognition.abort(); } catch(e) {}

  try {
    recognitionActive = true;
    isRecording = true;
    document.getElementById('record-btn').classList.add('listening');
    document.getElementById('record-label').textContent = '正在听...';
    document.getElementById('record-icon').textContent = '🔴';
    recognition.start();
  } catch (e) {
    isRecording = false;
    recognitionActive = false;
    stopRecordingUI();
    addMsg('system', '语音启动失败: ' + e.message);
  }
  toggleLock = false;
}

function stopRecording() {
  if (isRecording && recognition && recognitionActive) {
    recognition.stop();
    recognitionActive = false;
  }
  stopRecordingUI();
}

function sendText() {
  const input = document.getElementById('text-input');
  const text = input.value.trim();
  if (!text) return;
  addMsg('user', text);
  send({type: 'text', text: text});
  document.getElementById('spinner').style.display = 'inline-block';
  input.value = '';
}

function playAudio(base64Data, format) {
  document.getElementById('spinner').style.display = 'none';
  const mime = format === 'mp3' ? 'audio/mpeg' : 'audio/wav';
  const audio = new Audio('data:' + mime + ';base64,' + base64Data);
  audio.play().catch(e => console.log('Playback error:', e));
}

// Mouse events for hold-to-record
document.getElementById('record-btn').addEventListener('mousedown', (e) => { e.preventDefault(); toggleRecord(); });
document.getElementById('record-btn').addEventListener('mouseup', (e) => { e.preventDefault(); stopRecording(); });
document.getElementById('record-btn').addEventListener('mouseleave', () => { if(isRecording) stopRecording(); });
// Touch events for mobile
document.getElementById('record-btn').addEventListener('touchstart', (e) => { e.preventDefault(); toggleRecord(); });
document.getElementById('record-btn').addEventListener('touchend', (e) => { e.preventDefault(); stopRecording(); });

connect();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

class VoiceBridge:
    def __init__(
        self,
        asr_host: str = "127.0.0.1",
        asr_port: int = 10300,
        tts_host: str = "127.0.0.1",
        tts_port: int = 10200,
        llm_url: str = "http://127.0.0.1:8001/v1",
        ha_url: str = "http://127.0.0.1:8123/api",
        ha_token: str = "",
    ):
        self.asr_host = asr_host
        self.asr_port = asr_port
        self.tts_host = tts_host
        self.tts_port = tts_port
        self.llm_url = llm_url
        self.ha_url = ha_url
        self.ha_token = ha_token

    # ------------------------------------------------------------------
    # Home Assistant API
    # ------------------------------------------------------------------

    def _ha_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.ha_token:
            headers["Authorization"] = f"Bearer {self.ha_token}"
        return headers

    async def _ha_request(self, method: str, path: str, json_data: dict = None) -> dict:
        """Make a request to the Home Assistant REST API."""
        url = f"{self.ha_url.rstrip('/')}/{path.lstrip('/')}"
        headers = self._ha_headers()
        try:
            async with aiohttp.ClientSession() as session:
                if method == "GET":
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        return await resp.json() if resp.status == 200 else {}
                elif method == "POST":
                    async with session.post(url, headers=headers, json=json_data, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        return await resp.json() if resp.status in (200, 201) else {}
        except Exception:
            return {}

    async def get_devices(self) -> list[dict]:
        """Fetch light/switch devices from HA."""
        result = await self._ha_request("GET", "states")
        if not result:
            return []
        devices = []
        for entity in result:
            eid = entity.get("entity_id", "")
            # filter common device types
            if any(eid.startswith(p) for p in ("light.", "switch.", "fan.", "climate.", "cover.", "input_boolean.", "media_player.", "vacuum.", "humidifier.", "camera.", "lock.")):
                state = entity.get("state", "unknown")
                attrs = entity.get("attributes", {})
                devices.append({
                    "entity_id": eid,
                    "name": attrs.get("friendly_name", eid),
                    "state": state,
                    "domain": eid.split(".")[0],
                })
        return devices

    async def call_service(self, domain: str, service: str, entity_id: str) -> dict:
        """Call a HA service."""
        return await self._ha_request("POST", f"services/{domain}/{service}", {
            "entity_id": entity_id,
        })

    # ------------------------------------------------------------------
    # LLM chat
    # ------------------------------------------------------------------

    async def llm_chat(self, user_text: str) -> str:
        """Send text to AX650 LLM and get response."""
        system_prompt = (
            "你是一个智能家居语音助手。回答要简洁自然，像一个朋友在说话。"
            "每次回复不超过两句话。"
        )
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": "AXERA-TECH/Qwen3-0.6B",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_text},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 256,
                }
                async with session.post(
                    f"{self.llm_url.rstrip('/')}/chat/completions",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw = data["choices"][0]["message"]["content"]
                    import re
                    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                    return raw
        except Exception as e:
            _LOGGER.warning("LLM error: %s", e)
        return "抱歉，我暂时无法处理这个请求。"

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    async def process_audio(self, pcm_data: bytes, rate: int = 16000, width: int = 2, channels: int = 1) -> tuple[str, str, bytes | None]:
        """Full pipeline: ASR → LLM → TTS."""
        # Step 1: ASR
        text = await wyoming_transcribe(self.asr_host, self.asr_port, pcm_data, rate, width, channels)
        if not text:
            return "", "未识别到语音内容", None

        # Step 2: LLM
        response = await self.llm_chat(text)

        # Step 3: TTS
        audio = await wyoming_synthesize(self.tts_host, self.tts_port, response)

        return text, response, audio

    async def process_text(self, user_text: str) -> tuple[str, bytes | None]:
        """Text → LLM → edge-tts."""
        response = await self.llm_chat(user_text)
        audio = await self._edge_tts(response)
        return response, audio

    async def _edge_tts(self, text: str, voice: str = "zh-CN-XiaoxiaoNeural") -> bytes | None:
        """Use Microsoft Edge TTS (free, no API key needed)."""
        if not text.strip():
            return None
        try:
            import edge_tts, tempfile, asyncio
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp_path = f.name
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(tmp_path)
            with open(tmp_path, "rb") as f:
                data = f.read()
            import os
            os.unlink(tmp_path)
            return data
        except Exception as e:
            _LOGGER.warning("edge-tts error: %s", e)
            return None


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024)
    await ws.prepare(request)
    bridge: VoiceBridge = request.app["bridge"]

    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            continue

        msg_type = data.get("type")

        if msg_type == "text":
            user_text = data.get("text", "").strip()
            if not user_text:
                continue

            # LLM → TTS
            response, audio_bytes = await bridge.process_text(user_text)

            # Device control: keyword match user input against HA devices
            command_info = ""
            try:
                devices = await bridge.get_devices()
                user_lower = user_text.lower()
                for d in devices:
                    name = d.get("name", "").lower()
                    eid = d["entity_id"]
                    domain = d["domain"]
                    # Check if user mentioned this device
                    if name and name in user_lower:
                        if any(w in user_lower for w in ["打开", "开", "turn on", "open"]):
                            await bridge.call_service(domain, "turn_on", eid)
                            command_info = "已打开 " + d["name"]
                        elif any(w in user_lower for w in ["关闭", "关", "turn off", "close"]):
                            await bridge.call_service(domain, "turn_off", eid)
                            command_info = "已关闭 " + d["name"]
                        elif any(w in user_lower for w in ["toggle", "切换"]):
                            await bridge.call_service(domain, "toggle", eid)
                            command_info = "已切换 " + d["name"]
                        break
            except Exception as _e:
                import traceback
                traceback.print_exc()

            # Check for ACTION tags from LLM too (backup)
            display_text = response
            for line in response.split("\n"):
                if line.strip().startswith("[ACTION:"):
                    action_str = line.strip()[8:-1]
                    display_text = response.replace(line, "").strip()
                    parts = action_str.split("|")
                    if len(parts) == 2:
                        service_path, entity_id = parts
                        if "." in service_path:
                            domain, service = service_path.rsplit(".", 1)
                            try:
                                await bridge.call_service(domain, service, entity_id)
                                command_info = f"执行: {service_path} → {entity_id}"
                            except Exception:
                                pass
                    break

            await ws.send_json({"type": "text", "text": display_text, "command": command_info})

            if audio_bytes:
                import base64
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio_bytes).decode(), "format": "mp3"})

        elif msg_type == "audio":
            # Audio path: browser sends PCM/WAV for Wyoming ASR
            # Requires voice stack (ASR on 10300) to be running
            await ws.send_json({"type": "error", "text": "ASR 服务未启动。请使用浏览器语音识别（Chrome 自带），或按提示启动语音栈。"})

        elif msg_type == "get_devices":
            devices = await bridge.get_devices()
            await ws.send_json({"type": "text", "text": "", "devices": devices})

        elif msg_type == "device_cmd":
            entity_id = data.get("entity_id", "")
            action = data.get("action", "")
            domain = entity_id.split(".")[0] if "." in entity_id else ""
            service = "toggle"
            if action == "turn_on":
                service = "turn_on"
            elif action == "turn_off":
                service = "turn_off"
            result = await bridge.call_service(domain, service, entity_id)
            await ws.send_json({"type": "text", "text": f"{entity_id} → {action}", "command": str(result)})

        else:
            await ws.send_json({"type": "error", "text": f"Unknown message type: {msg_type}"})

    return ws


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=HTML_PAGE, content_type="text/html", charset="utf-8")


async def handle_health(request: web.Request) -> web.Response:
    bridge: VoiceBridge = request.app["bridge"]
    # quick ASR/LLM/HA health check
    status = {"asr": "unknown", "llm": "unknown", "ha": "unknown"}
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(bridge.asr_host, bridge.asr_port), timeout=2)
        writer.close()
        status["asr"] = "ok"
    except Exception:
        status["asr"] = "unavailable"

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{bridge.llm_url.rstrip('/')}/models", timeout=aiohttp.ClientTimeout(total=3)) as r:
                status["llm"] = "ok" if r.status == 200 else "unavailable"
    except Exception:
        status["llm"] = "unavailable"

    try:
        async with aiohttp.ClientSession() as s:
            h = {"Authorization": f"Bearer {bridge.ha_token}"} if bridge.ha_token else {}
            async with s.get(f"{bridge.ha_url.rstrip('/')}/", headers=h, timeout=aiohttp.ClientTimeout(total=3)) as r:
                status["ha"] = "ok" if r.status == 200 else "unavailable"
    except Exception:
        status["ha"] = "unavailable"

    return web.json_response(status)


def main():
    parser = argparse.ArgumentParser(description="AX650 Voice Web Bridge")
    parser.add_argument("--host", default="0.0.0.0", help="Listen host")
    parser.add_argument("--port", type=int, default=8080, help="Listen port")
    parser.add_argument("--asr-host", default="127.0.0.1")
    parser.add_argument("--asr-port", type=int, default=10300)
    parser.add_argument("--tts-host", default="127.0.0.1")
    parser.add_argument("--tts-port", type=int, default=10200)
    parser.add_argument("--llm-url", default="http://127.0.0.1:8001/v1")
    parser.add_argument("--ha-url", default="http://127.0.0.1:8123/api")
    parser.add_argument("--ha-token", default="")
    parser.add_argument("--ssl-cert", default="", help="SSL certificate PEM file for HTTPS")
    parser.add_argument("--ssl-key", default="", help="SSL private key PEM file for HTTPS")
    args = parser.parse_args()

    bridge = VoiceBridge(
        asr_host=args.asr_host,
        asr_port=args.asr_port,
        tts_host=args.tts_host,
        tts_port=args.tts_port,
        llm_url=args.llm_url,
        ha_url=args.ha_url,
        ha_token=args.ha_token,
    )

    app = web.Application()
    app["bridge"] = bridge
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/health", handle_health)

    _LOGGER.info("Voice Web Bridge starting on http://%s:%s", args.host, args.port)
    _LOGGER.info("  ASR: %s:%s", args.asr_host, args.asr_port)
    _LOGGER.info("  TTS: %s:%s", args.tts_host, args.tts_port)
    _LOGGER.info("  LLM: %s", args.llm_url)
    _LOGGER.info("  HA:  %s", args.ha_url)

    ssl_context = None
    if args.ssl_cert and args.ssl_key:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(args.ssl_cert, args.ssl_key)
        _LOGGER.info("HTTPS enabled with cert=%s", args.ssl_cert)

    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_context, print=lambda *a, **k: None)


if __name__ == "__main__":
    main()
