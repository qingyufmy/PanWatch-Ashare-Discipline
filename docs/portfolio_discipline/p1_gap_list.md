# P1 精确实施差距清单

1. **真实持仓输入**：复用 `Account`/`Position` 与 `src/modules/portfolio` 服务，明确账户来源、同票跨账户聚合、成本精度、交易日内买入锁定、`sellable_qty` 与无法取得字段时的异常语义。先取得脱敏真实样本和对账样本。
2. **PortfolioTruthSnapshot**：新增带 `fetched_at/source/freshness/logical_hash/anomaly_flags` 的只追加快照和明细引用，定义过期、重复、负数、金额不平等异常；不能把仓库种子股票当作真实持仓。
3. **PositionPlan/State/Thesis**：新增不可覆盖的计划版本、状态/论点枚举和状态事件；明确合法转移表、拒绝/REVIEW 记录与当前有效版本查询，不接模型和执行。
4. **兼容迁移**：在 `src/platform/persistence/migrations.py` 新增下一版迁移，旧 `positions`/`stock_suggestions`/模拟盘可读，空库和已有库均验证；迁移前做备份。
5. **API 与测试**：只读 Plan 当前/历史接口；单元和集成测试覆盖聚合、加权成本、T+1 字段、freshness、logical hash、异常、全部状态迁移和旧库升级。
6. **分阶段边界**：P1 不添加 PolicyGate、Actionable Signal、Prompt 改写或真实执行。P2 再处理 `reduce` 与 `sell` 现有整仓平仓语义，并建立执行日志。
