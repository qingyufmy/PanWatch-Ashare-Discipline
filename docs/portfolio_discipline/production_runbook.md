# 本地持仓系统运行手册

## 启动与检查

Windows 计划任务 `PanWatch Local Backend` 运行 `scripts/ensure-local-service.ps1`，服务监听 `127.0.0.1:8000`。先检查 `/health`，再登录核对 `/discipline` 的 11 只持仓计划、`/api/portfolio-workflow/schedule` 和当日 `/api/portfolio-workflow/runs`。健康接口只证明进程可访问，不证明交易日业务链通过。

每日交易前确认最新 PortfolioTruth 的数量、可卖数量、总资产和来源。总资产 50 万元、可卖数量等于持仓数量目前仅有用户声明；账户可用现金不得由总资产减持仓市值推算。07:20/15:05 的快照保持 REVIEW_ONLY，直到有可核实的券商账本来源。风险价与仓位上限见 [risk_plan_2026-09-22.md](risk_plan_2026-09-22.md)，次日开盘前按新行情复核。

关键业务证据依次核对：`portfolio_workflow_runs` 08:50 成功或 REVIEW、`daily_portfolio_plans` 覆盖 11 只、`signal_events` 每条关联 Truth/Plan/Evidence/Model/Prompt/Policy、`actionable_signals` 仅含通过门禁的结果、`execution_events` 人工记录、15:05 EOD Truth 与对账状态。缺行情或模型时记录 Issue，不补写事后假信号。所有真实订单均由用户自行操作，程序不连接券商下单。

每日 21:20 的 `P10_REVIEW` 写入 `data/reviews/YYYY-MM-DD/daily.json`；周五写 weekly.json，双周写 biweekly.json，月末交易日写 monthly.json。每周或月末写 upstream_watch.json，仅比较仓库 HEAD、关键依赖版本与已观察模型 ID，不自动升级。门禁字段为 `FAIL_CLOSED` 时，生产发布申请不得继续。

故障恢复：先备份 SQLite 在线副本，再检查 `system_issues` 与失败 run 的 `run_id`、slot、错误签名。服务重启后 `recover_missed` 记录过期槽，不调用模型补跑已过时的决策。修复后用相同冻结证据回放并记录 fix commit 和 regression test。

## 通知与价位候选版本（2026-09-23）

实施合同见 [notification_contract_v2.md](notification_contract_v2.md)、[technical_levels_contract_v1.md](technical_levels_contract_v1.md)、[notification_delivery_contract_v1.md](notification_delivery_contract_v1.md)；隔离回放见 `reports/notification_replay/2026-09-23/`。这些是候选代码及合成回放证据，运行版没有自动升级。`portfolio_discipline_mode` 默认 `disabled`。新 Prompt、Policy 和模板必须先完成冻结回放、自然日 Shadow、真实通知审阅以及用户批准，才能切换生产生效状态。

候选版本启用前，以 SQLite 在线备份核对迁移、真实持仓 Truth、可用现金、每只证券规则的来源和有效时刻，再检查 11 只持仓的 Feature/Level 质量与计划版本。运行后用 `/api/portfolio-workflow/notifications` 与 `/decisions` 核对发送、拒绝、未知和执行状态；私有通知包用 `python scripts/export-notification-review.py --trade-date YYYY-MM-DD` 生成于忽略目录 `data/notification_reviews/`。对外仅使用 `scripts/export-public-daily.py` 的白名单摘要。平台接受、用户已读、用户登记及券商核对分别统计。回滚时保留新账本和 Issue，不删历史记录。
