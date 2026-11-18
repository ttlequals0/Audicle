"""Library noise (warnings, print, tqdm) must reach the log as JSON, not raw lines."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import warnings
from pathlib import Path

import pytest

from log_setup import capture_stdout, setup_logging

_WRAPPER_DIR = str(Path(__file__).resolve().parent.parent)


@pytest.fixture
def json_logging(capsys):
    """Restores root logging; each test calls ``_configure`` in the call phase so the
    handler binds the stream capsys holds then (it is re-created between phases)."""

    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield capsys
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    logging.captureWarnings(False)


def _configure() -> None:
    # pytest swaps warnings.showwarning per test; reset so captureWarnings re-hooks it.
    logging.captureWarnings(False)
    setup_logging("DEBUG", "json")


def _records(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_warning_becomes_one_json_record(json_logging) -> None:
    _configure()
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn_explicit(
            "LoRACompatibleLinear is deprecated.\nUse PEFT instead.",
            FutureWarning,
            "diffusers/models/lora.py",
            42,
        )
    out = json_logging.readouterr().out
    assert len(out.splitlines()) == 1
    record = json.loads(out)
    assert record["logger"] == "py.warnings"
    assert record["level"] == "WARNING"
    assert "LoRACompatibleLinear is deprecated." in record["message"]


def test_captured_print_becomes_debug_record_not_stdout(json_logging) -> None:
    _configure()
    with capture_stdout("tts.engine.lib"):
        print("S3 Token -> Mel Inference...")
        sys.stdout.write("loaded PerthNet (Implicit) at step 250,000\n\n")
        sys.stdout.write("partial line without newline")
    records = _records(json_logging.readouterr().out)
    assert [r["message"] for r in records] == [
        "S3 Token -> Mel Inference...",
        "loaded PerthNet (Implicit) at step 250,000",
        "partial line without newline",
    ]
    assert {r["level"] for r in records} == {"DEBUG"}
    assert {r["logger"] for r in records} == {"tts.engine.lib"}


def test_capture_restores_stdout_and_survives_overlap(json_logging) -> None:
    _configure()
    original = sys.stdout
    entered = threading.Event()
    release = threading.Event()

    def other() -> None:
        with capture_stdout("tts.engine.lib"):
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=other)
    thread.start()
    entered.wait(5)
    with capture_stdout("tts.engine.lib"):
        pass
    assert sys.stdout is not original  # still captured while the other holds it
    release.set()
    thread.join()
    assert sys.stdout is original


def test_progress_bars_are_disabled_before_tqdm_can_import() -> None:
    code = (
        "import sys, os; sys.path.insert(0, sys.argv[1]);"
        "assert 'tqdm' not in sys.modules;"
        "import log_setup;"
        "print(os.environ['TQDM_DISABLE'])"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, _WRAPPER_DIR],
        capture_output=True,
        text=True,
        env={},
        check=False,
    )
    assert result.stdout.strip() == "1", result.stderr
