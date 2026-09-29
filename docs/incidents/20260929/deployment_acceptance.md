# 2026-09-29 本地修复验收

北京时间 09:13 记录；代码提交 `05c74fc`。

## 已验证

- 66 项相关 pytest 回归通过：事实比较、冻结来源、错误账户范围、触价后持有/加仓、纸盘独立门禁、十三条政策 ID、T+1、迟到模型、开盘调度与去重、新错误建议覆盖旧建议、股票名称与通知去重。
- `pnpm build` 通过。安装后的静态首页与构建产物 SHA256 一致，浏览器刷新后可见历史模拟表现核查说明。构建环境 Node 22.23.1，项目声明 24.14.0；本次类型检查和生产构建均成功。
- 历史原始模型输出中三条反向比较均被规则识别。独立数据库中一次冻结证据模型调用覆盖全部持仓并通过比较事实检查；没有生成信号、成交或通知。
- 09:11 完成本地服务更新，进程来自目标项目。日历已加载，模拟扫描间隔 60 秒。API 返回 09:30、09:35、09:40、09:45 开盘批次；激活时间 09:11:23，历史时段不回填。
- 专用盘中 Prompt `intraday_review / 1.0.4-declared-facts` 已激活，策略版本 `p3-v3-declared-facts`；原复盘 Prompt 保持独立。
- 一条新的名称模板验收通知得到 Lark `SENT_ACCEPTED`，含股票名称与代码，明确不是买卖建议。没有重放历史通知。平台接受不等于用户已读。
- 更新前后历史模拟成交和实盘登记记录数量一致；历史证据未改写。两项根因 Issue 进入 MONITORING。

## 尚未完成的自然运行验收

记录时尚未开盘。09:30/09:35/09:40 的自然模型批次、后续行情条件下的 PAPER_ONLY 成交，以及全天账户表现尚不能认定通过。持仓仍为用户声明，未升级为券商核对值；未标 PROD，未合并 main。

## 复现入口

`tests/test_proposal_integrity.py`、`test_portfolio_paper_execution.py`、`test_portfolio_intraday_shadow.py`、`test_portfolio_notifications.py`、`test_daily_workflow_p7.py`、`test_portfolio_advice_api.py`、`test_policy_gate_p3.py`、`test_prompt_registry_p5.py`、`test_notification_decision_router.py`、`test_portfolio_risk_observation.py`、`test_portfolio_live_risk.py`。

私有备份、冻结输入、模型验证输出和 Lark 验收回执只保存在本机忽略目录。公开分支只同步源码、合成回归测试与本验收摘要，不上传资金、股数、成本、数据库、密钥、通知地址或模型原文。
