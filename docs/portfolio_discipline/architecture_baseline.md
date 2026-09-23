# PanWatch P0 架构基线

## 证据范围

2026-09-22 从 [PanWatch](https://github.com/TNT-Likely/PanWatch) 新克隆。基线 `main@89bdf3f`，工作分支 `codex/p0-baseline-freeze`。本地没有用户账户、真实持仓或已运行实例，因此以下为源码与离线测试事实。`requirements.txt` 锁定 [TradingAgents](https://github.com/TauricResearch/TradingAgents) `v0.5.0`，安装解析到 `7fe225224431aeb3adfe1a6a23885c03fc43620a`；仓库 `VERSION` 为 `dev`。

## 当前调用链

`src/platform/marketdata/marketdata_client.py` 通过数据库 `DataSource` 优先级提供行情、K 线和新闻。`src/modules/research/signals/signal_pack.py:SignalPackBuilder` 汇集报价、技术摘要、持仓、新闻、资金流与事件，带来源和缺失字段。`src/modules/automation/intraday_monitor.py` 从 SignalPack 构造 Prompt，调用 AI，宽松解析建议，写入 `suggestion_pool.save_suggestion` 和预测结果。`src/modules/automation/base.py` 处理运行与通知；`server.py:build_scheduler` 只注册数据库中已启用且有 schedule 的 workflow Agent。

模拟盘是另一条链：`StrategySignalRun` → `src/modules/paper_trading/paper_trading_engine.py` 的入场/退出扫描 → `PaperTradingPosition`/`PaperTradingTrade`。它不是所有 `StockSuggestion` 的通用执行日志。现有 `reduce`/`sell` 反转信号均调用 `_close_position` 整仓平仓；见 `paper_trading_engine.py` 的 `_check_exits`。

TradingAgents 路径：`src/modules/market/api/stocks.py` 的单股触发 → `src/modules/automation/tradingagents/agent.py` → `runtime_support.build_ta_llm_config` → `toolkit_adapter` 的 A/HK 数据路由 → `TradingAgentsGraph.propagate(symbol, date, portfolio=...)` → `decision.map_state_to_result` → AnalysisResult/可选建议与模拟盘信号。`data_context.to_tradingagents_portfolio` 负责原生持仓上下文转换。`decision.py` 保留 `rating_raw` 五档，但兼容 action 压成 `buy/hold/sell`；`REVIEW` 在兼容 action 表示为 `hold`，并标记人工复核。`emit_paper_trading_signal` 默认关闭。

## 持久化和差距

SQLite 路径见 `src/platform/persistence/database.py:DB_PATH`，模型定义见 `models.py`，版本迁移见 `migrations.py`（最高 v126）。已有 `stocks`、`positions`、`agent_configs`、`agent_runs`、`stock_suggestions`、`strategy_signal_runs`、`paper_trading_*` 等表。当前源码没有实施说明中要求的 PortfolioTruthSnapshot、PositionPlan/State、Signal/Execution Journal、PolicyGate；这些在 P1 及以后阶段处理。此 P0 未执行用户旧数据库迁移，也未验证真实数据完整性。
