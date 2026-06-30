from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install_on_board.sh"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_install_script_has_valid_shell_syntax() -> None:
    result = subprocess.run(
        ["/bin/sh", "-n", str(SCRIPT)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_install_script_dry_run_prints_expected_steps(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "commands.log"

    write_executable(
        bin_dir / "docker",
        f"#!/bin/sh\nprintf 'docker %s\\n' \"$*\" >> {log_file}\nexit 0\n",
    )
    write_executable(
        bin_dir / "git",
        f"#!/bin/sh\nprintf 'git %s\\n' \"$*\" >> {log_file}\nexit 0\n",
    )
    write_executable(
        bin_dir / "curl",
        f"#!/bin/sh\nprintf 'curl %s\\n' \"$*\" >> {log_file}\nexit 0\n",
    )
    write_executable(
        bin_dir / "python3",
        f"#!/bin/sh\nprintf 'python3 %s\\n' \"$*\" >> {log_file}\nexit 0\n",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "COMPOSE": "docker compose",
            "MIN_FREE_MB": "1",
        }
    )

    result = subprocess.run(
        [
            "/bin/sh",
            str(SCRIPT),
            "--dry-run",
            "--skip-vendor",
            "--skip-models",
            "--skip-build",
            "--smoke-timeout",
            "1",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "Skipping upstream source cache fetch" in result.stdout
    assert "Skipping model downloads" in result.stdout
    assert "Skipping compose build/start" in result.stdout
    assert "+ 'mkdir'" in result.stdout
    assert "+ docker compose ps" in result.stdout
    assert "scripts/smoke_check.py" in result.stdout
    assert "--public-only" in result.stdout


def test_install_script_validate_only_skips_downloads_and_build(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "commands.log"

    write_executable(
        bin_dir / "docker",
        "#!/bin/sh\nif [ \"$1 $2\" = \"compose version\" ]; then exit 0; fi\nexit 0\n",
    )
    write_executable(bin_dir / "git", "#!/bin/sh\nexit 0\n")
    write_executable(bin_dir / "curl", "#!/bin/sh\nexit 0\n")
    write_executable(
        bin_dir / "python3",
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {log_file}\nexit 0\n",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "MIN_FREE_MB": "1",
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(SCRIPT), "--dry-run", "--validate-only"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "Downloading ASR model" not in result.stdout
    assert "Starting voice stack" not in result.stdout
    assert "+ docker compose ps" in result.stdout
    assert "scripts/smoke_check.py" in result.stdout
