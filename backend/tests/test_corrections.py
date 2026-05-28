from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services import corrections

# --- apply() ---------------------------------------------------------------


def test_apply_whole_word_match_only() -> None:
    out = corrections.apply("kubectl-helper runs kubectl", {"kubectl": "kube control"})
    # kubectl-helper has 'kubectl' at the start but hyphen breaks the \b match
    # on the right; only the bare 'kubectl' word is replaced.
    assert out == "kubectl-helper runs kube control"


def test_apply_is_case_sensitive() -> None:
    out = corrections.apply(
        "PostgreSQL is fast, postgresql is the same.",
        {"PostgreSQL": "post gres Q L"},
    )
    assert "post gres Q L" in out
    assert "postgresql" in out  # lowercase form untouched


def test_apply_longest_first_via_alternation() -> None:
    out = corrections.apply("kubectl is great", {"kubectl": "kube control", "kube": "WRONG"})
    # The longer key wins because regex alternation picks the leftmost
    # alternative when starting positions tie.
    assert out == "kube control is great"


def test_apply_auto_escapes_regex_specials() -> None:
    out = corrections.apply(
        "I love C++ and node.js patterns.",
        {"C++": "see plus plus", "node.js": "node J S"},
    )
    assert "see plus plus" in out
    assert "node J S" in out


def test_apply_empty_dictionary_returns_original() -> None:
    assert corrections.apply("hello world", {}) == "hello world"


def test_apply_no_match_returns_original() -> None:
    assert corrections.apply("hello world", {"xyz": "abc"}) == "hello world"


# --- validate() ------------------------------------------------------------


def test_validate_accepts_normal_dictionary() -> None:
    result = corrections.validate({"kubectl": "kube control"}, max_entries=500)
    assert result.ok is True
    assert result.failures == []


def test_validate_rejects_non_dict_root() -> None:
    result = corrections.validate([{"k": "v"}], max_entries=500)
    assert result.ok is False
    assert result.failures[0].key == "<root>"


def test_validate_rejects_too_many_entries() -> None:
    too_many = {f"k{i}": "v" for i in range(11)}
    result = corrections.validate(too_many, max_entries=10)
    assert result.ok is False
    assert "too many entries" in result.failures[0].reason


@pytest.mark.parametrize(
    "key,value,expected_reason",
    [
        ("", "v", "non-empty"),
        ("x" * 101, "v", "key length"),
        (" leading", "v", "whitespace"),
        ("trailing ", "v", "whitespace"),
        ("k", "", "non-empty"),
        ("k", "x" * 201, "value length"),
        ("k", "with\x07bell", "control characters"),
    ],
)
def test_validate_individual_failures(key: str, value: str, expected_reason: str) -> None:
    result = corrections.validate({key: value}, max_entries=500)
    assert result.ok is False
    assert any(expected_reason in f.reason for f in result.failures)


def test_validate_collects_all_failures() -> None:
    result = corrections.validate({"": "v", "ok": ""}, max_entries=500)
    assert result.ok is False
    assert len(result.failures) >= 2


# --- load / save round-trip ------------------------------------------------


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "pronunciation.json"
    corrections.save(target, {"kubectl": "kube control"})
    loaded = corrections.load(target)
    assert loaded == {"kubectl": "kube control"}


def test_save_is_atomic_no_partial_file_left_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pronunciation.json"
    corrections.save(target, {"original": "kept"})

    # Inject a failure during write by patching os.replace.
    import os

    def _boom(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        corrections.save(target, {"new": "lost"})

    # Original file should be unchanged and no .tmp files lingering.
    assert corrections.load(target) == {"original": "kept"}
    assert not list(tmp_path.glob(".pronunciation-*.tmp"))


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert corrections.load(tmp_path / "nope.json") == {}


def test_load_empty_file_returns_empty(tmp_path: Path) -> None:
    target = tmp_path / "pronunciation.json"
    target.write_text("   \n")
    assert corrections.load(target) == {}


def test_load_rejects_non_object_root(tmp_path: Path) -> None:
    target = tmp_path / "pronunciation.json"
    target.write_text(json.dumps(["not", "a", "dict"]))
    with pytest.raises(ValueError, match="JSON object"):
        corrections.load(target)
