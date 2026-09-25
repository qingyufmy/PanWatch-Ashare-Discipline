# 2026-09-23 消息与技术价位基线

读取时间：2026-09-23 16:40 CST。隔离开发基线 `5c82d00`，运行版数据来自 `D:\盯盘\PanWatch\data\panwatch.db` 的只读 SQLite 连接与已公开的 `daily_archive/2026-09-23/manifest.json`。本文件不含账户、持仓数量、消息正文或凭据。附件代码基线 `ac88635` 不是当前本机 HEAD。

## 自然日证据

- 公开归档报告：267 次工作流运行、539 条 Signal、5929 条 Policy 规则裁决、0 条 ActionableSignal、11 只分钟行情覆盖、4 条 Issue，P10 门禁 `FAIL_CLOSED`。其中 `HARD_RISK` 206 次 SUCCEEDED、1 次 REVIEW、3 次 MISSED；`FEATURE_REFRESH` 28 次 SUCCEEDED、15 次 REVIEW。EOD Truth 为 `REVIEW_ONLY`。公开归档不含消息正文。
- 只读私有库：迁移版本 134；盘中 Agent 成功运行 82 次，其中 `notify_sent` 为真的运行 19 次；盘前 Agent 与日报 Agent 各 1 次发送成功。`PREMARKET_PLAN` 工作流的通知状态为 SENT 1 次。当天建议池新增 528 行，分布为 alert 218、hold 86、reduce 121、sell 7、watch 96。以上是数据库字段计数，不等于独立飞书消息、用户已读或成交。
- 实际送达消息条数、去重率、冲突率、无证据价位率、无执行状态率、投递未知数：**null / NOT_OBSERVED**。旧实现没有逐条正文与投递账本，现有 AgentRun 布尔值无法还原这些指标。不能以 0 代替。

## 发声链

| 入口 | 提案与审查 | 发布路径 | 当前缺口 |
|---|---|---|---|
| 盘中旧 Agent | `save_suggestion` → SignalJournal → PolicyGate | `IntradayMonitorAgent.run/run_single` → NotifierManager | 原始建议可在保存失败或 Policy 待复核后发声；批量路径未执行事件门控 |
| 盘前旧 Agent、日报及图表 Agent | 各 Agent 分析与建议 | `base.py` / `chart_analyst.py` → NotifierManager | 与纪律工作流并行，缺统一持仓决策 |
| 纪律工作流 | DailyPlan → SignalJournal → PolicyGate；硬风险由确定性扫描产生 | `daily_workflow.py` → `send_portfolio_notice` → NotifierManager | 盘前正文缺动作解释；风险按红色集合去重 |
| 纸面交易与价格提醒 | 独立纸面/行情域 | 自己的 NotifierManager | 未纳入持仓动作视图，需按来源区分 |

当前 `MONITOR_STARTS` 下午截止 14:30，15:00 前只有单次 `CLOSING_RISK`；这不满足收盘前持续保护。下阶段修复调度范围并用固定时间样本验证。

## 证据限制

用户声明的总资产与可卖数量，均未获券商流水核实。现行 Truth 为 `REVIEW_ONLY`，不得据此把建议标记为已批准或已执行。隔离回放不触发付费模型、真实飞书或券商订单。
