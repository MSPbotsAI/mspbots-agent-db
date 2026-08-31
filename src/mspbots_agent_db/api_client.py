import asyncio
from typing import Any

import httpx

from ._json import error_envelope

# The Agent Data Core (pg-data-ingest) app is mounted at this path prefix on
# the tenant host: "https://<host>/apps/pg-data-ingest/api/agent-data".
# X-MSP-Host only carries the bare host; do not hardcode the prefix elsewhere.
# Callers pass the full sub-path below this prefix, e.g. "/agents/<id>/stats".
_APP_PREFIX = "/apps/pg-data-ingest/api/agent-data"

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
# Transport-level auto-retry only for genuinely transient failures. 409
# (AGENT_DELETING) is deliberately excluded — an in-progress DROP PARTITION
# won't resolve within our short backoff window, so we surface it as a
# retryable *error* instead and let the caller decide when to retry.
_RETRYABLE_STATUS = {429, 502, 503, 504}
_MAX_RETRIES = 3
_MAX_BACKOFF_SECONDS = 20.0

# One shared connection pool for the process lifetime. No credentials are
# ever stored on it — the API key/agent id/host are passed per-request via
# headers, so this is safe to share across tenants/requests (see server.py's
# contextvar-based credential isolation, which is what actually keeps
# tenants — and agents within a tenant — apart).
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True)
    return _http_client


# status_code -> (error code, retryable). status_code 0 means a network/
# connection-level failure (no response at all). Retryable flags follow the
# API doc's own guidance, not a blanket ">=500 is retryable" assumption: 500
# INTERNAL is a real fault ("真故障", not retryable); 502/503/504 and a bare
# network failure are infra-transient; 409 AGENT_DELETING and 429
# RATE_LIMITED are explicitly documented as retryable.
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    409: ("conflict", True),
    413: ("payload_too_large", False),
    429: ("rate_limited", True),
    500: ("upstream_error", False),
}


def _classify(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    return "invalid_argument", False


class AgentDataError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Agent Data Core API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = _classify(self.status_code)
        return error_envelope(code, self.message, retryable)


def agent_path(agent_id: str, suffix: str = "") -> str:
    """Build a path under the given agent, e.g. agent_path("42", "/stats").

    agent_id is the data-isolation key by design (one LIST partition per
    agent) — it is not a credential. Authorization is the API key alone;
    any valid key can address any agent_id (a known, documented gap of the
    underlying app — see README Known Gaps), so tool callers are trusted
    to pass their own agent_id, same as every other mspbotsagent*-family
    tool in this platform takes agent_id as a plain argument.
    """
    return f"/agents/{agent_id}{suffix}"


class AgentDataClient:
    """Async httpx client wrapping the MSPbots Agent Data Core (pg-data-ingest)
    read API.

    Reuses the module-level connection pool (see _get_http_client) across
    every call made through this instance, rather than opening a new
    connection per request.
    """

    def __init__(self, api_key: str, host: str):
        self._api_key = api_key
        self._base_url = host.rstrip("/") + _APP_PREFIX

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _clean_params(self, params: dict | None) -> dict:
        if not params:
            return {}
        return {k: v for k, v in params.items() if v is not None}

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json_body: Any = None) -> Any:
        return await self._request("POST", path, json_body=json_body)

    async def _request(
        self, method: str, path: str, params: dict | None = None, json_body: Any = None
    ) -> Any:
        client = _get_http_client()
        url = f"{self._base_url}{path}"
        headers = self._headers()
        params = self._clean_params(params)

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await client.request(
                    method, url, headers=headers, params=params, json=json_body
                )
            except httpx.RequestError as e:
                last_exc = e
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(min(2**attempt, _MAX_BACKOFF_SECONDS))
                    continue
                raise AgentDataError(0, f"{e or type(e).__name__} (url={url})") from e

            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                delay = self._retry_delay(resp, attempt)
                await asyncio.sleep(delay)
                continue

            return self._handle(resp)

        # Unreachable in practice (loop always returns or raises above), but
        # keeps type checkers happy and guards against future edits.
        if last_exc:
            raise AgentDataError(0, f"{last_exc}") from last_exc
        raise AgentDataError(0, "request failed with no response")

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), _MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
        return min(2**attempt, _MAX_BACKOFF_SECONDS)

    def _handle(self, resp: httpx.Response) -> Any:
        if not resp.content:
            return None
        try:
            body = resp.json()
        except ValueError:
            body = {"raw_response": resp.text}
        if resp.status_code >= 400:
            # The API's own error body is {code, message, request_id} — code
            # is informative (e.g. "AGENT_NOT_FOUND") but we classify by
            # status_code for the envelope (see _classify) and fold the API's
            # own code into the message so nothing is lost.
            if isinstance(body, dict):
                api_code = body.get("code")
                message = body.get("message") or "unknown error"
                if api_code:
                    message = f"[{api_code}] {message}"
            else:
                message = str(body)
            raise AgentDataError(resp.status_code, message)
        return body
