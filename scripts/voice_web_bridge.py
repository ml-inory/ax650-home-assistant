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
import socket
import time as _time
import hashlib as _hashlib

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
.btn-refresh{background:none;border:none;font-size:1.2em;cursor:pointer;padding:2px 6px;
  margin-left:auto;border-radius:6px;transition:all 0.2s}
.btn-refresh:hover{background:rgba(0,212,170,0.2)}
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
    <button class="btn-refresh" onclick="loadDevices()" title="刷新设备列表">🔄</button>
    <span style="font-size:0.75em;color:#666" id="device-count"></span>
  </div>

  <div id="chat-box"></div>

  <input type="text" id="text-input" placeholder="输入文字指令，如：打开客厅灯" 
    onkeydown="if(event.key==='Enter'){sendText();return false}">

  <button class="btn btn-record" id="record-btn" onclick="toggleRecord()">
    <span id="record-icon">🎤</span> <span id="record-label">按住说话</span>
  </button>

  <div class="devices-bar" id="devices-bar"></div>

  <details style="margin-top:16px" id="mi-panel">
    <summary style="color:#888;cursor:pointer;font-size:0.85em">🔑 小米设备接入</summary>
    <div style="margin-top:12px;display:flex;flex-direction:column;gap:8px">
      <button class="btn btn-device" onclick="xiaomiScan()" style="width:100%;padding:12px;background:rgba(0,212,170,0.2);margin:0">
        📡 扫描局域网小米设备
      </button>
      <div style="border-top:1px solid rgba(255,255,255,0.1);margin:4px 0"></div>
      <div style="font-size:0.75em;color:#888">☁️ 云端登录 (弹窗验证):</div>
      <input type="text" id="mi-user" placeholder="小米账号 (手机号/邮箱)" style="flex:1">
      <input type="password" id="mi-pass" placeholder="小米密码" style="flex:1">
      <select id="mi-server" style="padding:10px;border-radius:10px;background:#222;color:#eee;border:1px solid #444">
        <option value="cn">中国大陆 (cn)</option>
        <option value="de">德国 (de)</option>
      </select>
      <button class="btn btn-device" onclick="xiaomiLogin()" style="width:100%;padding:12px;background:rgba(255,103,0,0.3);margin:0">
        🔍 云端获取设备
      </button>
      <div style="border-top:1px solid rgba(255,255,255,0.1);margin:4px 0"></div>
      <div style="font-size:0.75em;color:#888">✏️ 手动输入设备:</div>
      <input type="text" id="mi-manual-ip" placeholder="设备 IP (如 192.168.31.x)">
      <input type="text" id="mi-manual-token" placeholder="设备 Token (32位十六进制)">
      <input type="text" id="mi-manual-name" placeholder="设备名称 (可选)">
      <button class="btn btn-device" onclick="xiaomiAddManual()" style="width:100%;padding:12px;background:rgba(0,180,255,0.2);margin:0">
        ➕ 手动添加设备
      </button>
      <div style="font-size:0.7em;color:#666;margin-top:4px">
        Token 获取方法：<br>
        1. 用 <a href="https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor" target="_blank" style="color:#00d4aa">Xiaomi-cloud-tokens-extractor</a> 提取<br>
        2. 或在手机小米 Home App 中找到设备网络信息
      </div>
    </div>
    <div id="mi-result" style="margin-top:10px;font-size:0.8em;max-height:300px;overflow-y:auto"></div>
  </details>
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
      document.getElementById('mi-result').innerHTML = '<span style="color:#f44">' + data.text + '</span>';
    } else if (data.type === 'xiaomi_need_verify') {
      document.getElementById('mi-result').innerHTML = '<span style="color:#ffa500">需要验证码</span>';
      // Add verify link
      const verifyHtml = '<br><a id="mi-verify-url" href="' + data.verify_url + '" target="_blank" style="color:#00d4aa;font-size:0.8em">打开验证页面</a> ' +
        '<button onclick="xiaomiStartVerify(\'' + data.flow_id + '\')" style="background:#00d4aa;color:#000;border:none;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:0.8em">弹窗验证</button>';
      document.getElementById('mi-result').innerHTML += verifyHtml;
      _miCreds = {u: document.getElementById('mi-user').value, p: document.getElementById('mi-pass').value};
    } else if (data.type === 'xiaomi_devices') {
      let html = '<b>找到 ' + data.devices.length + ' 个设备:</b>';
      for (const d of data.devices) {
        html += '<div style="margin:4px 0;padding:6px;background:rgba(255,255,255,0.05);border-radius:6px">';
        html += '<b>' + d.name + '</b> (' + d.model + ')<br>';
        html += '<span style="font-size:0.75em;color:#888">IP: ' + (d.ip || 'N/A') + ' | Token: ' + (d.token || 'N/A').substring(0,12) + '...</span><br>';
        html += '<button class="btn btn-device" onclick="xiaomiAddDevice(' +
          JSON.stringify(d.token) + ',' + JSON.stringify(d.ip) + ',' +
          JSON.stringify(d.name) + ',' + JSON.stringify(d.model) +
          ')" style="margin-top:4px">➕ 添加到HA</button>';
        html += '</div>';
      }
      document.getElementById('mi-result').innerHTML = html;
    } else if (data.type === 'xiaomi_scan_result') {
      let html = '<b>📡 扫描结果 (' + data.devices.length + ' 个设备):</b>';
      if (data.devices.length === 0) {
        html += '<div style="color:#ffa500;margin-top:4px">未发现小米设备。请确认设备在同一局域网，或使用下方手动输入。</div>';
      }
      for (const d of data.devices) {
        html += '<div style="margin:4px 0;padding:6px;background:rgba(0,212,170,0.08);border-radius:6px">';
        html += '<b>' + d.name + '</b> (' + d.model + ')<br>';
        html += '<span style="font-size:0.75em;color:#888">IP: ' + d.ip + ' | ID: ' + d.device_id + '</span><br>';
        html += '<span style="font-size:0.7em;color:#666">Token 需手动输入下方</span>';
        html += '</div>';
      }
      document.getElementById('mi-result').innerHTML = html;
    } else if (data.type === 'xiaomi_added') {
      document.getElementById('mi-result').innerHTML = '<span style="color:#0f0">' + data.text + '</span>';
      loadDevices();
    }
  };
}

function loadDevices() {
  send({type: 'get_devices'});
}

function xiaomiLogin() {
  const u = document.getElementById('mi-user').value.trim();
  const p = document.getElementById('mi-pass').value.trim();
  const s = document.getElementById('mi-server').value;
  if (!u || !p) { addMsg('system', '请输入小米账号和密码'); return; }
  document.getElementById('mi-result').innerHTML = '<span style="color:#ffa500">正在连接小米云端...</span>';
  send({type: 'xiaomi_login', username: u, password: p, server: s});
}

function xiaomiAddDevice(token, ip, name, model) {
  document.getElementById('mi-result').innerHTML += `<br><span style="color:#00d4aa">添加 ${name}...</span>`;
  send({type: 'xiaomi_add', token: token, ip: ip, name: name, model: model});
}

function xiaomiScan() {
  document.getElementById('mi-result').innerHTML = '<span style="color:#ffa500">📡 正在扫描局域网小米设备 (UDP 54321)...</span>';
  send({type: 'xiaomi_scan'});
}

function xiaomiAddManual() {
  const ip = document.getElementById('mi-manual-ip').value.trim();
  const token = document.getElementById('mi-manual-token').value.trim();
  const name = document.getElementById('mi-manual-name').value.trim() || 'Xiaomi Device';
  if (!ip || !token) {
    addMsg('system', '请输入设备 IP 和 Token');
    return;
  }
  document.getElementById('mi-result').innerHTML += `<br><span style="color:#00d4aa">添加 ${name} (${ip})...</span>`;
  send({type: 'xiaomi_add_manual', ip: ip, token: token, name: name});
}

function renderDevices() {
  document.getElementById('device-count').textContent = deviceList.length + ' 个设备';
  const bar = document.getElementById('devices-bar');
  bar.innerHTML = '<span style="font-size:0.8em;color:#888">设备:</span>';
  for (const d of deviceList) {
    const btn = document.createElement('button');
    btn.className = 'btn btn-device';
    if (d.is_button) {
      btn.textContent = '🔘 ' + (d.name || d.entity_id);
      btn.onclick = () => {
        send({type: 'device_cmd', entity_id: d.entity_id, action: 'press'});
      };
    } else if (d.domain === 'select') {
      btn.textContent = '📋 ' + (d.name || d.entity_id) + ': ' + (d.state || '?');
      btn.onclick = () => {
        send({type: 'device_cmd', entity_id: d.entity_id, action: 'select_next'});
      };
    } else {
      btn.textContent = (d.state === 'on' ? '💡 ' : '⚫ ') + (d.name || d.entity_id);
      btn.onclick = () => {
        const action = d.state === 'on' ? 'turn_off' : 'turn_on';
        send({type: 'device_cmd', entity_id: d.entity_id, action: action});
      };
    }
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
setInterval(loadDevices, 30000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Xiaomi miIO LAN Discovery (no external deps)
# ---------------------------------------------------------------------------

MIIO_DISCOVERY_PORT = 54321
MIIO_HELLO = bytes([
    0x21, 0x31,  # magic
    0x00, 0x20,  # length (32 bytes)
    0x00, 0x00, 0x00, 0x00,  # unknown
    0xFF, 0xFF, 0xFF, 0xFF,  # device ID (broadcast)
    0x00, 0x00, 0x00, 0x00,  # stamp
    # checksum (16 bytes, zero for discovery)
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])

def miio_discover(timeout: float = 3.0) -> list[dict]:
    """Broadcast miIO discovery and collect device responses."""
    devices = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)

    try:
        sock.sendto(MIIO_HELLO, ('255.255.255.255', MIIO_DISCOVERY_PORT))

        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
                if len(data) < 32:
                    continue
                # Parse response header
                magic = struct.unpack('>H', data[0:2])[0]
                if magic != 0x2131:
                    continue
                pkt_len = struct.unpack('>H', data[2:4])[0]
                did = struct.unpack('>I', data[8:12])[0]
                stamp = struct.unpack('>I', data[12:16])[0]
                # Device ID in hex
                did_hex = format(did, '08x')

                # Body starts at offset 32, try to extract JSON
                body = data[32:pkt_len] if pkt_len > 32 else data[32:]
                info = {}
                try:
                    # Find JSON by looking for '{'
                    json_start = body.find(b'{')
                    if json_start >= 0:
                        # The JSON might be null-terminated
                        json_bytes = body[json_start:].split(b'\x00')[0]
                        info = __import__('json').loads(json_bytes.decode('utf-8', errors='replace'))
                except Exception:
                    pass

                devices.append({
                    "ip": addr[0],
                    "device_id": did_hex,
                    "model": info.get("model", "unknown"),
                    "name": info.get("name", info.get("model", "Xiaomi Device")),
                    "fw_ver": info.get("fw_ver", ""),
                    "mac": info.get("mac", ""),
                })
            except socket.timeout:
                break
            except Exception:
                continue
    except Exception as e:
        _LOGGER.warning("miIO discover error: %s", e)
    finally:
        sock.close()

    return devices


# Xiaomi Cloud Client (lightweight)
# ---------------------------------------------------------------------------

class XiaomiCloud:
    def __init__(self):
        self.session = None

    async def login(self, username: str, password: str, server: str = "cn") -> dict:
        import hashlib, urllib.parse
        self.session = aiohttp.ClientSession()
        self.session.headers.update({"User-Agent": "MIUI-AN/14 Android/14"})

        server_map = {
            "cn": "https://account.xiaomi.com",
            "de": "https://account.xiaomiyoupin.com",
            "us": "https://us.account.xiaomi.com",
        }
        base = server_map.get(server, server_map["cn"])
        api_base = base.replace("account.", "api.")

        # Step 1: Get sign
        sign_url = f"{base}/pass/serviceLogin?sid=xiaomiio&_json=true"
        async with self.session.get(sign_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            raw = await r.text()
            data = json.loads(raw.lstrip("&&&START&&&"))

        sign = data.get("_sign", "")
        qs = data.get("qs", "")
        callback = data.get("callback", "")
        service_param = data.get("serviceParam", "{}")

        # Step 2: Login
        login_url = f"{base}/pass/serviceLoginAuth2"
        payload = {
            "user": username,
            "hash": hashlib.md5(password.encode()).hexdigest().upper(),
            "sid": "xiaomiio",
            "_sign": sign,
            "_json": "true",
            "callback": callback,
            "qs": qs,
            "serviceParam": service_param,
        }
        async with self.session.post(login_url, data=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            raw = await r.text()
            result = json.loads(raw.lstrip("&&&START&&&"))

        if result.get("code") != 0:
            return {"error": result.get("desc", "Login failed")}

        # Extract required fields
        ssecurity = result.get("ssecurity", "")
        user_id = result.get("userId", "")
        cuser_id = result.get("cUserId", "")
        location = result.get("location", "")
        psecurity = result.get("passToken", "")

        if not location:
            return {"error": "Login requires CAPTCHA verification. Please try interactive mode."}

        # Step 3: Get service token
        service_token = ""
        try:
            async with self.session.get(location, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=True) as r2:
                pass
            cookies = self.session.cookie_jar.filter_cookies(api_base)
            service_token = str(cookies.get("serviceToken", "")).split("=")[-1].split(";")[0] if cookies else ""
        except Exception:
            pass

        if not service_token and ssecurity and user_id:
            try:
                token_url = f"{api_base}/v2/miid/getServiceToken?sid=xiaomiio"
                params = {
                    "ssecurity": ssecurity,
                    "userId": str(user_id),
                }
                encoded = urllib.parse.urlencode(params)
                async with self.session.get(f"{token_url}&{encoded}", timeout=aiohttp.ClientTimeout(total=10)) as r3:
                    token_data = await r3.json()
                    service_token = token_data.get("data", {}).get("serviceToken", "")
            except Exception:
                pass

        if not service_token:
            return {"error": "Cannot get service token. Server may require CAPTCHA."}

        # Step 4: List devices
        dev_url = f"{api_base}/v2/home/device_list_page?data={{}}&serviceToken={service_token}"
        self.session.headers["x-xiaomi-protocal-flag-cli"] = "PROTOCAL-HTTP2"
        self.session.headers["Cookie"] = f"serviceToken={service_token}; userId={user_id}"

        try:
            async with self.session.get(dev_url, timeout=aiohttp.ClientTimeout(total=10)) as r4:
                dev_data = await r4.json()
        except Exception:
            return {"error": "Failed to fetch device list"}

        devices = []
        dev_list = dev_data.get("data", {}).get("deviceInfo", [])
        if not dev_list and "result" in dev_data:
            dev_list = dev_data["result"].get("list", [])

        for d in dev_list:
            devices.append({
                "name": d.get("name", "Unknown"),
                "model": d.get("model", "Unknown"),
                "ip": d.get("localip", ""),
                "token": d.get("token", ""),
                "did": d.get("did", ""),
            })

        return {"devices": devices}

    async def login_with_callback(self, auth_data):
        ssecurity = auth_data.get("ssecurity", "")
        user_id = auth_data.get("userId", "")
        location = auth_data.get("location", "")
        service_token = auth_data.get("serviceToken", "")
        
        if not service_token and ssecurity and user_id:
            import urllib.parse
            session = aiohttp.ClientSession()
            session.headers.update({"User-Agent": "MIUI-AN/14 Android/14"})
            try:
                token_url = "https://api.io.mi.com/app/v2/miid/getServiceToken?sid=xiaomiio"
                async with session.get(token_url, params={"ssecurity": ssecurity, "userId": str(user_id)}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
                    service_token = data.get("data", {}).get("serviceToken", "")
            except Exception:
                pass
            finally:
                await session.close()
        
        if not service_token:
            return {"error": "Can not get service token. Please login in popup first."}
        
        session2 = aiohttp.ClientSession()
        session2.headers.update({
            "User-Agent": "MIUI-AN/14 Android/14",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "Cookie": f"serviceToken={service_token}; userId={user_id}",
        })
        try:
            dev_url = f"https://api.io.mi.com/v2/home/device_list_page?data={{}}&serviceToken={service_token}"
            async with session2.get(dev_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                dev_data = await r.json()
        except Exception:
            await session2.close()
            return {"error": "Failed to fetch device list"}
        finally:
            if not session2.closed:
                await session2.close()
        
        devices = []
        dev_list = dev_data.get("data", {}).get("deviceInfo", [])
        if not dev_list and "result" in dev_data:
            dev_list = dev_data["result"].get("list", [])
        for d in dev_list:
            devices.append({
                "name": d.get("name", "Unknown"),
                "model": d.get("model", "Unknown"),
                "ip": d.get("localip", ""),
                "token": d.get("token", ""),
                "did": d.get("did", ""),
            })
        return {"devices": devices}

    async def close(self):
        if self.session:
            await self.session.close()


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
            if any(eid.startswith(p) for p in ("light.", "switch.", "fan.", "climate.", "cover.", "input_boolean.", "media_player.", "vacuum.", "humidifier.", "camera.", "lock.", "button.", "select.")):
                state = entity.get("state", "unknown")
                attrs = entity.get("attributes", {})
                domain = eid.split(".")[0]
                is_button = domain == "button"
                name = attrs.get("friendly_name", eid)
                
                # Skip internal Xiaomi Home button entities that aren't user-facing
                if is_button:
                    skip_keywords = [
                        "press_home", "press_menu", "press_settings", "press_back",
                        "press_left", "press_right", "press_up", "press_down", "press_ok",
                        "消息转发", "额外设备", "平台ID", "勿扰", "睡眠模式", "音箱模式",
                        "进入音箱", "退出音箱", "启用时间段"
                    ]
                    if any(kw in eid for kw in skip_keywords):
                        continue
                    if any(kw in name for kw in skip_keywords):
                        continue
                
                devices.append({
                    "entity_id": eid,
                    "name": name,
                    "state": state,
                    "domain": domain,
                    "is_button": is_button, "domain": domain,
                })
        return devices

    async def call_service(self, domain: str, service: str, entity_id: str, extra: dict = None) -> dict:
        """Call a HA service."""
        payload = {"entity_id": entity_id}
        if extra:
            payload.update(extra)
        return await self._ha_request("POST", f"services/{domain}/{service}", payload)

    # ------------------------------------------------------------------
    # LLM chat
    # ------------------------------------------------------------------

    async def llm_chat(self, user_text: str) -> str:
        """Send text to AX650 LLM and get response with device control."""
        try:
            devices = await self.get_devices()
        except Exception:
            devices = []
        
        # Build dynamic prompt with actual device IDs
        device_lines = []
        for d in devices[:20]:
            name = d.get("name", d["entity_id"])
            eid = d["entity_id"]
            domain = d["domain"]
            if domain == "media_player":
                device_lines.append(f"- 电视: {eid} (开关: button.press|button.xxx_turn_on_a_6_1 / button.xxx_turn_off_a_7_1, 音量: media_player.volume_up/down|{eid}, 播放暂停: media_player.media_play_pause|{eid})")
            elif domain == "fan":
                device_lines.append(f"- {name}: {eid} (开关: fan.turn_on/off, 风速: fan.set_percentage|{eid}, 加大/减小: fan.increase_speed/decrease_speed)")
            elif domain == "button":
                if "toggle" in eid or "开关" in name:
                    device_lines.append(f"- {name}: {eid} (开关: button.press|{eid})")
            elif domain in ("light", "switch", "input_boolean"):
                device_lines.append(f"- {name}: {eid} (开关: {domain}.turn_on/off)")
        
        device_list_str = "\n".join(device_lines[:12]) if device_lines else "无设备"
        
        system_prompt = (
            "你是智能家居语音助手。用自然中文回复，每次不超过两句。如果需要控制设备，在回复末尾加指令标签。\n\n"
            "可用设备和正确指令格式：\n"
            f"{device_list_str}\n\n"
            "重要规则：\n"
            "1. ACTION标签格式：[ACTION:domain.service|entity_id]，只能使用上面列出的entity_id！\n"
            "2. 风扇调速用 fan.set_percentage，值是0-100的数字\n"
            "3. 按钮设备用 button.press\n"
            "4. 示例：\n"
            '  用户:"打开风扇" → 你:"好的，已打开风扇[ACTION:fan.turn_on|fan.xiaomi_cn_659020744_p51_s_2_fan]"\n'
            '  用户:"风扇调到80%" → 你:"已调到80%[ACTION:fan.set_percentage|fan.xiaomi_cn_659020744_p51_s_2_fan]"\n'
            '  用户:"电视声音大一点" → 你:"好的[ACTION:media_player.volume_up|media_player.xiaomi_cn_802661350_mih1]"\n'
            "5. 纯聊天不输出ACTION"
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
                    # Strip leading/trailing whitespace from each line
                    raw = "\n".join(line.strip() for line in raw.split("\n"))
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
                _LOGGER.info("Device control: user=%s, devices=%d", user_text, len(devices))
                # Extract device keywords from user text for matching
                device_keywords = ["电视", "音箱", "灯", "空调", "风扇", "扫地", "窗帘", "门锁", "加湿", "净化", "开关", "插座", "音量", "风速", "挡位", "档位"]
                # Check for global media control keywords
                media_action = None
                if any(w in user_lower for w in ["音量", "大声", "小声", "调高", "调低", "静音", "播放", "暂停", "继续"]):
                    if any(w in user_lower for w in ["静音"]):
                        media_action = ("volume_mute", None)
                    elif any(w in user_lower for w in ["播放", "开始"]):
                        media_action = ("media_play", None)
                    elif any(w in user_lower for w in ["暂停"]):
                        media_action = ("media_pause", None)
                    elif any(w in user_lower for w in ["调高", "大声"]):
                        media_action = ("volume_up", None)
                    elif any(w in user_lower for w in ["调低", "小声"]):
                        media_action = ("volume_down", None)
                    elif "音量" in user_lower:
                        import re
                        vm = re.search(r'(\d+)', user_text)
                        if vm:
                            vol = min(100, max(0, int(vm.group(1)))) / 100.0
                            media_action = ("volume_set", {"volume_level": vol})
                        else:
                            media_action = ("volume_up", None)
                matched_device = None
                for d in devices:
                    name = d.get("name", "").lower()
                    eid = d["entity_id"]
                    domain = d["domain"]
                    # Check if any part of device name appears in user text
                    # Split device name into words and check each
                    name_parts = name.replace(".", " ").replace("-", " ").replace("_", " ").split()
                    # Also check predefined keywords
                    matched = False
                    for part in name_parts:
                        if len(part) >= 2 and part in user_lower:
                            matched = True
                            break
                    if not matched:
                        for kw in device_keywords:
                            if kw in user_lower and kw in name:
                                matched = True
                                break
                    if matched:
                        matched_device = d
                        _LOGGER.info("Matched device: %s (domain=%s) for '%s'", d['name'], domain, user_text)
                        # If global media action and this is a media_player
                        if media_action and domain == "media_player":
                            svc, extra = media_action
                            await bridge.call_service("media_player", svc, eid, extra)
                            command_info = f"电视: {svc}"
                            break
                        # Power commands - for media_player, find button entities
                        power_on = any(w in user_lower for w in ["打开", "开", "turn on", "开机", "启动"])
                        power_off = any(w in user_lower for w in ["关闭", "关", "turn off", "关机", "停"])
                        if power_on or power_off:
                            if domain == "media_player":
                                # Find the corresponding button entity for power
                                if power_on:
                                    btn_eid = eid.replace("media_player.", "button.") + "_turn_on_a_6_1"
                                else:
                                    btn_eid = eid.replace("media_player.", "button.") + "_turn_off_a_7_1"
                                # Also try generic button turn_on
                                await bridge.call_service("button", "press", btn_eid)
                                command_info = ("已打开 " if power_on else "已关闭 ") + d["name"]
                            elif domain == "button":
                                # All buttons use press - toggle, switch, power buttons
                                await bridge.call_service("button", "press", eid)
                                command_info = ("已打开 " if power_on else "已关闭 ") + d["name"]
                            elif power_on:
                                await bridge.call_service(domain, "turn_on", eid)
                                command_info = "已打开 " + d["name"]
                            elif power_off:
                                await bridge.call_service(domain, "turn_off", eid)
                                command_info = "已关闭 " + d["name"]
                        elif any(w in user_lower for w in ["toggle", "切换"]):
                            await bridge.call_service(domain, "toggle", eid)
                            command_info = "已切换 " + d["name"]
                        # Fan specific
                        elif domain == "fan":
                            # Check for percentage / max / min
                            import re as _fan_re
                            pct_match = _fan_re.search(r'(\d+)\s*[%％]|百分之\s*(\d+)|调到\s*(\d+)', user_text)
                            if any(w in user_lower for w in ["最大", "最高"]):
                                await bridge.call_service("fan", "set_percentage", eid, {"percentage": 100})
                                command_info = "已设置 " + d["name"] + " 为最大风速"
                            elif any(w in user_lower for w in ["最小", "最低"]):
                                await bridge.call_service("fan", "set_percentage", eid, {"percentage": 1})
                                command_info = "已设置 " + d["name"] + " 为最小风速"
                            elif pct_match:
                                pct = int(pct_match.group(1) or pct_match.group(2) or pct_match.group(3))
                                pct = min(100, max(1, pct))
                                await bridge.call_service("fan", "set_percentage", eid, {"percentage": pct})
                                command_info = f"已设置 {d['name']} 风速为 {pct}%"
                            elif any(w in user_lower for w in ["直吹", "自然风", "直吹风"]):
                                mode = "直吹风" if any(w in user_lower for w in ["直吹"]) else "自然风"
                                await bridge.call_service("fan", "set_preset_mode", eid, {"preset_mode": mode})
                                command_info = f"已设置 {d['name']} 为 {mode}"
                            elif any(w in user_lower for w in ["风速", "挡位", "档位", "加大", "调大", "调高"]):
                                await bridge.call_service("fan", "increase_speed", eid)
                                command_info = "已加大 " + d["name"] + " 风速"
                            elif any(w in user_lower for w in ["减小", "调小", "调低", "减速"]):
                                await bridge.call_service("fan", "decrease_speed", eid)
                                command_info = "已减小 " + d["name"] + " 风速"
                            elif any(w in user_lower for w in ["摇头", "摆风", "转向"]):
                                await bridge.call_service("fan", "oscillate", eid, {"oscillating": True})
                                command_info = "已开启 " + d["name"] + " 摆风"
                        
                        # Select (dropdown) - for fan modes etc
                        elif domain == "select":
                            # Try to match option from user text
                            try:
                                # Get available options from HA
                                state_data = await bridge._ha_request("GET", f"states/{eid}")
                                options = state_data.get("attributes", {}).get("options", [])
                                for opt in options:
                                    if opt in user_text:
                                        await bridge.call_service("select", "select_option", eid, {"option": opt})
                                        command_info = f"已设置 {d['name']} 为 {opt}"
                                        break
                                if not command_info:
                                    await bridge.call_service("select", "select_next", eid)
                                    command_info = "已切换 " + d["name"]
                            except Exception:
                                await bridge.call_service("select", "select_next", eid)
                                command_info = "已切换 " + d["name"]
                        
                        # Media player specific
                        elif domain == "media_player":
                            if any(w in user_lower for w in ["播放", "继续", "开始"]):
                                await bridge.call_service("media_player", "media_play", eid)
                                command_info = "已播放 " + d["name"]
                            elif any(w in user_lower for w in ["暂停", "停"]):
                                await bridge.call_service("media_player", "media_pause", eid)
                                command_info = "已暂停 " + d["name"]
                            elif any(w in user_lower for w in ["音量", "大声", "小声", "调高", "调低", "静音"]):
                                if any(w in user_lower for w in ["静音"]):
                                    await bridge.call_service("media_player", "volume_mute", eid)
                                    command_info = "已静音 " + d["name"]
                                elif any(w in user_lower for w in ["调高", "大声", "音量+", "音量增加"]):
                                    await bridge.call_service("media_player", "volume_up", eid)
                                    command_info = "已调高 " + d["name"] + " 音量"
                                elif any(w in user_lower for w in ["调低", "小声", "音量-", "音量减小"]):
                                    await bridge.call_service("media_player", "volume_down", eid)
                                    command_info = "已调低 " + d["name"] + " 音量"
                                elif "音量" in user_lower:
                                    # Try to extract volume percentage
                                    import re
                                    vol_match = re.search(r'(\d+)', user_text)
                                    if vol_match:
                                        vol = min(100, max(0, int(vol_match.group(1)))) / 100.0
                                        await bridge.call_service("media_player", "volume_set", eid, {"volume_level": vol})
                                        command_info = f"已设置 {d['name']} 音量为 {int(vol*100)}%"
                                    else:
                                        await bridge.call_service("media_player", "volume_up", eid)
                                        command_info = "已调高 " + d["name"] + " 音量"
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
            extra = data.get("extra", None)
            
            if action == "press" and domain == "button":
                result = await bridge.call_service("button", "press", entity_id)
            elif action == "select_next":
                result = await bridge.call_service("select", "select_next", entity_id)
            elif action in ("volume_up", "volume_down", "volume_mute", "media_play_pause", "media_play", "media_pause"):
                result = await bridge.call_service(domain, action, entity_id)
            elif action == "volume_set" and extra:
                result = await bridge.call_service(domain, "volume_set", entity_id, extra)
            else:
                service = "toggle"
                if action == "turn_on": service = "turn_on"
                elif action == "turn_off": service = "turn_off"
                result = await bridge.call_service(domain, service, entity_id)
            await ws.send_json({"type": "text", "text": f"{entity_id} → {action}", "command": str(result)})

        elif msg_type == "xiaomi_verify_ticket":
            flow_id = data.get("flow_id", "")
            ticket = data.get("ticket", "")
            u = data.get("username", "")
            p = data.get("password", "")
            if flow_id and ticket and u and p:
                try:
                    result = await bridge._ha_request("POST", f"config/config_entries/flow/{flow_id}", {
                        "username": u,
                        "password": p,
                        "server_country": "cn",
                        "conn_mode": "auto",
                        "trans_options": False,
                        "filter_models": False,
                        "verify_ticket": ticket
                    })
                    step_type = result.get("type", "")
                    if step_type == "create_entry":
                        await ws.send_json({"type": "text", "text": "小米集成添加成功！正在刷新设备列表..."})
                        devices = await bridge.get_devices()
                        await ws.send_json({"type": "text", "text": "", "devices": devices})
                    elif result.get("errors"):
                        await ws.send_json({"type": "error", "text": f"验证失败: {result['errors']}"})
                    else:
                        await ws.send_json({"type": "text", "text": f"MIoT 集成: {step_type}"})
                except Exception as e:
                    await ws.send_json({"type": "error", "text": f"验证提交失败: {e}"})
            else:
                await ws.send_json({"type": "error", "text": "验证信息不完整"})

        elif msg_type in ("xiaomi_login", "xiaomi_login_retry"):
            u = data.get("username", "").strip()
            p = data.get("password", "").strip()
            s = data.get("server", "cn")
            is_retry = (msg_type == "xiaomi_login_retry")
            if not u or not p:
                await ws.send_json({"type": "error", "text": "请输入小米账号和密码"})
            else:
                try:
                    # Try HA xiaomi_miot config flow directly
                    flow_result = await bridge._ha_request("POST", "config/config_entries/flow", {
                        "handler": "xiaomi_miot",
                        "show_advanced_options": False,
                    })
                    flow_id = flow_result.get("flow_id", "")
                    if flow_id:
                        # Submit account mode
                        await bridge._ha_request("POST", f"config/config_entries/flow/{flow_id}", {
                            "action": "account"
                        })
                        # Submit credentials
                        login_result = await bridge._ha_request("POST", f"config/config_entries/flow/{flow_id}", {
                            "username": u,
                            "password": p,
                            "server_country": s,
                            "conn_mode": "auto",
                            "trans_options": False,
                            "filter_models": False
                        })
                        if login_result.get("type") == "create_entry":
                            await ws.send_json({"type": "text", "text": "小米集成添加成功！正在刷新设备..."})
                            devices = await bridge.get_devices()
                            await ws.send_json({"type": "text", "text": "", "devices": devices})
                        elif login_result.get("errors", {}).get("base") == "need_verify":
                            tip = login_result.get("description_placeholders", {}).get("tip", "")
                            verify_url = ""
                            import re
                            match = re.search(r'\[([^\]]+)\]\(([^)]+)\)', tip)
                            if match:
                                verify_url = match.group(2)
                            # Store for proxy access
                            XIAOMI_VERIFY_FLOW["flow_id"] = flow_id
                            XIAOMI_VERIFY_FLOW["verify_url"] = verify_url
                            await ws.send_json({"type": "xiaomi_need_verify", "flow_id": flow_id, "verify_url": "/xiaomi/verify"})
                        else:
                            await ws.send_json({"type": "error", "text": f"登录失败: {login_result.get('errors', '未知错误')}"})
                    else:
                        # Fallback to XiaomiCloud direct login
                        if is_retry:
                            _LOGGER.info("Retry Xiaomi login after popup for user %s", u)
                        xc = XiaomiCloud()
                        result = await xc.login(u, p, s)
                        await xc.close()
                        if "error" in result:
                            if is_retry and "CAPTCHA" in result.get("error", ""):
                                await ws.send_json({"type": "error", "text": "验证码登录仍失败。请使用手动输入方式：用 Xiaomi-cloud-tokens-extractor 工具提取 Token 后填入下方。"})
                            else:
                                await ws.send_json({"type": "error", "text": result["error"]})
                        else:
                            await ws.send_json({"type": "xiaomi_devices", "devices": result["devices"]})
                except Exception as e:
                    await ws.send_json({"type": "error", "text": f"登录失败: {e}"})

        elif msg_type == "xiaomi_auth_callback":
            auth_data = data.get("data", {})
            _LOGGER.info("Xiaomi auth callback: %s", auth_data)
            try:
                xc = XiaomiCloud()
                result = await xc.login_with_callback(auth_data)
                await xc.close()
                if "error" in result:
                    await ws.send_json({"type": "error", "text": result["error"]})
                else:
                    await ws.send_json({"type": "xiaomi_devices", "devices": result["devices"]})
            except Exception as e:
                await ws.send_json({"type": "error", "text": f"回调处理失败: {e}"})

        elif msg_type == "xiaomi_add":
            token = data.get("token", "")
            ip = data.get("ip", "")
            name = data.get("name", "")
            model = data.get("model", "")
            if not token or not ip:
                await ws.send_json({"type": "error", "text": "缺少 token 或 IP"})
            else:
                try:
                    # Add to HA via xiaomi_miot config entry
                    result = await bridge._ha_request("POST", "config/config_entries/flow", {
                        "handler": "xiaomi_miot",
                        "show_advanced_options": False,
                    })
                    flow_id = result.get("flow_id", "")
                    if flow_id:
                        step_result = await bridge._ha_request("POST", f"config/config_entries/flow/{flow_id}", {
                            "action": "account",
                        })
                        step_result2 = await bridge._ha_request("POST", f"config/config_entries/flow/{flow_id}", {
                            "cloud_server": "cn",
                            "username": name,
                            "password": token,
                            "scan_interval": 30,
                        })
                        await ws.send_json({"type": "xiaomi_added", "text": f"已添加 {name}"})
                    else:
                        await ws.send_json({"type": "error", "text": "无法创建配置流"})
                except Exception as e:
                    await ws.send_json({"type": "error", "text": f"添加失败: {e}"})

        elif msg_type == "xiaomi_scan":
            await ws.send_json({"type": "text", "text": "正在扫描局域网小米设备..."})
            try:
                # Run miio_discover in a thread since it's blocking
                loop = asyncio.get_event_loop()
                devices = await loop.run_in_executor(None, miio_discover, 3.0)
                await ws.send_json({"type": "xiaomi_scan_result", "devices": devices})
            except Exception as e:
                await ws.send_json({"type": "error", "text": f"扫描失败: {e}"})

        elif msg_type == "xiaomi_add_manual":
            ip = data.get("ip", "")
            token = data.get("token", "")
            name = data.get("name", "Xiaomi Device")
            if not ip or not token:
                await ws.send_json({"type": "error", "text": "缺少 IP 或 Token"})
            else:
                try:
                    # Add to HA via xiaomi_miot config entry with manual IP+token
                    result = await bridge._ha_request("POST", "config/config_entries/flow", {
                        "handler": "xiaomi_miot",
                        "show_advanced_options": False,
                    })
                    flow_id = result.get("flow_id", "")
                    if flow_id:
                        # First step: choose account mode
                        await bridge._ha_request("POST", f"config/config_entries/flow/{flow_id}", {
                            "cloud_server": "cn",
                            "username": name,
                            "password": token,
                            "scan_interval": 30,
                        })
                        await ws.send_json({"type": "xiaomi_added", "text": f"已手动添加 {name} ({ip})"})
                    else:
                        # Try direct miot integration
                        await ws.send_json({"type": "error", "text": "无法创建配置流，请在 HA 页面手动添加 Xiaomi Miot Auto 集成"})
                except Exception as e:
                    await ws.send_json({"type": "error", "text": f"手动添加失败: {e}"})

        else:
            await ws.send_json({"type": "error", "text": f"Unknown message type: {msg_type}"})

    return ws


XIAOMI_CALLBACK_HTML = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Xiaomi Login Helper</title></head>
<body style="background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;text-align:center">
<h3 style="color:#00d4aa">小米登录助手</h3>
<div id="status" style="margin:20px;padding:15px;background:rgba(255,255,255,0.05);border-radius:10px">
  <p>请在弹出的窗口中完成小米登录（含验证码）</p>
  <p style="font-size:0.8em;color:#888">登录完成后，复制以下脚本到浏览器控制台运行：</p>
  <textarea readonly onclick="this.select()" style="width:100%;height:80px;background:#000;color:#0f0;font-family:monospace;font-size:0.75em;padding:8px;border-radius:8px">
var c=document.cookie;var s=c.match(/serviceToken=([^;]+)/);
var u=c.match(/userId=([^;]+)/);
if(s&&u){var d={type:'xiaomi_auth',serviceToken:s[1],userId:u[1]};
window.opener.postMessage(d,'*');alert('Sent! Close this window.');}
else{alert('No token found. Please login first.');}
</textarea>
  <p style="font-size:0.7em;color:#666;margin-top:10px">F12 打开控制台 → 粘贴运行 → 自动传回设备列表</p>
</div>
<button onclick="window.close()" style="padding:10px 30px;border-radius:20px;background:#444;color:#fff;border:none;cursor:pointer;margin-top:10px">关闭</button>
<script>
// Check URL params for auth data from redirect
var params = new URLSearchParams(window.location.search);
if (params.get('serviceToken')) {
  window.opener.postMessage({
    type: 'xiaomi_auth',
    serviceToken: params.get('serviceToken'),
    userId: params.get('userId'),
    ssecurity: params.get('ssecurity'),
    location: params.get('location')
  }, '*');
  document.getElementById('status').innerHTML = '<p style="color:#0f0">认证数据已传回！正在获取设备列表...</p>';
  setTimeout(function(){ window.close(); }, 2000);
}
</script>
</body></html>'''

async def handle_xiaomi_callback(request):
    return web.Response(text=XIAOMI_CALLBACK_HTML, content_type="text/html", charset="utf-8")


XIAOMI_VERIFY_FLOW = {"flow_id": "", "verify_url": ""}

async def handle_xiaomi_proxy(request: web.Request) -> web.StreamResponse:
    """Reverse proxy for Xiaomi verification page."""
    path = request.match_info.get("path", "")
    verify_url_base = XIAOMI_VERIFY_FLOW.get("verify_url", "")
    
    if not verify_url_base:
        return web.Response(text="No active verification. Please go back and retry.", status=400)
    
    # Build target URL
    from urllib.parse import urljoin, urlparse
    target_base = "https://account.xiaomi.com"
    qs = request.query_string
    target_url = f"{target_base}/{path}"
    if qs:
        target_url += f"?{qs}"
    
    _LOGGER.info("Proxy: %s -> %s", request.url, target_url)
    
    # Forward request
    method = request.method
    headers = {k: v for k, v in request.headers.items() 
               if k.lower() not in ('host', 'origin', 'referer')}
    headers["Host"] = "account.xiaomi.com"
    
    body = await request.read() if method in ("POST", "PUT") else None
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, target_url, headers=headers, data=body,
                                       allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                proxy_resp = web.StreamResponse(status=resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ('transfer-encoding', 'content-encoding'):
                        proxy_resp.headers[k] = v
                # Set cookies for our domain
                if 'Set-Cookie' in proxy_resp.headers:
                    cookies = proxy_resp.headers['Set-Cookie']
                    cookies = cookies.replace('Domain=.xiaomi.com', 'Domain=192.168.31.201')
                    cookies = cookies.replace('Domain=xiaomi.com', 'Domain=192.168.31.201')
                    proxy_resp.headers['Set-Cookie'] = cookies
                
                await proxy_resp.prepare(request)
                data = await resp.read()
                await proxy_resp.write(data)
                await proxy_resp.write_eof()
                return proxy_resp
    except Exception as e:
        return web.Response(text=f"Proxy error: {e}", status=502)

async def handle_xiaomi_verify_page(request: web.Request) -> web.Response:
    """Serve the verification page with iframe-based proxy."""
    verify_url = XIAOMI_VERIFY_FLOW.get("verify_url", "")
    flow_id = XIAOMI_VERIFY_FLOW.get("flow_id", "")
    
    if not verify_url:
        return web.Response(text="No active verification session", status=400)
    
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Xiaomi Verification</title>
<style>
body{{margin:0;padding:0;background:#1a1a2e;font-family:sans-serif;color:#eee;height:100vh;display:flex;flex-direction:column}}
#header{{padding:12px 20px;background:rgba(0,0,0,0.3);display:flex;align-items:center;gap:10px;font-size:0.9em}}
#header span{{color:#00d4aa}}
iframe{{flex:1;border:none;width:100%}}
</style></head>
<body>
<div id="header">
  <span>🔐</span> 请在下方完成小米验证码
  <span id="status" style="margin-left:auto;color:#ffa500">等待验证...</span>
</div>
<iframe id="verify-frame" src="{verify_url}" sandbox="allow-scripts allow-same-origin allow-forms allow-popups"></iframe>
<script>
var flowId = '{flow_id}';
// Monitor iframe for navigation to check completion
var checkCount = 0;
setInterval(function() {{
  checkCount++;
  try {{
    var frame = document.getElementById('verify-frame');
    var href = frame.contentWindow.location.href;
    if (href.includes('ticket=') || href.includes('code=')) {{
      document.getElementById('status').textContent = '验证成功！正在添加...';
      document.getElementById('status').style.color = '#0f0';
      var params = new URLSearchParams(href.split('?')[1] || '');
      var ticket = params.get('ticket') || params.get('code') || '';
      if (ticket && window.opener) {{
        window.opener.postMessage({{type:'xiaomi_verify_done', flow_id:flowId, ticket:ticket}}, '*');
        setTimeout(function(){{ window.close(); }}, 2000);
      }}
    }}
  }} catch(e) {{}}
  if (checkCount > 300) {{
    document.getElementById('status').textContent = '验证超时，请关闭重试';
    document.getElementById('status').style.color = '#f44';
  }}
}}, 1000);
</script>
</body></html>"""
    return web.Response(text=html, content_type="text/html", charset="utf-8")


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
    app.router.add_get("/xiaomi/callback", handle_xiaomi_callback)
    

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
