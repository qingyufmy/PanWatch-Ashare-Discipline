# 2026-09-24 风险提案未触达：冻结基线

冻结时刻：2026-09-24 16:39 北京时间。工作目录 `D:\盯盘\PanWatch`，分支
`codex/trading-day-readiness`，HEAD `ac48c4d9d4dabe449ffae09d9424c5525567de8a`。
冻结前 `git status --short` 为空。服务进程 PID 53800/3540 于当日 12:50:04
从该目录的 `server.py` 启动；源码编辑不会改变已加载进程。本文件只确认启动路径，
不以健康接口代替业务验收。运行环境的完整配置和数据库留在本机忽略目录。

SQLite 在线只读连接通过 `sqlite3.Connection.backup` 冻结到
`data/incidents/20260924/panwatch-20260924-frozen.db`。源库未因本次审计改写。
冻结副本 88,064,000 字节，SHA-256：
`2c150ec2e784a25c6fcee0e86fb9360b596cce2e1add83fa995e4b30df538f20`。
最高成功迁移版本为 140。私有副本包含账户、模型、通知资料，禁止公开上传。

当日工作流窗口 UTC 2026-09-23 22:50:00 至 2026-09-24 07:30:41；
信号生成记录窗口 UTC 2026-09-24 01:02:15 至 07:49:36。
计数由 `scripts/audit-portfolio-incident-20260924.py` 从冻结副本重建：
364 条 Signal、32 条批准封套（其中调仓 0）、118 条减仓/退出方向提案、
34 个按同票 30 分钟间隔划分的风险事件段、2 条通知、0 条模拟成交、
0 条用户执行记录。30 条模型生成信号的时间早于模型完成。

`requirements.txt` SHA-256 为
`ea2a175dd81e5f40639fe339e52395752510a8cc14a273abdd30c64695cf7667`；
`pyproject.toml` 为
`b6f8a525b4af500f045bd42557fafe65a6fc8ac0c3a7c3d8eed12068f0f7d424`；
`frontend/pnpm-lock.yaml` 为
`ffa23a1ba2796664e7eb78d8672d47ca4a2ade65b3fa6a6c078024ab532c18ed`。

账户资金和持仓为用户声明，券商真相未核验。公开运行归档与本地私有库的证据
粒度不同，不能用公开摘要推断模型原文、真实成交或用户已读。
