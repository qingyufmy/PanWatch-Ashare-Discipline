# P7/P9 本地部署验收记录（2026-09-22）

## P7 全天工作流

- 完成内容：迁移 134；06:50 至 21:20 的固定时间槽、周五 20:30、盘中 60 秒 Hard Risk 与 5 分钟 Feature Refresh；交易日历守卫；每槽唯一运行记录、重启漏跑标记和卡住任务发现；08:50 一次模型请求覆盖全部持仓并生成版本化日计划；日计划提案进入 P3 PolicyGate，SignalEvent 关联日计划版本；14:50 审计未执行卖出、15:05 EOD 人工真相快照、21:10 次日待复核事项。前端纪律日志展示当日日计划与运行状态。盘前计划向默认 Lark 渠道一次性推送 11 只摘要，盘中 RED 风险按日期和股票集合去重提醒；发送尝试和故障可追溯，内容明确待人工核对。
- 关键设计：Hard Risk 每分钟批量读取腾讯源时间戳报价，触及止损时以新浪分钟线交叉核对；失败回退本地分钟归档和 YELLOW/REVIEW，不依赖模型；行情原始时间超 120 秒或止损缺失时 YELLOW/REVIEW；DEEP 只针对有证据的 RED/重大事件，周五全组合一次。手动/人工持仓快照不升级为券商可信真相。任何模型输出经 P3 门禁后才可能成为 ActionableSignal。
- 变更文件：`daily_workflow.py`、`api/workflow.py`、`signal_journal.py`、`api/journal.py`、`server.py`、数据库模型/迁移、`SignalJournal.tsx`。
- 迁移兼容：新增表 `daily_portfolio_plans`、`portfolio_workflow_runs`，`signal_events.daily_plan_version` 可空，旧信号保持可读。未改历史建议。
- 验证：`pytest tests/test_daily_workflow_p7.py tests/test_issue_ledger_p9.py tests/test_signal_journal_p2.py tests/test_policy_gate_p3.py tests/test_minute_features_p6.py -q`，22 通过；模拟完整交易日所有槽无重复；重启例子 06:50/07:20/07:30/08:00 四槽正确标记 MISSED；11 只批量模拟生成 11 条 REVIEW_REQUIRED 信号，均关联日计划 v1。前端 `pnpm build` 与 Vitest 43/43 通过。
- 未解决风险：尚未跨越 2026-09-23 自然交易日，无法证明关键槽 miss=0；用户声明总资产 50 万元后，已按 2026-09-22 双源核对收盘价形成 11 只持仓的版本化止损与仓位上限，详见 risk_plan_2026-09-22.md；次日新行情和账户资产仍待复核，Hard Risk 缺实时证据时为 REVIEW；盘前 07:20 和盘后 15:05 真相为用户维护数据，不是券商对账。竞价、公告与部分免费数据源仅在可得证据范围内运行，缺证据留 REVIEW；不能标成 PROD 通过。

## P9 Issue Ledger

- 完成内容：迁移 133；按 category/code/source 生成 error_signature，重复故障归并到同一 Issue 并增加 occurrence_count、按 UTC 日计数；保存最近 100 条上下文及关联 symbol/trace 等；OPEN→INVESTIGATING→FIXED→MONITORING，修复须记录 root cause、fix commit、regression test；复发自动 REGRESSION；登录后列表、详情、日报、周报 API。
- 变更文件：`issue_ledger.py`、`api/issues.py`、数据库模型/迁移、`application.py`；P7 调度、模型和分钟源错误写入此台账。
- 验证：`pytest tests/test_issue_ledger_p9.py -q`，1 通过，覆盖重复聚合、修复证据、复发和日/周统计。
- 未解决风险：历史 P0/P1 既有故障尚未逐条补 root cause/fix commit；自然交易日首次运行前无真实 Issue 趋势。不能把空台账视为零故障。

## 发布与后续

P7/P9 已合入本地运行版，P8/P10 另见 p8_p10_acceptance.md。模拟验证不能代替 2026-09-23 自然交易日验收。
