# P0 当前数据源

源码入口：`src/platform/marketdata/marketdata_client.py:DbConfigProvider` 读取 `DataSource`，按 `priority` 排序，只使用已启用配置；`packages/marketdata/src/marketdata/registry.py` 注册供应商。实际优先级是实例数据库配置，不能由仓库默认值推断。

| 类型 | 已注册实现 | 已核实边界 |
| --- | --- | --- |
| Quote | Tencent、Sina、Eastmoney | 实例启用顺序未取得；`SignalPackBuilder` 在无 DB 配置时默认 Tencent。 |
| 日 K | Tencent、Eastmoney，另有美股源 | A/HK 保留 Tencent；美股存在特殊回退逻辑。这里不是 1m 原始行情。 |
| 资金流 | Eastmoney、Sina | `SignalPackBuilder` 无 DB 配置时默认 Eastmoney。 |
| 公告/新闻/快讯 | Eastmoney、Xueqiu、CLS、Sina 等 | 雪球依赖可选凭证；端点存活和内容质量未实测。 |
| 基本面 | Tencent、Eastmoney，适配层还使用 AkShare | TradingAgents A 股工具会尝试 PanWatch 本地数据路由。 |

`SignalPack` 有 `computed_at`、`sources`、`missing`，但这不等同于 P1 要求的带 freshness/anomaly/hash 的 PortfolioTruth。未取得用户实例 `DataSource` 行和健康指标；本阶段不发起实时行情或模型请求。
