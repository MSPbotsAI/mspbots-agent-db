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

    agent_id addresses one agent's records within the caller's tenant — it
    is not a credential. Since the 2026-09 partition-model change, the LIST
    partition is per-*tenant* (not per-agent as before); cross-tenant access
    is now blocked by the app-level tenant_id check (see AgentDataClient),
    but within one tenant, authorization still only answers "is this token
    valid", never "which agent may it touch" — any valid token for a tenant
    can address any agent_id within that tenant (a known, documented gap of
    the underlying app — see README Known Gaps), so tool callers are trusted
    to pass their own agent_id, same as every other mspbotsagent*-family
    tool in this platform takes agent_id as a plain argument.
    """
    return f"/agents/{agent_id}{suffix}"


class AgentDataClient:
    """Async httpx client wrapping the MSPbots Agent Data Core (pg-data-ingest)
    API.

    Reuses the module-level connection pool (see _get_http_client) across
    every call made through this instance, rather than opening a new
    connection per request.

    Auth is the platform's own EdDSA JWT, forwarded verbatim as
    `Authorization: Bearer <token>` — not an X-API-Key. This was a
    deliberate choice, not the only option: the currently-live INT build
    still also accepts X-API-Key (a separate credential type configured
    per tenant), but pg-data-ingest's own upcoming release (@0.0.4, INT
    branch, not yet deployed as of this writing — its own /health response
    still includes `api_keys_configured`, the documented tell for old vs.
    new) deletes the X-API-Key code path entirely and keeps only the JWT.
    Standardizing on the JWT now means this client keeps working across
    that upgrade with no further change, and it also matches the
    convention every other mb-platform-* client in this fleet
    (mspbots-agent-mcp, mspbots-fleet-mcp) already uses.

    X_Tenant_ID is required on every request even though pg-data-ingest's
    own auth docs never mention it: it's deployed one pod per tenant, and
    the shared APISIX gateway in front of every `/apps/*` app on this host
    needs X_Tenant_ID to pick which tenant's pod to route to — without it
    the gateway returns a generic {"error":"App not found"} 404 before the
    request ever reaches pg-data-ingest's own auth/routing at all.
    Confirmed by direct testing: a token alone -> "App not found"; token +
    X_Tenant_ID (as a plain header, no cookie needed) -> real data. This is
    a routing-layer requirement, independent of which auth method is used.

    Separately, since the MCP-API.md handover (2026-09-01, partition model
    moved from per-agent to per-tenant), every *data* endpoint now also
    requires an app-level `tenant_id` that pg-data-ingest checks against
    the JWT's own tenant claim (mismatch -> 403) — as a query parameter on
    a call with no body, or merged into the JSON body on a call that has
    one (never both; this matches MCP-API.md §1's own reference
    implementation exactly, not a guess). Per that doc's explicit
    instruction, this value must never be an LLM-visible tool argument —
    it's resolved here via `GET /whoami` (bearer token only) and injected
    into every subsequent call on this instance. It is NOT cached across
    requests/instances (this server keeps no state between calls, same as
    the rest of this fleet) — one extra `/whoami` round trip per tool
    call, resolved once per AgentDataClient instance and reused for any
    further calls that instance happens to make.
    """

    def __init__(self, token: str, host: str, gateway_tenant_id: str):
        self._token = token
        self._gateway_tenant_id = gateway_tenant_id
        self._base_url = host.rstrip("/") + _APP_PREFIX
        self._resolved_tenant_id: str | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "X_Tenant_ID": self._gateway_tenant_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _clean_params(self, params: dict | None) -> dict:
        if not params:
            return {}
        return {k: v for k, v in params.items() if v is not None}

    async def _resolve_tenant_id(self) -> str:
        """Resolve the JWT's own tenant_id via /whoami, once per instance.

        Deliberately ignores self._gateway_tenant_id for this purpose — that
        header is only for APISIX pod routing (see class docstring). This
        value is what pg-data-ingest's own app-level authorization actually
        checks the tool-call tenant_id against, so it must come from the
        token itself, not from a value a caller supplied.
        """
        if self._resolved_tenant_id is None:
            resp = await self._send_with_retry(
                "GET", f"{self._base_url}/whoami", headers=self._headers()
            )
            body = self._handle(resp)
            self._resolved_tenant_id = body["tenant_id"]
        return self._resolved_tenant_id

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json_body: Any = None) -> Any:
        return await self._request("POST", path, json_body=json_body)

    async def put(self, path: str, json_body: Any = None) -> Any:
        return await self._request("PUT", path, json_body=json_body)

    async def _request(
        self, method: str, path: str, params: dict | None = None, json_body: Any = None
    ) -> Any:
        tenant_id = await self._resolve_tenant_id()
        params = self._clean_params(params)
        # MCP-API.md's own reference implementation (§1) injects tenant_id
        # into the JSON body when one exists, and into the query string only
        # when it doesn't — never both. A body-carrying call (POST/PUT) that
        # also got a tenant_id query param would still work today, but the
        # doc treats query-string tenant_id on a body call as undefined
        # behavior, so match its own sample exactly rather than relying on
        # that.
        if json_body is not None:
            json_body = {**json_body, "tenant_id": tenant_id}
        else:
            params["tenant_id"] = tenant_id
        resp = await self._send_with_retry(
            method,
            f"{self._base_url}{path}",
            headers=self._headers(),
            params=params,
            json_body=json_body,
        )
        return self._handle(resp)

    async def _send_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        client = _get_http_client()
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

            return resp

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
