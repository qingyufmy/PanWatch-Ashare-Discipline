# 2026-09-25 休市日 Shadow 核查与旧日报隔离

## 事实

上海证券交易所及深圳证券交易所公告：2026-09-25 至 09-27 中秋休市，09-28 起恢复交易（[上交所公告](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)、[深交所公告](https://www.szse.cn/www/disclosure/notice/general/t20260917_622911.html)）。本机服务 10:35 启动的进程来自 `D:\盯盘\PanWatch\server.py`，健康接口 200，数据库版本 142；启动日志显示 A 股日历覆盖至 2026-12-31。

截至北京时间 15:57，组合工作流 303 次自然调度全部为 `SKIPPED/NON_TRADING_DAY`，包括 10:00、13:30、15:05、15:30；今日无 PortfolioTruth、日计划、ModelRun、风险观察、组合通知、模拟成交或模拟净值。最新用户声明持仓为前一交易日 10 只，尚未券商对账。FAST/DEEP 都指向 Pro；默认 Lark 已配置，候选 `portfolio_discipline_mode=disabled`，模拟盘为 `paper_only`。9 月 24 日 P10 门禁仍为 `FAIL_CLOSED`。因此今日不能验收 9 月 24 日整改的自然盘中效果。

隔离缺口：旧 `daily_report` Agent 在 15:30 的星期工作日 cron 下仍执行，形成 11 条 `REVIEW_REQUIRED` 信号、10 条当天“当前”决议；其 `agent_runs` 记录 `notify_sent=1`，即一条旧日报通知被推送。没有正式组合通知账本记录、模拟成交或实盘订单。原始信号、决议与发送记录保留，不改写为交易日成果，也不重发。

## 修复

定时 `daily_report` 和 `premarket_outlook` 在构建上下文、调用模型前要求已加载日历明确证明 A 股开市。休市或日历覆盖未知时，写 `agent_runs` 的 `skipped` 与具体原因。持仓决议拒绝使用前一日 Truth 生成新交易日的“当前”决议。当前建议接口在休市或日历未知时返回空集及明确状态，前端显示“休市”或“交易日待核查”；历史建议仍可查。

上述修复不删除当天误生成记录，不伪造 09-25 的模拟净值或风险观察。9 月 28 日才具备下一次自然交易日样本；正式动作通知、证券规则和券商对账仍未达到生产门禁。

## 部署验证

代码提交 `0b834dd` 已于 16:07 在本机重启加载；重启前执行 SQLite 在线备份。组合与调度相关 101 个测试通过，前端 `pnpm build` 通过，静态 Stocks 资源与构建产物字节数一致。新进程来自本项目，`/api/health` 与持仓页均为 200；经登录读取 `/api/portfolio-workflow/advice` 返回 `market_status=NON_TRADING_DAY`、`items={}`。重启日志显示日历加载至 2026-12-31，旧日报的下次调度为 09-28 15:30。今日没有新增 Issue Ledger 记录；休市日没有 15:45 纸盘净值，9 月 24 日 P10 门禁继续 `FAIL_CLOSED`。这只能验收休市隔离，不能替代下一交易日自然盘中样本。
