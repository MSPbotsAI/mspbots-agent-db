# mspbots-agent-db

MCP server for the **MSPbots Agent Data Core** (`pg-data-ingest`) — a per-tenant
service where every agent's business-log records live in a single JSONB table,
partitioned by agent, with no per-business-type table to design or migrate. This
server exposes only the **read** side (schema discovery, filtered query, single-record
lookup, usage stats); every tool takes an `agent_id` to say which agent's partition
to read.

It follows the same design as the sibling `mspbots-agent-mcp` service: stateless, no
stored credentials, per-request header authentication over the
[Model Context Protocol](https://modelcontextprotocol.io/) (Streamable HTTP/SSE
transport).

## When would you use this

This lets an agent (or a builder debugging one) look at the business data an
agent has already logged — never write/delete it:

- "What kinds of records has this agent logged, and what fields do they have?" →
  `mspbotsagentdb_get_schemas`
- "Show me the ticket_sync records with status open" / "how many refunds over
  $500 last week" → `mspbotsagentdb_query_records`
- "What's in record T-1042?" → `mspbotsagentdb_get_record`
- "How much data has this agent logged, is it near quota?" →
  `mspbotsagentdb_get_stats`

Writing new records, deleting them, and admin operations (`init`, `DELETE
/agents/:id`, `POST /maintenance/run`, listing every agent) are **intentionally
not exposed** — an agent writes its own logs directly via the app's own HTTP
API (platform JWT), not through this MCP.

## Tools

授权需要 `X-MSP-Token` / `X-MSP-Host` / `X-MSP-Tenant-Id` 三个请求头；`agent_id` 是**普通工具参数**，
由调用方在每次工具调用里显式传入——见下方 Known Gaps 关于这个设计取舍的说明。

| Tool | 功能 | 参数 |
|---|---|---|
| `mspbotsagentdb_get_schemas` | 列出该 agent 已登记的数据字典（business_type/schema_version/json_schema/source） | `agent_id`(必填)、`business_type`(可选，按业务类型过滤) |
| `mspbotsagentdb_query_records` | 受限 Filter DSL 条件查询 + keyset 游标分页，主力读接口 | `agent_id`(必填)、`filters`(可选，≤10个，AND 关系)、`limit`(默认 20，上限 100)、`cursor`(可选，翻页用) |
| `mspbotsagentdb_get_record` | 按 record_id 读单条记录详情 | `agent_id`(必填)、`record_id`(必填) |
| `mspbotsagentdb_get_stats` | 该 agent 的存储用量与业务分布统计 | `agent_id`(必填) |

`filters[]` 每项 `{ field, op, value }`：

- `field`：`business_type` / `record_id` / `created_at` / `updated_at` / `schema_version`，或 `data.<key>`（最多 5 层）
- `op`：`eq` `neq` `gt` `gte` `lt` `lte` `in` `contains` `exists`（`contains`/`exists` 仅限 `data.*`，用在实体列上会 400）

> **翻页坑**：满页时 `next_cursor` 一定非空，即使那已是最后一页——以 `records` 数组为空作为终止条件，不要只看 `next_cursor === null`。

> Backing endpoints: `GET /agents/:agentId/schemas`,
> `POST /agents/:agentId/records:query`, `GET /agents/:agentId/records/:recordId`,
> `GET /agents/:agentId/stats`。`agentId` 直接来自工具的 `agent_id` 参数。

## Quick Start

### Docker (recommended)

```bash
docker compose up --build
```

The server starts on `http://localhost:8080`.

### Local (uv)

```bash
uv sync
python -m mspbots_agent_db
```

## Health Check

```bash
curl http://localhost:8080/health
# {"status": "ok"}
```

No credentials are required for the health endpoint.

## 授权参数说明 (Authentication)

Every request to `/mcp` must include the following HTTP headers (provided by the
MCP caller/gateway):

| Header | 类型 | 是否必填 | 字段描述 | Example |
|---|---|---|---|---|
| `X-MSP-Token` | string | 必填 | 平台签发的 EdDSA JWT。本服务原样转发为下游请求的 `Authorization: Bearer <token>`——这是 Agent Data Core 唯一会长期支持的凭证类型（见下方 Known Gaps 的版本迁移说明）。 | `X-MSP-Token: <jwt>` |
| `X-MSP-Host` | string | 必填 | Agent Data Core API 所在的 host。 | `X-MSP-Host: https://agentint.mspbots.ai` |
| `X-MSP-Tenant-Id` | string | 必填 | **不是App级鉴权**，是共享网关（APISIX）用来决定转发到哪个租户pod的路由标识（pg-data-ingest一租户一pod）。本服务转发为下游请求的 `X_Tenant_ID` header。缺这个会在网关层直接被拦，返回一个跟pg-data-ingest无关的通用 `{"error":"App not found"}`，不会到达App自己的鉴权/业务逻辑——见 Known Gaps。 | `X-MSP-Tenant-Id: <tenant-uuid>` |

Missing any of the three headers returns `401 Unauthorized`. `agent_id` is **not** a
header — it's a required argument on every tool call (see Known Gaps for why).

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_HTTP_PORT` | `8080` | Listening port |
| `MCP_HTTP_HOST` | `0.0.0.0` | Listening host |

## MCP Endpoint

```
POST http://localhost:8080/mcp
```

Connect your MCP client with:
- Transport: `http` (Streamable HTTP / SSE)
- Headers: `X-MSP-Token`, `X-MSP-Host`, `X-MSP-Tenant-Id` (all required)

## 测试示例 (Test Example)

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-MSP-Token: <jwt>" \
  -H "X-MSP-Host: https://agentint.mspbots.ai" \
  -H "X-MSP-Tenant-Id: <tenant-uuid>" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": { "name": "mspbotsagentdb_get_schemas", "arguments": { "agent_id": "123" } }
  }'
```

> ⚠️ 本仓库为公开仓库，请勿在任何提交的文件中写入真实的 JWT / tenant id 等敏感信息，
> 上面的 `<jwt>` 仅为占位符。

## Known Gaps

- **Migrated 2026-08-31: auth is now the platform JWT (`X-MSP-Token` →
  `Authorization: Bearer <token>`), not `X-API-Key`.** Per a newer API doc
  (`new_api.md`, ClickUp PRD-17749 comment) covering `@app/pg-data-ingest@0.0.4`
  on the INT branch: X-API-Key has been deleted from pg-data-ingest's code
  entirely — the JWT is the only credential that survives. As of this
  writing INT is still running the *old* build (`/health`'s response still
  includes `api_keys_configured`, the documented tell for old vs. new — its
  absence will mean the new build has landed), so X-API-Key still works
  today, but migrating now was strictly safer: the JWT already works against
  the currently-live old build too (verified end-to-end for real, both
  before and after this change), it's forward-compatible with the upcoming
  cutover, and it matches the convention every sibling `mb-platform-*`
  client in this fleet (`mspbots-agent-mcp`, `mspbots-fleet-mcp`) already
  uses. The connector's platform-side registration currently has a
  manually-typed "Agent Data Core API Key" credential field — that should be
  replaced with an auto-injected `X-MSP-Token` (same as `X-MSP-Host` and
  `X-MSP-Tenant-Id` already are), not something an admin re-types.
- **Golden-set tool-selection test run 2026-08-31** (20 utterances covering
  all 4 tools + the `query_records`/`get_record` overlap + 3 negative
  controls for write/delete/list-all): 19/20 unambiguous correct dispatches
  through the real MCP `tools/call` protocol. The one finding — a query
  deliberately violating the `contains`/`exists`-on-entity-field rule wasn't
  rejected locally, only by the upstream API (already the documented,
  deliberate design — see the client-side-validation bullet below) — led to
  clarifying the `query_records` docstring to say the 400 comes from the
  backend, not from this MCP.
- **Fixed 2026-08-31: every tool call was failing in production** with
  `{"code":"not_found","message":"unknown error"}` — a live agent hit this
  calling `mspbotsagentdb_get_schemas`. Root cause: `X-MSP-Tenant-Id` was
  missing entirely from this server's credential set. `pg-data-ingest` is one
  pod per tenant, and the shared APISIX gateway in front of every `/apps/*`
  app on the host needs a tenant id to route to the right pod — without it,
  the gateway itself returns a generic `{"error":"App not found"}` 404 before
  the request ever reaches `pg-data-ingest`'s own auth or business logic. This
  is unrelated to X-API-Key vs. platform-JWT auth (confirmed both fail
  identically without it, and both succeed identically with it) — isolated by
  testing header/cookie combinations one at a time against a real endpoint.
  Now fixed: `X-MSP-Tenant-Id` is a required header, forwarded as `X_Tenant_ID`.
- **Verified against a live INT deployment** (`https://agentint.mspbots.ai/apps/pg-data-ingest`)
  on 2026-08-31: `/health`, `POST /agents/:id/init`, `PUT .../records/:id`, `GET
  .../records/:id`, `POST .../records:query`, and `GET /agents/:id/schemas` were
  all called for real (first with a real tenant API key, then again with a
  real platform JWT after the auth migration above), through this server's
  own MCP `tools/call` protocol (not just bare HTTP) — a real fix, not a
  guess. Every response shape matched what this server's tools parse. Not yet
  verified: the full `filters[]` operator matrix (only a single `eq` filter
  has been tried) and error paths other than 401/404 (`409 AGENT_DELETING`,
  `413`, `429`, `500` are still only unit-tested against synthetic responses,
  not a live trigger).
- **`agent_id` is a plain tool argument, not bound to the connection — by
  deliberate choice, not an oversight.** The original handover doc's §5
  known-gap list states: "API key之间没有隔离——任何有效key都能读写任意agent
  的数据……MCP如果对LLM暴露查询，这条必须先解决". The newer `new_api.md`
  confirms this gap survives the auth migration to JWT, worded the same way
  around the new credential: "认证只回答「token 是否有效」，不回答「它能碰
  哪个 agent」—— 任何一个有效的租户 token 都能读写、删掉任意 agent 的数据"
  (§11) — i.e. neither the old nor the new auth model has per-agent scoping,
  so any tool call here that names a different `agent_id` than "the caller's
  own" will succeed and return that other agent's data. An earlier draft of
  this server closed that gap at the MCP layer (binding one `agent_id` per
  connector instance via a header, no tool argument at all); this was
  deliberately reverted per explicit product direction: `agent_id` is the
  app's own data-isolation key by design (one partition per agent, matching
  how `mspbots-agent-mcp`/`mspbots-fleet-mcp` already take `agent_id` as a
  free parameter for the same reason), and authorization is scoped to the
  token alone. **Net effect: this server inherits the underlying app's own
  documented isolation gap unmitigated, and that gap is not going away with
  the pg-data-ingest upgrade** — closing it (e.g. binding a token to one
  agent_id server-side) is on the `pg-data-ingest` app / platform, not this
  MCP.
- **`business_type`/`record_id` server-side validation is not duplicated
  client-side.** The API already rejects malformed values with a structured
  `400 INVALID_ARGUMENT` (surfaced here as a normal error envelope), so this
  server doesn't re-validate the regex patterns from the doc (§4.0) before
  calling — that would just be dead code duplicating a check the API already
  does correctly.
- **Write/delete/admin endpoints are not wrapped**: `POST /agents/:id/init`,
  `DELETE /agents/:id`, `PUT .../records/:id` (single/batch upsert), `DELETE
  .../records/:id`, `GET /agents` (list all), `POST /maintenance/run`. These
  are either the agent's own direct-HTTP logging path (writes) or
  platform-JWT-only admin operations, both out of scope for an LLM-facing
  read MCP. If a future PRD needs one of these exposed, it should go through
  the same confirm-gate pattern as this fleet's other destructive tools
  (see `mspbots-agent-mcp`'s `mspbotsagent_clear_sop_section` /
  `mspbots-forms-mcp`'s `mspbots_forms_form_delete`), not a bare wrapper.
- **Tested only against `.venv`-local `pytest`, not the PG14→PG18 gap the
  handover doc flags.** The handover doc notes the underlying app's own test
  suite (415 assertions) was run on PostgreSQL 14.12, while INT runs
  PostgreSQL 18.1 — this repo has no PostgreSQL dependency of its own (it's a
  thin HTTP client), so that gap doesn't apply here, but it's worth knowing
  if a query behaves unexpectedly on INT.
