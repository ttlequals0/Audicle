"""StyleTTS2 engine tests that don't require torch / styletts2 / a GPU.

The heavy deps are imported lazily inside ``load()``, so the module imports and
the pure phoneme-injection logic + Protocol attributes are testable here. Clone
fidelity and real inference are validated separately on the GPU host (the spike).
"""

from __future__ import annotations

from config import Config
from style_engine import StyleTTS2Engine, inject_phonemes


def _fake_phonemize(text: str) -> str:
    return "<" + text.replace(" ", "_") + ">"


def test_inject_phonemes_splices_curated_ipa() -> None:
    out = inject_phonemes("the Qatar team", {"Qatar": "ˈkɑtɑɹ"}, _fake_phonemize)
    assert "ˈkɑtɑɹ" in out  # curated IPA verbatim
    assert "<the>" in out and "<team>" in out  # surrounding spans phonemized
    assert "<Qatar>" not in out  # the override term was NOT phonemized


def test_inject_phonemes_no_overrides_phonemizes_whole() -> None:
    assert inject_phonemes("hello world", None, _fake_phonemize) == "<hello_world>"
    assert inject_phonemes("hello world", {}, _fake_phonemize) == "<hello_world>"


def test_inject_phonemes_longest_term_wins() -> None:
    out = inject_phonemes(
        "New York City", {"New York": "ipa1", "New York City": "ipa2"}, _fake_phonemize
    )
    assert "ipa2" in out and "ipa1" not in out


def _config() -> Config:
    import os

    os.environ["TTS_ENGINE"] = "styletts2"
    return Config.from_env()


def test_engine_attributes_and_lazy_construction() -> None:
    # Constructing must not import torch/styletts2 (only load() does).
    engine = StyleTTS2Engine(_config())
    assert engine.name == "styletts2"
    assert engine.supports_phonemes is True
    assert engine.model_loaded is False
    assert engine.reference_loaded is False


def test_config_selects_engine() -> None:
    cfg = _config()
    assert cfg.engine == "styletts2"
    assert cfg.style_phonemizer_lang == "en-us"
