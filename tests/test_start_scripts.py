from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def run_script(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env.update(env)
    return subprocess.run(
        ["/bin/sh", str(REPO_ROOT / script)],
        cwd=REPO_ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        timeout=10,
    )


def make_fake_path(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(
        bin_dir / "python",
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$PYTHON_ARGS_FILE\"\nexit 0\n",
    )
    write_executable(bin_dir / "curl", "#!/bin/sh\nexit 0\n")
    return bin_dir


def test_asr_start_fails_without_server_binary(tmp_path: Path) -> None:
    result = run_script(
        "asr/start.sh",
        {
            "AX_ASR_SERVER_BIN": str(tmp_path / "missing-asr-server"),
            "AX_ASR_MODEL_PATH": str(tmp_path),
            "AX_ASR_WAIT_TIMEOUT": "1",
            "AX_ASR_BUILD_IF_MISSING": "0",
        },
    )

    assert result.returncode == 1
    assert "ASR server binary not executable" in result.stderr


def test_tts_start_fails_without_model_path(tmp_path: Path) -> None:
    server_bin = tmp_path / "tts_server"
    write_executable(server_bin, "#!/bin/sh\nsleep 30\n")

    result = run_script(
        "tts/start.sh",
        {
            "AX_TTS_SERVER_BIN": str(server_bin),
            "AX_TTS_MODEL_PATH": str(tmp_path / "missing-models"),
            "AX_TTS_WAIT_TIMEOUT": "1",
            "AX_TTS_BUILD_IF_MISSING": "0",
        },
    )

    assert result.returncode == 1
    assert "TTS model path does not exist" in result.stderr


def test_asr_adapter_only_execs_adapter_with_configured_args(tmp_path: Path) -> None:
    bin_dir = make_fake_path(tmp_path)
    args_file = tmp_path / "python-args.txt"

    result = run_script(
        "asr/start.sh",
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PYTHON_ARGS_FILE": str(args_file),
            "AX_ASR_ADAPTER_ONLY": "1",
            "AX_ASR_ADAPTER_URI": "tcp://127.0.0.1:10301",
            "AX_ASR_HTTP_URL": "http://asr.internal:18080",
            "AX_ASR_MODEL": "whisper_turbo",
            "AX_ASR_LANGUAGE": "zh",
        },
    )

    assert result.returncode == 0
    args = args_file.read_text()
    assert "/app/wyoming_adapter.py" in args
    assert "tcp://127.0.0.1:10301" in args
    assert "http://asr.internal:18080" in args
    assert "whisper_turbo" in args
    assert "zh" in args


def test_tts_start_launches_server_waits_then_execs_adapter(tmp_path: Path) -> None:
    bin_dir = make_fake_path(tmp_path)
    args_file = tmp_path / "python-args.txt"
    server_args = tmp_path / "server-args.txt"
    model_path = tmp_path / "models"
    model_path.mkdir()
    server_bin = tmp_path / "tts_server"
    write_executable(
        server_bin,
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {server_args}\n",
    )

    result = run_script(
        "tts/start.sh",
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PYTHON_ARGS_FILE": str(args_file),
            "AX_TTS_SERVER_BIN": str(server_bin),
            "AX_TTS_MODEL_PATH": str(model_path),
            "AX_TTS_SERVER_PORT": "18081",
            "AX_TTS_HTTP_URL": "http://127.0.0.1:18081",
            "AX_TTS_ADAPTER_URI": "tcp://127.0.0.1:10201",
            "AX_TTS_WAIT_TIMEOUT": "1",
            "AX_TTS_ESPEAK_DATA_PATH": "/opt/espeak",
            "AX_TTS_JIEBA_DICT_PATH": "/opt/jieba",
        },
    )

    assert result.returncode == 0
    server_text = server_args.read_text()
    assert "--port\n18081\n--model_path\n" in server_text
    assert "--espeak_data_path\n/opt/espeak\n--jieba_dict_path\n/opt/jieba" in server_text
    args = args_file.read_text()
    assert "/app/wyoming_adapter.py" in args
    assert "tcp://127.0.0.1:10201" in args
    assert "http://127.0.0.1:18081" in args


def test_asr_start_builds_server_when_missing(tmp_path: Path) -> None:
    bin_dir = make_fake_path(tmp_path)
    args_file = tmp_path / "python-args.txt"
    server_bin = tmp_path / "asr_server"
    build_log = tmp_path / "build.log"
    model_path = tmp_path / "models"
    model_path.mkdir()
    build_script = tmp_path / "build_asr.sh"
    write_executable(
        build_script,
        f"#!/bin/sh\nprintf build > {build_log}\ncat > {server_bin} <<'SH'\n#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path / 'server-args.txt'}\nSH\nchmod +x {server_bin}\n",
    )

    result = run_script(
        "asr/start.sh",
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PYTHON_ARGS_FILE": str(args_file),
            "AX_ASR_SERVER_BIN": str(server_bin),
            "AX_ASR_BUILD_SCRIPT": str(build_script),
            "AX_ASR_MODEL_PATH": str(model_path),
            "AX_ASR_WAIT_TIMEOUT": "1",
        },
    )

    assert result.returncode == 0
    assert build_log.read_text() == "build"
    assert server_bin.exists()
