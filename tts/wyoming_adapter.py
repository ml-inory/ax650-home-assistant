#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import io
import logging
import math
import wave
from dataclasses import dataclass
from functools import partial
from typing import Protocol

import httpx
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
)

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger("ax650-tts-wyoming")


class AsyncHttpClient(Protocol):
    async def post(self, url: str, **kwargs) -> httpx.Response:
        ...


@dataclass(frozen=True)
class TtsConfig:
    api_url: str = "http://127.0.0.1:8081"
    model: str = "kokoro"
    language: str = "zh"
    voice: str = "jm_kumo"
    response_format: str = "wav"
    speed: float = 1.0
    timeout: float = 120.0
    samples_per_chunk: int = 1024

    @property
    def speech_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/v1/audio/speech"


async def request_speech(
    client: AsyncHttpClient,
    config: TtsConfig,
    text: str,
    voice: str | None = None,
) -> bytes:
    payload = {
        "model": config.model,
        "input": text,
        "instructions": config.language,
        "voice": voice or config.voice,
        "response_format": config.response_format,
        "speed": config.speed,
    }
    response = await client.post(config.speech_url, json=payload, timeout=config.timeout)
    response.raise_for_status()
    return response.content


def iter_wav_audio_chunks(wav_bytes: bytes, samples_per_chunk: int) -> tuple[AudioStart, list[AudioChunk], AudioStop]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        rate = wav_file.getframerate()
        width = wav_file.getsampwidth()
        channels = wav_file.getnchannels()
        audio_bytes = wav_file.readframes(wav_file.getnframes())

    audio_start = AudioStart(rate=rate, width=width, channels=channels)
    bytes_per_chunk = max(1, width * channels * samples_per_chunk)
    chunk_count = int(math.ceil(len(audio_bytes) / bytes_per_chunk))
    chunks = []
    for index in range(chunk_count):
        offset = index * bytes_per_chunk
        chunk = audio_bytes[offset : offset + bytes_per_chunk]
        chunks.append(AudioChunk(audio=chunk, rate=rate, width=width, channels=channels))

    return audio_start, chunks, AudioStop()


def build_info(config: TtsConfig) -> Info:
    return Info(
        tts=[
                TtsProgram(
                    name="ax650-tts",
                    description="AX650 ax_tts_api Wyoming adapter",
                    version=None,
                    attribution=Attribution(
                        name="AXERA-TECH/ax_tts_api",
                        url="https://github.com/AXERA-TECH/ax_tts_api",
                ),
                installed=True,
                voices=[
                    TtsVoice(
                        name=config.voice,
                        description=f"AX650 {config.model} voice {config.voice}",
                        version=None,
                        attribution=Attribution(
                            name="AXERA-TECH/ax_tts_api",
                            url="https://github.com/AXERA-TECH/ax_tts_api",
                        ),
                        installed=True,
                        languages=[config.language],
                    )
                ],
                supports_synthesize_streaming=True,
            )
        ]
    )


class AxTtsEventHandler(AsyncEventHandler):
    def __init__(
        self,
        reader,
        writer,
        config: TtsConfig,
        client: AsyncHttpClient,
    ) -> None:
        super().__init__(reader, writer)
        self.config = config
        self.client = client
        self.info_event = build_info(config).event()
        self.is_streaming = False
        self.stream_text = ""
        self.stream_voice = None

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.info_event)
            return True

        try:
            if Synthesize.is_type(event.type):
                if self.is_streaming:
                    return True
                await self._handle_synthesize(Synthesize.from_event(event))
                return True

            if SynthesizeStart.is_type(event.type):
                start = SynthesizeStart.from_event(event)
                self.is_streaming = True
                self.stream_text = ""
                self.stream_voice = start.voice
                return True

            if SynthesizeChunk.is_type(event.type):
                chunk = SynthesizeChunk.from_event(event)
                self.stream_text += chunk.text
                return True

            if SynthesizeStop.is_type(event.type):
                await self._handle_synthesize(Synthesize(text=self.stream_text, voice=self.stream_voice))
                await self.write_event(SynthesizeStopped().event())
                self.is_streaming = False
                self.stream_text = ""
                self.stream_voice = None
                return True
        except Exception as err:
            _LOGGER.exception("TTS synthesis failed")
            await self.write_event(Error(text=str(err), code=err.__class__.__name__).event())
            return True

        return True

    async def _handle_synthesize(self, synthesize: Synthesize) -> None:
        text = " ".join(synthesize.text.strip().splitlines())
        voice = synthesize.voice.name if synthesize.voice is not None else None

        if not text:
            await self.write_event(AudioStart(rate=22050, width=2, channels=1).event())
            await self.write_event(AudioStop().event())
            return

        wav_bytes = await request_speech(self.client, self.config, text, voice)
        audio_start, chunks, audio_stop = iter_wav_audio_chunks(wav_bytes, self.config.samples_per_chunk)
        await self.write_event(audio_start.event())
        for chunk in chunks:
            await self.write_event(chunk.event())
        await self.write_event(audio_stop.event())


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="tcp://0.0.0.0:10200")
    parser.add_argument("--api-url", default="http://127.0.0.1:8081")
    parser.add_argument("--model", default="kokoro")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--voice", default="jm_kumo")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--samples-per-chunk", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    config = TtsConfig(
        api_url=args.api_url,
        model=args.model,
        language=args.language,
        voice=args.voice,
        speed=args.speed,
        samples_per_chunk=args.samples_per_chunk,
        timeout=args.timeout,
    )
    server = AsyncServer.from_uri(args.uri)

    _LOGGER.info("Starting AX650 TTS Wyoming adapter on %s", args.uri)
    async with httpx.AsyncClient() as client:
        await server.run(partial(AxTtsEventHandler, config=config, client=client))


if __name__ == "__main__":
    asyncio.run(main())
