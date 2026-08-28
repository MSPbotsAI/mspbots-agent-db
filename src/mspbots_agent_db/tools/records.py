"""Record read tools — filtered query + single-record lookup, one fixed agent.

⚠️ UNVERIFIED — built entirely from the API contract in the PRD-17749
HANDOVER-MCP.md handover doc, not by calling a live deployment. Endpoint
paths/params/response shapes below match that doc; see README Known Gaps.
"""

from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped
from ..api_client import AgentDataClient, AgentDataError
from ._common import NO_TOKEN

# Hard safety ceiling on top of the underlying API's own documented cap
# (default 20, max 100) — we clamp to the tighter of the two (100).
_MAX_LIMIT = 100


def register(mcp: FastMCP, client_factory: Callable[[], AgentDataClient | None]) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsagentdb_query_records(
        filters: Annotated[
            list[dict] | None,
            Field(
                description=(
                    "Optional list of up to 10 filter objects, combined with AND (no OR "
                    "in this API — for an OR, make two separate calls and merge). Each: "
                    '{"field": "...", "op": "...", "value": ...}. '
                    "field is one of business_type | record_id | created_at | updated_at "
                    "| schema_version, OR a data.<key> path from "
                    "mspbotsagentdb_get_schemas (up to 5 dot-levels deep, e.g. "
                    '"data.ticket.assignee.name") — any other field is rejected. '
                    "op is one of eq | neq | gt | gte | lt | lte | in | contains | exists. "
                    "gt/gte/lt/lte do numeric or timestamp comparison; a row whose value "
                    "at that path isn't a number/timestamp simply doesn't match (no "
                    "error). in takes an array (≤50 items) for value. contains and "
                    "exists are data.<key>-only — using them on an entity field "
                    "(business_type, record_id, ...) is rejected. exists doesn't need "
                    "value, and a JSON-explicit null still counts as existing. "
                    'Example: [{"field": "business_type", "op": "eq", "value": '
                    '"ticket_sync"}, {"field": "data.status", "op": "eq", "value": '
                    '"open"}]. Omit entirely to list all of this agent\'s records.'
                )
            ),
        ] = None,
        limit: Annotated[
            int | None,
            Field(description="Optional page size (default 20, max 100 — server clamps)."),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(
                description=(
                    "Optional opaque pagination cursor from a previous response's "
                    "next_cursor — pass it back unchanged to get the next page."
                )
            ),
        ] = None,
    ) -> str:
        """Search this agent's logged records — the main way to read its data.

        Use for "show me the ticket_sync records with status open", "how
        many refund records over $500 in the last week", "find record
        T-1042" (id-only lookups also work here, but
        mspbotsagentdb_get_record is more direct for one known id).
        Results are always sorted newest first (created_at DESC).

        PAGINATION GOTCHA: next_cursor is non-null on every full page, even
        the actual last one — the server can't tell it's the last page
        until you ask for one more and get nothing back. Stop when a
        response's records array is empty, not when next_cursor looks
        non-null.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        if limit is not None:
            limit = min(limit, _MAX_LIMIT)
        body: dict = {}
        if filters is not None:
            body["filters"] = filters
        if limit is not None:
            body["limit"] = limit
        if cursor is not None:
            body["cursor"] = cursor
        try:
            result = await client.post(client.agent_path("/records:query"), json_body=body)
            return dump_json_capped(result)
        except AgentDataError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsagentdb_get_record(
        record_id: Annotated[str, Field(description="Required record ID.")],
    ) -> str:
        """Get one record's full detail by its exact record_id.

        Use once you already have a specific record_id (e.g. from
        mspbotsagentdb_query_records, or the operator names one directly
        — "what's in record T-1042"). Returns id (an internal, monotonically
        increasing surrogate key — usable as a watermark, not the same as
        record_id), record_id, business_type, data (the full JSON as
        stored), schema_version, created_at, updated_at.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.get(client.agent_path(f"/records/{record_id}"))
            return dump_json_capped(result)
        except AgentDataError as e:
            return e.to_envelope()
