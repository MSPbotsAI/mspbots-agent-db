"""AgentDataClient tenant_id resolution: the /whoami round trip, caching
within one instance, and injection into every subsequent call — the core
mechanic added for the 2026-09 per-tenant partition model (MCP-API.md §1).

Uses httpx.MockTransport (built into httpx, no extra test dependency) so
these are real request/response round trips through AgentDataClient's own
code, not stubbed-out client methods.
"""

import httpx
import pytest

from mspbots_agent_db import api_client as api_client_module
from mspbots_agent_db.api_client import AgentDataClient


def _install_mock_transport(monkeypatch, handler):
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(api_client_module, "_get_http_client", lambda: mock_client)
    return mock_client


@pytest.mark.asyncio
async def test_whoami_is_called_once_and_tenant_id_injected_into_every_call(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/whoami"):
            return httpx.Response(200, json={"tenant_id": "tenant-xyz"})
        return httpx.Response(200, json={"agent_id": "42", "records": 3})

    _install_mock_transport(monkeypatch, handler)

    client = AgentDataClient("token-123", "https://agentint.mspbots.ai", "gateway-tenant-1")
    await client.get("/agents/42/stats")
    await client.get("/agents/42/schemas")

    whoami_calls = [r for r in calls if r.url.path.endswith("/whoami")]
    data_calls = [r for r in calls if not r.url.path.endswith("/whoami")]

    # Resolved once per AgentDataClient instance, not once per call.
    assert len(whoami_calls) == 1
    assert len(data_calls) == 2

    # The resolved tenant_id (from /whoami) is what gets injected — not the
    # gateway-routing tenant id the client was constructed with.
    for r in data_calls:
        assert r.url.params["tenant_id"] == "tenant-xyz"

    # The gateway-routing header is still sent on every call, whoami included.
    for r in calls:
        assert r.headers["x_tenant_id"] == "gateway-tenant-1"
        assert r.headers["authorization"] == "Bearer token-123"


@pytest.mark.asyncio
async def test_whoami_failure_surfaces_as_agent_data_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "UNAUTHORIZED", "message": "bad token"})

    _install_mock_transport(monkeypatch, handler)

    from mspbots_agent_db.api_client import AgentDataError

    client = AgentDataClient("bad-token", "https://agentint.mspbots.ai", "gateway-tenant-1")
    with pytest.raises(AgentDataError) as exc_info:
        await client.get("/agents/42/stats")
    assert exc_info.value.status_code == 401
