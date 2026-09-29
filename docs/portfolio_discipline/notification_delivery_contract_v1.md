# 持仓通知投递合同 V1（候选）

版本日期：2026-09-23。Outbox 与信号、Policy、持仓决策在同一事务提交；提交后才能请求 Lark。账本正文只存私有 SQLite，本机私有复盘包置于忽略的 `data/notification_reviews/`。

状态：`PENDING → SENDING → SENT_ACCEPTED` 仅表示 Webhook 接受；明确失败为 `FAILED_RETRYABLE`，超时或发送中重启为 `DELIVERY_UNKNOWN`，旧版本被替代为 `CANCELLED`，过期为 `EXPIRED`。失败需显式重试并复核当前决策与有效期。`DELIVERY_UNKNOWN` 不盲目重发，因为平台可能已接收；记录 Issue 并人工核对。普通 Webhook 无读取回执时 `ack_status=NOT_OBSERVED`。不能声称严格恰好一次送达。

唯一键按每持仓交易日、计划版本、决策 revision、事件原因生成，不以整组红色股票集合或每次行情价格为键。A→A+B→A 不重复发送 A，风险原因或动作升级有新键。发出前核对当前决策和过期时间；被替代或过期不发送。盘前、盘后组合摘要各自独立唯一键。`PortfolioDecision` 只记录已持仓动作，模型 REVIEW 保留于私有信号，不伪装成 HOLD。

公开日归档只输出匿名持仓关联键、动作类型、模板版本、内容 hash、投递状态、原因码、质量关联标记及统计，不输出正文、数量、成本、账户、模型原文、密钥或 Webhook。私有审计区分提案、决策变化、入队、平台接受、未知、明确失败、用户报告与券商核实；没有读取回执时已读数为 NULL。旧自然日没有完整逐条通知账本时重复率、冲突率和消息条数也为 NULL。

回滚时关闭 `portfolio_discipline_mode` 并恢复上一代码版本；先做 SQLite 在线备份，保留已写的证据账本与 Issue，不删历史数据。回滚可能恢复旧多入口通知行为，须在交易窗口前以单独验收决定。新代码、Prompt、Policy 与模板只在冻结回放、自然日 Shadow 和人工批准后进入生产生效状态。
