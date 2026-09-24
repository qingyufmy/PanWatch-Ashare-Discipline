# 风险观察候选合同

`PortfolioRiskObservation` 是已留痕的风险观察，不是交易指令。当前候选触发条件为同一批次中至少 3 个新鲜主要指数共同形成 `RISK_OFF`，该股报价新鲜，且存在未过期的旧 `AGENT` 减仓或退出待复核提案。它**不**断言个股支撑位已失守，也不批准模型提出的数量。

一个交易日、批次、标的和风险类型形成一个 `episode_key`；同键重试幂等。观察保存来源信号 ID、市场证据 ID、数据质量、`NEEDS_CONFIRMATION` 执行资格、通知 ID、通知结果和有效期。通知正文明确“待核查”，不会展示未经核对的价格、数量和模型原文。通知账本当前写 `SUPPRESSED/SHADOW_ONLY`，分发器拒绝发送该状态。

人工持仓仍为 `USER_ATTESTED_UNRECONCILED`。政策门禁继续负责数量、可卖、证券规则、行情和授权；观察绝不生成 `ActionableSignal`、`ExecutableIntent` 或模拟/真实成交。经认证的 `/api/portfolio-discipline/risk-observations` 只读接口供人工审阅 Shadow 结果。

当前缺口：尚无个股技术事实的独立严重度、跨批次冲突裁决、人工确认版本与失效协议，以及可发送的四类动作模板。自然日 Shadow 审阅通过前，禁止将此候选流转为真实飞书通知。
