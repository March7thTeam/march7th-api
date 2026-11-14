# 错误码与费用状态

客户端先检查 HTTP 状态、JSON `error.code`（旧业务网关可能为字符串 `error`），再保留 `X-Request-ID`。SSE 已返回 200 后仍可能发送错误事件，不能仅以建立连接判断成功。

| HTTP / 代码 | 含义 | 处理 |
| --- | --- | --- |
| 400 `billing_limit_invalid` / `billing_shape_unsupported` | 输出上限无效或请求形状不支持计费 | 修改参数，避免直接重复相同请求 |
| 400 `price_unavailable` | 当前发行无可结算价格 | 选择已授权且已计价模型 |
| 401 | 密钥缺失或无效 | 检查服务端环境变量，不在报告中贴密钥 |
| 402 `insufficient_credits` | 可用积分不足以冻结预算 | 充值、减少上下文或降低显式输出上限 |
| 402 `key_credit_limit_exceeded` | 已结算及占用超过密钥上限 | 所有者检查自设限额与管理员限制 |
| 403 `key_expired` / `model_not_allowed` | 已到期或不在有效模型交集 | 修改自己的限制或联系管理员 |
| 403 同源校验错误 | 网页会话写操作来源不匹配 | 返回本站表单；不要跨站调用会话接口 |
| 404 `catalog_disabled` | 运营者关闭业务 API 目录 | 该部署仅开放模型网关入口 |
| 409 `credit_reconciliation_required` / `payment_reconciliation_required` | 存在已知超冻结费用或支付待核 | 管理员核实后继续；不立即重复支付或生成 |
| 429 `rate_limited` / `quota_exceeded` / `monthly_quota_exceeded` | 密钥频率或配额已满 | 退避重试，失败/取消也可能计入次数 |
| 502 / 504 | 上游回执异常、响应过大或超时 | 按请求 ID 核对，避免产生重复费用 |
| 503 | 必需上游凭据缺失、临时数据库/准入不可用 | 运营者检查配置；原生 Responses 不静默换上游 |

费用状态：`held` 为冻结中；`settled` 为已扣实际费用；`released` 为解冻；`reconciliation_required` 为人工核账；`admin_exempt` 是明确授权的管理员免计费。未知用量、矛盾回执、图片预算超限或密钥结算上限不足都不会按猜测金额自动扣费。

会话回执：`GET /api/developer/requests/{request_id}` 默认返回当前用户最新同 ID 记录；附 `usage_id` 可精确匹配。页面链接使用内部记录编号。普通用户只能看自己的记录，管理员可看全站，不返回提示词、回答、密钥或上游凭据。
