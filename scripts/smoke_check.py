#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def check_tcp(name: str, host: str, port: int, timeout: float) -> CheckResult:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return CheckResult(name=name, ok=True, detail=f"tcp://{host}:{port} reachable")
    except OSError as err:
        return CheckResult(name=name, ok=False, detail=f"tcp://{host}:{port} failed: {err}")


def http_get_json(url: str, timeout: float) -> tuple[int, dict | list | str]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = response.getcode()
        body = response.read().decode("utf-8", errors="replace")

    if not body:
        return status, ""

    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, body


def check_http_any(name: str, urls: Iterable[str], timeout: float) -> CheckResult:
    errors = []
    for url in urls:
        try:
            status, payload = http_get_json(url, timeout)
        except (OSError, urllib.error.URLError) as err:
            errors.append(f"{url}: {err}")
            continue

        if 200 <= status < 300:
            summary = payload
            if isinstance(payload, dict):
                summary = payload.get("status") or payload.get("object") or "ok"
            return CheckResult(name=name, ok=True, detail=f"{url} returned {status} ({summary})")

        errors.append(f"{url}: HTTP {status}")

    return CheckResult(name=name, ok=False, detail="; ".join(errors))


def run_checks(args: argparse.Namespace) -> list[CheckResult]:
    asr_http = f"http://{args.host}:{args.asr_http_port}/healthz"
    tts_http = f"http://{args.host}:{args.tts_http_port}/healthz"
    llm_base = f"http://{args.host}:{args.llm_port}"

    return [
        check_http_any("asr-http", [asr_http], args.timeout),
        check_http_any("tts-http", [tts_http], args.timeout),
        check_http_any("llm-http", [f"{llm_base}/health", f"{llm_base}/v1/models"], args.timeout),
        check_tcp("asr-wyoming", args.host, args.asr_wyoming_port, args.timeout),
        check_tcp("tts-wyoming", args.host, args.tts_wyoming_port, args.timeout),
        check_tcp("wakeword-wyoming", args.host, args.wakeword_port, args.timeout),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke-check the AX650 Home Assistant voice stack.")
    parser.add_argument("--host", default="127.0.0.1", help="Host or board IP to check.")
    parser.add_argument("--asr-http-port", type=int, default=8080)
    parser.add_argument("--tts-http-port", type=int, default=8081)
    parser.add_argument("--llm-port", type=int, default=8001)
    parser.add_argument("--asr-wyoming-port", type=int, default=10300)
    parser.add_argument("--tts-wyoming-port", type=int, default=10200)
    parser.add_argument("--wakeword-port", type=int, default=10400)
    parser.add_argument("--timeout", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results = run_checks(args)

    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"{status} {result.name}: {result.detail}")

    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
