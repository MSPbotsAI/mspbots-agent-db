"""tools/list snapshot + error-envelope mapping tests.

No network calls: tool enumeration goes through FastMCP's in-process
list_tools(), and the error-code mapping is tested directly against
AgentDataError, independent of any real HTTP request.
"""

import json

import pytest
from mcp.server.fastmcp import FastMCP

from mspbots_agent_db.api_client import AgentDataError, agent_path
from mspbots_agent_db.config import Settings
from mspbots_agent_db.server import create_mcp_server

# name -> (required params, expected annotation hint set to True)
EXPECTED_TOOLS = {
    "mspbotsagentdb_get_schemas": ({"agent_id"}, {"readOnlyHint"}),
    "mspbotsagentdb_query_records": ({"agent_id"}, {"readOnlyHint"}),
    "mspbotsagentdb_get_record": ({"agent_id", "record_id"}, {"readOnlyHint"}),
    "mspbotsagentdb_get_stats": ({"agent_id"}, {"readOnlyHint"}),
    "mspbotsagentdb_list_agents": (set(), {"readOnlyHint"}),
    "mspbotsagentdb_write_record": (
        {"agent_id", "record_id", "business_type", "data"},
        {"idempotentHint"},
    ),
    "mspbotsagentdb_write_records_batch": ({"agent_id", "records"}, {"idempotentHint"}),
    "mspbotsagentdb_delete_record": (
        {"agent_id", "record_id"},
        {"destructiveHint", "idempotentHint"},
    ),
}

# mspbotsagentdb_list_agents is tenant-scoped, not agent-scoped — it has no
# agent_id argument at all (see EXPECTED_TOOLS above).
_NO_AGENT_ID_TOOLS = {"mspbotsagentdb_list_agents"}

# This tool's description exceeds the SOP's 500-char guideline (§2.2, a
# "should" not a hard rule) because it has to spell out the real field
# whitelist, the op-to-field-type applicability table, and the
# next_cursor-is-never-null-on-a-full-page pagination gotcha — trimming any
# of these would leave an agent guessing at values the API will just 400 on.
_LONG_DESCRIPTION_EXCEPTIONS = {
    "mspbotsagentdb_query_records",
}


@pytest.mark.asyncio
async def test_tools_list_snapshot():
    mcp = create_mcp_server(Settings())
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == set(EXPECTED_TOOLS), f"unexpected tool set: {names}"

    by_name = {t.name: t for t in tools}
    for name, (expected_required, expected_hints) in EXPECTED_TOOLS.items():
        tool = by_name[name]
        required = set(tool.inputSchema.get("required", []))
        assert required == expected_required, f"{name}: required={required}"

        # agent_id is a required argument on every agent-scoped tool by
        # design — within a tenant, authorization is the platform JWT alone;
        # any valid token can address any agent_id in its own tenant,
        # matching how every other mspbotsagent*-family tool in this
        # platform takes agent_id as a plain argument. See README Known
        # Gaps. mspbotsagentdb_list_agents is the one exception — it lists
        # every agent in the tenant, so it takes no agent_id at all.
        properties = tool.inputSchema.get("properties", {})
        if name not in _NO_AGENT_ID_TOOLS:
            assert "agent_id" in properties, f"{name}: agent_id must be a tool argument"

        # tenant_id must NEVER be an LLM-visible tool argument (MCP-API.md
        # §1) — it's resolved server-side via AgentDataClient._resolve_tenant_id
        # (a /whoami call), not supplied by the caller. If this ever fires,
        # someone added tenant_id as a Field() on a tool by mistake.
        assert "tenant_id" not in properties, f"{name}: tenant_id must never be a tool argument"

        description = tool.description or ""
        if name not in _LONG_DESCRIPTION_EXCEPTIONS:
            assert len(description) <= 500, f"{name}: description too long ({len(description)})"
        first_line = description.strip().splitlines()[0] if description.strip() else ""
        assert len(first_line) <= 100, f"{name}: first line too long: {first_line!r}"
        assert "API:" not in description, f"{name}: leaked implementation detail"
        assert "GET /" not in description and "POST /" not in description, (
            f"{name}: leaked implementation detail"
        )

        annotations = tool.annotations
        actual_hints = set()
        if annotations is not None:
            for hint in ("readOnlyHint", "destructiveHint", "idempotentHint"):
                if getattr(annotations, hint, None) is True:
                    actual_hints.add(hint)
        assert actual_hints == expected_hints, f"{name}: hints={actual_hints}"


@pytest.mark.asyncio
async def test_service_instructions_present_and_bounded():
    mcp = create_mcp_server(Settings())
    assert mcp.instructions
    assert len(mcp.instructions) <= 1500


@pytest.mark.parametrize(
    "status_code,expected_code,expected_retryable",
    [
        (0, "upstream_error", True),
        (400, "invalid_argument", False),
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (409, "conflict", True),  # AGENT_DELETING — explicitly retryable per API doc
        (413, "payload_too_large", False),
        (429, "rate_limited", True),
        (500, "upstream_error", False),  # INTERNAL — a real fault, NOT retryable per API doc
        (502, "upstream_error", True),
        (503, "upstream_error", True),
        (504, "upstream_error", True),
    ],
)
def test_error_envelope_mapping(status_code, expected_code, expected_retryable):
    err = AgentDataError(status_code, "boom")
    envelope = json.loads(err.to_envelope())
    assert envelope["error"]["code"] == expected_code
    assert envelope["error"]["retryable"] is expected_retryable
    assert envelope["error"]["message"] == "boom"


def test_agent_path_builds_paths_for_the_given_agent():
    assert agent_path("789") == "/agents/789"
    assert agent_path("789", "/stats") == "/agents/789/stats"
    assert agent_path("789", "/records/T-1") == "/agents/789/records/T-1"
    # Different agent_id -> different path, proving it's a real per-call
    # argument, not baked into the client instance.
    assert agent_path("1", "/stats") == "/agents/1/stats"


@pytest.mark.asyncio
async def test_query_records_clamps_limit_before_calling_api():
    captured = {}

    class _StubClient:
        async def post(self, path, json_body=None):
            captured["path"] = path
            captured["body"] = json_body
            return {"records": [], "next_cursor": None}

    from mspbots_agent_db.tools import records

    mcp = FastMCP(name="test")
    records.register(mcp, lambda: _StubClient())
    await mcp.call_tool("mspbotsagentdb_query_records", {"agent_id": "999", "limit": 500})

    assert captured["path"] == "/agents/999/records:query"
    assert captured["body"]["limit"] == 100  # clamped from 500 to the 100 ceiling


@pytest.mark.asyncio
async def test_no_credentials_returns_not_configured_without_calling_api():
    from mspbots_agent_db.tools import stats

    mcp = FastMCP(name="test")
    stats.register(mcp, lambda: None)
    result = await mcp.call_tool("mspbotsagentdb_get_stats", {"agent_id": "1"})
    text = result[0][0].text if isinstance(result, tuple) else str(result)
    assert "not_configured" in text


@pytest.mark.asyncio
async def test_list_agents_calls_the_tenant_scoped_endpoint():
    captured = {}

    class _StubClient:
        async def get(self, path, params=None):
            captured["path"] = path
            return {"agents": [{"agent_id": "1"}]}

    from mspbots_agent_db.tools import agents

    mcp = FastMCP(name="test")
    agents.register(mcp, lambda: _StubClient())
    await mcp.call_tool("mspbotsagentdb_list_agents", {})

    assert captured["path"] == "/agents"


@pytest.mark.asyncio
async def test_write_record_puts_business_type_and_data():
    captured = {}

    class _StubClient:
        async def put(self, path, json_body=None):
            captured["path"] = path
            captured["body"] = json_body
            return {"inserted": True, "updated": False}

    from mspbots_agent_db.tools import records

    mcp = FastMCP(name="test")
    records.register(mcp, lambda: _StubClient())
    await mcp.call_tool(
        "mspbotsagentdb_write_record",
        {
            "agent_id": "999",
            "record_id": "T-1",
            "business_type": "ticket_sync",
            "data": {"status": "open"},
        },
    )

    assert captured["path"] == "/agents/999/records/T-1"
    assert captured["body"] == {"business_type": "ticket_sync", "data": {"status": "open"}}


@pytest.mark.asyncio
async def test_write_records_batch_posts_to_batch_upsert_endpoint():
    captured = {}

    class _StubClient:
        async def post(self, path, json_body=None):
            captured["path"] = path
            captured["body"] = json_body
            return {"written": 2, "deduplicated": 0}

    from mspbots_agent_db.tools import records

    mcp = FastMCP(name="test")
    records.register(mcp, lambda: _StubClient())
    batch = [
        {"record_id": "T-1", "business_type": "ticket_sync", "data": {"a": 1}},
        {"record_id": "T-2", "business_type": "ticket_sync", "data": {"a": 2}},
    ]
    await mcp.call_tool(
        "mspbotsagentdb_write_records_batch", {"agent_id": "999", "records": batch}
    )

    assert captured["path"] == "/agents/999/records:batchUpsert"
    assert captured["body"] == {"records": batch}


@pytest.mark.asyncio
async def test_delete_record_calls_the_single_record_endpoint():
    captured = {}

    class _StubClient:
        async def delete(self, path):
            captured["path"] = path
            return {"deleted": True}

    from mspbots_agent_db.tools import records

    mcp = FastMCP(name="test")
    records.register(mcp, lambda: _StubClient())
    await mcp.call_tool(
        "mspbotsagentdb_delete_record", {"agent_id": "999", "record_id": "T-1"}
    )

    assert captured["path"] == "/agents/999/records/T-1"
