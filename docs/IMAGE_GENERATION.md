# 图像生成

接口：`POST /v1/images/generations`，Content-Type 为 `application/json`。
服务端以 `GATEWAY_IMAGE_GENERATION=true` 和 `GATEWAY_IMAGE_API_KEY` 开启。
固定直连 `https://api.openai.com/v1/images/generations`；没有自定义上游 URL。
文本 CodexProxy、文本价格版本与图像输入开关保持独立。

需要精确权限 `images:generate` 和对应模型白名单。旧 `codexproxy:use` 私有权限
不隐式授予此新接口。新自助密钥默认包含图片模型；旧密钥需重新签发或由管理员
显式更新，绝不批量扩大。读取目录仍需 `models:read`。

| 字段 | 取值 |
| --- | --- |
| model | 必填：gpt-image-2 / gpt-image-1.5 |
| prompt | 必填：非空文本，最多 32,000 字符 |
| quality | low / medium / high；省略或 auto 归一 medium |
| size | 1024x1024 / 1024x1536 / 1536x1024；省略或 auto 归一1024x1024 |
| n | 只能为整数1，省略为1 |
| output_format | 只能png，省略为png |
| stream | 只能false，省略为false |

其他字段全部400，包括response_format、background、moderation、tools、
partial_images、用户指定的URL等。不支持edits、批量与图片SSE。

```sh
curl "$LUOMO_BASE_URL/v1/images/generations" \
  -H "Authorization: Bearer $LUOMO_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-image-2","prompt":"一列穿过星空的列车","quality":"low","size":"1024x1024","n":1}'
```

```python
import os
import base64
from openai import OpenAI

client = OpenAI(base_url=os.environ["LUOMO_BASE_URL"] + "/v1",
                api_key=os.environ["LUOMO_API_KEY"])
result = client.images.generate(model="gpt-image-2", prompt="一列穿过星空的列车",
                               quality="low", size="1024x1024", n=1,
                               output_format="png")
with open("generated.png", "wb") as image:
    image.write(base64.b64decode(result.data[0].b64_json))
```

响应为一张有效PNG的`data[0].b64_json`与官方分模态`usage`。网关实际有界解压
原生8-bit RGB/RGBA、非交错PNG，核对请求尺寸、精确扫描行长度和合法过滤器，
损坏压缩流、非法IHDR、尺寸不符或解压量超限不能作为成功图片扣费。响应头含
`X-Request-ID`；浏览器试用只以登录会话选自己的密钥，无需发送密钥原文。
图片以安全的PNG数据地址预览，通过同源HTTP表单下载；不会放进用量预览或日志。
登录后的`POST /developer/images/download`仅回送当前用户提交的`b64_json`和
`size`对应PNG。表单上限20MiB，服务器复核base64、尺寸和有界PNG解码后，以
固定安全文件名返回image/png附件并no-store。此下载不读取URL或文件、不调用
上游、不缓存图片，也不产生用量或积分账本记录。

## 计费与失败恢复

[官方 Standard 费率](https://developers.openai.com/api/docs/pricing#image-generation)，
单位美元/百万token：

| 模型 | 文本输入 | 图片输入 | 图片输出 | 文本输出 |
| --- | ---: | ---: | ---: | ---: |
| gpt-image-2 | 5 | 8 | 30 | 不支持 |
| gpt-image-1.5 | 5 | 8 | 32 | 10 |

纯文生图必须有input image_tokens=0。按$1=720积分换算；Decimal最终向上取整到
百万分之一积分。本接口不应用缓存折扣；明确出现未知缓存/模态字段会待核。
两种模型的输入text/image明细、总量必须一致；1.5还必须报告输出text/image明细。
不能将其文本输出token按图片费率计算。缺失明细、矛盾、非整数、负数、未知模态、
响应模型不符，都会保留冻结并待核，不猜测费用。

冻结预算使用prompt的UTF-8字节数+4096文本输入token；方形图片输出low/medium/high
分别预留512/2048/8192token。两个矩形尺寸按1.5倍面积预留768/3072/12288token。
gpt-image-1.5额外预留4096文本输出token。这是保守准入预算，
不是官方固定每张图价或最终扣费。实际usage超出任一上界则待核，保留规范化
分模态用量与精确实际费用，不能使用余额自动补超额。有效PNG和可计价回执仍以
200交付图片，响应头`X-Credit-Status: reconciliation_required`表示费用待核；
单项超界但总价未超冻结也保留本笔冻结。总价超过冻结的已知差额纳入钱包风险，
后续生成返回409，重复或冲突回执不能抹掉该费用。有效结算释放剩余冻结；
明确的生成前拒绝释放本笔冻结。

网关在SQLite短事务中保留预算与次数，再标记dispatch，随后网络请求，最后按
可靠receipt幂等结算。所有密钥共用账户钱包，并发不能重复花费。管理员免计费
仍检查请求形状、权限、模型、配额和回执，只不冻结扣款。超时、断连与无用量的
已发送请求均待核；未发送的失败释放。安全恢复复用已有hold/watch/dispatch队列。
对同一usage重复结算不会重复扣费；用户重试HTTP请求是新调用，可能再次生成收费，
发生待核请提供请求ID处理，勿盲目重试。

仅100MB有界读取上游响应，不转发重定向、不处理上游cookie、不转发用户自定义
供应商头；API密钥保留在服务端。关闭生成开关后路由返回404，文本接口继续运行。配置启用图片生成后，两个已计价
图像模型独立合入目录；CodexProxy目录缺图像、为空或故障均不隐藏图片试用选项。
文本上游状态保留实际探测结果，图片目录可见不表示已执行付费生成探测。
