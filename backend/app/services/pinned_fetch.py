"""Shared SSRF-pinned HTTP GET for the in-process extraction engines.

The direct engine (returns HTML) and the reader-proxy engine (returns markdown) both
fetch caller-supplied URLs in-process, so each pins the connection to a validated public
IP for the initial request and every redirect hop, maps the response status to the typed
extraction errors, and size-caps the body before returning it. This is that shared step;
callers supply their own request headers + retry loop and parse the returned text.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings
from app.services import ssrf
from app.services.extraction_types import (
    BLOCKED_STATUS_CODES,
    ExtractionBlockedError,
    ExtractionPermanentError,
    ExtractionTransientError,
)


async def get_text_retrying(
    url: str,
    settings: Settings,
    *,
    headers: dict[str, str],
    max_bytes: int,
    timeout_seconds: float,
) -> str:
    """``get_text`` wrapped in the shared transient-retry policy (the ``FIRECRAWL_*`` knobs).

    Transient failures (5xx, DNS blip, timeout) are retried with exponential backoff;
    blocked/permanent errors propagate on the first try. The in-process engines (direct,
    reader) use this so the retry policy lives in one place.
    """

    retrying = AsyncRetrying(
        stop=stop_after_attempt(settings.FIRECRAWL_RETRY_COUNT),
        wait=wait_exponential(
            multiplier=settings.FIRECRAWL_BACKOFF_BASE_SECONDS,
            min=settings.FIRECRAWL_BACKOFF_BASE_SECONDS,
        ),
        retry=retry_if_exception_type(ExtractionTransientError),
        reraise=True,
    )
    text = ""
    async for attempt in retrying:
        with attempt:
            text = await get_text(
                url,
                settings,
                headers=headers,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
            )
    return text


async def get_text(
    url: str,
    settings: Settings,
    *,
    headers: dict[str, str],
    max_bytes: int,
    timeout_seconds: float,
) -> str:
    """One SSRF-pinned GET of ``url`` -> decoded body text, capped at ``max_bytes``.

    Resolves and pins the host to a public IP, re-pins every redirect hop, maps 5xx ->
    transient, ``BLOCKED_STATUS_CODES`` -> blocked, other 4xx -> permanent, and caps the
    body. ``Host`` is set to the URL host so the IP-pinned request still carries the right
    name. Raises the typed extraction errors; the caller's retry loop handles transients.
    """

    host = urlsplit(url).hostname or ""
    try:
        pinned_ip = await ssrf.resolve_public_host(host)
    except ssrf.BlockedHostError as exc:
        # A confirmed non-public address is a real SSRF hit (permanent); a resolution
        # failure (DNS blip, no records) is transient, so it gets retried.
        if exc.blocked:
            raise ExtractionPermanentError(
                "The URL resolves to a non-public address and was blocked."
            ) from exc
        raise ExtractionTransientError(f"Could not resolve the host: {exc.reason}") from exc

    pinned_url = ssrf.pin_url_to_ip(url, pinned_ip)
    req_headers = {**headers, "Host": host}
    timeout = httpx.Timeout(timeout_seconds, connect=10.0)
    try:
        async with (
            httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                event_hooks={"request": [ssrf.build_redirect_pin_hook(pinned_ip)]},
            ) as client,
            client.stream(
                "GET", pinned_url, headers=req_headers, extensions={"sni_hostname": host}
            ) as response,
        ):
            if response.is_server_error:
                raise ExtractionTransientError(f"Upstream host returned {response.status_code}")
            if response.status_code in BLOCKED_STATUS_CODES:
                # A block (auth wall / forbidden / rate-limited), not a missing page: the same
                # client won't get further, but a bypass might. Signal the orchestrator to run
                # the fallback cascade rather than failing the job here.
                raise ExtractionBlockedError(f"Upstream host returned {response.status_code}")
            if response.is_client_error:
                raise ExtractionPermanentError(f"Upstream host returned {response.status_code}")
            advertised = response.headers.get("Content-Length")
            if advertised is not None:
                try:
                    if int(advertised) > max_bytes:
                        raise ExtractionPermanentError("The page exceeds the size cap.")
                except ValueError:
                    pass  # malformed header; the streaming cap below still bounds memory
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > max_bytes:
                    break  # stop reading once the cap is reached
            return bytes(buffer).decode(response.encoding or "utf-8", errors="replace")
    except ssrf.BlockedHostError as exc:
        # The redirect-pin hook re-resolves each hop and raises here when a redirect points
        # at a non-public address -- httpx never connects to it. Convert to the same typed
        # errors as the initial resolve so a blocked redirect fails cleanly.
        if exc.blocked:
            raise ExtractionPermanentError(
                "A redirect pointed to a non-public address and was blocked."
            ) from exc
        raise ExtractionTransientError(f"Could not resolve a redirect target: {exc.reason}") from exc
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ExtractionTransientError(f"Could not reach the host: {exc}") from exc
