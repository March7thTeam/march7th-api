# Gateway Model

## Route registry

调用者不能提交目标 URL。每条公开路径在数据库中绑定一个已登记 API、一个 HTTPS base URL、固定 method 和固定 target path。查询参数和请求体可以转发，但不能改变上游主机。

目标 URL 必须满足：

- 仅 HTTPS；
- 不含 username/password；
- 字面 IP 必须是公网地址；
- DNS 的全部当前解析结果都必须是公网地址；
- 网关不跟随上游重定向。

DNS 检查是纵深防御，不替代管理员对上游域名的审核。不要把可由不受信任用户控制解析结果的域名加入目录。

目录网关在预留事务内重新读取路由及上游配置，逐项比对目标、方法、路径、scope、匿名许可、启停、认证方式与秘密引用及更新时间。配置变化返回 `route_changed`，停用返回 `route_disabled`，均为 JSON 404，并且不发送上游请求。取得 HTTP 客户端并构造请求后、发送前再次复核；失败时结束预留并记录对应拒绝结果，归还该次请求配额，不扣积分。

最终复核与网络发送之间仍存在短暂窗口。已经通过最终复核、正在发送的请求，可能在停用操作返回后完成。

目录网关与 `/v1` 都记录发送状态。预留状态仍参与并发准入检查，发送前异常以 `gateway_error_not_dispatched` 完成，配置拒绝保留具体原因，同时释放积分与次数预留。历史日志的空发送状态沿用原计数规则。调用 `send` 后的传输错误、超时与结算沿用原计费规则。

发送前收尾使用独立数据库连接，依赖连接的 `busy_timeout`，最多尝试两次，间隔 50 毫秒。成功时将日志完成为 `not_dispatched`，全额释放积分预留并归还次数配额。持续写入失败时，以错误日志记录 `usage_id`、`request_id` 并返回 5xx，数据库保留未完成预留，等待过期恢复。

数据库中的 `usage_dispatch_pending` 是未发送恢复的唯一候选来源。`reserve_credit` 创建新 hold 的同一事务必须入队；`reserve_usage` 也在创建 `reserved` 日志的事务中入队，覆盖 `/v1`、业务路由及没有积分 hold 的请求。两处使用同一日志的 request_id 和 started_at（仅 NULL 时回退 created_at），以 usage_id 幂等插入；已有行的时间、身份与重试证据均不被覆盖。正常请求在调用账本前已在同一事务中将日志标为 `reserved`，因此两处入队值一致。队列的 `created_at` 与 `next_attempt_at` 统一为 UTC ISO 微秒格式，正常请求初次到期时间为日志开始时间加 15 分钟。提交发送标记、发送前失败收尾、任意日志完成及人工处理的事务同时删除队列行。发送标记提交成功后才允许调用上游。

队列表示待复核候选，不能单凭入队证明请求未发送。正常应用入队时日志为 `reserved` 且未完成，但直接调用账本创建 hold 时，日志可能仍为 NULL 或其他发送状态；账本保留原始证据，不将其补写为 `reserved`。这类请求初次 next_attempt_at 为日志开始时间，使启动恢复立即有机会检查；若 hold 的 created_at 尚未满 15 分钟或无法解析，则将 next_attempt_at 延后 60 秒重试。转待核只根据 hold 的 created_at 判龄，不受近期日志或队列 created_at 阻挡，且不改动任何金额。释放仍须满足下文所有严格条件。未发送日志不在队列中的例外包括：

- 冲突出队会保留 `reserved`、未完成的日志。有活跃 hold 时持久化待核状态，已结算或已释放的 hold 保留原状态。
- 人工处理始终出队。非零核账，以及不满足双方未发送、未完成、过期检查的零金额核账，会保留原发送状态及完成状态。
- 达到连续失败上限后转人工并出队，日志可能仍为 `reserved` 且未完成，配额占用保留至原有收尾或人工流程处理。

启动时运行一次恢复。预留、余额估算及钱包请求共享每进程每库 60 秒的限频，失败尝试同样计入。恢复使用 `BEGIN IMMEDIATE`，从 `idx_usage_dispatch_pending_next(next_attempt_at, usage_id)` 直接按 `next_attempt_at <= ?` 及相同顺序读取至多 100 条，候选查询不连接日志或 hold。每条记录在保存点内重新读取日志和 hold，检查日志明确为 `reserved`、未完成、开始时间超过 15 分钟，以及 hold 不存在或明确为 `reserved` 且状态为 `held` / `reconciliation_required`。存在 hold 时还要求其创建时间过期，归属一致，且没有既有计费收据或确认金额。释放 hold 产生正常的 `credit_release` 流水，并将日志收尾为 `not_dispatched`、完成时间、500/失败、`gateway_error_not_dispatched`，同时归还次数配额并出队。无 hold 的业务请求完成相同日志收尾和配额归还。

日志发送状态不为明确的 `reserved`（包括 NULL）且 hold 仍为 `held` 时，不能释放；hold 超时后在同一保存点持久化 `reconciliation_required` / `dispatch_conflict`，同步日志的 `credit_status` 与 `credit_reason` 并出队。该规则也适用于已达失败上限的队列项。保留余额、冻结、收据、发送状态与完成状态。没有活跃 held 的同类日志，或发送状态为 `reserved` 但已完成的日志，只删除队列项。hold 已发送、已结算、已释放或无法安全判断时同样出队。若 hold 仍为 `held`，同一保存点将其设为 `reconciliation_required`，原因设为 `dispatch_conflict`，并同步日志的 `credit_status` 与 `credit_reason`。原本已待核的记录保持现状，已结算或已释放的记录保留原状态。记录 usage_id、request_id 和状态供人工核对。原始计费证据或归属异常触发单条错误并保留冻结。时间复核支持 ISO 秒/微秒、`Z`、偏移、空格以及无时区 UTC，采用精确微秒比较。空值仅在 `started_at IS NULL` 时回退到 `created_at`，不可解析的时间暂缓重试。

处理失败先回滚该条保存点，再在另一个独立保存点内写入 `attempts + 1`、`last_error` 和 60 秒后的 `next_attempt_at`。退避写入也失败时，仅回滚退避保存点、记录两类错误并继续下一条。错误日志均包含 usage_id 和 request_id。退避提交后由各进程共同遵守，强制恢复同样遵守到期时间，前 100 条失败后会让出后续候选的位置。

连续处理失败达到常量 `MAX_DISPATCH_RECOVERY_ATTEMPTS = 5` 后，在独立保存点内重新核对队列身份、失败次数、日志 `reserved` 和未完成条件。活跃 hold 变为 `reconciliation_required`，已待核 hold 保留状态并更新原因，原因统一为 `dispatch_recovery_failed`。日志仅写 `credit_status` 和 `credit_reason`，金额、收据、发送与完成状态均保留。然后删除队列项，以结构化错误日志记录 usage_id、request_id、last_error。没有 hold 的业务请求同样记录原因并出队。已结算或已释放的 hold 保留其原状态。达到上限的记录每次只尝试转人工。转人工失败时回滚该保存点，再用独立保存点推进 `next_attempt_at` 至本次恢复时间加 60 秒、记录 `last_error`，`attempts` 保持不低于上限。退避写入再次失败时只记录错误，继续其他记录。

阶段 B 使用独立的 `credit_hold_watch` 到期队列。`mark_usage_dispatched` 的发送事务调用 `mark_credit_dispatched` 时，从待发送队列删除有 hold 的请求，同时将其写入 watch。没有 hold 的请求仅从待发送队列出队。watch 的 `due_at` 为 hold 原始 `created_at` 加 15 分钟，统一使用 UTC ISO 微秒格式。重复发送标记保留已有的到期与重试证据。结算、释放、转待核、冲突转待核、失败上限转人工及人工核账的状态转换事务都会删除对应 watch 行。

watch 同样使用单向不变量。有 watch 行意味着 hold 为 `held`，已经通过发送标记并退出待发送队列。反向的例外包括尚未发送的 `held` 请求，以及迁移后绕过应用事务直接插入的记录。迁移还会收纳历史发送状态不明的 hold，这些记录缺少可安全释放的证据，仅按原规则超时转待核，watch 的存在不能补足其历史发送证据。外部写库造成的残留 watch 行由阶段 B 自我修复。

阶段 B 通过 `idx_credit_hold_watch_due(due_at, usage_id)` 查询 `due_at <= ?`，按相同顺序至多读取 `WATCH_RECOVERY_BATCH_SIZE = 100` 条，无需扫描 held 或读取游标。每条在保存点内以 `status='held'` 为条件改为 `reconciliation_required`、原因 `stale_hold`，同步日志并删除 watch 行。若 hold 已离开 `held`，仅删除残留 watch 行。失败时回滚该条，在独立保存点写 `attempts + 1`、`last_error` 和 60 秒后的 `due_at`。退避写入也失败时只记日志。阶段 A 和阶段 B 各自最多读取 100 条，单次共至多 200 条候选。

迁移相对 main 仅增加两张队列表及其索引。待发送队列不回填。首次创建 watch 表时，在同一个迁移事务中一次性回填当时全部 `status='held'` 且不在待发送队列的记录，包含日志发送状态为 NULL、hold 发送状态为 `reserved` 或 `dispatched` 的历史记录。watch 表存在时不再回填，失败回滚后可完整重试。回填及超时转待核均保留原始金额和流水。历史创建时间无法解析或加 15 分钟溢出时，watch 保留该行、写入错误并将到期时间设为 UTC 最大时间，等待人工核查。新发送遇到同类非法时间会失败并沿发送前失败路径收尾。

代码已移除游标表定义、held 分页查询和本分支新增的两个扫描索引定义。已创建的开发库遗留结构不再被运行代码引用，迁移不会删除 main 的任何表、列或索引。

管理员零金额处理待核 hold 时，在同一事务中复用恢复的双方未发送、未完成及过期检查和日志收尾，产生一条释放流水并删除队列项。人工先处理会保留人工审计，恢复先处理后的人工调用沿用幂等返回，两种顺序最终日志、配额、冻结及释放流水一致。人工非零核账和不满足安全收尾条件的零金额核账沿用原计费行为并出队。

## Key policy

key 可通过 `Authorization: Bearer` 或 `X-API-Key` 提交。每次调用依次验证账号、key、scope、每分钟限制与 UTC 日配额。撤销 key 后立即失效。

上游认证值来自路由记录中的环境变量名称，例如 `ASTRBOT_API_KEY`；数据库只保存名称，不保存秘密本身。

## Logging

使用日志保留公共路径、耗时、状态码、IP 哈希与截断后的预览。带 `token`、`password`、`secret`、`key`、`cookie` 或授权字段的数据会被掩码。对高度敏感的 API，建议完全关闭 body preview，而不是只依赖关键词脱敏。

等待期间被删除的路由以空 `route_id` 保留原 public_path、请求 ID 与拒绝原因。日志写入失败会产生包含请求 ID 的错误记录。

开发者与管理员用量分析页的图表、统计卡及明细共同使用 `chat.completions`、`responses` 协议范围。`usage_analytics(protocols=...)` 支持显式范围，省略时继续统计所有协议，`/api/*/usage/analytics` 与 `/api/developer/usage` 保持原有范围。


## Catalog administration

管理员在 `/admin/routes` 新增、启停、编辑、复制和删除公开路由：

```text
GET/POST /admin/routes/{id}/edit
POST     /admin/routes/{id}/copy
POST     /admin/routes/{id}/delete
POST     /admin/routes/{id}/toggle
POST     /apis/{id}/edit
POST     /apis/{id}/toggle
POST     /apis/{id}/endpoints/{endpoint_id}/edit
POST     /apis/{id}/endpoints/{endpoint_id}/delete
```

新增与编辑共用校验：`public_path` 按 `/` 分段，仅允许 `[a-z0-9._~-]`，不得含 `..` 或单独 `.` 段，必须唯一；`target_path` 必须以 `/` 开头且无 scheme、host、反斜线和控制字符；HTTP 方法必须来自 `ALLOWED_METHODS`；非空 scope 必须形如 `name:action`，空 scope 表示无额外 scope 要求；目标 API 必须存在，关联端点必须属于该 API。上游地址复用 HTTPS/无用户信息校验，运行时仍逐次检查公网 DNS 并钉定地址。

复制生成唯一公开路径且默认停用。任何 `api_usage_logs.route_id` 引用都会阻止硬删除（409，提示改为停用）；已有日志与账本不被清理。列表显示近 7 天调用数与最后一次调用时间。删除端点会解除可选关联，公开路由保留固定方法和路径。种子目录只初始化一次，重复启动或查看 `/secrets` 不会覆盖编辑或恢复已删除条目。管理员提交非法 key 状态返回 400，原状态保持。

## Session playground

浏览器会话保存在 `user_sessions`，cookie 为 32 随机字节的 URL-safe 令牌，数据库仅存 SHA-256；有效期 12 小时，最近活动最多每 5 分钟刷新。`/account` 支持改密、列设备与撤销。登出撤销当前设备，改密撤销其他设备，重置密码及管理员暂停/停用撤销全部设备；旧签名 cookie 直接失效。

`GET/POST /forgot-password` 按邮箱与来源 IP 限流，响应不暴露账户是否存在或 SMTP 是否配置；新增 `send_password_reset_email` 复用现有 SMTP。`GET/POST /reset-password?token=…` 使用哈希存储、60 分钟有效的一次性令牌，GET 不消费，POST 在同一事务中改密、消费令牌并撤销会话。重置页面设置 `Cache-Control: no-store` / `Referrer-Policy: no-referrer`。

登录开发者可使用 `POST /developer/playground/run`（必须 `Content-Type: application/json`）：

```json
{"key_id": 123, "endpoint": "responses", "payload": {"model": "gpt-6-luna", "input": "你好", "stream": true}}
```

仅允许 `chat.completions` / `responses`，key 必须属于当前用户且有效。执行器与 `/v1` 完全共用模型授权、scope、RPM、日/月配额、计价、积分预留、JSON/SSE 转发、结算及日志。预留事务再次校验会话和归属；日志 `public_path` 为 `developer/playground/run/{endpoint}`。`GET /developer/playground/models?key_id=123` 返回上游可用、key 授权且可计费的文本模型。Origin 校验覆盖这些浏览器 POST。

## Usage analytics

`GET /api/developer/usage/analytics?days=14` 按用户隔离；`GET /api/admin/usage/analytics?days=14` 需要管理员权限，汇总全站。`days` 仅支持 7、14、30，响应均为 `no-store`。

返回 `timezone`、`start_date`、`end_date`、`totals`、补零的 `daily`，以及 `by_model` / `by_key` 前 10 和“其他”。每项含 `requests`、`succeeded`、`failed`、`in_flight`、`input_tokens`、`output_tokens` 与精确十进制字符串 `credits`。按请求开始时间（旧记录退回 `created_at`）的 UTC 日归组；积分来自对应 `credit_holds.actual_units` 的已结算行，不计 held/released/reconciliation_required，成功与失败产生的实际扣费均计入。概览、开发者/管理员用量页提供 14 天 `analytics` 模板上下文。

## Image admission and reservation

`GATEWAY_IMAGE_INPUT` 默认 `false`。启用后只允许 `app/model_capabilities.json` 显式列出的已计价文本模型输入图片，不包含 `gpt-image-*` / `*codex-spark*`。公开目录和 `/models` 的模型数据增加 `vision: bool`，价格表和版本不变。

允许的形状：

```json
{"model":"gpt-6-luna","messages":[{"role":"user","content":[{"type":"text","text":"描述图片"},{"type":"image_url","image_url":{"url":"https://example.com/image.png","detail":"auto"}}]}]}
```

Responses 使用 `input[].content[]` 中的 `{"type":"input_image","image_url":"https://example.com/image.png","detail":"auto"}`。`detail` 可省略，允许 `auto` / `low` / `high`。URL 仅 HTTPS 或 PNG/JPEG/WebP/GIF base64 data URL；最多 16 张，单张 data 解码后最多 8 MiB，总请求体仍为 20 MiB。网关不下载 URL；文件、音频、内置工具和其他非文本形状仍拒绝。

预留输入上界 = 仅替换图像 data URL 后的请求字节数 + 4096 + `images * IMAGE_INPUT_TOKEN_CEILING`（8000）。正文内的 data URL 文本不会被替换。该上界参与长上下文判断，按输入价（含既有 cache-write 保守上界）预留，快照保存 `images` / `image_token_ceiling`。结算仍依赖上游真实 token 和缓存拆分，图像实际费用超出预留时转人工待核（即使尚有可用余额），异常/余额不足也沿用既有待核；既有纯文本超额结算策略保持不变，禁止静默少扣和透支。
