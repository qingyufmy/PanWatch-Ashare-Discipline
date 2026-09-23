# P0 数据契约盘点

本页记录当前接口，不宣称附件目标已实现。权威源码见 `src/platform/persistence/models.py`、`src/modules/research/signals/signal_pack.py`、`src/modules/automation/suggestion_pool.py` 和 `src/modules/automation/tradingagents/decision.py`。

| 对象 | 当前字段/行为 | 附件目标缺口 |
| --- | --- | --- |
| `Position` | `account_id/stock_id/quantity/cost_price` | 无可卖数量、当日锁定、快照时间/源/异常标记。 |
| `SignalPack` | `computed_at/quote/technical/position/news/events/sources/missing` | 不是冻结的 EvidenceSnapshot；无统一 logical hash。 |
| `StockSuggestion` | action 与理由、到期、来源等建议池字段 | 不是带 `signal_id/plan_version/policy_decision` 的审计链。 |
| `StrategySignalRun` | 策略/日期/股票/动作/状态/trace 等 | 用于策略及模拟盘；不是完整 ActionProposal→ActionableSignal 生命周期。 |
| TradingAgents `AnalysisResult` | `rating_raw` 保留五档，兼容 action 三档 | 无 OPEN/ADD/HOLD/REDUCE/EXIT + 目标权重的完整动作契约。 |
| `PaperTradingPosition/Trade` | 持仓与已平仓记录 | 当前 reduce 反转执行整仓关闭，无部分减仓交易记录。 |

建议的 P1 新契约以用户附件为准，但要先取得真实持仓输入字段与账户对账样本。P0 无新增表或迁移。
