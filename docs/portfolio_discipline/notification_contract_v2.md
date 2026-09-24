# 持仓动作与事实引用合同 V2（候选）

版本日期：2026-09-23。适用范围：已登记的至多 20 只 A 股持仓，研究建议、人工执行登记与审计。该版本仍为 CANDIDATE；`portfolio_discipline_mode` 默认 `disabled`。

## 决策主链

`原始行情 → 标准化分钟线 → FeatureSnapshot/LevelSnapshot → 模型提案或确定性风险 → SignalEvent/EvidenceSnapshot → Policy 逐规则裁决 → 每持仓当前 PortfolioDecision → Notification Outbox → Lark 平台接受状态 → 用户执行报告 → 独立对账`。

模型提案先写建议池与 Signal，不用模型正文直接发动作。`save_suggestion_result` 返回保存、信号、裁决与通知 ID；任一步失败回滚事务且不发。相同建议也保留原始提案审计，通知只在动作、批准数量、失守的冻结支撑、执行状态等语义改变时入队。旧响应、旧计划或待复核响应不得覆盖更新的已批准决策。盘中事件门控先选有变化的股票；没有事件时模型调用数为 0。确定性 Hard Risk 独立于模型，扫描延续到 15:00。

用户视图的动作只有 `ADD/REDUCE/HOLD/EXIT`。`REVIEW` 是内部未定方向，`OPEN` 属于未持仓研究，不转成持仓 `ADD`。动作与以下状态分列：`decision_status`、`risk_status`、`data_status`、`execution_status`、`delivery_status`。未知、冲突、模型失败、行情失联不填成正常 HOLD。待复核方向可以存于私有决策账本，不能附获批股数发送可执行动作。

## 数量与价位授权

Policy 不使用模型的 `qty_hint` 授权股数。ADD 使用已核实 NAV、可用现金、目标权重、当时价格与证券规则；REDUCE/EXIT 使用已核实总持仓、可卖数、目标权重、报价与证券规则。规则须按股票登记并标明来源、有效时间、最小买量及递增单位、最小卖量及递增单位、最小报价单位；质量为 `VERIFIED` 才进入批准。不可用时返回 REVIEW。实际交易规则以对应证券的当期交易所及券商主数据为准，不能仅按代码前缀猜测。

旧系统中用户声明的总资产、可卖数等于总持仓数仍是 `REVIEW_ONLY`，不能将其写为券商已核实。用户登记 `USER_REPORTED` 不等于成交；数量与后续快照相等只记 `QUANTITY_MATCHED_UNVERIFIED`，只有独立核实后才可使用已对账状态。手动登记的 `client_request_id` 保证重复提交返回同一记录，累计不得超过批准量；页面显示尚余批准量。

模型只引用同一冻结 Feature/Level 的 ID、时间与事实。自由文本中的价格、目标数量、胜率与成交声明不进入通知渲染。若 Feature/Level ID 不匹配、过时、质量不足或价格非有限正数，动作卡片不发布数字价位；需要的字段缺失则仅保留明确的复核或确定性风险告警。盘前计划注明未触发，盘中实时触价与已收线确认分别记录，硬退出线仅来自已批准计划，不能把观察线当订单。

## 对外消息

标题以清仓离场、减仓、加仓、持有观察开头。消息依次显示决策与执行状态、批准数量及目标、现价和 `market_data_asof`、至多三条可引用数字事实、已确认支撑压力、失效条件与人工复核指引。正常 HOLD 只在组合摘要中出现；升级、撤销及风险变化产生新版本。盘前和盘后各一份组合摘要，Hard Risk 每持仓按交易日、计划版本及原因去重。发送成功只表示 Lark 接口接受，不能推断已读或已成交。

## 启用边界

新 Prompt `1.1.0-notification-candidate`、Policy `p3-v2-candidate`、模板 `portfolio-text-v2-candidate` 均保持候选；现有 Prompt 不被启动 seed 自动替换。先用冻结样本回放，再观察新代码自然交易日 Shadow，审阅真实消息和 P10 门禁后，由用户批准切换。当前本地隔离分支测试不构成真实自然日验收。
