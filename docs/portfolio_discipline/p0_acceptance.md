# P0 基线验收报告

## 结论

本地源码基线、调用链、数据源和调度盘点完成，前端及四个独立包测试通过，新增模拟盘现状契约通过；后端原测试存在独立基线失败。因此 **P0 全量测试门禁为 `FAIL_CLOSED`，V1 PROD Gate 也为 `FAIL_CLOSED`**。本次未进入 P1，没有真实订单、模型调用或用户持仓数据。

## 版本与本地启动

- PanWatch 来源 `https://github.com/TNT-Likely/PanWatch`，基线 `89bdf3f61e2b3e635e1427b3c5112ac488a7f436`；本地分支 `codex/p0-baseline-freeze`；`VERSION=dev`。
- `requirements.txt` 锁定 TradingAgents `v0.5.0`，pip 解析上游提交 `7fe225224431aeb3adfe1a6a23885c03fc43620a`。本机 Python 3.11.15，Node 22.23.1，pnpm 9.15.9；前端声明 Node 24.14.0，构建虽通过但有 engine warning。
- 本地 `127.0.0.1:18000` 烟测：`/api/health` 返回 `status=ok`，`/api/version` 返回 `dev`；启动后 SQLite 迁移到 v126，建 26 条数据源种子、0 条真实持仓，默认只注册 1 个启用 Agent（日终复盘）。服务已正常停止。此项不等于业务验收。

## 测试与失败分类

| 命令/范围 | 结果 | 分类 |
| --- | --- | --- |
| `python -m pytest tests/ -q --disable-warnings`，新增测试前 | 781 passed，1 skipped，5 failed，13 warnings | 基线测试未全过 |
| `tests/test_makefile_windows.py` 4 项初始失败 | 本机没有名为 `make` 的可执行文件 | 本环境失败 |
| 用本机 `mingw32-make.exe` 的临时 `make.exe` 重跑该文件 | 5 passed，1 failed | 剩余失败是 MinGW make 无法处理仓库中文路径，仍是本环境失败 |
| `tests/test_sse_infra.py` 单独重跑 | 6 passed，1 failed | 已有源码/测试语义冲突：TTL=0 的 `_prune` 使用 `>`，当前时钟精度下创建后立即取仍未超过 0；P0 不改生产代码 |
| `tests/test_portfolio_discipline_p0_contract.py` | 2 passed | 新增 P0 现状契约 |
| 四个 `packages/*/tests` 分别运行 | 188 + 39 + 3 + 8 = 238 passed | 包测试通过；同进程合跑因重复模块名 `test_registry` 收集冲突 |
| `pnpm exec vitest run` | 18 files，43 passed | 前端测试通过；有现有 React warnings |
| `pnpm build` | 通过 | Node engine warning，构建产物只在本地 |

现有契约覆盖：`test_tradingagents_5_tier_rating.py` 锁定五档来源和三档兼容映射，`test_tradingagents_agent.py` 与 `test_tradingagents_v050_compat.py` 覆盖 PortfolioContext/上游签名，`test_tradingagents_a_share_data_routes.py` 覆盖 A 股工具数据路由，`test_intraday_monitor_json_format.py` 覆盖当前盘中宽松 action 解析。本轮新增 `test_portfolio_discipline_p0_contract.py` 只补模拟盘 reduce 现状。

没有生产代码或旧数据库的修改；新增测试仅锁定当前 `reduce`/`sell` 均整仓平仓。P1 的准确工作项见 `p1_gap_list.md`。所有测试是本地新克隆结果，未代表用户现有部署。
