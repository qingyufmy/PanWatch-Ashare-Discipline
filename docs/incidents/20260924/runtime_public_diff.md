# 运行版与公开仓库对应关系

- 本地运行进程 2026-09-24 12:50 从 `D:\盯盘\PanWatch\server.py` 启动，
  当时源码 HEAD 为 `ac48c4d9d4dabe449ffae09d9424c5525567de8a`。
  运行中的 Python 模块不会因工作树编辑自动更新。
- 公开仓库 `main@80fbfeceaaac307ca6757c5ec924d9d3a4ee698c` 是
  15:52 的白名单运行归档提交，不应称为实际运行源码的提交。
- 公开审查分支 `codex/trading-day-review@379f6517bb02216a78ee7cbb3fb6fc0b9313fad9`
  包含此前待审源码。该仓库与本地运行仓库有独立 Git 历史；文件相同不等于
  提交对象相同。
- 本次整改先写入本地 `codex/trading-day-readiness`。新代码尚未通过部署、
  自然日 Shadow 和用户审阅；不得把新表或消息行为说成 9 月 24 日已运行。

比较命令：

```powershell
git -C D:\盯盘\PanWatch rev-parse HEAD
git -C D:\盯盘\PanWatch-public-review rev-parse HEAD
git -C D:\盯盘\PanWatch-public-archive rev-parse HEAD
```
