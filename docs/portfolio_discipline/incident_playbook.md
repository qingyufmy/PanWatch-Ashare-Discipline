# 运行故障处置

1. 查 `system_issues` 按错误签名、严重度和首次/末次出现时间聚合，定位对应 `portfolio_workflow_runs.run_id`、模型 run、行情源、Evidence 与信号 ID。
2. 行情断源或超时：停止生成新的 ActionableSignal，保留 REVIEW/YELLOW 与源错误；修复数据源后仅处理新时点证据，不回填过期行动。
3. 模型超时、Schema 错或 FAST/DEEP 冲突：保留失败响应与 ModelRun，规则级 Hard Risk 独立继续；候选输出不得越过 PolicyGate。
4. 调度漏跑或数据库锁：在线备份数据库，修复进程和锁，再由 `recover_missed` 记录漏槽。关键槽漏跑计入 P10 门禁，不以事后补跑覆盖记录。
5. 结案需要 root cause、fix commit、regression test；复发改为 REGRESSION 并重新审查。若已产生人工执行记录，应以用户可核实回单对账，不自行归因或篡改原始记录。
