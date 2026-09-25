# 信号、政策与通知时钟合同

`scheduled_at` 是调度槽；`market_data_asof` 是行情来源时间；两者都不能
代替模型完成后的真实决策时刻。模型信号写入前记录 `model_finished_at`。
新信号的 `generated_at` 来自模型返回后的真实 UTC 时钟，随后执行 Policy，
再写 `PortfolioDecision` 和通知队列。正常因果顺序为：

```text
model_finished_at <= signal.generated_at <= policy.created_at
                  <= decision.created_at <= notification.queued_at
```

信号临发或模拟成交仍需重新校验 TTL、行情、数量、证券规则和当前版本。
9 月 24 日历史记录不修改；冻结审计的 `timeline_audit.csv` 标注 30 条
模型信号记录时间早于模型完成。业务展示不能把历史 `approved_at` 当作当时
真实可执行时刻。显式回放使用注入时钟，生产工作流使用系统真实时钟。
