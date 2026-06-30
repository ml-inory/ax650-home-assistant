#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import io
import logging
import tempfile
import wave
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol

import httpx
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger("ax650-asr-wyoming")


class AsyncHttpClient(Protocol):
    async def post(self, url: str, **kwargs) -> httpx.Response:
        ...


@dataclass(frozen=True)
class AsrConfig:
    api_url: str = "http://127.0.0.1:8080"
    model: str = "sensevoice"
    language: str = "auto"
    response_format: str = "json"
    timeout: float = 120.0

    @property
    def transcription_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/v1/audio/transcriptions"


def pcm_to_wav_bytes(audio: bytes, rate: int, width: int, channels: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(rate)
        wav_file.writeframes(audio)
    return output.getvalue()


async def transcribe_wav(
    client: AsyncHttpClient,
    config: AsrConfig,
    wav_bytes: bytes,
) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav") as wav_file:
        wav_file.write(wav_bytes)
        wav_file.flush()
        with Path(wav_file.name).open("rb") as file_handle:
            response = await client.post(
                config.transcription_url,
                data={
                    "model": config.model,
                    "language": config.language,
                    "response_format": config.response_format,
                },
                files={"file": ("audio.wav", file_handle, "audio/wav")},
                timeout=config.timeout,
            )

    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = response.json()
        return str(payload.get("text", "")).strip()

    return response.text.strip()


def build_info(config: AsrConfig) -> Info:
    return Info(
        asr=[
                AsrProgram(
                    name="ax650-asr",
                    description="AX650 ax_asr_api Wyoming adapter",
                    version=None,
                    attribution=Attribution(
                        name="AXERA-TECH/ax_asr_api",
                        url="https://github.com/AXERA-TECH/ax_asr_api",
                ),
                installed=True,
                models=[
                    AsrModel(
                        name=config.model,
                        description=f"AX650 ASR model {config.model}",
                        attribution=Attribution(
                            name="AXERA-TECH/ax_asr_api",
                            url="https://github.com/AXERA-TECH/ax_asr_api",
                        ),
                        installed=True,
                        version=None,
                        languages=["auto", "zh", "en", "yue", "ja", "ko"],
                    )
                ],
            )
        ]
    )


class AxAsrEventHandler(AsyncEventHandler):
    def __init__(
        self,
        reader,
        writer,
        config: AsrConfig,
        client: AsyncHttpClient,
    ) -> None:
        super().__init__(reader, writer)
        self.config = config
        self.client = client
        self.audio = bytearray()
        self.rate = 16000
        self.width = 2
        self.channels = 1
        self.info_event = build_info(config).event()

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.info_event)
            return True

        if Transcribe.is_type(event.type):
            self.audio.clear()
            return True

        if AudioStart.is_type(event.type):
            audio_start = AudioStart.from_event(event)
            self.rate = audio_start.rate
            self.width = audio_start.width
            self.channels = audio_start.channels
            self.audio.clear()
            return True

        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            self.rate = chunk.rate
            self.width = chunk.width
            self.channels = chunk.channels
            self.audio.extend(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            text = ""
            if self.audio:
                wav_bytes = pcm_to_wav_bytes(bytes(self.audio), self.rate, self.width, self.channels)
                try:
                    text = await transcribe_wav(self.client, self.config, wav_bytes)
                except Exception:
                    _LOGGER.exception("ASR transcription failed")

            await self.write_event(Transcript(text=text).event())
            self.audio.clear()
            return True

        return True


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="tcp://0.0.0.0:10300")
    parser.add_argument("--api-url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", default="sensevoice")
    parser.add_argument("--language", default="auto")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    config = AsrConfig(api_url=args.api_url, model=args.model, language=args.language, timeout=args.timeout)
    server = AsyncServer.from_uri(args.uri)

    _LOGGER.info("Starting AX650 ASR Wyoming adapter on %s", args.uri)
    async with httpx.AsyncClient() as client:
        await server.run(partial(AxAsrEventHandler, config=config, client=client))


if __name__ == "__main__":
    asyncio.run(main())
