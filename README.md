# march7th api

march7th api 是洛墨站的自托管 API 网关，线上运行于 <https://api.march7th.cn>。它同时做两件事：

- **模型换乘**：对外提供 OpenAI 兼容的 `/v1` 入口，按 key 授权模型、限速并用积分计费；
- **API 目录**：管理员登记内部上游与固定路由，开发者申请带 scope 的 key，经 `/api/public/v1/...` 受控调用。

站点按“夜行线换乘枢纽”设计：模型是线路，状态页是发车表，API key 是乘车券，积分钱包是 IC 卡。控制台全中文。

## 功能一览

| 模块 | 内容 |
| --- | --- |
| 模型网关 | `GET /v1/models`、`POST /v1/chat/completions`、`POST /v1/responses`，支持 JSON 与 SSE 流式；其他 `/v1` 路径一律拒绝 |
| 积分计费 | 先预留后结算的整数积分账本；价格表带版本（`app/credit_prices.json`）；兑换码；可选 Stripe Checkout 充值 |
| API 目录 | 上游 API、端点、公开路由登记；key 申请与审批；每 key 的 scope、每分钟限制与日配额 |
| 开发者控制台 | 自助创建模型 key、用量与日志、在线调试台（playground）、积分钱包 |
| 管理后台 | 用户、key、路由、审批、积分发放与冻结处理、模型用量、日志 |
| 公开页面 | 首页、模型线路、实时发车表 `/status`、价格 `/pricing`、文档 `/docs`、安全说明 `/security` |

## 快速开始

需要 Docker 与 Compose。示例 Compose 会接入外部网络 `luomocore_default`，第一次运行前先创建：

```bash
docker network create luomocore_default
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

把生成的随机值写入 `.env` 的 `SESSION_SECRET`（至少 32 个字符，否则 `/ready` 返回 503）。然后设置管理员密码并启动：

```bash
mkdir -p data
docker compose build
docker compose run --rm luomoapi-hub python scripts/set_admin_password.py
docker compose up -d
curl http://127.0.0.1:8790/ready
```

服务只映射到 `127.0.0.1:8790`。生产环境放在 HTTPS 反向代理或 Tunnel 之后，并按实际代理链设置 `TRUSTED_PROXY_PEERS`（见下文）。

## 调用模型

OpenAI 官方 SDK 只需改 `base_url`：

```python
import os
from openai import OpenAI

client = OpenAI(base_url="https://api.march7th.cn/v1", api_key=os.environ["LUOMO_API_KEY"])
response = client.responses.create(model="gpt-6-luna", input="你好")
print(response.output_text)
```

```bash
curl https://api.march7th.cn/v1/chat/completions \
  -H "Authorization: Bearer $LUOMO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-6-luna", "messages": [{"role": "user", "content": "你好"}]}'
```

上游地址由 `CODEXPROXY_BASE_URL` / `CODEXPROXY_API_KEY` 指定，可以是任意 OpenAI 兼容服务。`/api/public/ai-catalog` 返回公开的模型目录。

### Key 类型

| 类型 | 来源 | 说明 |
| --- | --- | --- |
| 自助模型 key | 开发者控制台 | scope 为 `models:read`、`chat:completions`、`responses:create`；按积分扣费。管理员可在 `/admin/ai-keys/{id}` 限定模型、RPM、日/月额度和到期时间 |
| 目录 key | 申请后由管理员审批 | 用于 `/api/public/v1/...` 的路由调用，按 scope、速率和日配额放行 |
| 管理员免计量 key | `scripts/issue_admin_unmetered_key.py` | 需要精确 scope `admin:unmetered`，且所有者必须是在职管理员；每次调用都实时复核 |

Key 只在创建时显示一次，数据库只保存哈希。支持 `Authorization: Bearer` 与 `X-API-Key` 两种提交方式。

### 积分规则

- 1 元 = 100 积分，1 美元 = 720 积分；模型单价取自 OpenAI 官方 Standard 价格，按版本记录在 `app/credit_prices.json`。
- 每次调用先在短事务里预留积分，连接关闭后才访问上游；完成后按上游返回的真实 token 用量结算，未用完的预留退回钱包。
- 拿不到可靠用量、实际费用超出可用余额或回执冲突时，记录转为“待核”，由管理员在 `/admin/credits` 人工处理并留审计记录；系统不会自动透支。
- 只有经过签名校验的 Stripe 回执才会入账；`CREDIT_PAYMENT_PROVIDER` 留空时不开放在线支付，可用兑换码或管理员发放。

## 账户与服务端会话

登录后访问 `/account` 可查看用户名、邮箱与验证状态、角色、注册时间，修改密码及管理活动设备。会话有效期仍为 12 小时，cookie 使用 `secrets.token_urlsafe(32)`；`user_sessions` 只保存 SHA-256 摘要。最近活动最多每 5 分钟写入一次。旧签名 cookie 不再接受，升级后用户需重新登录。

- `POST /logout` 撤销当前会话；`POST /account/password` 校验当前密码及两次新密码（至少 10 字符，不得相同），成功后撤销其他会话。
- `POST /account/sessions/{id}/revoke` 退出某个自己的设备；`POST /account/sessions/revoke-others` 退出其他全部设备。
- `GET/POST /forgot-password` 按邮箱及来源 IP 原子限流；存在、不存在、限流及 SMTP 未配置时均显示同一提示。邮箱每小时最多 5 次，至少间隔 60 秒；来源每小时最多 120 次。
- `GET/POST /reset-password?token=…` 使用 60 分钟内有效的一次性令牌，数据库只保存哈希。GET 不消费令牌；成功重置会使该用户全部重置令牌失效、撤销全部会话，跳转 `/login?reset=1`。页面使用 `no-store` 与 `no-referrer`。
- 管理员将用户设为 `suspended` / `disabled` 时撤销全部会话，重新启用也不会复活旧 cookie。

数据库迁移只新增表与索引，启动自动执行；保留已有用户、key 与账本数据。`SESSION_SECRET` 仍用于自助密钥及充值表单 nonce 等签名。

## 控制台试用与用量分析

`/developer/playground` 直接选择自己名下的有效 key，无需保存或粘贴原文。服务端登录开发者接口：

```text
POST /developer/playground/run
Content-Type: application/json
{"key_id": 123, "endpoint": "chat.completions", "payload": {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "你好"}]}}
GET /developer/playground/models?key_id=123
```

`endpoint` 也支持 `responses`。试用与 `/v1` 共用权限、模型白名单、限速、配额、积分预留、上游转发、SSE 和结算执行器。用量日志路径标识为 `developer/playground/run/{endpoint}`；会话与密钥归属在预留事务中再次检查。POST 受同源 Origin 校验保护。

`GET /api/developer/usage/analytics?days=14` 返回自己的聚合；管理员使用 `GET /api/admin/usage/analytics?days=14` 获取全站聚合。`days` 仅支持 7 / 14 / 30。按 UTC 日补零，包含请求数、成功/失败/进行中、输入/输出 token 和 `credits` 十进制字符串；积分只统计 `credit_holds.status='settled'` 的 `actual_units`，不把预留或待核当作扣费。`by_model` / `by_key` 各列前 10，其余合并为“其他”。两接口均为 `Cache-Control: no-store`。开发者概览及用量页的 `analytics` 上下文提供 14 天数据，供后续服务端图表使用。

## 图像输入（默认关闭）

设置 `GATEWAY_IMAGE_INPUT=true` 后，`app/model_capabilities.json` 明确列出的已计价文本模型支持图像输入。`gpt-image-*` 与 `*codex-spark*` 不在清单中；未计价模型不能借此调用。公开目录 `/api/public/ai-catalog` 与 `/models` 的每个模型均带 `vision: bool`，表示模型能力，实际启用仍取决于环境变量。

- Chat：在 `messages[].content[]` 中使用 `{"type":"image_url","image_url":{"url":"https://example.com/image.png","detail":"auto"}}`。
- Responses：在 `input[].content[]` 中使用 `{"type":"input_image","image_url":"https://example.com/image.png","detail":"auto"}`。
- `detail` 可省略，支持 `auto` / `low` / `high`。URL 仅允许 HTTPS 或 `data:image/(png|jpeg|webp|gif);base64,…`；每请求最多 16 张，data 解码后每张最多 8 MiB，总请求体仍最多 20 MiB。网关不自行下载图片。文件、音频、内置工具等仍拒绝。
- 输入预留上界为：仅把图像字段中的 data URL 换成短占位符后的请求 UTF-8 字节数 + 4096 + 图像数 × 8000。按此合并上界判断长上下文价格，快照记录 `images` 和 `image_token_ceiling`。结算使用上游真实 token；图像请求实际费用超出预留即转人工待核，余额不足、缺失/冲突回执也进入既有待核流程，不改变价格表版本或允许透支。

## API 目录网关

```bash
curl https://api.example.com/api/public/v1/demo/status \
  -H "Authorization: Bearer <api-key>"
```

调用方不能提交目标 URL：每条公开路径在数据库中绑定一个已登记的 API、HTTPS base URL、固定方法和固定目标路径。匿名路由需要管理员逐条开启。完整路由模型见 [docs/GATEWAY.md](docs/GATEWAY.md)。

## 安全设计

- **SSRF 防护**：上游只允许 HTTPS、不含用户信息；字面 IP 与全部 DNS 解析结果都必须是公网地址，并把请求钉到已校验的地址；不跟随重定向。
- **有界传输**：请求体上限 20 MB，响应上限 100 MB；上游客户端不保存、不转发 Cookie。
- **登录限流**：15 分钟窗口内，单账号最多 8 次失败、单来源最多 40 次；每次尝试先原子占位，并发请求也无法绕过。
- **真实来源识别**：只信任 `TRUSTED_PROXY_PEERS` 中代理传来的 `X-Forwarded-For`，其余一律按直连地址处理。
- **日志脱敏**：记录路径、耗时、状态码、IP 哈希与截断预览；`token`、`password`、`secret`、`key`、`cookie` 及授权字段会被掩码。
- **页面**：CSP 无内联样式；静态资源带内容指纹，可长期缓存。

漏洞报告方式见 [SECURITY.md](SECURITY.md)。

## 配置

完整列表见 [.env.example](.env.example)。常用项：

| 变量 | 作用 |
| --- | --- |
| `SESSION_SECRET` | 表单 nonce 等服务端签名密钥，至少 32 个字符；会话 cookie 本身为不透明随机令牌 |
| `APP_BASE_URL` | 对外地址，用于邮件链接和页面展示 |
| `DATABASE_PATH` | SQLite 路径，默认 `/app/data/luomoapi.db` |
| `TRUSTED_PROXY_PEERS` | 可信反向代理地址，逗号分隔；默认 `127.0.0.1,::1,172.20.0.1` |
| `CODEXPROXY_BASE_URL` / `CODEXPROXY_API_KEY` | 模型网关的 OpenAI 兼容上游 |
| `CREDIT_PAYMENT_PROVIDER` / `CREDIT_STRIPE_*` | 在线充值，留空即关闭 |
| `GATEWAY_IMAGE_INPUT` | 默认 `false`；仅为能力清单中已计价文本模型开放图像输入 |
| `SMTP_*` | 注册邮箱验证及找回密码邮件 |
| `SECURITY_CONTACT` | `/.well-known/security.txt` 的联系方式 |

## 健康检查与公开接口

```text
GET /health                 进程存活
GET /ready                  数据库表、会话密钥检查；未就绪返回 503
GET /api/public/status      实时发车表数据
GET /api/public/ai-catalog  公开模型目录
GET /api/public/stats       公开路由统计
GET /api/public/routes      公开路由列表
GET /.well-known/security.txt
```

## 目录结构

```text
app/
  main.py                 create_app、lifespan、中间件与静态资源
  routes/                 public、auth、account、developer、admin、catalog_admin 路由
  auth.py                 服务端会话、密码哈希、权限依赖
  cliproxy_gateway.py     /v1 模型网关
  public_gateway_secure.py  目录网关与共享的 key / 配额 / 日志工具
  upstream_security.py    上游解析与地址钉定
  ai_*.py                 模型目录、key 策略、用量统计、控制台
  credit_*.py             积分账本、定价、支付、积分控制台
  image_input.py          图像形状、URL、大小与计价输入上界
  model_capabilities.json  显式模型图像输入能力清单
  login_throttle.py       登录限流
  client_ip.py            可信代理与真实来源
  web/templates/          Jinja2 模板
  web/static/             interchange.css、前端脚本、字体
scripts/                  运维脚本
tests/                    unittest 测试
docs/GATEWAY.md           路由模型说明
```

## 运维脚本

| 脚本 | 用途 |
| --- | --- |
| `scripts/init_db.py` | 初始化数据库结构 |
| `scripts/set_admin_password.py` | 交互式设置管理员密码 |
| `scripts/seed_default_apis.py` | 写入示例 API 目录 |
| `scripts/create_admin_api_key.py` | 创建管理员目录 key |
| `scripts/issue_admin_unmetered_key.py` | 签发管理员免计量模型 key，原文只写入服务器私有文件 |
| `scripts/admin_ops.py` | 受控维护操作 |
| `scripts/test_smtp.py` | 检查邮件配置 |
| `scripts/credit_browser_fixture.py` | 本地一次性浏览器测试环境，不连接真实上游与邮件 |
| `scripts/credit_candidate_probe.py` | 对隔离候选库做有界的真实上游验收 |

`data/`、`output/`、`.env` 与备份目录已在 `.gitignore` 中排除。

本地夹具启动：`.venv/bin/python scripts/credit_browser_fixture.py`，仅监听 `127.0.0.1:8793`。数据库位于项目内 `output/browser-fixture.sqlite`。提供管理员 `qa_admin`、开发者 `qa_dev`（3 把 key、跨 14 天 60 条成功/失败与多模型的已结算用量）、兼容账号 `qa_registered`、2 条公开文档路由；夹具密码均为 `FixtureOnly-2026`。路由及其假上游默认停用，邮件/支付/模型上游凭据在进程内清空，重复运行不重复加分或播种用量。夹具只用于本地。

## 开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -c requirements.lock
python -m unittest discover -s tests
```

测试使用临时数据库和假上游，不需要网络与真实密钥。CI（`.github/workflows/ci.yml`）在 Python 3.12 上执行 compileall、应用导入和完整测试。

## License

[MIT](LICENSE) © 2026 Luomo

### 图像生成

启用 `GATEWAY_IMAGE_GENERATION=true` 并设置服务端 `GATEWAY_IMAGE_API_KEY` 后，
`POST /v1/images/generations` 通过固定的官方 OpenAI Images API 支持
`gpt-image-2`、`gpt-image-1.5` 文生图，每次一张 PNG。文本请求仍使用 CodexProxy；
`GATEWAY_IMAGE_INPUT` 独立控制已有文本模型的图像输入。

新自助密钥包含 `images:generate` 和已计价图像模型，已有密钥权限保持原样。
图像调用复用原有账户、模型白名单、配额、积分冻结与失败恢复流程，价格使用
独立 `app/image_generation_prices.json` 版本。登录 `/developer/images` 可预览下载，
参数、计费与服务端 SDK 示例见 [图像生成说明](docs/IMAGE_GENERATION.md)。


### Responses 工具与输出预算

开启原生 Responses 后，`app/text_model_capabilities.json` 中22个明确型号直接走
官方 Responses，保留工具、推理和 `max_output_tokens`。同一能力表限制自动与显式
输出预算：4o-mini 不会再默认请求32768；超上限显式值会在派发前400。
工具验证与纯文本历史验证分开显示。可运行 `scripts/issue8_contract_probe.py`
执行真实契约验收，具体环境变量、隔离账本和JSON输出见 `docs/GATEWAY.md`。
