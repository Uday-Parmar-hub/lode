"""HTTP transport and rate limiting for the Kaleidoscope fetcher.

Houses every transport-level v4.6 lesson, isolated from the orchestration
in :mod:`prm.fetchers.kscope`: the per-endpoint :class:`_RateLimiter`
(L4) and the :class:`_RequestExecutor` (L1 key-on-URL plus an event-hook
safety net, L2 no ``Content-Type`` on GETs, L3 429 ``reset_in`` honoring,
L12 transient retry/backoff via tenacity, and structured error raising).

It imports value types and exceptions from
:mod:`prm.fetchers.kscope_models` and nothing else from the package, so it
can never import the client back (no circular dependency).
"""

from __future__ import annotations

import random
import re
import time
from collections import deque
from collections.abc import Callable
from threading import Lock
from urllib.parse import quote, urlsplit

import httpx
import structlog
from tenacity import RetryError, Retrying, retry_if_exception_type, stop_after_attempt

from .kscope_models import (
    FetchAttempt,
    KscopeAuthError,
    KscopeFetchError,
    KscopeNotFoundError,
    KscopeRateLimitError,
    _RetryableRateLimitError,
    _RetryableServerError,
)

log = structlog.get_logger(__name__)

_ROLLING_WINDOW_S = 3600.0  # the rate-limit budget window

_USER_AGENT = "prm-kscope-fetcher/1.0"
# v4.6 L2: Accept covers JSON listings + HTML/PDF bodies. NO Content-Type on GETs.
_ACCEPT = "application/json, text/html, application/pdf, */*"

_KEY_QS_RE = re.compile(r"([?&]key=)[^&]*")

# Transient network failures worth retrying. Note that ``LocalProtocolError``
# (a malformed request we built — a programming bug) is deliberately excluded,
# so it propagates instead of being retried and swallowed.
_RETRYABLE_NETWORK_ERRORS: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)
_RETRYABLE_TYPES: tuple[type[Exception], ...] = (
    *_RETRYABLE_NETWORK_ERRORS,
    _RetryableServerError,
    _RetryableRateLimitError,
)


def _redact_key(url: str) -> str:
    """Replace any ``key=...`` query value with ``REDACTED`` for logs/errors."""
    return _KEY_QS_RE.sub(r"\1REDACTED", url)


# =====================================================================
# RATE LIMITER (v4.6 lesson L4) — inline token/window, not a library
# =====================================================================


class _RateLimiter:
    """Per-endpoint pacing + rolling hourly budget.

    One logical bucket per Kscope endpoint family. Each bucket enforces a
    minimum inter-call interval and a rolling one-hour call budget, and is
    guarded by its own lock — so a single shared limiter paces correctly
    across threads, and a slow call on one endpoint never blocks another.
    """

    def __init__(
        self,
        *,
        hourly_budget: int,
        min_interval_s: float,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> None:
        self._hourly_budget = hourly_budget
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._sleeper = sleeper
        self._registry_lock = Lock()
        self._locks: dict[str, Lock] = {}
        self._calls: dict[str, deque[float]] = {}
        self._last_call: dict[str, float] = {}

    def _bucket_lock(self, bucket: str) -> Lock:
        with self._registry_lock:
            lock = self._locks.get(bucket)
            if lock is None:
                lock = Lock()
                self._locks[bucket] = lock
                self._calls[bucket] = deque()
            return lock

    def acquire(self, bucket: str) -> None:
        """Block until a call on ``bucket`` is allowed; raise if the budget is spent.

        Raises:
            KscopeRateLimitError: If the rolling hourly budget for ``bucket``
                is already exhausted.
        """
        lock = self._bucket_lock(bucket)
        with lock:
            now = self._clock()
            calls = self._calls[bucket]
            window_start = now - _ROLLING_WINDOW_S
            while calls and calls[0] < window_start:
                calls.popleft()
            if len(calls) >= self._hourly_budget:
                raise KscopeRateLimitError(
                    f"local hourly budget of {self._hourly_budget} reached "
                    f"for endpoint {bucket!r}",
                    http_status=None,
                )
            last = self._last_call.get(bucket)
            if last is not None:
                gap = now - last
                if gap < self._min_interval_s:
                    self._sleeper(self._min_interval_s - gap)
                    now = self._clock()
            self._last_call[bucket] = now
            calls.append(now)


# =====================================================================
# REQUEST EXECUTOR — L1, L2, L3, retry/backoff (L12), structured errors
# =====================================================================


class _RequestExecutor:
    """Owns the httpx client and every transport-level v4.6 lesson.

    Builds URLs with the API key appended last (L1), sends no
    ``Content-Type`` on GETs (L2), consults the rate limiter before every
    attempt (L4), retries transient 5xx/network errors with exponential
    backoff (L12) while honouring 429 ``reset_in`` (L3), records a
    :class:`FetchAttempt` per try, and raises structured
    :class:`KscopeFetchError` subclasses on persistent failure.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        limiter: _RateLimiter,
        max_retries: int,
        backoff_base_s: float,
        backoff_max_s: float,
        max_rate_wait_s: int,
        timeout_s: float,
        sleeper: Callable[[float], None],
        transport: httpx.BaseTransport | None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._limiter = limiter
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._max_rate_wait_s = max_rate_wait_s
        self._sleeper = sleeper
        self._client = httpx.Client(
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT},  # L2: no Content-Type
            transport=transport,
            event_hooks={"request": [self._assert_key_on_wire]},
        )

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    # ----- L1: URL construction + on-the-wire key assertion -----

    def _with_key(self, url: str) -> str:
        """Append our active key as the last query param, replacing any existing key."""
        if re.search(r"[?&]key=", url):
            return _KEY_QS_RE.sub(lambda m: m.group(1) + self._api_key, url, count=1)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}key={self._api_key}"

    def _build_url(self, path_or_url: str, params: dict[str, object] | None) -> str:
        """Build a full request URL, key appended last (never via httpx ``params=``)."""
        url = path_or_url if path_or_url.startswith("http") else f"{self._base_url}{path_or_url}"
        extra: list[str] = []
        for raw_key, raw_value in (params or {}).items():
            if raw_value is None:
                continue
            extra.append(f"{quote(str(raw_key), safe='')}={quote(str(raw_value), safe=',:;_-')}")
        if extra:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{'&'.join(extra)}"
        # Match on the real HOST, never a substring of the URL — so the key is only ever
        # appended to an actual api.kscope.io request, not a hostile URL that merely contains
        # the literal "api.kscope.io" in a path/param.
        if (urlsplit(url).hostname or "").lower() == "api.kscope.io":
            url = self._with_key(url)
        return url

    def _assert_key_on_wire(self, request: httpx.Request) -> None:
        """Event hook: guarantee ``key`` is on every Kscope request (L1 safety net).

        Catches any code path that bypassed :meth:`_build_url` (e.g. a body
        URL handed back by Kscope without a key). Re-adds the active key and
        logs a warning rather than letting a keyless request hit the wire.
        """
        if (request.url.host or "").lower() != "api.kscope.io":
            return
        if request.url.params.get("key") != self._api_key:
            log.warning("kscope_key_missing_on_wire_readded", path=request.url.path)
            request.url = request.url.copy_set_param("key", self._api_key)

    # ----- retry plumbing -----

    def _wait_strategy(self, retry_state: object) -> float:
        """tenacity wait: honour 429 ``reset_in``, else exponential backoff + jitter."""
        outcome = getattr(retry_state, "outcome", None)
        exc = outcome.exception() if outcome is not None else None
        if isinstance(exc, _RetryableRateLimitError):
            return float(min(exc.reset_in, self._max_rate_wait_s))
        attempt = getattr(retry_state, "attempt_number", 1)
        delay = min(self._backoff_base_s * (2 ** (attempt - 1)), self._backoff_max_s)
        return delay + random.uniform(0, self._backoff_base_s)

    @staticmethod
    def _extract_reset_in(resp: httpx.Response) -> int | None:
        """Read the retry delay from a 429 body (``error.reset_in``) or ``Retry-After``."""
        try:
            body = resp.json()
        except ValueError:  # includes json.JSONDecodeError
            body = None
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                value = err.get("reset_in")
                if isinstance(value, (int, float)):
                    return int(value)
                if isinstance(value, str) and value.isdigit():
                    return int(value)
        retry_after = resp.headers.get("retry-after", "")
        if retry_after.isdigit():
            return int(retry_after)
        return None

    def _execute(
        self,
        target: str,
        params: dict[str, object] | None,
        bucket: str,
        *,
        label: str,
    ) -> tuple[httpx.Response, list[FetchAttempt]]:
        """Run one logical request (with retries) and return ``(response, attempts)``."""
        url = self._build_url(target, params)
        attempts: list[FetchAttempt] = []

        def _attempt() -> httpx.Response:
            self._limiter.acquire(bucket)
            start = time.perf_counter()
            try:
                resp = self._client.get(url)
            except _RETRYABLE_NETWORK_ERRORS as exc:
                attempts.append(
                    FetchAttempt(
                        strategy=label,
                        url=_redact_key(url),
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                raise
            elapsed = (time.perf_counter() - start) * 1000
            status = resp.status_code
            n_bytes = len(resp.content or b"")
            attempts.append(
                FetchAttempt(
                    strategy=label,
                    url=_redact_key(url),
                    http_status=status,
                    bytes=n_bytes,
                    elapsed_ms=elapsed,
                )
            )
            if status == 200:
                return resp
            if status == 429:
                reset_in = self._extract_reset_in(resp)
                if reset_in is not None and reset_in <= self._max_rate_wait_s:
                    raise _RetryableRateLimitError(reset_in)
                raise KscopeRateLimitError(
                    f"rate limited on {label} (reset_in={reset_in})",
                    url=_redact_key(url),
                    http_status=429,
                    attempts=list(attempts),
                )
            if status in (401, 403):
                raise KscopeAuthError(
                    f"authentication/permission failure ({status}) on {label}",
                    url=_redact_key(url),
                    http_status=status,
                    attempts=list(attempts),
                )
            if status == 404:
                raise KscopeNotFoundError(
                    f"not found (404) on {label}",
                    url=_redact_key(url),
                    http_status=404,
                    attempts=list(attempts),
                )
            if status >= 500:
                raise _RetryableServerError(status)
            raise KscopeFetchError(
                f"unexpected HTTP {status} on {label}",
                url=_redact_key(url),
                http_status=status,
                attempts=list(attempts),
            )

        retrying = Retrying(
            stop=stop_after_attempt(self._max_retries + 1),
            retry=retry_if_exception_type(_RETRYABLE_TYPES),
            wait=self._wait_strategy,
            sleep=self._sleeper,
            reraise=False,
        )
        try:
            resp = retrying(_attempt)
        except RetryError as retry_err:
            last_exc = retry_err.last_attempt.exception()
            if isinstance(last_exc, _RetryableRateLimitError):
                raise KscopeRateLimitError(
                    f"rate-limit retries exhausted on {label}",
                    url=_redact_key(url),
                    http_status=429,
                    attempts=list(attempts),
                ) from last_exc
            status = getattr(last_exc, "status", None)
            raise KscopeFetchError(
                f"transient failure retries exhausted on {label}",
                url=_redact_key(url),
                http_status=status,
                attempts=list(attempts),
            ) from last_exc
        return resp, attempts

    def request_json(self, endpoint: str, params: dict[str, object] | None, *, bucket: str) -> dict:
        """GET a listing endpoint and return its parsed JSON object."""
        resp, attempts = self._execute(endpoint, params, bucket, label="listing")
        try:
            payload = resp.json()
        except ValueError as exc:  # includes json.JSONDecodeError
            raise KscopeFetchError(
                f"expected JSON from {endpoint}",
                url=_redact_key(str(resp.request.url)),
                http_status=resp.status_code,
                attempts=attempts,
            ) from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    def request_bytes(
        self, url: str, *, bucket: str, label: str
    ) -> tuple[httpx.Response, list[FetchAttempt]]:
        """GET a document body URL and return the raw ``(response, attempts)``."""
        return self._execute(url, None, bucket, label=label)


__all__ = ["_RateLimiter", "_RequestExecutor", "_redact_key"]
