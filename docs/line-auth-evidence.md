# 线身份与钥匙轨的生效证据

> 这份文档是**仓内证据**，不属于对外的 `line-manifest.json`。
> 清单的每个字段都会随本线 `/.well-known/line-manifest.json` **匿名公开、可被索引**，
> 所以清单里只留产品语义（2026-11-15 起；键闭集见
> `openspec/changes/converge-line-identity-and-keys/specs/line-manifest/spec.md`）。
> 内部代码路径、Secret 名、环境变量、翻转步骤、实测判据一律写在这里。

## 以下原文 2026-11-15 从清单移出，逐字保留，按原键路径分节

### note

哲学概念（SEP 引用层）/ 人物与六轴坐标 / 关系边（含极性）/ 立场分布（PhilPapers Survey）/ 论文元数据（OpenAlex CC0）。四层外部真源对接组装、逐结果溯源（Atlas 溯源链），不自建抽取。识律（lex）第四/第五模块。 **8.6 线自有钥匙**：`auth.py` 与 fd-info/fd-text-core/fd-open-data-mcp/fd-cn-report 的门同族；门对 /health(z) 精确豁免，探针不受影响。

### identity.note

生产态**已启用入站凭据校验**（2026-11-13 翻转，change converge-line-identity-and-keys 8.6）：`fd_zhesi_reference/auth.py` 的 `BearerAuthMiddleware`（恒时比较、`MCP_BEARER_TOKEN` 每请求重读、lifespan 放行、`/health` `/healthz` 与 `/.well-known/line-manifest.json` 精确豁免，豁免表在 `auth.EXEMPT_PATHS` 单处定义）在 `MCP_BEARER_TOKEN` 存在时挂到 HTTP 面。密钥由注册表服务器级 backend auth 在出口注入，客户不持线内令牌；`jit_policy=off`，无断言兑换端点。原料（接入前）：本仓零入站校验——`FastMCP(SERVER_NAME, ...)` 未传 auth、未挂中间件，HTTP 面是 `/mcp` + 无门 `/healthz`。

### auth_tracks[].note

线自有共享钥匙（`MCP_BEARER_TOKEN`），**已在运行路径生效**（2026-11-13 翻转，change converge-line-identity-and-keys 8.6）。两步：① 注册表服务器级 backend auth（该 server 行 `auth_scheme=bearer` + `auth_credential` = 同一把令牌，出口由 auth-server 把 `Authorization` **换成**它；`PATCH /api/servers/<path>/auth-credential`）→ ② 线侧 Secret `fd-zhesi-line-token` 注入 `MCP_BEARER_TOKEN` 并滚版。**翻转前提不是** paas 侧 X-Authorization 中继——`fd-infra-deploy/mcp-registry/prod/auth-server.yaml:75-82` 记着 law-bench 2026-11-11 加进 `INGRESS_RELAY_SERVERS` 当天就撤出的实测：`/validate` 把 `X-Authorization` 当调用方身份，共享不透明令牌放那儿会在边缘被判非法 401。**判据**：匿名直连 NodePort **401**、带同一令牌 **200**、探针路径 `/health` `/healthz` 与线自描述端点 `/.well-known/line-manifest.json` 仍豁免（匿名 200）；注册表行 `scheme=bearer`、`is_enabled=true`、`health_status=healthy`、`num_tools=6`（探活带凭据才拿得到真实工具数）。**这就是 fallback 轨**：摘掉平台后，持有该令牌的客户仍能直接消费本线。⚠️ 共享钥匙**不承载客户身份**——客户级隔离/配额仍要靠平台（词元轨，见 `ciyuan-sk` 那条的 migration_task）。

### auth_tracks[].note

未接：本仓不解析入站凭据。

### billing.note

本仓零计费代码（grep `sub2api` 无命中）：按调用扣费发生在网关/registry 的词元平面，不在本线代码里。只读参照系，外部真源对接；无计量动作。

## 相关

- 清单键闭集与内容判据：`openspec/changes/converge-line-identity-and-keys/specs/line-manifest/spec.md`
- 体检：`python3 scripts/line_manifest_check.py --line fd-zhesi-reference`（正本 `fd-infra-deploy/tools/`）
