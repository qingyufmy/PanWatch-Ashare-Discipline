# 根因与替代假设

## 已由冻结数据库与源码确认

1. 旧 Agent 建议池把 118 条 REDUCE/EXIT 方向写入 SignalJournal，全部停在
   `REVIEW_REQUIRED`。对应 Evidence 没有可用的独立模型、行情时刻与技术价位
   引用。`record_position_decision` 只给获批且有新鲜 Level、可信 Truth 的动作
   生成通知；审核方向不会入队。当天通知账本只有盘前恢复与盘后复盘两条。
2. 10:00 和 13:30 批量模型各给 10 条 HOLD；两个批次的市场上下文为
   `RISK_OFF`。这些 HOLD 是另一生产者的当前决策，旧建议与新决策没有统一
   的冲突解释事件。不能把原始卖出提案视为已获批准。
3. 30 条模型生成信号把调度传入的时刻写入 `generated_at` 和 Policy
   `approved_at`。例如 10:00 信号链接的模型 10:00:05 开始、10:01:37
   结束，批准时间却为 10:00:00。根因位于 `daily_workflow.py` 向
   `record_signal`、`evaluate_signal` 和 `record_position_decision` 传入槽时间。

## 尚不能断言

- 未记录 ModelRun 引用不等于旧 Agent 未调用模型；需要检查其私有调用日志。
- 没有通知记录不等于飞书传输故障；断点发生在资格与入队之前。
- 未见用户执行登记不等于券商账户没有交易。
- 不能从模拟收益推断应卖出数量或策略收益。

## 本次修复方向

模型后的真实时钟修复新信号因果链。风险观察单独留痕，并在独立
Shadow Outbox 写入 `SUPPRESSED/SHADOW_ONLY`，不把审核提案提升为
可执行动作，也不向真实 Lark 重发历史指令。是否开放真实风险提醒，
等待下一交易日 Shadow 样本和消息审阅。
