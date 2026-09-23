# Shadow 到 PROD 门禁

重要的模型、Prompt、Policy、数据源与上游版本更改依序经过 DEV → Test → Frozen Replay → Shadow → Acceptance → 人工批准 → PROD。Challenger 只写结果，不能写生产 Prompt 状态、ActionableSignal 或委托。当前仅完成 10 类合成冻结案例回放，尚无与不同 Challenger 版本的真实双跑；不能把 10/10 用例通过解释成模型优胜。

`review_gate.acceptance_gate` 同时要求：freshness≥99.5%、critical stale actionable=0、traceability=100%、policy bypass=0、duplicate actionable=0、schema success≥99%、critical scheduler missed=0、EOD reconcile=100%、stop widening=0、FAST P95<20秒、HardRisk P95<30秒。零样本不得通过；缺自然交易日、无可靠账户真相或未完人工批准时维持 FAIL_CLOSED。即使全部数值通过，返回状态也仅是 `ELIGIBLE_FOR_HUMAN_APPROVAL`，不会自动激活 PROD。

验收人应核对 `data/reviews` 的原始样本数量与日期、`docs/portfolio_discipline/eval_2026-09-22/prompt_comparison.json` 的输入哈希、IssueLedger 未解决 P0/P1、11 只信号的追溯链、前后端版本和本地任务运行状态。2026-09-22 晚间尚不能验收 2026-09-23 自然交易日。
