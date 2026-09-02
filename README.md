# mspbots-agent-db

MCP server for the **MSPbots Agent Data Core** (`pg-data-ingest`) — a per-tenant
service where every agent's business-log records live in a single JSONB table, with
no per-business-type table to design or migrate. Since the 2026-09 handover
(`MCP-API.md`, ClickUp PRD-17749), the LIST partition is per-**tenant** (previously
per-agent); most tools additionally take an `agent_id` to scope to one agent within
that tenant.

It follows the same design as the sibling `mspbots-agent-mcp` service: stateless, no
stored credentials, per-request header authentication over the
[Model Context Protocol](https://modelcontextprotocol.io/) (Streamable HTTP/SSE
transport).

## When would you use this

- "What agents do we have data for?" → `mspbotsagentdb_list_agents`
- "What kinds of records has this agent logged, and what fields do they have?" →
  `mspbotsagentdb_get_schemas`
- "Show me the ticket_sync records with status open" / "how many refunds over
  $500 last week" → `mspbotsagentdb_query_records`
- "What's in record T-1042?" → `mspbotsagentdb_get_record`
- "Log this event" / "update record T-1042" → `mspbotsagentdb_write_record`
- "Log these 50 events at once" → `mspbotsagentdb_write_records_batch`
- "How many records has this agent logged, what types does it have most of?" →
  `mspbotsagentdb_get_stats`

**There is no delete tool, and there never will be one against this API**: as of
the 2026-09-02 MCP-API.md revision, both DELETE endpoints (single record, entire
agent) were removed from pg-data-ingest itself — any DELETE call now 404s
unconditionally, for any token. Data leaves only via the 30-day retention sweep, or
direct DBA action on the database. Re-registering a deleted agent
(`POST /agents/:id/init`) and triggering maintenance are also **intentionally not
exposed** — MCP-API.md §3 calls these out by name as capabilities that should never
be handed to an LLM (admin-only, not a per-record operation).

## Tools

授权需要 `X-MSP-Token` / `X-MSP-Host` / `X-MSP-Tenant-Id` 三个请求头；`agent_id` 是**普通工具参数**
（除 `list_agents` 外），由调用方在每次工具调用里显式传入——见下方 Known Gaps 关于这个设计取舍的说明。
`tenant_id` **不是**工具参数，也不是上面三个 header 之一——本服务自己用 `X-MSP-Token` 换 `GET /whoami`
解析出真正的 app 级 tenant_id 并注入到每次下游调用（无 body 的调用放查询参数，有 body 的调用合并进
JSON body——跟 body 一起走，绝不同时出现在两处，`AgentDataClient._request` 严格照 MCP-API.md §1 自带的
参考实现做），详见 Authentication 一节。

| Tool | 功能 | 参数 |
|---|---|---|
| `mspbotsagentdb_list_agents` | 列出调用方所在租户的全部 agent | 无 |
| `mspbotsagentdb_get_schemas` | 列出该 agent 已登记的数据字典（business_type/schema_version/json_schema/source） | `agent_id`(必填)、`business_type`(可选，按业务类型过滤) |
| `mspbotsagentdb_query_records` | 受限 Filter DSL 条件查询 + keyset 游标分页，主力读接口 | `agent_id`(必填)、`filters`(可选，≤10个，AND 关系)、`limit`(默认 20，上限 100)、`cursor`(可选，翻页用) |
| `mspbotsagentdb_get_record` | 按 record_id 读单条记录详情 | `agent_id`(必填)、`record_id`(必填) |
| `mspbotsagentdb_write_record` | 新建或更新单条记录（幂等，同 record_id 再写是原地覆盖） | `agent_id`(必填)、`record_id`(必填)、`business_type`(必填)、`data`(必填)、`schema_version`(可选) |
| `mspbotsagentdb_write_records_batch` | 单事务批量写入 ≤500 条，全成全败 | `agent_id`(必填)、`records`(必填，每项同上单条写入的字段) |
| `mspbotsagentdb_get_stats` | 该 agent 的记录数与业务分布统计（`tenant_total_bytes`/`tenant_estimated_rows` 是整租户口径，不是该 agent 的） | `agent_id`(必填) |

**没有删除 tool**——`DELETE /agents/:id/records/:recordId` 和 `DELETE /agents/:id` 两个端点已经从
pg-data-ingest 服务端整体下线，调用一律 404，跟 token 无关，所以本服务也没有对应的 tool 可以做。

`agent_id` 现在是字符串（`A-Za-z0-9_.-`，1-128 位，不含冒号），**不再限定纯数字**，且前导零是有意义的
（`"007"` 和 `"7"` 是两个不同的 agent）——之前"1-18位数字、不允许前导零"的旧约束已经作废。

`filters[]` 每项 `{ field, op, value }`：

- `field`：`business_type` / `record_id` / `created_at` / `updated_at` / `schema_version`，或 `data.<key>`（最多 5 层）
- `op`：`eq` `neq` `gt` `gte` `lt` `lte` `in` `contains` `exists`（`contains`/`exists` 仅限 `data.*`，用在实体列上会 400）

> **翻页坑**：满页时 `next_cursor` 一定非空，即使那已是最后一页——以 `records` 数组为空作为终止条件，不要只看 `next_cursor === null`。
>
> **Filter 坑**：语法合法但没有登记过的 `data.<key>` 不会报错，会安静地匹配 0 行（200 空结果），比 400 更难发现是字段名错了还是真的没数据——建议先查一下 `mspbotsagentdb_get_schemas`。

> Backing endpoints: `GET /agents`, `GET /agents/:agentId/schemas`,
> `POST /agents/:agentId/records:query`, `GET|PUT /agents/:agentId/records/:recordId`,
> `POST /agents/:agentId/records:batchUpsert`, `GET /agents/:agentId/stats`。
> `agentId` 直接来自工具的 `agent_id` 参数；`tenant_id` 由本服务自己解析注入，从不出现在任何工具参数里。

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
header — it's a required argument on every agent-scoped tool call (see Known Gaps
for why; `mspbotsagentdb_list_agents` is the one tool that takes no `agent_id`).

**`tenant_id` 是第四个必需的值，但既不是 header 也不是工具参数。** MCP-API.md §1 明确要求：这个值
必须由 MCP server 自己解析、绝不能做成 LLM 可见的参数（填对了没有收益，填错了才会触发本来不该出现的
403）。本服务的做法：每次真正发起下游调用前，用调用方传入的同一个 `X-MSP-Token` 去调
`GET /whoami`，把返回的 `tenant_id` 缓存在这次工具调用用到的 client 实例上，再注入每个真实数据接口
调用——**没有 body 的调用（GET）放查询参数，有 body 的调用（POST/PUT）合并进 JSON body，两者不会
同时出现**，严格照抄 MCP-API.md §1 自己给的参考实现，不是本服务自创的做法。它跟 `X-MSP-Tenant-Id`
header 是两个不同的值，服务用途也不同——一个是网关路由用的，一个是 App 自己的租户鉴权（不匹配返回
403，`_classify` 里映射成 `unauthorized`）。

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

- **Corrected 2026-09-02 (same-day follow-up): tenant_id was going into the
  query string on every call, including POST/PUT calls that already had a
  JSON body.** MCP-API.md §1's own reference implementation puts tenant_id
  in the query string ONLY when there's no body, and merges it into the
  JSON body when there is one — never both. This build's first pass always
  used the query string regardless of method, diverging from the doc's own
  sample code (missed on first read since the sample was split across two
  chunks during extraction). Fixed in `AgentDataClient._request`; regression
  test added in `test_api_client.py`
  (`test_tenant_id_goes_in_body_not_query_string_when_a_body_is_sent`).
- **Corrected 2026-09-02 (same-day follow-up): `mspbotsagentdb_delete_record`
  removed — the underlying DELETE endpoints no longer exist.** A newer
  `MCP-API.md` revision (same ClickUp comment thread, "Deprecate and remove
  the API endpoint") states both `DELETE /agents/:id/records/:recordId` and
  `DELETE /agents/:id` have been removed from pg-data-ingest entirely — any
  DELETE call now 404s unconditionally, not just for LLM-facing callers.
  `POST /maintenance/run` was removed too (cleanup is now scheduler-only,
  no HTTP entry point) — this server never wrapped that one so no code
  change was needed there. Also per this revision: `agent_id`'s allowed
  shape widened from "1-18 digits, no leading zero" to a general string
  (`^[A-Za-z0-9_.-]{1,128}$`, leading zeros now significant) — no client-side
  validation existed for this in either shape, so no code change was needed,
  just awareness that agent_id is no longer numeric-only.
- **Migrated 2026-09-02: partition model moved from per-agent to
  per-tenant, and this server now writes too — not just reads.**
  Per the newer handover doc (`MCP-API.md`, ClickUp PRD-17749 comment,
  superseding `new_api.md`): the LIST partition is now per-tenant, and
  every data endpoint requires an app-level `tenant_id` query parameter
  that pg-data-ingest checks against the JWT's own tenant claim (mismatch
  -> 403). This closes the *cross-tenant* half of the isolation gap the
  previous doc/README called out — a token can no longer reach another
  tenant's data at all. Implemented here as `AgentDataClient._resolve_tenant_id`:
  one `GET /whoami` call (bearer token only) per tool-call's client
  instance, never a caller-supplied value, exactly as MCP-API.md §1
  requires — `tenant_id` never appears in any tool's input schema (see
  `test_tools.py`'s `tenant_id must never be a tool argument` assertion).
  Three new tools were added straight from the doc's §2 tool list:
  `mspbotsagentdb_list_agents`, `mspbotsagentdb_write_record`,
  `mspbotsagentdb_write_records_batch` (a fourth, `mspbotsagentdb_delete_record`,
  was added then removed again the same day — see the newer bullet above:
  the underlying DELETE endpoint was deprecated and removed from the API
  entirely). Deliberately still NOT exposed, per the doc's own §3 "don't
  hand these to an LLM" list: re-registering a deleted agent
  (`POST /agents/:id/init`) and triggering maintenance — admin-only, not
  per-record operations. **Not yet verified against a live deployment with
  real credentials** — unit-tested (including the `/whoami` round trip via
  `httpx.MockTransport` in `test_api_client.py`) and smoke-tested through
  the real MCP protocol locally (`tools/list` returns all 7 tools with the
  expected schemas), but a dummy-credential call against the real INT host
  only got as far as confirming the `/whoami` request actually goes out
  over the wire — not a genuine end-to-end data round trip. The **within-
  tenant** isolation gap below is explicitly still open per MCP-API.md §8
  ("同一租户内部任何有效 token 都能读写任意 agent 的数据") — this migration
  did not touch that.
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
  (§11) — i.e. neither the old nor the new auth model has per-agent scoping
  *within one tenant*, so any tool call here that names a different
  `agent_id` than "the caller's own" (but the same tenant) will succeed and
  return/modify that other agent's data. **This is narrower than it used to
  be**: the 2026-09-02 migration above closed the *cross-tenant* half of
  this — a token can no longer touch a different tenant's agents at all,
  confirmed by MCP-API.md §8 itself, which explicitly still calls out the
  within-tenant gap as unsolved while saying nothing further about
  cross-tenant. An earlier draft of this server closed the within-tenant
  gap at the MCP layer (binding one `agent_id` per connector instance via a
  header, no tool argument at all); this was deliberately reverted per
  explicit product direction: `agent_id` is the app's own data-isolation
  key by design (matching how `mspbots-agent-mcp`/`mspbots-fleet-mcp`
  already take `agent_id` as a free parameter for the same reason), and
  authorization within a tenant is scoped to the token alone. **Net
  effect: this server inherits the underlying app's own documented
  within-tenant isolation gap unmitigated** — closing it (e.g. binding a
  token to one agent_id server-side) is on the `pg-data-ingest` app /
  platform, not this MCP.
- **`business_type`/`record_id` server-side validation is not duplicated
  client-side.** The API already rejects malformed values with a structured
  `400 INVALID_ARGUMENT` (surfaced here as a normal error envelope), so this
  server doesn't re-validate the regex patterns from the doc (§4.0) before
  calling — that would just be dead code duplicating a check the API already
  does correctly.
- **`POST /agents/:id/init` (re-register a deleted agent) is still not
  wrapped, by design** — the one remaining admin-only operation MCP-API.md
  §3 explicitly says not to hand an LLM. `DELETE /agents/:id` (delete an
  entire agent) and `POST /maintenance/run` used to be in this same bucket
  but no longer need a design justification at all — both were removed
  from pg-data-ingest itself (see the 2026-09-02 Known Gaps entry above),
  so there is nothing left to deliberately not-wrap. Per-record write
  (`PUT .../records/:id`, batch upsert) and listing agents (`GET /agents`)
  WERE wrapped in the 2026-09-02 migration below. If a future PRD needs
  `init` exposed, it should go through the same confirm-gate pattern as
  this fleet's other destructive tools (see `mspbots-agent-mcp`'s
  `mspbotsagent_clear_sop_section` / `mspbots-forms-mcp`'s
  `mspbots_forms_form_delete`), not a bare wrapper.
- **Tested only against `.venv`-local `pytest`, not the PG14→PG18 gap the
  handover doc flags.** The handover doc notes the underlying app's own test
  suite (415 assertions) was run on PostgreSQL 14.12, while INT runs
  PostgreSQL 18.1 — this repo has no PostgreSQL dependency of its own (it's a
  thin HTTP client), so that gap doesn't apply here, but it's worth knowing
  if a query behaves unexpectedly on INT.
