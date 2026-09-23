# 公开版本与每日归档

本项目从 [TNT-Likely/PanWatch](https://github.com/TNT-Likely/PanWatch) 派生，保留原项目的 MIT 许可证及署名；AI 投研组件参考 [TradingAgents](https://github.com/TauricResearch/TradingAgents)。本仓库公开本地 A 股持仓纪律工作流的源码快照及按交易日筛选的运行证据。

`daily_archive/YYYY-MM-DD/` 由 `scripts/export-public-daily.py` 生成。`manifest.json` 记录源代码版本、生成时间、盘后步骤是否出现以及各类记录数量；其余 JSON 包含持仓快照摘要、工作流、计划、信号、规则判定、模型调用元数据、分钟数据覆盖、问题台账和 P10 验收指标。`FAIL_CLOSED`、`REVIEW` 与 `MISSED` 均是原样结果，不代表验收通过或交易执行。

公开归档只导出明确列出的字段。账户编号、资产/现金、持仓数量与成本、模型原文、通知地址、API 密钥、原始 SQLite 和本地服务日志均不上传。分钟行情仅公布覆盖情况，不转载行情原始数据。原始证据保留在本机。系统不连接券商下单。

收盘后在本地运行：

```powershell
.\.venv\Scripts\python.exe scripts\export-public-daily.py --trade-date YYYY-MM-DD --output-root D:\盯盘\PanWatch-public\daily_archive
```

每日归档反映生成时已完成的自然日步骤。21:20 的 P10 定期复核晚于收盘归档，收盘文件中的门禁状态是生成时快照。
