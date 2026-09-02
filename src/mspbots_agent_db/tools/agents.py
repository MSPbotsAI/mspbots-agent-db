"""Agent listing — read only, scoped to the caller's own tenant."""

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .._json import dump_json_capped
from ..api_client import AgentDataClient, AgentDataError
from ._common import NO_TOKEN


def register(mcp: FastMCP, client_factory: Callable[[], AgentDataClient | None]) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsagentdb_list_agents() -> str:
        """List every agent registered under the caller's own tenant.

        Use this first when you don't already know which agent_id to pass
        to the other tools — e.g. "what agents do we have data for". Only
        ever lists the caller's own tenant; there is no cross-tenant view.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.get("/agents")
            return dump_json_capped(result)
        except AgentDataError as e:
            return e.to_envelope()
