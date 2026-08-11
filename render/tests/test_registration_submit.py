"""Tests for the only code that types the operator's address into a page.

``camoufox`` is not installed in the test environment (the sidecar imports it
lazily so the pure helpers stay testable), so a stub module stands in for it.
"""

from __future__ import annotations

import sys
import types

_camoufox = types.ModuleType("camoufox")
_async_api = types.ModuleType("camoufox.async_api")
_async_api.AsyncCamoufox = object
sys.modules.setdefault("camoufox", _camoufox)
sys.modules.setdefault("camoufox.async_api", _async_api)

from camoufox_renderer import CamoufoxRenderer, _submit_registration  # noqa: E402
from renderer import RenderResult  # noqa: E402


class _Input:
    def __init__(self, fail: bool = False) -> None:
        self.filled: str | None = None
        self.pressed: str | None = None
        self.fail = fail

    async def fill(self, value: str, timeout: int | None = None) -> None:
        if self.fail:
            raise TimeoutError("no such field")
        self.filled = value

    async def press(self, key: str) -> None:
        self.pressed = key


class _Inputs:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    async def evaluate_all(self, _js: str) -> list[str]:
        return self.names


class _Form:
    def __init__(self, names: list[str], text: str, email_input: _Input) -> None:
        self.names = names
        self.text = text
        self.email_input = email_input

    def locator(self, selector: str):
        if selector == "input":
            return _Inputs(self.names)
        return types.SimpleNamespace(first=self.email_input)

    async def inner_text(self) -> str:
        return self.text


class _Forms:
    def __init__(self, forms: list[_Form]) -> None:
        self.forms = forms

    async def all(self) -> list[_Form]:
        return self.forms


class _Page:
    def __init__(self, forms: list[_Form], body: str = "gated body") -> None:
        self.forms = forms
        self.body = body
        self.url = "https://publisher.test/story"
        self.settled = False

    def locator(self, _selector: str) -> _Forms:
        return _Forms(self.forms)

    async def inner_text(self, _selector: str) -> str:
        return self.body

    async def wait_for_load_state(self, _state: str, timeout: int | None = None) -> None:
        return None

    async def wait_for_timeout(self, _ms: int) -> None:
        self.settled = True


def _registration_form(email_input: _Input | None = None) -> _Form:
    return _Form(
        ["npe", "newspack_reader_registration"],
        "Continue Reading This Story for FREE!",
        email_input or _Input(),
    )


async def test_submits_the_address_to_the_registration_form() -> None:
    email_input = _Input()
    page = _Page([_Form(["s"], "Search", _Input()), _registration_form(email_input)])

    assert await _submit_registration(page, "reader@example.test") is True
    assert email_input.filled == "reader@example.test"
    assert email_input.pressed == "Enter"
    assert page.settled


async def test_no_registration_form_leaves_the_page_alone() -> None:
    search = _Input()
    page = _Page([_Form(["s"], "Search", search)])

    assert await _submit_registration(page, "reader@example.test") is False
    assert search.filled is None


async def test_a_failed_fill_reports_nothing_submitted() -> None:
    page = _Page([_registration_form(_Input(fail=True))])

    assert await _submit_registration(page, "reader@example.test") is False


async def test_the_address_is_submitted_once_across_retries() -> None:
    """A retry re-rolls the fingerprint, not the operator's relationship with the
    publisher: the address goes out on one attempt, never on all three."""

    seen: list[str | None] = []

    class _Renderer(CamoufoxRenderer):
        async def _render_once(self, url, expand, attempt, email=None):
            seen.append(email)
            return RenderResult(status="captcha"), email is not None

    result = await _Renderer()._render_with_retries(
        "https://publisher.test/story", True, "reader@example.test"
    )
    assert result.status == "captcha"
    assert seen == ["reader@example.test", None, None]

