# 风险观察候选合同

`PortfolioRiskObservation` 是已留痕的风险观察，不是交易指令。普通 `RISK_OFF_DIRECTIONAL_REVIEW` 候选触发条件为同一批次中至少 3 个新鲜主要指数共同形成 `RISK_OFF`，该股报价新鲜，且存在未过期的旧 `AGENT` 减仓或退出待复核提案。它不声称个股支撑已失守，也不批准模型提出的数量。

高严重度 `CONFIRMED_SUPPORT_BREAK` 则独立验证同日上一版已收线 S1、Feature/Level 哈希与来源时间、当前分钟价和新鲜报价共同跌破该 S1。缺一项即不升级。价位来自上一版 Level，不使用模型自述价位；即使当前版本因历史不足而没有 MA20，也不虚构 MA20。每票每批次至多一个可见风险观察；原始提案继续留存。

一个交易日、批次、标的和风险类型形成一个 `episode_key`；同键重试幂等。观察保存严重度、来源信号 ID、市场证据 ID、可选 Level ID、数据质量、`NEEDS_CONFIRMATION` 执行资格、通知 ID、通知结果和有效期。普通观察正文明确“待核查”；已证实破位只显示证据可追溯价位，不显示未经核对的数量或模型原文。通知账本当前写 `SUPPRESSED/SHADOW_ONLY`，分发器拒绝发送该状态。页面将仍有效的观察显示为单一“风险复核”，未经证实的 HOLD 显示“待核查”；历史决议保留在详情。

人工持仓仍为 `USER_ATTESTED_UNRECONCILED`。政策门禁继续负责数量、可卖、证券规则、行情和授权；观察绝不生成 `ActionableSignal`、`ExecutableIntent` 或模拟/真实成交。经认证的 `/api/portfolio-discipline/risk-observations` 只读接口供人工审阅 Shadow 结果。

当前缺口：仅实现收线 S1 破位这一种个股技术事实；相对弱势、盈利回吐与行业集中度仍缺可核实数据。跨批次冲突裁决、人工确认版本与失效协议，以及可发送的四类动作模板仍未完成。自然日 Shadow 审阅通过前，不将候选流转为真实飞书通知。
