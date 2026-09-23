# P1 持仓真相与计划状态验收记录

## 结论

P1 的本地数据结构、服务、迁移、读取接口和状态审计已经落地。11 只手工维护的持仓已有两版只追加快照及各三版计划。用户于 2026-09-22 明确确认“持仓数量就是可卖数量”；第二版快照逐只记录 `sellable_qty=total_qty`、`today_locked_qty=0`，并标记 `source=user_attested`。**P1 验收仍为 `FAIL_CLOSED`**：这份人工确认没有券商对账证明，也没有逐只确认的入场论点及风控参数。当前计划为 `WATCH/UNKNOWN`、`REVIEW_ONLY`，不产生可执行信号。

## Phase 与范围

- Phase：P1 PortfolioTruth + PositionPlan/State。
- 分支：`codex/p1-portfolio-truth`。
- 代码提交：见本阶段提交；本文件随同提交。
- 本阶段不接模型决策、不接真实交易。前一提交的盘中批量调用属于现有 Agent 的本地运行配置，不计作 P1 政策链。

## 完成内容

1. 新增 `portfolio_truth_snapshots`、`portfolio_truth_positions`、`position_plans`、`position_state_events` 四张表，迁移版本 v127。旧持仓、建议池、模拟盘表不改字段。
2. 同一股票跨账户聚合保留账户 ID/持仓 ID、数量、加权成本和可选的可卖/今日锁定数量。缺少可卖字段时保持 `NULL`，记录 `SELLABLE_UNKNOWN`；来源时间缺失记 `SOURCE_TIME_UNKNOWN`。
3. 真相快照只追加，保存 `fetched_at/source/source_asof/freshness/logical_hash/anomaly_flags`。逻辑哈希只基于规范化账户和持仓内容，与采集时间无关。
4. 计划按股票和市场递增版本；状态集合为 WATCH、STARTER、CORE、ADD_ALLOWED、OVERWEIGHT、REDUCE_REQUIRED、EXIT_PENDING、CLOSED；论点集合为 VALID、WEAKENING、INVALID、UNKNOWN。非法跃迁写 `REVIEW_REQUIRED` 事件，不创建新计划。
5. 提供真相捕获、最新/历史快照、计划当前/历史版本和状态事件 API。11 只持仓已创建当前 `WATCH/UNKNOWN` 计划。首次导入时错误推断为 CORE 的 v1 记录被保留，并由有审计事件的 v2 纠正；人工可卖数量确认再生成 v3；不覆盖历史。
6. 人工确认的可卖数量走显式 `sellable_equals_quantity` 输入，快照标记 `USER_ATTESTED_UNRECONCILED`，仍为 `REVIEW_ONLY`；读取 API 实时计算 `current_freshness`，超过 300 秒显示 STALE，不改写采集时证据。

## 设计决定

- 手工 UI 持仓可证明“用户在本机录入了这些数字”，不能证明券商当前可卖数量或实时性；因此不把 `updated_at` 冒充券商 `source_asof`。
- 不从“有持仓”推断 STARTER/CORE 等策略状态。当前 `WATCH/UNKNOWN` 只表示等待用户补完计划。
- `logical_hash` 相同允许多次只追加采集，便于盘前与盘后比较；计划和事件独立保留版本。
- 本阶段 API 的计划写入只创建审计版本，不产生订单或 Actionable Signal。

## 迁移与验证

- 升级前使用 SQLite 在线备份；副本迁移后 `schema_migrations` v127 成功，原有 11 条持仓、1 个模型及 1 个通知渠道数量保持不变。
- 当前本地库 v127 成功；新增人工确认前用 SQLite 在线备份到 `data/backups/panwatch-before-attestation-20260922-212552.db`。真相快照 2、快照聚合持仓 22、计划 33（每只 v1/v2/v3）、状态事件 33。第二版快照 11/11 可卖数量等于持仓数量，今日锁定数量为 0，状态 `REVIEW_ONLY`。
- 单测：`.venv/Scripts/python.exe -m pytest tests/test_portfolio_discipline_p1.py -q`，9 passed。包括人工确认、快照只追加与过期检查。
- 相关回归：`python -m pytest tests/test_evaluation_migrations.py tests/test_portfolio_accounts.py tests/test_portfolio_service.py tests/test_portfolio_discipline_p1.py -q`，12 passed、2 个既有 Pydantic 警告。
- 后端全量：`python -m pytest tests -q --disable-warnings`，791 passed、1 skipped、6 failed。4 项为本机 `make` 环境，1 项为原有 SSE TTL 边界；新增的 SSE endpoint 顺序/时序失败在单文件重跑时 5/5 通过。全量门禁仍不通过。

## 性能与数据质量

- 11 只跨账户聚合和 64 组状态迁移在本地测试中通过；P1 未做 20 只真实账户源性能压测。
- 首版快照保留 `SELLABLE_UNKNOWN` 和 `SOURCE_TIME_UNKNOWN`；第二版快照 `REVIEW_ONLY`，异常为 `USER_ATTESTED_UNRECONCILED`。NAV 与市值尚无可信来源，因此为空。

## 未解决事项与 PROD Gate

1. 需要有明确来源和时间戳的真实账户/持仓对账样本，核对人工确认的 `sellable_qty`、`today_locked_qty`，才能把真相从 REVIEW 提升为 TRUSTED。第二版快照的 300 秒新鲜度不跨日延续。
2. 11 只持仓逐只的 entry thesis、目标/上限仓位、止损和风险预算尚需用户确认；当前计划不可用于 P2/P3 的可执行判定。
3. P0 全量测试仍非零失败，P1 也缺上述业务数据；**PROD Gate：`FAIL_CLOSED`**。

下一阶段可以开发 P2 的信号与执行日志及部分减仓语义，但在 P1 真实数据和 P3 确定性政策门禁通过前，不启用真实自动执行。
