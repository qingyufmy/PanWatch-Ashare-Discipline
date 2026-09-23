# P0 本地复现与验收运行手册

环境：Windows PowerShell，Python 3.11，Node 22.23.1（前端声明要求 24.14.0），pnpm 9.15.9。以下步骤只用于本地新克隆的 PanWatch，不连接用户账户、模型 Key 或券商。

1. 在仓库根目录创建 `.venv`，运行 `.venv\Scripts\python.exe -m pip install -r requirements.txt`；前端目录运行 `pnpm install --frozen-lockfile`。本机 Python 对含中文路径的 editable `.pth` 误按 GBK 读取，曾在隔离环境内将四个 `_editable_impl_*.pth` 转为 GBK 后恢复；该兼容处理未改源码。
2. 后端：`.venv\Scripts\python.exe -m pytest tests/ -q --disable-warnings`。四个 `packages/*/tests` 目录分别运行 pytest，避免两个不同包的 `test_registry.py` 在同一进程中发生导入名冲突。前端：`pnpm exec vitest run`、`pnpm build`。
3. 本地启动烟测：设置 `PLAYWRIGHT_SKIP_BROWSER_INSTALL=1`，运行 `.venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 18000`，检查 `/api/health`、`/api/version` 与本地 SQLite schema。验证后 Ctrl+C 停止。启动会建立本地数据库、种子数据和调度器；不可对用户现有 DB 直接照搬。
4. 验收时分别标记源码、离线测试、本地启动、真实账户、实盘业务链的证据级别。`/api/health` 仅证明 HTTP 就绪，不证明持仓、行情、信号和执行链可用。

本次 P0 的具体结果见 `p0_acceptance.md`。无需运行任何真实下单命令。
