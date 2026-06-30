from __future__ import annotations

import io
import wave

import httpx
import pytest

from tts.wyoming_adapter import AxTtsEventHandler, TtsConfig, iter_wav_audio_chunks, request_speech
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.tts import Synthesize


class FakeTtsClient:
    def __init__(self, wav_bytes: bytes) -> None:
        self.wav_bytes = wav_bytes
        self.calls = []

    async def post(self, url: str, **kwargs) -> httpx.Response:
        self.calls.append((url, kwargs["json"]))
        return httpx.Response(
            200,
            content=self.wav_bytes,
            headers={"content-type": "audio/wav"},
            request=httpx.Request("POST", url),
        )


class CapturingTtsHandler(AxTtsEventHandler):
    def __init__(self, config: TtsConfig, client: FakeTtsClient) -> None:
        super().__init__(None, None, config=config, client=client)
        self.events = []

    async def write_event(self, event):
        self.events.append(event)


def make_wav_bytes(frames: bytes = b"\x01\x00\x02\x00\x03\x00\x04\x00") -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(frames)
    return output.getvalue()


@pytest.mark.asyncio
async def test_request_speech_posts_openai_style_json() -> None:
    wav_bytes = make_wav_bytes()
    client = FakeTtsClient(wav_bytes)
    config = TtsConfig(
        api_url="http://tts:8081",
        model="kokoro",
        language="zh",
        voice="jm_kumo",
        speed=1.0,
    )

    result = await request_speech(client, config, "打开客厅灯")

    assert result == wav_bytes
    assert client.calls == [
        (
            "http://tts:8081/v1/audio/speech",
            {
                "model": "kokoro",
                "input": "打开客厅灯",
                "instructions": "zh",
                "voice": "jm_kumo",
                "response_format": "wav",
                "speed": 1.0,
            },
        )
    ]


def test_iter_wav_audio_chunks_streams_audio_events() -> None:
    wav_bytes = make_wav_bytes()

    audio_start, chunks, audio_stop = iter_wav_audio_chunks(wav_bytes, samples_per_chunk=2)

    assert audio_start.rate == 22050
    assert audio_start.width == 2
    assert audio_start.channels == 1
    assert [chunk.audio for chunk in chunks] == [b"\x01\x00\x02\x00", b"\x03\x00\x04\x00"]
    assert audio_stop is not None


@pytest.mark.asyncio
async def test_tts_handler_streams_wyoming_audio_events() -> None:
    wav_bytes = make_wav_bytes()
    client = FakeTtsClient(wav_bytes)
    handler = CapturingTtsHandler(
        TtsConfig(api_url="http://tts:8081", samples_per_chunk=2),
        client,
    )

    await handler.handle_event(Synthesize(text="打开客厅灯").event())

    assert AudioStart.is_type(handler.events[0].type)
    assert [event.type for event in handler.events] == [
        AudioStart(rate=1, width=2, channels=1).event().type,
        AudioChunk(audio=b"", rate=1, width=2, channels=1).event().type,
        AudioChunk(audio=b"", rate=1, width=2, channels=1).event().type,
        AudioStop().event().type,
    ]
    assert client.calls[0][1]["input"] == "打开客厅灯"
