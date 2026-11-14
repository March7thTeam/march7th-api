# 部署、价格与数据库运维

## 独立部署

默认 Compose 无外部网络与固定容器名。端口绑定通过 `BIND_ADDRESS` / `APP_PORT` 调整，应用容器内固定 8790。数据库、账本与持久化队列位于 `./data`。密钥预算依赖日志与账本同库的原子事务，此版本不拆日志数据库。

反向代理必须保留 Host，正确传递协议并只信任实际代理 IP。`/health` 只说明应用存活；`/ready` 检查必需数据库表与会话密钥，未配上游不阻止本地管理。`/status` 说明上游配置和目录探测范围。没有上游凭据时不会主动访问种子地址。

`API_CATALOG_ENABLED=false` 用于只运营模型网关：目录管理、测试器、目录 JSON 和 `/api/public/v1/*` 返回 404，目录相关文案与导航隐藏；既有目录数据保留，可重新启用。AI 的 `/models` 页面与 `/v1/models` 是模型目录，继续可用。

## 价格发行

首次迁移导入仓库 JSON，快照带独立的 `price_release` 和单价来源版本。后续 JSON 文件编辑或环境变量变化不会替换数据库当前费率。CLI 在可信服务器上运行，不通过公开 HTTP 接口暴露运营权限。

```sh
# 使用宿主机 Python；容器操作可用 docker compose exec march7th-api python -m scripts.prices ...
python -m scripts.prices seed > release.json
# 编辑发行 version 为全新 ID，按需调整 text/image.models、usd_to_cny、billing_multiplier。
python -m scripts.prices import release.json
python -m scripts.prices history
python -m scripts.prices activate YOUR_NEW_VERSION
```

容器导入本地文件时，先将文件复制到容器，例如 `docker compose cp release.json march7th-api:/tmp/release.json`，再执行 `docker compose exec march7th-api python -m scripts.prices import /tmp/release.json`。不要把上游凭据放入价格 JSON。

CLI 拒绝浮点费率、非有限值、非法模型卡、无来源链接与不合理数值。价格版本和启用历史不可更新或删除。同 ID 同内容导入幂等；同 ID 不同内容拒绝。导入不会自动启用；启用指针与审计历史在 `BEGIN IMMEDIATE` 事务中写入。可启用旧发行作为价格回退。正在执行的请求继续按自己的完整快照结算。

文本与图像都使用当前发行的汇率和倍率，估价器由服务端当前价格填充。页面估价仅作参考，实际冻结和扣费使用精确算术。支付人民币兑换恒定 100 积分/元；美元人工发放使用当前基础汇率，不含模型收费倍率。没有可靠用量时保留冻结等待人工核账；不自动按文本长度或“小额费用”猜扣。

手动核账也检查当前密钥预算，不能绕过用户上限。实际费用高于上限时，管理员需先与所有者核对并明确调整限制，再处理待核记录。所有发放、扣费和核账保留操作人和理由。

## 升级与迁移

1. 停止接收新生成请求，等待在途完成；对于未知状态保留记录，勿删除冻结或队列。
2. 停止服务：`docker compose stop`。
3. 完整备份 `.env` 和 `data`，并限制备份访问权限。SQLite 使用 WAL 时备份数据库及同目录文件；停服后复制整个目录最稳妥。不要只复制一个正在写入的 `.db` 文件。
4. 更新源码或镜像，执行 `docker compose build && docker compose up -d`。
5. 检查 `/ready`，运行 `python -m scripts.prices history`，核对价格当前版本、旧钱包余额、待核冻结和旧请求详情。

新增 schema 通过 `app.migrations` 版本入口执行。旧表的幂等兼容初始化仍保留，新增迁移只追加；迁移与版本记录在同一事务提交。版本 1 为密钥总额，版本 2 为所有者限制叠加，版本 3 为不可变价格发行。旧用量不补扣，旧快照继续使用原 `credits_per_usd`。迁移失败不会写成功标记，应修复原因后重启重试。

## 回退与恢复

费率回退使用 `prices activate OLD_VERSION`，不要修改旧发行内容。代码/数据库回退需要停服并同时恢复配套旧代码、完整数据库与配置备份；请勿在旧程序上继续写入新 schema 后声称完成降级。备份之后产生的支付或请求必须先对账，不能重复发放或丢弃已知费用。

重启恢复依赖持久化派发边界：确认未发送的请求可释放冻结；已经提交发送边界但结果未知的请求保持待核，不自动重发付费生成。无论 HTTP 连接取消还是客户端重试，都应先按回执核对费用。`X-Request-ID` 用于关联，不是付费生成幂等键；重复发送可能产生多笔费用。

多实例共享 SQLite 需要同一可正确锁定的本地文件系统；此版本未承诺跨主机共享网络盘或高可用。先使用单实例，后续多上游与存储扩展见 [ROADMAP](../ROADMAP.md)。
