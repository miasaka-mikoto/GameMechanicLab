from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_linux_builder_is_checked_in_and_posix_executable() -> None:
    script = ROOT / "build_linux.sh"
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert os.access(script, os.X_OK)
    assert "--onefile" in text
    assert "--console" in text
    assert "--headless --runs 1" in text


def test_linux_ci_covers_native_and_portable_smoke_paths() -> None:
    workflow = (ROOT / ".github/workflows/linux-package.yml").read_text(encoding="utf-8")
    assert "runs-on: ubuntu-latest" in workflow
    assert "python3-tk" in workflow
    assert "./build_linux.sh --clean" in workflow
    assert "./build_portable.sh" in workflow
    assert "simulation_result.json" in workflow
    assert "demo_results.json" in workflow


def test_portable_linux_archive_is_documented() -> None:
    script = (ROOT / "build_portable.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "GameMechanicLab-portable-linux.zip" in script
    assert "GameMechanicLab-portable-linux.zip" in readme
