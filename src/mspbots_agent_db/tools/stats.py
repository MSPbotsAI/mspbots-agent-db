"""Usage/storage stats — read only, one fixed agent."""

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .._json import dump_json_capped
from ..api_client import AgentDataClient, AgentDataError
from ._common import NO_TOKEN


def register(mcp: FastMCP, client_factory: Callable[[], AgentDataClient | None]) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsagentdb_get_stats() -> str:
        """Check this agent's storage usage and business_type breakdown.

        Use for "how much data has this agent logged", "are we near the
        storage quota", "what record types does it have the most of" —
        volume/usage, not content (use mspbotsagentdb_query_records for
        that). estimated_rows = -1 means "never analyzed", not "empty".
        business_types (top 50 by count) is an unbounded full-partition
        scan — avoid calling this in a tight loop on a large agent.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.get(client.agent_path("/stats"))
            return dump_json_capped(result)
        except AgentDataError as e:
            return e.to_envelope()
