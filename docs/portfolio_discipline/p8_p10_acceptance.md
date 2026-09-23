# P8 / P10 实施与验收记录（2026-09-22）

## P8

`evaluation.py` 从冻结 JSON 读取 Evidence、Truth、Plan 和两个版本的 Proposal；逐项比较时间戳，任何案例时间之后的输入直接拒绝。离线回放不访问实时 DB、行情或模型。支持传入 Champion/Challenger 的 Prompt、Model、Policy 版本元数据，Challenger 固定为 `SHADOW_ONLY`。10 类模板覆盖正确/错误减仓、正常 ATR 误报、Thesis invalid、高位回吐、错误抄底、T+1、stale、schema error 和 FAST/DEEP 冲突。命令 `scripts/replay-portfolio-eval.py` 生成 [prompt_comparison.json](eval_2026-09-22/prompt_comparison.json) 和 [prompt_comparison.md](eval_2026-09-22/prompt_comparison.md)，本次两个版本均为当前同一版本，10/10 仅验证确定性回放契约；尚无不同 Prompt/Model 的真实 A/B 效果证据。

## P10

`review_gate.py` 按日/周/双周/月生成原始样本数、Signal/Position/Discipline/Model/Data/System 指标与 Shadow→PROD 门禁。缺样本明确 FAIL_CLOSED，指标全通过也只到 `ELIGIBLE_FOR_HUMAN_APPROVAL`。`P10_REVIEW` 于交易日 21:20 留存 `data/reviews/YYYY-MM-DD/`，周五运行 Upstream Watch，双周标记 Shadow 复核，月末交易日复评模型、数据源和阈值。`upstream_watch.py` 只读检查 PanWatch/TradingAgents GitHub HEAD、关键包版本、配置与实际报告的模型 ID，变化仅记 REVIEW，不执行升级。

本地实测 Upstream Watch 访问两个参考仓库成功，依赖版本和模型 ID 可读。2026-09-22 当晚 Acceptance Gate 为 FAIL_CLOSED：当天 P10 尚未激活，Hard Risk、Actionable、EOD 等样本缺失；不能宣称 PROD 通过。2026-09-23 的自然日运行与人工账户对账待观察。后端定向测试 7/7；完整测试结果见交付记录。

运行文件：[production_runbook.md](production_runbook.md)、[shadow_to_prod_checklist.md](shadow_to_prod_checklist.md)、[weekly_review_spec.md](weekly_review_spec.md)、[incident_playbook.md](incident_playbook.md)。
