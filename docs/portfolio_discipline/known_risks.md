# P0 已知差距与风险

1. **真实持仓真相缺口**：`positions` 保存账户、数量、成本，但源码基线没有 `sellable_qty`、T+1 今日锁定、快照 freshness/hash/anomaly。未接入用户账户或券商只读对账，不能声称持仓准确。
2. **评级语义压缩**：`decision.RATING_ACTION_MAP` 将 Overweight→buy、Underweight→sell，虽有 `rating_raw`，兼容 action 丢失 ADD/REDUCE 与目标权重。P2 才可修复，不能在 P0 悄悄改前端/模拟盘行为。
3. **模拟盘减仓等于退出**：`paper_trading_engine._check_exits` 对 `sell` 与 `reduce` 都调用 `_close_position`，未支持部分减仓；P2 需单独改造并验证现金、数量、T+1。
4. **缺少确定性门禁**：当前建议池不是带 TTL/dedupe/evidence hash/PolicyDecision 的权威 Signal Journal。附件要求的 PolicyGate 属 P3。
5. **数据源与调度实际状态未知**：新克隆无用户实例 DB，不能证明数据库优先级、真实持仓、运行任务、数据新鲜度、20/20 覆盖或端点可用性。
6. **模型解释安全**：盘中使用宽松 JSON 解析；TradingAgents 在未知评级时兼容 action 默认为 `hold`，REVIEW 虽有单独标记，仍须在后续结构化契约中避免静默误解。

P0 只记录这些差距，不降低现有保护措施，也不修改策略参数或 Prompt。
