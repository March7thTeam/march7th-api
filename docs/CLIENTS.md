# 客户端接入

Base URL 使用部署地址加 `/v1`，不要重复附 `/v1`。密钥放在服务端或可信桌面客户端；网页公开 JavaScript 不应携带原始密钥。

```sh
export MARCH7TH_API_KEY='YOUR_API_KEY'
curl https://api.example.com/v1/chat/completions \
  -H "Authorization: Bearer $MARCH7TH_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"YOUR_AUTHORIZED_MODEL","messages":[{"role":"user","content":"你好"}],"max_completion_tokens":512}'
```

```python
import os
from openai import OpenAI
client = OpenAI(base_url="https://api.example.com/v1", api_key=os.environ["MARCH7TH_API_KEY"])
reply = client.responses.create(model="YOUR_AUTHORIZED_MODEL", input="你好", max_output_tokens=512)
print(reply.output_text)
```

```js
import OpenAI from 'openai';
const client = new OpenAI({baseURL:'https://api.example.com/v1', apiKey:process.env.MARCH7TH_API_KEY});
const reply = await client.chat.completions.create({
  model:'YOUR_AUTHORIZED_MODEL', messages:[{role:'user',content:'你好'}], max_completion_tokens:512,
});
console.log(reply.choices[0].message.content);
```

Cherry Studio、AIRI 等选择 OpenAI Compatible，并填写相同地址、密钥和完整模型 ID。是否支持某个模型参数取决于当前上游；温度、推理强度并非每个模型都接受。

多轮对话需把此前消息显式重发，历史上下文也计输入费用。Responses 的服务端会话引用、后台执行与内置托管工具不在当前计费契约中。function/custom tools 由客户端手动执行并提交工具结果，网关不会运行工具；网页试用当前仅提供文本/图像输入，多轮工具编辑器在路线图中。

SSE 需完整消费结束事件与用量回执。调用取消、连接断开或超时不表示上游未开始计费；先查回执再重试。`X-Request-ID` 是关联编号，不提供生成请求幂等性。
