# P0 实施清单

基线：PanWatch `89bdf3f61e2b3e635e1427b3c5112ac488a7f436`，本地分支 `codex/p0-baseline-freeze`。本阶段只固定源码行为、测试与差距，不改变运行逻辑。

- [x] 读取 `AGENTS.md`、项目结构、数据库模型/迁移、调度器、MarketData、TradingAgents 适配、模拟盘和现有测试。
- [x] 记录源码调用链、默认配置、版本与现状差异。
- [x] 在隔离 Python/Node 环境运行现有测试并分类失败。
- [x] 添加必要的最小契约测试，锁定现有行为。
- [x] 完成 baseline acceptance、P1 变更清单与 Git diff 检查。

边界：无数据库迁移；无 Prompt、阈值、策略、调度或交易行为变更；无真实订单接口。证据只覆盖本地新克隆源码及离线测试，不代表用户的生产实例。
