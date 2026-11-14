# march7th api

面向个人、小团队与社区的通用自托管模型网关。一个 OpenAI 兼容入口，配合账户、密钥权限、精确积分账本与可追溯请求回执。界面采用服务端 Jinja、原生 JavaScript 和严格 CSP，沿用“模型线路 / 换乘枢纽”的交互语言。

支持 Chat Completions、Responses 的 JSON / SSE、调用方自行执行的 function / custom tools，以及可选的图像输入和文生图。费用只根据可靠用量结算；缺失、矛盾或超过允许预算的回执进入人工核账。项目不提供上游账户或免费模型额度。

## Docker 快速开始

需要 Docker Compose。默认部署拥有自己的网络，不依赖本站其他项目、外部 Docker 网络或固定容器名。

```sh
git clone https://github.com/March7thTeam/march7th-api.git
cd march7th-api
cp .env.example .env
docker compose build
# 交互生成管理员密码摘要和随机 SESSION_SECRET，写回宿主机 .env。
docker compose run --rm --no-deps -v "$PWD/.env:/app/.env" march7th-api python -m scripts.set_admin_password
# 编辑 .env 中的公开地址、运营者信息和上游连接后启动。
docker compose up -d
curl --fail http://localhost:8790/ready
```

打开 `http://localhost:8790`，用刚刚配置的管理员登录。创建用户后可自助签发 API 密钥；管理员通过“积分运营”加分或发行兑换码。未配置上游时账户、后台和 `/ready` 仍可工作，模型页会如实显示未配置，不向任何本站集成发请求。默认端口只绑定宿主机回环地址；通过自己的 HTTPS 反向代理公开服务。

`SESSION_SECRET` 至少 32 字符。不要将 `.env`、数据库或 API 密钥提交到代码库。反向代理后按真实代理地址设置 `TRUSTED_PROXY_PEERS`；不要随意信任整个公网。应用不开放自动 OpenAPI，静态 `/llms.txt` 提供入口说明。

## 配置

| 变量 | 用途 / 默认 |
| --- | --- |
| `APP_NAME` / `APP_BASE_URL` | 品牌名 `march7th api` / `http://localhost:8790` |
| `OPERATOR_NAME` / `OPERATOR_URL` | 运营者显示名与可选网址 |
| `PUBLIC_DESCRIPTION` | 首页与默认元描述文案 |
| `FOOTER_LINKS` | 链接数组，例如 `[{"label":"主页","url":"https://example.com"}]` |
| `API_CATALOG_ENABLED` | 默认 `true`，控制可选业务 API 目录；`false` 隐藏目录导航、页面、公开目录 JSON 与业务目录网关并返回 404。AI `/v1` 保持独立 |
| `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY` | OpenAI 兼容上游，地址不含 `/v1`；密钥仅保存在服务端 |
| `ADMIN_USERNAME` / `ADMIN_EMAIL` | 初始化管理员，默认 `admin` / `admin@example.com` |
| `DATABASE_PATH` | 默认 `/app/data/luomoapi.db`；保留历史文件名以兼容既有部署 |
| `SECURITY_CONTACT` | 安全报告邮箱；新部署请配置自己的联系地址 |
| `USD_TO_CNY` / `BILLING_MULTIPLIER` | **仅首次价格发行种子**，默认 `7.2` / `1`；后续通过价格 CLI 导入并启用版本 |
| `MODEL_VERIFICATION_PATH` | 可选的本部署模型实测记录 JSON 路径。默认不加载项目历史实测文件 |

`CODEXPROXY_BASE_URL` / `CODEXPROXY_API_KEY` 继续兼容；设置 `UPSTREAM_*` 后以规范变量为准，包括显式空值。旧集成变量继续支持，但 AstrBot、LuomoCore 等地址默认留空，只有显式配置时才建立相应目录种子。`APP_BASE_URL`、运营者网址与页脚网址必须为没有凭据、查询参数、片段的 HTTP(S) 绝对地址。配置修改需要重启。

SMTP 未配置时邮件功能不可用，但用户可注册并调用。支付、图片输入、图像生成、原生 Responses 都是独立可选能力；详见 [.env.example](.env.example)、[网关契约](docs/GATEWAY.md)、[图像生成](docs/IMAGE_GENERATION.md)。原生 Responses 和 Images API 的官方服务端凭据与通用上游凭据相互独立。

## 可运营计价

[价格版本与运营](docs/SELF_HOSTING.md#价格发行)包含完整命令。文本与图像 JSON 只用于新数据库的首次种子，运行中使用数据库当前发行版本。每次冻结保存价格卡、发行版本、来源、汇率与倍率；之后调价不改变旧请求的结算依据。

积分最小单位是百万分之一，账务使用整数与高精度 Decimal。人民币充值合同固定为 **¥1 = 100 积分**，已有支付订单、退款保持原合同。模型费用换算为 `美元单价 × USD_TO_CNY × 100 × BILLING_MULTIPLIER`；管理员按美元发放积分使用基础汇率 `USD_TO_CNY × 100`，不加模型倍率。页面会公开本次发行的汇率、倍率和单价。

密钥所有者可设总积分上限、到期日、RPM 与模型子集。用户限制独立叠加管理员限制，不能移除管理员设定；预留与结算都在同一数据库写事务中检查已结算、在途与待核金额。钱包余额不会透支。可信费用超出有效密钥上限会留待核，管理员需先明确调整限制才能核账，不静默截断或多扣。

## 客户端接入与请求排查

客户端地址填写 `https://你的域名/v1`，认证使用 `Authorization: Bearer YOUR_API_KEY`。支持 Python / JavaScript OpenAI SDK 和兼容客户端。在线试用提供页内多轮、输出上限、推理强度、温度与占位密钥 cURL；对话只留在当前页面内存。手动工具调用可通过 API 使用，网页不会执行工具。

每次响应保留 `X-Request-ID`。用户只能查看自己的回执，管理员可看全站；用量页的详情链接使用内部记录编号，避免调用方重复请求 ID 导致历史详情串行。`GET /api/developer/requests?request_id=...` 需要登录会话，支持含斜杠等分隔符的 1–128 字符请求 ID（参数需要 URL 编码），默认取当前可见范围内最新一条；可附 `&usage_id=123` 精确匹配。旧路径 `/api/developer/requests/{request_id}` 保持兼容。API Key 本身不能读取网页会话回执。筛选与 CSV 支持请求 ID、模型、协议、密钥、计费状态和 UTC 日期；CSV 最多导出 10,000 条并防公式注入。

[客户端示例](docs/CLIENTS.md) · [错误码](docs/ERROR_CODES.md) · [升级、迁移与回退](docs/SELF_HOSTING.md) · [更新日志](CHANGELOG.md) · [路线图](ROADMAP.md)

## 本地开发与测试

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -c requirements.lock
cp .env.example .env
.venv/bin/python -m scripts.set_admin_password
# 本地数据库路径应覆盖容器默认值。
DATABASE_PATH=./data/local.db .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8790
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q app scripts tests
```

测试使用临时数据库和假上游，不代表真实上游或支付端到端通过。公开状态仅探测应用、数据库和模型目录，不代表生成成功或长期可用率。运营者应针对自己的上游验证模型和工具，不能把仓库内历史实测记录当作新部署证据。
