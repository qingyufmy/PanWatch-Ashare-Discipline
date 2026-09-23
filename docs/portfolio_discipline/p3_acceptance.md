# P3 确定性 Policy Gate 验收记录

## 结论

P3 已实现 11 条确定性规则及只由规则全通过后创建的 ActionableSignal，隔离库和浏览器业务链验证通过，并于 2026-09-22 以人工复核能力部署到本地正式实例。**当前用户持仓的人工确认快照为 `REVIEW_ONLY`，且缺少可核验的行情来源时间及逐只风控计划，因此现有建议必须保持 `REVIEW_REQUIRED`；V1 PROD Gate 仍为 `FAIL_CLOSED`。**

## 规则与持久化

`SIGNAL_TTL`、`SCHEMA_INVALID`、`MODEL_CONFLICT`、`DATA_FRESHNESS`、`PORTFOLIO_TRUTH`、`MAX_POSITION`、`THESIS_INVALID`、`STOP_WIDENING`、`HARD_STOP`、`T_PLUS_ONE`、`DUPLICATE` 均保存 rule_id、输入 hash、裁决和原因码。缺关键证据为 REVIEW 或 BLOCK；模型调用和真实订单不在政策服务内。

仅全部通过时创建包含批准数量、仓位及政策版本的 ActionableSignal，随后写 APPROVED 生命周期。STOP_WIDENING、INVALID thesis、超仓、重复可执行信号会阻断；超出可卖数量的部分进入 NextDayAction，可卖部分仍需其余规则全部通过。信号过期进入 EXPIRED。未批准的人工实际操作仍可登记为计划外行为以供纪律审计，但不能获得 ActionableSignal。

## 验证

- 合成可信快照下，全 11 条规则 PASS 后才有批准封套；T+1 测试将 1000 股 EXIT 拆为 300 股可卖和 700 股次日待办。
- 止损放宽与 INVALID thesis 双重 BLOCK、陈旧行情与 Schema 未证实 REVIEW、重复评估幂等，均有定向测试。
- 本地真实数据副本上，建议仍为 REVIEW_REQUIRED，11 条裁决落库，ActionableSignal=0；证明当前资料不足时不放行。
- 后端全量在英文路径隔离工作区运行：819 passed、1 skipped、13 warnings。原中文路径下同一套源码为 813 passed、1 skipped、1 failed；唯一失败是已安装 GNU Make 无法读取中文绝对路径，不是政策链失败。四个独立包测试为 188 + 39 + 3 + 8 = 238 passed。
- 前端 `pnpm build` 通过；`pnpm exec vitest run` 为 43 passed（修正新增路由的枚举测试后）。
- 正式本地服务升级后，数据库 v129、`intraday_monitor` 仍为 batch，健康、登录和 `/discipline` 均 HTTP 200；浏览器登录后的持仓页与纪律页通过。正式库当前 SignalEvent=0、ActionableSignal=0，真实交易日自然信号和政策结果仍待观察。

## 发布门禁

当前仅可作为人工复核与审计能力上线。要让动作可执行，必须补齐当日可信持仓与可卖数量、行情原始时间戳、逐只计划和严格 Schema，并完成真实交易日自然周期验收。所有条件未齐前，`FAIL_CLOSED`。
