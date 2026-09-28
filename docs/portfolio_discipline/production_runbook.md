# 本地持仓系统运行手册

## 非交易日与盘前语义（2026-09-28 更新）

本地自动分析、机会扫描、模拟盘与价格警报只在确认的中国交易日进入业务；未知日历停止业务。服务守护、日历更新和日志维护可以继续。Portfolio 工作流已注册时，旧 premarket_outlook/daily_report 定时入口记 SUPERSEDED_BY_PORTFOLIO_WORKFLOW 后退出，避免重复摘要。

盘前使用上一确认交易日收盘形成条件预案，不能套用盘中实时行情时效规则；海外来源日期和未核实时区分开标注。`/discipline` 展示最新日计划的组合建议区间、逐股条件及失效条件。数字依据是声明总资产与持仓市值，不是成本占比或券商核对值。规则预案与模型信号分别留痕，模型失败不凭空生成信号。

机会页默认只显示北京时间当日候选；候选 ID 关联到其他股票的历史策略结果被读取接口隔离。历史原件保留，不重跑历史信号。具体问题、修复提交与验收证据见 [2026-09-28 检查记录](../incidents/20260928/premarket_audit.md)。

## 启动与检查

Windows 计划任务 `PanWatch Local Backend` 每日 08:00 至 22:00 每 5 分钟运行 `scripts/ensure-local-service.ps1`，服务监听 `127.0.0.1:8000`。先检查 `/health`，再登录核对 `/discipline` 的当日持仓计划、`/api/portfolio-workflow/schedule` 和当日 `/api/portfolio-workflow/runs`。健康接口只证明进程可访问，不证明交易日业务链通过。

每日交易前确认最新 PortfolioTruth 的数量、可卖数量、总资产和来源。总资产和可卖数量目前仅有用户声明；账户可用现金不得由总资产减持仓市值推算。07:20/15:05 的快照保持 REVIEW_ONLY，直到有可核实的券商账本来源。风险价与仓位上限见 [risk_plan_2026-09-22.md](risk_plan_2026-09-22.md)，次日开盘前按新行情复核。

关键业务证据依次核对：`portfolio_workflow_runs` 08:50 成功或 REVIEW、`daily_portfolio_plans` 覆盖最新 PortfolioTruth 中的全部持仓、`signal_events` 每条关联 Truth/Plan/Evidence/Model/Prompt/Policy、`actionable_signals` 仅含通过门禁的结果、`execution_events` 人工记录、15:05 EOD Truth 与对账状态。缺行情或模型时记录 Issue，不补写事后假信号。所有真实订单均由用户自行操作，程序不连接券商下单。

每日 21:20 的 `P10_REVIEW` 写入 `data/reviews/YYYY-MM-DD/daily.json`；周五写 weekly.json，双周写 biweekly.json，月末交易日写 monthly.json。每周或月末写 upstream_watch.json，仅比较仓库 HEAD、关键依赖版本与已观察模型 ID，不自动升级。门禁字段为 `FAIL_CLOSED` 时，生产发布申请不得继续。

故障恢复：先备份 SQLite 在线副本，再检查 `system_issues` 与失败 run 的 `run_id`、slot、错误签名。服务重启后 `recover_missed` 记录过期槽，不调用模型补跑已过时的决策。修复后用相同冻结证据回放并记录 fix commit 和 regression test。

## 2026-09-24 模拟盘试运行

模拟盘基线、盘前市场证据、10:00/13:30 批量修订、PAPER_ONLY 成交门禁和 15:45 净值快照见 [paper_mirror_2026-09-24.md](paper_mirror_2026-09-24.md)。核对 `/api/paper-trading/account` 的 `paper_mode` 和与私有基线报告一致的持仓、净值，`/api/paper-trading/portfolio-fills` 的逐笔信号关联，以及 `/api/paper-trading/metrics` 的逐日净值。核对 08:50、09:25、10:00、13:30 和 15:45 自然运行证据；服务健康不等于这些业务步骤成功。

## 通知与价位候选版本（2026-09-23）

实施合同见 [notification_contract_v2.md](notification_contract_v2.md)、[technical_levels_contract_v1.md](technical_levels_contract_v1.md)、[notification_delivery_contract_v1.md](notification_delivery_contract_v1.md)；隔离回放见 `reports/notification_replay/2026-09-23/`。这些是候选代码及合成回放证据，运行版没有自动升级。`portfolio_discipline_mode` 默认 `disabled`。新 Prompt、Policy 和模板必须先完成冻结回放、自然日 Shadow、真实通知审阅以及用户批准，才能切换生产生效状态。

候选版本启用前，以 SQLite 在线备份核对迁移、真实持仓 Truth、可用现金、每只证券规则的来源和有效时刻，再检查 11 只持仓的 Feature/Level 质量与计划版本。运行后用 `/api/portfolio-workflow/notifications` 与 `/decisions` 核对发送、拒绝、未知和执行状态；私有通知包用 `python scripts/export-notification-review.py --trade-date YYYY-MM-DD` 生成于忽略目录 `data/notification_reviews/`。对外仅使用 `scripts/export-public-daily.py` 的白名单摘要。平台接受、用户已读、用户登记及券商核对分别统计。回滚时保留新账本和 Issue，不删历史记录。

## 盘中运行链路（2026-09-28 修复）

盘中持仓改用每次覆盖全组合的统一批次，具体时间见 `/api/portfolio-workflow/schedule`；旧五分钟 intraday_monitor 自动入口由新工作流替代。FAST 使用关闭思考的 JSON 响应、80 秒超时且不自动重试；模型失败或超出行情时效不生成操作信号。每分钟 PAPER_ONLY 扫描只处理每股最新信号，记录具体跳过原因。页面 `/paper-trading` 展示批次与扫描状态。每股最新提案不等于实盘批准；用户填报值仍未券商核对。

核查 `/api/portfolio-workflow/runtime`、逐笔模拟成交、十三条政策记录及行情源时间。数据库迁移 143 保存模型终止元数据；盘中海外历史后验维护延后，风险报价交叉校验在工作线程执行，避免阻塞异步调度。新增批次漏跑从激活时刻起记录，不回填。修复与真实链路证据见 [盘中稳定性修复](../incidents/20260928/intraday_stability_repair.md)。全天运行与 P10 发布资格仍需分别验收。
