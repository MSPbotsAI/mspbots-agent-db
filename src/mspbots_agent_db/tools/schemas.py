"""Data-dictionary tools — read only.

The underlying app auto-registers a JSON Schema the first time any given
(business_type, schema_version) is written, so this dictionary needs no
manual upkeep — it just reflects whatever the agent has actually logged.
"""

from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped
from ..api_client import AgentDataClient, AgentDataError, agent_path
from ._common import NO_TOKEN


def register(mcp: FastMCP, client_factory: Callable[[], AgentDataClient | None]) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsagentdb_get_schemas(
        agent_id: Annotated[str, Field(description="Which agent's data dictionary to read.")],
        business_type: Annotated[
            str | None,
            Field(
                description=(
                    'Optional filter to one business_type (lowercase, e.g. "ticket_sync"). '
                    "Omit to list every business_type this agent has ever logged."
                )
            ),
        ] = None,
    ) -> str:
        """List the field structures this agent's logged data actually has.

        Call this FIRST, before mspbotsagentdb_query_records — e.g. "what
        fields does the ticket_sync data have" — so filters use real field
        names, not guesses. Each entry: business_type, schema_version,
        json_schema, source ("client" = agent-declared, "inferred" = server
        guessed, looser — may omit optional fields), created_at. A stale
        business_type may have zero live records (schemas outlive deleted
        data) — cross-check with a query.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.get(
                agent_path(agent_id, "/schemas"), params={"business_type": business_type}
            )
            return dump_json_capped(result)
        except AgentDataError as e:
            return e.to_envelope()
