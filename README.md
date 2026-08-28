# mspbots-agent-data-mcp

MCP server for the **MSPbots Agent Data Core** (`pg-data-ingest`) — a per-tenant
service where every agent's business-log records live in a single JSONB table,
partitioned by agent, with no per-business-type table to design or migrate. This
server exposes only the **read** side (schema discovery, filtered query, single-record
lookup, usage stats) for **one fixed agent**.

It follows the same design as the sibling `mspbots-agent-mcp` service: stateless, no
stored credentials, per-request header authentication over the
[Model Context Protocol](https://modelcontextprotocol.io/) (Streamable HTTP/SSE
transport).

## When would you use this

This lets an agent (or a builder debugging one) look at the business data that
agent has already logged — never another agent's, and never write/delete it:

- "What kinds of records has this agent logged, and what fields do they have?" →
  `mspbotsagentdata_get_schemas`
- "Show me the ticket_sync records with status open" / "how many refunds over
  $500 last week" → `mspbotsagentdata_query_records`
- "What's in record T-1042?" → `mspbotsagentdata_get_record`
- "How much data has this agent logged, is it near quota?" →
  `mspbotsagentdata_get_stats`

Writing new records, deleting them, and admin operations (`init`, `DELETE
/agents/:id`, `POST /maintenance/run`, listing every agent) are **intentionally
not exposed** — an agent writes its own logs directly via the app's HTTP API
(`X-API-Key`), not through this MCP. See "Known Gaps" below for why this MCP
doesn't take an `agent_id` argument either.

## Tools

所有工具的凭证均来自请求头（`X-MSP-Api-Key` / `X-MSP-Agent-Id` / `X-MSP-Host`），
工具参数里既不需要传 API key，也**不需要、不能**传 `agent_id`——每个连接器实例
在网关侧就已经绑死了唯一一个 agent，见下方 Known Gaps。

| Tool | 功能 | 参数 |
|---|---|---|
| `mspbotsagentdata_get_schemas` | 列出该 agent 已登记的数据字典（business_type/schema_version/json_schema/source） | `business_type`(可选，按业务类型过滤) |
| `mspbotsagentdata_query_records` | 受限 Filter DSL 条件查询 + keyset 游标分页，主力读接口 | `filters`(可选，≤10个，AND 关系)、`limit`(默认 20，上限 100)、`cursor`(可选，翻页用) |
| `mspbotsagentdata_get_record` | 按 record_id 读单条记录详情 | `record_id`(必填) |
| `mspbotsagentdata_get_stats` | 该 agent 的存储用量与业务分布统计 | 无 |

`filters[]` 每项 `{ field, op, value }`：

- `field`：`business_type` / `record_id` / `created_at` / `updated_at` / `schema_version`，或 `data.<key>`（最多 5 层）
- `op`：`eq` `neq` `gt` `gte` `lt` `lte` `in` `contains` `exists`（`contains`/`exists` 仅限 `data.*`，用在实体列上会 400）

> **翻页坑**：满页时 `next_cursor` 一定非空，即使那已是最后一页——以 `records` 数组为空作为终止条件，不要只看 `next_cursor === null`。

> Backing endpoints: `GET /agents/:agentId/schemas`,
> `POST /agents/:agentId/records:query`, `GET /agents/:agentId/records/:recordId`,
> `GET /agents/:agentId/stats`。`agentId` 由本服务从 `X-MSP-Agent-Id` header 注入，
> 从不出现在 MCP 工具的入参 schema 里。

## Quick Start

### Docker (recommended)

```bash
docker compose up --build
```

The server starts on `http://localhost:8080`.

### Local (uv)

```bash
uv sync
python -m mspbots_agent_data_mcp
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
| `X-MSP-Api-Key` | string | 必填 | Agent Data Core 的租户 API key。本服务原样转发为下游请求的 `X-API-Key: <key>`。 | `X-MSP-Api-Key: <api-key>` |
| `X-MSP-Agent-Id` | string | 必填 | 本连接实例唯一绑定的 agent id。**不接受、也不透传给调用方选择**——所有工具调用都只作用于这一个 agent。 | `X-MSP-Agent-Id: 123` |
| `X-MSP-Host` | string | 必填 | Agent Data Core API 所在的 host。 | `X-MSP-Host: https://agentint.mspbots.ai` |

Missing any of the three headers returns `401 Unauthorized`.

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
- Headers: `X-MSP-Api-Key`, `X-MSP-Agent-Id`, `X-MSP-Host` (all required)

## 测试示例 (Test Example)

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-MSP-Api-Key: <api-key>" \
  -H "X-MSP-Agent-Id: 123" \
  -H "X-MSP-Host: https://agentint.mspbots.ai" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": { "name": "mspbotsagentdata_get_schemas", "arguments": {} }
  }'
```

> ⚠️ 本仓库为公开仓库，请勿在任何提交的文件中写入真实的 API key / agent id 等敏感信息，
> 上面的 `<api-key>` 仅为占位符。

## Known Gaps

- ⚠️ **UNVERIFIED — built entirely from the PRD-17749 `HANDOVER-MCP.md` handover
  doc, not by calling a live deployment.** Endpoint paths/params/response shapes
  match that doc; none have been called against a real `pg-data-ingest` instance.
  Verify against a live INT deployment before treating this as production-ready.
- **Why `agent_id` is not a tool parameter, on purpose.** The handover doc's own
  §5 known-gap list states: "API key之间没有隔离——任何有效key都能读写任意agent
  的数据……MCP如果对LLM暴露查询，这条必须先解决" — i.e. the underlying app has
  no per-agent credential scoping yet, so a naive MCP that took `agent_id` as a
  free argument would let any agent's LLM read any other agent's data in the
  same tenant just by naming a different id. This server closes that gap at the
  MCP layer instead of waiting on an app-side fix: `agent_id` is bound once, at
  connector-configuration time, via the `X-MSP-Agent-Id` header — no tool
  accepts or forwards a caller-supplied agent id. **This requires the platform
  to push this connector's credentials per-agent, not per-tenant** (unlike
  `mspbots-agent-mcp`/`mspbots-fleet-mcp`, which are registered once per tenant
  and take `agent_id` as a free parameter because their own APIs already
  enforce tenant-scoped, not agent-scoped, access).
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
