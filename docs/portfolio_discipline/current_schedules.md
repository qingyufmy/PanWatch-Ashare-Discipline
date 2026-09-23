# P0 当前调度

`src/modules/automation/agent_catalog.py:AGENT_SEED_SPECS` 是新库种子；实际任务由 `server.py:build_scheduler` 从实例 `agent_configs` 中选启用项，`src/modules/automation/agent_scheduler.py` 解析 schedule。下表是种子值，不能当作用户实例的实际运行时刻。

| Agent | 种子启用 | 种子 schedule | 模式 |
| --- | --- | --- | --- |
| premarket_outlook | 否 | `0 9 * * 1-5` | batch |
| intraday_monitor | 否 | `*/5 9-15 * * 1-5` | single，默认 event_only |
| daily_report | 是 | `30 15 * * 1-5` | batch |
| tradingagents | 否 | 空，按需触发 | single |
| news_digest/chart_analyst | 否 | 空，能力组件 | 不独立调度 |

`src/modules/paper_trading/paper_trading_scheduler.py` 另有扫描间隔任务、09:00 盘前通知与 15:30 日终摘要，并做交易日检查。`server.py` 还有 04:00 MCP 审计日志清理。当前没有附件要求的 06:50—21:10 全日纪律 Workflow，也没有已验证的 60 秒 20/20 Hard Risk 扫描。此阶段未启动服务或定时任务。
