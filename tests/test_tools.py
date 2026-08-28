"""tools/list snapshot + error-envelope mapping tests.

No network calls: tool enumeration goes through FastMCP's in-process
list_tools(), and the error-code mapping is tested directly against
AgentDataError, independent of any real HTTP request.
"""

import json

import pytest
from mcp.server.fastmcp import FastMCP

from mspbots_agent_db.api_client import AgentDataClient, AgentDataError
from mspbots_agent_db.config import Settings
from mspbots_agent_db.server import create_mcp_server

# name -> (required params, expected annotation hint set to True)
EXPECTED_TOOLS = {
    "mspbotsagentdb_get_schemas": (set(), {"readOnlyHint"}),
    "mspbotsagentdb_query_records": (set(), {"readOnlyHint"}),
    "mspbotsagentdb_get_record": ({"record_id"}, {"readOnlyHint"}),
    "mspbotsagentdb_get_stats": (set(), {"readOnlyHint"}),
}

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

        # Security-critical regression guard: agent_id must NEVER be a tool
        # argument. It is bound once per connector instance (X-MSP-Agent-Id,
        # see server.py), which is the whole fix for the underlying API's
        # documented gap that any valid API key can read any agent's data.
        # If a future edit adds agent_id as a parameter here, that gap
        # reopens at the MCP layer even though the app itself never changed.
        properties = tool.inputSchema.get("properties", {})
        assert "agent_id" not in properties, f"{name}: agent_id must not be a tool argument"

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


def test_agent_data_client_paths_are_scoped_to_its_bound_agent():
    client = AgentDataClient(api_key="k", host="https://agentint.mspbots.ai", agent_id="789")
    assert client.agent_path() == "/agents/789"
    assert client.agent_path("/stats") == "/agents/789/stats"
    assert client.agent_path("/records/T-1") == "/agents/789/records/T-1"


@pytest.mark.asyncio
async def test_query_records_clamps_limit_before_calling_api():
    captured = {}

    class _StubClient:
        def agent_path(self, suffix: str = "") -> str:
            return f"/agents/999{suffix}"

        async def post(self, path, json_body=None):
            captured["path"] = path
            captured["body"] = json_body
            return {"records": [], "next_cursor": None}

    from mspbots_agent_db.tools import records

    mcp = FastMCP(name="test")
    records.register(mcp, lambda: _StubClient())
    await mcp.call_tool("mspbotsagentdb_query_records", {"limit": 500})

    assert captured["path"] == "/agents/999/records:query"
    assert captured["body"]["limit"] == 100  # clamped from 500 to the 100 ceiling


@pytest.mark.asyncio
async def test_no_credentials_returns_not_configured_without_calling_api():
    from mspbots_agent_db.tools import stats

    mcp = FastMCP(name="test")
    stats.register(mcp, lambda: None)
    result = await mcp.call_tool("mspbotsagentdb_get_stats", {})
    text = result[0][0].text if isinstance(result, tuple) else str(result)
    assert "not_configured" in text
