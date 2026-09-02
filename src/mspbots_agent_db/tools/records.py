"""Record tools — filtered query, single-record lookup, write, and delete.

Endpoint paths/params/response shapes for the read tools were checked
against a real `pg-data-ingest` INT deployment (not just the handover
doc) — see README Known Gaps for exactly what was and wasn't exercised
live; the write/delete tools below are new with the 2026-09 MCP-API.md
revision and have not yet had the same live check.
"""

from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped
from ..api_client import AgentDataClient, AgentDataError, agent_path
from ._common import NO_TOKEN

# Hard safety ceiling on top of the underlying API's own documented cap
# (default 20, max 100) — we clamp to the tighter of the two (100).
_MAX_LIMIT = 100


def register(mcp: FastMCP, client_factory: Callable[[], AgentDataClient | None]) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsagentdb_query_records(
        agent_id: Annotated[str, Field(description="Which agent's records to search.")],
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
                    '"data.ticket.assignee.name") — any other field gets a 400 from the '
                    "backend (not validated here). "
                    "op is one of eq | neq | gt | gte | lt | lte | in | contains | exists. "
                    "gt/gte/lt/lte do numeric or timestamp comparison; a row whose value "
                    "at that path isn't a number/timestamp simply doesn't match (no "
                    "error). in takes an array (≤50 items) for value. contains and "
                    "exists are data.<key>-only — using them on an entity field "
                    "(business_type, record_id, ...) gets a 400 from the backend, not "
                    "validated here. exists doesn't need "
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

        FILTER GOTCHA: a well-formed data.<key> path that matches no
        registered schema field is accepted, not rejected — it just
        silently matches zero rows (200, empty), which is easy to mistake
        for "no matching data" instead of "wrong field name". Check
        mspbotsagentdb_get_schemas if a filter you expect to match returns
        nothing.
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
            result = await client.post(agent_path(agent_id, "/records:query"), json_body=body)
            return dump_json_capped(result)
        except AgentDataError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsagentdb_get_record(
        agent_id: Annotated[str, Field(description="Which agent owns this record.")],
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
            result = await client.get(agent_path(agent_id, f"/records/{record_id}"))
            return dump_json_capped(result)
        except AgentDataError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbotsagentdb_write_record(
        agent_id: Annotated[str, Field(description="Which agent this record belongs to.")],
        record_id: Annotated[
            str,
            Field(
                description="Your own id for this record, unique within the agent "
                "(letters/digits/_/-/:/. only, max 128 chars)."
            ),
        ],
        business_type: Annotated[
            str,
            Field(
                description="Lowercase business-type tag for this record's shape "
                '(letters/digits/_/- only, max 64 chars, e.g. "ticket_sync").'
            ),
        ],
        data: Annotated[
            dict | list,
            Field(
                description="The record's JSON payload (object or array, not a bare "
                "string/number). Max 100KB serialized."
            ),
        ],
        schema_version: Annotated[
            int | None,
            Field(description="Optional structure version for this business_type (default 1)."),
        ] = None,
    ) -> str:
        """Create or update one record. Idempotent: writing the same
        record_id again overwrites it in place (the response's `inserted`
        is false on an overwrite) — safe to retry after a timeout without
        risk of a duplicate.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {"business_type": business_type, "data": data}
        if schema_version is not None:
            body["schema_version"] = schema_version
        try:
            result = await client.put(agent_path(agent_id, f"/records/{record_id}"), json_body=body)
            return dump_json_capped(result)
        except AgentDataError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbotsagentdb_write_records_batch(
        agent_id: Annotated[str, Field(description="Which agent these records belong to.")],
        records: Annotated[
            list[dict],
            Field(
                description="1-500 records to write in ONE transaction — all succeed or "
                'none do. Each: {"record_id", "business_type", "data", '
                '"schema_version" (optional)} — same constraints as '
                "mspbotsagentdb_write_record's arguments. Duplicate record_id values "
                "within the same call collapse to the last occurrence "
                "(response's `deduplicated` says how many)."
            ),
        ],
    ) -> str:
        """Write up to 500 records in one all-or-nothing transaction.

        Use this instead of many mspbotsagentdb_write_record calls when
        writing several records at once — one failure rolls back the
        whole batch, so a partial batch never lands.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.post(
                agent_path(agent_id, "/records:batchUpsert"), json_body={"records": records}
            )
            return dump_json_capped(result)
        except AgentDataError as e:
            return e.to_envelope()
