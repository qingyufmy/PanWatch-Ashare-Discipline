# P4–P6 隔离验收记录（2026-09-22）

## P4 模型路由

- 完成：迁移 130/132；FAST、FAST_BACKUP、DEEP、DEEP_BACKUP 可配置角色；每次尝试记录模型 ID、用量、延迟、降级、Schema 与原始响应；有界超时和失败留痕；登录后 API 可读写角色配置并执行能力探测。
- 设计：业务层只传逻辑角色，角色解析既有 AIModel/AIService；Schema 失败立即拦截；后备角色仅在配置后启用。06:50 调度由 P7 接线。
- 变更：`model_router.py`、`model_roles.py`、`ai_client.py`、数据库模型/迁移、`server.py`。
- 测试：`pytest tests/test_model_router_p4.py tests/test_ai_provider_sniff.py tests/test_ai_failover.py -q`，33 通过。隔离库 FAST 能力探测 `OK`，requested_model `deepseek-v4-pro`，reported_model `deepseek-v4-pro-ga-260813`。失败主模型与降级尝试有独立记录。
- 限制：FAST 11 只批量成功耗时约 30 秒，高于 P10 的 FAST P95 20 秒门槛；DEEP 与 tool calling 尚未做真实能力探测；当前既有 Agent 还未切换到角色路由。

## P5 Prompt Registry

- 完成：迁移 131；flash/deep/review 的版本、输入/输出 Schema、角色、父版本、原因和 hash；严格 Pydantic ActionProposal/PortfolioActionPlan；全覆盖、无重复、交易日及数量语义校验；一次请求覆盖 11 只。
- 设计：模型结果只生成提案，不绕过 P3 PolicyGate；六位持仓代码与带交易所前缀的模型输出作确定性归一化；旧失败原文和新版本同时留存。无效 Schema 不生成可执行信号。
- 变更：`prompt_registry.py`、数据库模型/迁移、`model_roles.py`、`probe-portfolio-batch.py`。
- 测试：`pytest tests/test_prompt_registry_p5.py -q`，2 通过。隔离库真实 FAST 批量运行 ID `6abebe7fc002460f9dcb50da1b45e1f8`：11/11 提案、全部 HOLD、Schema 通过。首轮 800 tokens 截断、次轮代码表示不一致的失败尝试均留在 `model_runs`；归一化后同一份冻结原文复验 11/11 通过。
- 限制：这次输入是过时行情和人工持仓真相，因此全 HOLD 仅证明契约可运行，不能当作交易日建议；实际盘前工作流由 P7 接线。

## P6 分钟线与特征

- 完成：新浪 1m 主源、东财 1m 备用接口；原始行情时间、来源、哈希、缺口和重复校验；本地 5/15/30/60/120m 聚合及 MA/VWAP/MACD/RSI/KDJ/ATR/量比/涨跌幅/振幅/支撑阻力；trade_date/symbol 分区 Parquet 与 DuckDB 读取。
- 设计：午休分段，15:00 集合竞价单独保留；行情 asof 取供应商时间，抓取时间独立存储；缺口不填造价格。
- 变更：`minute_features.py`、`requirements.txt`；隔离环境安装 duckdb 1.5.5、pyarrow 25.0.1。
- 测试：`pytest tests/test_minute_features_p6.py -q`，4 通过；20 只 fixture 特征计算低于 30 秒。真实 `sh600519` 与三只持仓的 2026-09-22 历史日均返回 238 根，缺口 0；Parquet 写入并读回 238 根。东财接口本机实测断连，Sina 成功。
- 限制：尚无 2026-09-23 盘中实时自然日证明；备用源未通过连通测试，因此 P6 生产数据源门禁保持 `FAIL_CLOSED`。

## 发布与下一步

这些代码先在隔离工作树验证，再合入本地运行版；现有系统仍只提供建议、人工确认和模拟盘。P7 接入完整调度和自然日监测后，再判定 06:50 模型探测、分钟源 freshness 与 11 只持仓覆盖。P8–P10 继续独立实施并验收，不能据此记录作完成声明。
