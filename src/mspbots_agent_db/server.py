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
# Value is (token, host, gateway_tenant_id). agent_id is NOT a credential
# here — it's a plain tool argument (see api_client.agent_path's
# docstring): within one tenant, the underlying app's own auth only
# answers "is this token valid", never "which agent may it touch", so any
# valid token can address any agent_id in its own tenant (a known gap of
# the underlying app, not something this server papers over).
# gateway_tenant_id IS required, but for routing, not app-level auth: it's
# what the shared APISIX gateway needs to route to the right tenant's pod
# (pg-data-ingest is one pod per tenant). The tenant_id pg-data-ingest's
# own app-level authorization actually checks is a *separate* value,
# resolved by AgentDataClient itself via /whoami — see its docstring.
_gateway_creds_var: contextvars.ContextVar[tuple[str, str, str] | None] = contextvars.ContextVar(
    "mspbots_agent_data_gateway_creds", default=None
)


def get_client_from_context(settings: Settings) -> AgentDataClient | None:
    """Resolve the active AgentDataClient for the current request context."""
    creds = _gateway_creds_var.get()
    if not creds:
        return None
    token, host, gateway_tenant_id = creds
    return AgentDataClient(token, host, gateway_tenant_id)


class GatewayTokenMiddleware:
    """ASGI middleware.

    Reads X-MSP-Host, X-MSP-Token, and X-MSP-Tenant-Id (all required) from
    request headers and stores them in the contextvar. Returns 401 if any is
    missing on /mcp requests.

    X-MSP-Token carries the platform's own EdDSA JWT, forwarded verbatim as
    Authorization: Bearer <token> — same convention as the sibling
    mspbots-agent-mcp/mspbots-fleet-mcp connectors, and the only credential
    pg-data-ingest's own upcoming release still accepts (see
    AgentDataClient's docstring).
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
        token = request.headers.get("x-msp-token")
        host = request.headers.get("x-msp-host")
        tenant_id = request.headers.get("x-msp-tenant-id")
        if not token or not host or not tenant_id:
            response = JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": (
                        "This server requires the X-MSP-Token header (Agent Data Core "
                        "platform JWT), the X-MSP-Host header (Agent Data Core API host), "
                        "and the X-MSP-Tenant-Id header (needed for the shared gateway to "
                        "route to the right tenant's pod, not for app-level auth)"
                    ),
                    "required_headers": ["X-MSP-Token", "X-MSP-Host", "X-MSP-Tenant-Id"],
                    "optional_headers": [],
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        ctx_token = _gateway_creds_var.set((token, host, tenant_id))
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
            "per-business-type table to design or migrate. Data is partitioned per "
            "tenant; most tools also take an agent_id to scope to one agent within that "
            "tenant. Typical flow: mspbotsagentdb_list_agents if you don't already know "
            "the agent_id; mspbotsagentdb_get_schemas to learn what business_types and "
            "fields exist for that agent; mspbotsagentdb_query_records with a filter "
            "built from those fields; mspbotsagentdb_get_record for one record's full "
            "detail. mspbotsagentdb_write_record / _write_records_batch upsert one or "
            "many records; mspbotsagentdb_delete_record removes one. "
            "mspbotsagentdb_get_stats reports storage/usage, not record content. "
            "Deleting an entire agent, triggering maintenance, and re-registering a "
            "deleted agent are intentionally not exposed here — those are irreversible "
            "or admin-only operations, not something to hand an LLM."
        ),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    client_factory: Callable[[], AgentDataClient | None] = lambda: get_client_from_context(settings)

    from .tools import agents, records, schemas, stats

    agents.register(mcp, client_factory)
    schemas.register(mcp, client_factory)
    records.register(mcp, client_factory)
    stats.register(mcp, client_factory)

    return mcp
