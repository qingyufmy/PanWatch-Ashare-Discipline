# 附件要求逐项核对

核对对象：用户附件 V1.0 的附录 A–D。当前执行范围按附录 D 限定为 P0。状态 `完成` 仅指本地源码和测试证据，`未通过` 表示有明示失败，`后续` 表示本轮未获授权进入该阶段。

| 附录 D / P0 条款 | 状态 | 本地证据 |
| --- | --- | --- |
| 仓库、分支、提交、依赖与 TradingAgents 版本 | 完成 | `p0_acceptance.md` 版本段；`requirements.txt` |
| 读取架构、数据库、Agent、调度与数据源 | 完成 | `architecture_baseline.md`、`data_contracts.md`、`current_schedules.md`、`current_data_sources.md` |
| 真实调用链而非套用方案假设 | 完成 | `architecture_baseline.md` 两条调用链 |
| 跑完整测试并保存基线、分类失败 | 未通过 | `p0_acceptance.md`：后端 5 个基线失败；前端和包测试通过 |
| 五档评级、PortfolioContext、A 股路由、盘中 action | 完成 | 现有 `tests/test_tradingagents_5_tier_rating.py`、`test_tradingagents_agent.py`、`test_tradingagents_a_share_data_routes.py`、`test_intraday_monitor_json_format.py` |
| paper reduce 现状契约 | 完成 | 新增 `tests/test_portfolio_discipline_p0_contract.py`，2 passed |
| 不改策略/Prompt/阈值/交易行为，不实施 P1 | 完成 | 提交 diff 只有文档及测试；`p1_gap_list.md` |
| P0 完整报告与 P1 精确清单 | 完成 | `p0_acceptance.md`、`p1_gap_list.md` |

V1 目标阶段状态：P1 持仓真相与计划、P2 信号/执行日志、P3 PolicyGate、P4 模型路由、P5 Prompt Registry、P6 分钟线、P7 日流程、P8 Replay、P9 Issue Ledger、P10 持续迭代/PROD Gate **均为后续，未实现或未验收**。不能将 PanWatch 原有相似能力记作这些阶段已交付。
