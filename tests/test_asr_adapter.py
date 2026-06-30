from __future__ import annotations

import wave
import io
from pathlib import Path

import httpx
import pytest

from asr.wyoming_adapter import AxAsrEventHandler, AsrConfig, pcm_to_wav_bytes, transcribe_wav
from wyoming.asr import Transcript, Transcribe
from wyoming.audio import AudioChunk, AudioStart, AudioStop


class FakeAsrClient:
    def __init__(self) -> None:
        self.calls = []

    async def post(self, url: str, **kwargs) -> httpx.Response:
        file_tuple = kwargs["files"]["file"]
        wav_data = file_tuple[1].read()
        self.calls.append((url, kwargs["data"], wav_data))
        return httpx.Response(
            200,
            json={"text": "你好，客厅灯已打开"},
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", url),
        )


class CapturingAsrHandler(AxAsrEventHandler):
    def __init__(self, config: AsrConfig, client: FakeAsrClient) -> None:
        super().__init__(None, None, config=config, client=client)
        self.events = []

    async def write_event(self, event):
        self.events.append(event)


def test_pcm_to_wav_bytes_preserves_audio_format(tmp_path: Path) -> None:
    wav_bytes = pcm_to_wav_bytes(b"\x01\x00\x02\x00", rate=16000, width=2, channels=1)

    wav_path = tmp_path / "audio.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x01\x00\x02\x00")

    with wave.open(str(wav_path), "rb") as expected, wave.open(io.BytesIO(wav_bytes), "rb") as actual:
        assert actual.getframerate() == expected.getframerate()
        assert actual.getsampwidth() == expected.getsampwidth()
        assert actual.getnchannels() == expected.getnchannels()
        assert actual.readframes(actual.getnframes()) == b"\x01\x00\x02\x00"


@pytest.mark.asyncio
async def test_transcribe_wav_posts_openai_style_request(tmp_path: Path) -> None:
    client = FakeAsrClient()
    config = AsrConfig(api_url="http://asr:8080", model="sensevoice", language="auto")
    wav_bytes = pcm_to_wav_bytes(b"\x01\x00\x02\x00", rate=16000, width=2, channels=1)

    text = await transcribe_wav(client, config, wav_bytes)

    assert text == "你好，客厅灯已打开"
    assert len(client.calls) == 1
    url, data, uploaded = client.calls[0]
    assert url == "http://asr:8080/v1/audio/transcriptions"
    assert data == {
        "model": "sensevoice",
        "language": "auto",
        "response_format": "json",
    }
    assert uploaded.startswith(b"RIFF")


@pytest.mark.asyncio
async def test_asr_handler_returns_transcript_after_audio_stop() -> None:
    client = FakeAsrClient()
    handler = CapturingAsrHandler(
        AsrConfig(api_url="http://asr:8080", model="sensevoice", language="auto"),
        client,
    )

    await handler.handle_event(Transcribe().event())
    await handler.handle_event(AudioStart(rate=16000, width=2, channels=1).event())
    await handler.handle_event(AudioChunk(audio=b"\x01\x00\x02\x00", rate=16000, width=2, channels=1).event())
    await handler.handle_event(AudioStop().event())

    assert Transcript.from_event(handler.events[-1]).text == "你好，客厅灯已打开"
