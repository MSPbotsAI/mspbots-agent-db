import contextvars
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import AgentDataClient
from .config import Settings

# Per-request credential isolation via contextvars.
# GatewayTokenMiddleware sets this before the MCP handler runs.
# Python asyncio copies context per task, so concurrent SSE connections are isolated.
# Value is (api_key, host). agent_id is NOT a credential here — it's a plain
# tool argument (see api_client.agent_path's docstring): the underlying app's
# own auth is the API key alone, and any valid key can address any agent_id
# (a known gap of the underlying app, not something this server papers over).
_gateway_creds_var: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "mspbots_agent_data_gateway_creds", default=None
)


def get_client_from_context(settings: Settings) -> AgentDataClient | None:
    """Resolve the active AgentDataClient for the current request context."""
    creds = _gateway_creds_var.get()
    if not creds:
        return None
    api_key, host = creds
    return AgentDataClient(api_key, host)


class GatewayTokenMiddleware:
    """ASGI middleware.

    Reads X-MSP-Host and X-MSP-Api-Key (both required) from request headers
    and stores them in the contextvar. Returns 401 if either is missing on
    /mcp requests.
    """

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        api_key = request.headers.get("x-msp-api-key")
        host = request.headers.get("x-msp-host")
        if not api_key or not host:
            response = JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": (
                        "This server requires the X-MSP-Api-Key header (Agent Data Core "
                        "API key) and the X-MSP-Host header (Agent Data Core API host)"
                    ),
                    "required_headers": ["X-MSP-Api-Key", "X-MSP-Host"],
                    "optional_headers": [],
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        ctx_token = _gateway_creds_var.set((api_key, host))
        try:
            await self.app(scope, receive, send)
        finally:
            _gateway_creds_var.reset(ctx_token)


def create_mcp_server(settings: Settings) -> FastMCP:
    """Build the FastMCP server instance and register all Agent Data tools."""
    # DNS-rebinding protection is a browser-oriented safeguard that rejects
    # non-localhost Host headers with 421. Disable it so the server works
    # correctly behind a reverse proxy or docker network.
    mcp = FastMCP(
        name="mspbots-agent-db",
        instructions=(
            "MSPbots Agent Data Core (pg-data-ingest) is where an agent's business-log "
            "records live — one JSONB row per event, grouped by business_type, with no "
            "per-business-type table to design or migrate. Every tool takes an agent_id "
            "(one PostgreSQL partition per agent) — this server exposes only the READ "
            "side. Typical flow: mspbotsagentdb_get_schemas first, to learn what "
            "business_types and fields exist for that agent; then "
            "mspbotsagentdb_query_records with a filter built from those fields; then "
            "mspbotsagentdb_get_record for one record's full detail. "
            "mspbotsagentdb_get_stats reports storage/usage, not record content. "
            "Writing, deleting, and admin operations (upsert, delete, re-init, "
            "cross-agent listing) are intentionally not exposed here — an agent writes "
            "its own logs directly via the app's HTTP API, not through this MCP."
        ),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    client_factory: Callable[[], AgentDataClient | None] = lambda: get_client_from_context(settings)

    from .tools import records, schemas, stats

    schemas.register(mcp, client_factory)
    records.register(mcp, client_factory)
    stats.register(mcp, client_factory)

    return mcp
