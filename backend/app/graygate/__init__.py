"""Graygate — 4.0.3 灰度期临时监控模块。

灰度期每日拉取 Datadog Native 看板（`mbn-8h9-m2p`）11 项核心指标，按平台
（iOS/Android）× 版本口径（最新版/大盘）汇总，推送飞书「4.0灰度数据跟进群」。

独立模块，灰度结束可整体摘除，不侵入 crashguard / coreguard 现有告警链路。

隔离约束（本模块自定，不在 `.importlinter` 合约里，但同样要遵守）：
- 不 import `app.coreguard.*`（crashguard 已 import coreguard，反向 import 会成环）
- 不 import `app.models`（如需 DB 用 `app.db.database.get_session`）
- 依赖方向单向向下：graygate → {crashguard.datadog_client 的只读工具函数, services.feishu_cli, db.database}
"""
