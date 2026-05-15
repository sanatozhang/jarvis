"""
Crashguard 模块配置 — 独立配置段，与 jarvis 全局配置解耦。

加载顺序: env (CRASHGUARD_*) > config.yaml crashguard 段 > 默认值
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List, Tuple, Type

from pydantic import Field, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from app.config import PROJECT_ROOT, _load_yaml


def _is_in_container() -> bool:
    """检测是否运行在容器内（Docker / containerd / kubepods）。

    底层逻辑：容器内 socket().getsockname() 拿到的是 bridge 网段 IP（172.17/18.x.x），
    对外不可达。autodetect 在容器里没意义，必须强制要求显式 env。
    """
    import os
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r") as f:
            content = f.read()
        return any(k in content for k in ("docker", "kubepods", "containerd"))
    except Exception:
        return False


def _is_docker_bridge_ip(ip: str) -> bool:
    """识别 docker bridge 默认网段 172.16-31.x.x（RFC1918 b 段子集）。

    10.x / 192.168.x 是常见 LAN 段（10.0.52.100 也在里头），不能黑名单。
    只过滤 172.16-31，对应 Docker / Podman 默认 bridge 段。
    """
    parts = ip.split(".")
    if len(parts) != 4 or parts[0] != "172":
        return False
    try:
        second = int(parts[1])
    except ValueError:
        return False
    return 16 <= second <= 31


def _autodetect_frontend_base_url() -> str:
    """探测本机出口 IP，构造默认 frontend URL。

    底层逻辑：多机部署（10.0.52.100 / 102 / ...）若不显式配置 frontend_base_url，
    飞书消息里的链接会回环到 localhost——其他人点开打不到当前部署的页面。
    用 UDP socket connect 8.8.8.8（不真发包，仅触发路由表查询）拿到本机出口 IP。

    ⚠️ 容器内必须显式 env：`CRASHGUARD_FRONTEND_BASE_URL=http://10.0.52.x:3000`。
    本函数检测到在容器内 + 无显式 env 时不再回落 socket 拿到的 bridge IP，
    而是返回带"CONFIGURE"哨兵的 URL，让运维一眼看出来要补 env，避免飞书 PR/告警
    里出现死链 http://172.18.0.3:3000 还看不出来。
    """
    import logging
    import os
    import socket
    # 优先读 env：HOST_IP / DEPLOY_HOST（docker-compose 可注入宿主 IP）
    for key in ("CRASHGUARD_HOST_IP", "HOST_IP", "DEPLOY_HOST"):
        v = (os.environ.get(key) or "").strip()
        if v:
            if v.startswith("http://") or v.startswith("https://"):
                return v
            return f"http://{v}:3000"

    in_container = _is_in_container()

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip != "127.0.0.1":
            # 容器内 + bridge IP → 拒绝（对外不可达，飞书 / PR body 写出去就是死链）
            if in_container and _is_docker_bridge_ip(ip):
                logging.getLogger("crashguard.config").error(
                    "docker bridge IP %s detected inside container; "
                    "MUST set CRASHGUARD_FRONTEND_BASE_URL=http://<host_ip>:3000 in .env",
                    ip,
                )
                return "http://CONFIGURE_CRASHGUARD_FRONTEND_BASE_URL:3000"
            return f"http://{ip}:3000"
    except Exception:
        pass

    if in_container:
        logging.getLogger("crashguard.config").error(
            "running in container without CRASHGUARD_FRONTEND_BASE_URL / HOST_IP; "
            "feishu/PR links will be unreachable"
        )
        return "http://CONFIGURE_CRASHGUARD_FRONTEND_BASE_URL:3000"
    return "http://localhost:3000"


class _YamlSource(PydanticBaseSettingsSource):
    """从 config.yaml crashguard 段读取的低优先级 source"""

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> Tuple[Any, str, bool]:
        # 不实现单字段读取（用 __call__ 批量返回）
        return None, field_name, False

    def __call__(self) -> Dict[str, Any]:
        return _yaml_overrides()


class CrashguardSettings(BaseSettings):
    # Kill switches
    enabled: bool = True
    pr_enabled: bool = True
    feishu_enabled: bool = True
    # 多实例部署时，仅一台机器开启 scheduler，避免双发（兜底；DB 锁是主要去重）
    scheduler_enabled: bool = True

    # Datadog
    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"
    datadog_window_hours: int = 24
    # 哪个 track 含有崩溃数据：rum / logs / trace。Plaud 移动端崩溃在 RUM。
    # 多 track 用逗号分隔（如 "rum,logs"），空 = 单 track。
    datadog_tracks: str = "rum"
    # 搜索 query（event search 语法）。
    # ⚠️ 双路口径（C 路线，对齐 Datadog UI "Crashes" 与 "Errors" 两个独立看板）：
    #   - fatal  → 真崩溃 + ANR + App Hang（App 死/卡）
    #   - non_fatal → 业务侧捕获异常（runZonedGuarded / addError，App 没挂但流程中断）
    # 旧字段 datadog_query 保留兼容（单路全量），新代码请用 fatal/non_fatal。
    datadog_query: str = "*"
    datadog_query_fatal: str = "@error.is_crash:true OR @error.category:ANR OR @error.category:\"App Hang\""
    datadog_query_nonfatal: str = "@type:error -@error.is_crash:true -@error.category:ANR -@error.category:\"App Hang\""

    # Schedule
    morning_cron: str = "0 7 * * *"
    evening_cron: str = "0 17 * * *"
    # 晚报数据窗口（小时）。早报固定用 datadog_window_hours=24h，晚报用此值。
    # 默认 10h = 早报到晚报之间的工作日内增量；基线 = SHoW 上周同 weekday 同 10h 段。
    # 设计意图：早报=昨日 24h 总览，晚报=日内增量信号，两份卡片**不再冗余**。
    evening_window_hours: int = 10

    # Top N + thresholds
    max_top_n: int = 20
    # 批量自动 AI 分析的 Top N 上限
    analyze_top_n: int = 20
    surge_multiplier: float = 1.5
    surge_min_events: int = 10
    regression_silent_versions: int = 3
    feasibility_pr_threshold: float = 0.7
    # 早晚报关注点阈值（vs 昨日变化率）
    daily_surge_threshold: float = 0.10   # +10%
    daily_drop_threshold: float = -0.10   # -10%
    # 噪声治理：events 量级下限。低于此值的 surge / drop 不进 attention，
    # 但「新增 issue」(is_new_in_version) 不受此限制（新代码崩溃永远是信号）。
    daily_attention_min_events: int = 100
    # baseline 量级下限：基线 < 此值时 % 噪声过大，不进 surge attention
    # （5/13 实测案例：a64421ae baseline=172 → +81% 是假大，真增量仅 139；
    #  设 500 → 500 ev 以下的"上周同时段"不参与百分比 surge 判定）
    daily_baseline_min_events_for_pct: int = 500

    # Feishu
    feishu_target_chat_id: str = ""
    # 测试阶段可改用点对点推送给指定邮箱（优先级高于 chat_id）
    feishu_target_email: str = ""
    # 非早晚报告警（hourly_alert / core_metric_alert / job_health）专属投递目标。
    # 早晚报继续走 feishu_target_chat_id 进群；本字段设了 → 告警走点对点 email，
    # 避免噪声打扰群里所有人。空值则退化到 chat_id / target_email 老路径。
    feishu_alert_email: str = ""
    feishu_admin_open_ids: List[str] = Field(default_factory=list)
    # 飞书消息中链接前缀（指向 frontend）
    # 优先级：env CRASHGUARD_FRONTEND_BASE_URL > yaml.frontend_base_url > env HOST_IP 派生
    #        > 本机出口 IP 自动探测 > http://localhost:3000
    # 多机部署/Docker：建议显式 env 设值，避免容器内拿 bridge IP
    frontend_base_url: str = Field(default_factory=_autodetect_frontend_base_url)

    # 半自动 PR 仓库映射（按平台覆盖，未设回落 jarvis code_repo_app）
    repo_path_flutter: str = ""
    repo_path_android: str = ""
    repo_path_ios: str = ""
    # PR 去重窗口（同一 issue+platform 30 天内只允许一个 draft PR）
    pr_dedup_days: int = 30
    # PR 状态同步 cron（拉 GitHub 现态回填 DB）；默认每 30 分钟
    # 关闭/合并后 30min 内同步到 jarvis，DRAFT → CLOSED 不会残留
    pr_sync_cron: str = "*/30 * * * *"
    # 启动后延迟一次性跑 pipeline + auto-analyze（避免重启等到 07:00 才开始）
    warmup_on_startup: bool = True
    # 周期 pipeline cron（与早晚报解耦）；默认每 4 小时整点
    pipeline_cron: str = "0 */4 * * *"

    # 「线上最新版本」手动覆盖（按平台），留空则按崩溃数据自动派生
    current_release_flutter: str = ""
    current_release_android: str = ""
    current_release_ios: str = ""
    # 数据派生阈值：某版本累计 events 不足该值则不视作"线上版本"（过滤灰度/测试包）
    latest_version_min_events: int = 300
    # AI 分析去重窗口（小时）：自动触发场景下，若 issue 在该窗口内已有 success 分析，
    # 直接复用——避免 warmup/cron/batch 多入口重复烧 token。UI 重新分析按钮始终强制重跑。
    analysis_dedup_hours: int = 6
    # AI 分析定时小步分批：避免一次跑 20 个被杀。每 N 分钟 tick 一次，每次最多 K 个。
    # 默认每 5 分钟 2 个 → Top 20 backlog 约 50 分钟跑完（之前 1 个/tick 要 100min）；
    # 配套 scheduler._analyze_running 重入保护，前 tick 没跑完不会同时启第二批。
    # 上调依据：今日 funnel 显示 12/20 卡 no_analysis，是最大瓶颈。
    analyze_cron: str = "*/5 * * * *"
    analyze_max_per_tick: int = 2

    # === 3h 告警（SHoW-3h 同周同 3 小时块对比）===
    # 每 3 小时拉 Datadog，对比上周同 weekday 同 3h 块 events，超过阈值或新增 issue 发飞书告警。
    # 3h 块对齐到 UTC 00/03/06/09/12/15/18/21；小时颗粒度噪声大，工作日/周末活跃差异大时
    # 3 小时块是 P&L 平衡点。早晚报和此告警都不含 PR 修复内容。
    hourly_alert_enabled: bool = True
    # cron 每 3 小时块的第 5 分钟触发：Datadog ingest 延迟 3-5 分钟，避开数据未到位
    hourly_alert_cron: str = "5 */3 * * *"
    # 上涨阈值（百分比，默认 10%）
    hourly_alert_growth_threshold_pct: float = 10.0
    # 「新增」窗口：最近 N 天首次出现的 issue 视为新增
    hourly_alert_new_window_days: int = 30
    # SHoW 基线最小 events（< 此值不参与百分比计算，防小基数噪声）
    hourly_alert_min_baseline_events: int = 20
    # 卡片最多展示 issue 数（聚合 digest）
    hourly_alert_max_items: int = 10
    # 绝对量级阈值：单 issue 在窗口内 sessions_affected < 此值不入告警（脏数据/极低频噪声过滤）
    # 注：Plaud RUM 未 setUser，users_affected 全 0（已知 data hole），用 sessions 代理 user
    # （24h 内典型 1-3 sessions/user，相关性高）。卡片文案显示「受影响会话 ≥ N」。
    # 500 = 5/14 二次上调：100 仍贴线触发（如 AppHang 148 会话也告警，业务量级无意义）
    # 与 core_metric_min_sessions=500 对齐，颗粒度统一
    hourly_alert_min_sessions: int = 500
    # 绝对事件量底线：单 issue 在窗口内 events_h < N 不告警。
    # 抓手：基线 100 + 当前 120 = +20% 触发，但绝对增量才 20，对 Plaud 量级无业务意义。
    # 加这一闸把"小基数 issue 在 sessions 大涨时被反复挑出来"的噪声砍掉。
    hourly_alert_min_events_absolute: int = 200
    # 跨告警去重窗口（小时）：同 issue_id 在过去 N 小时已被 hourly 告警过 → 本 tick 跳过；
    # 早晚报 attention 列表也会扣掉这部分。0 = 关闭去重。
    # 默认 12h：覆盖早晚报+下一 hourly cron，防同 issue 跨告警类型反复点名。
    hourly_alert_dedup_hours: int = 12

    # ===== 通道 1：新版本桶（C3）=====
    hourly_alert_new_version_enabled: bool = True
    hourly_alert_new_version_shadow_mode: bool = True       # Phase 0 影子模式，仅写 audit log 不发卡
    hourly_alert_new_version_min_events: int = 30           # events 地板
    hourly_alert_new_version_user_rate_pct: float = 0.005   # 0.5% 用户占比

    # ===== 通道 3：全局新 crash 兜底（D3）=====
    hourly_alert_new_crash_enabled: bool = True
    hourly_alert_new_crash_shadow_mode: bool = True
    hourly_alert_new_crash_window_hours: int = 24           # 累计窗口
    hourly_alert_new_crash_min_events: int = 150            # events 地板
    hourly_alert_new_crash_min_sessions: int = 300          # sessions 地板

    # === 核心指标报警（10 分钟粒度 crash-free sessions % 监控）===
    # 底层逻辑：早晚报是 24h 大盘，hourly_alert 是单 issue 突增/新增；核心指标补的是
    # "整体健康度"颗粒度——即使没有单 issue 飙升，整体 crash-free 跌穿基线也要报警。
    # 用 Datadog Mobile RUM 原生口径：(1 - crashed_sessions/total_sessions) * 100。
    # 对比基线：当前 10min 窗口 vs 前 1h 平均 crash_free_pct。
    core_metric_enabled: bool = True
    core_metric_cron: str = "*/10 * * * *"
    # 报警触发阈值：crash_free_pct 相对前 1h 变化绝对值 >= N pp（percentage points）
    # 例：基线 99.5%，当前 99.0% → 变化 0.5 pp，>=0.3 触发
    core_metric_change_threshold_pp: float = 0.3
    # 绝对量级阈值：当前 10min 窗口 total_sessions < N 不告警（小流量噪声）
    # 500 = 覆盖晨间 / 周末低峰；高峰期 145 sess 这种数据统计上无意义（5/14 上调：原 100 太低）
    core_metric_min_sessions: int = 500
    # 最小 crashed_sessions 门槛：crashed < N 不告警（哪怕 rate Δ 过阈值）
    # 抓手：1 个 user 挂 1 次不应升级到"全平台健康度劣化"告警。
    # 与 min_sessions 双保险——前者过滤"分母不够"，后者过滤"分子绝对意义为 0"。
    core_metric_min_crashed_sessions: int = 3
    # 监控平台白名单（小写逗号串），空 = 不限制
    core_metric_platforms: str = "android,ios"

    # === Baseline 周度回填 ===
    # 底层逻辑：hourly_alert / 早晚报 SHoW 基线依赖 crash_hourly_snapshots /
    # crash_snapshots 表。日常 pipeline + tick 失败或限流时会留窗口空洞。每周一次
    # 调 Datadog 历史 API 幂等补齐近 3 天，保证基线持续可用。
    baseline_backfill_enabled: bool = True
    baseline_backfill_cron: str = "0 18 * * 0"   # 周日 UTC 18:00 = 北京 周一 02:00

    # === 定时任务健康度兜底告警 ===
    # 底层逻辑：cron 类任务静默失败是最大盲点。每 5 分钟扫一遍 heartbeat 表，
    # 任一任务 health ∈ (failing, stale) 且距上次告警 > cooldown 分钟 → 聚合发飞书
    job_health_alert_enabled: bool = True
    job_health_alert_cron: str = "*/5 * * * *"
    job_health_alert_cooldown_minutes: int = 30   # 同任务告警节流窗口（工作日）
    # 周末节流倍数：周末 cooldown × N（默认 4 = 2h 一次同任务告警；改 1 关闭周末降频）
    job_health_alert_weekend_multiplier: int = 4
    # 连续失败几次才告警（含自愈失败后；默认 2）
    job_health_alert_fail_threshold: int = 2
    # 自愈重跑节流（同任务多久内不重复重跑）
    job_health_alert_retry_throttle_minutes: int = 10
    # degraded 弱信号阈值：连续 N 次「部分失败」（含 degraded + failed 混合）
    # 才升级为 failing 发告警。抓手：避免 1/12 transient 误报，但持续 systemic
    # 问题仍能拦截。默认 6 = pr_sync 30min 间隔下连续 3h 才告警。
    job_health_alert_degraded_threshold: int = 6

    # === PR 质量闸门（12 道防线，按 ROI 默认全开）===
    # 输入端
    gate_confidence_enabled: bool = True       # Gate#3：confidence/feasibility 门槛
    gate_min_confidence: str = "high"           # 仅 high 放行；medium 进 attention 但不开 PR
    gate_force_route_enabled: bool = True       # Gate#2：stack→平台强制路由
    gate_path_verify_enabled: bool = True       # Gate#1：fix_diff 路径实存性
    gate_path_min_ratio: float = 0.5            # 实存比 < 50% 拒绝
    # 过程端
    # Gate#4 禁 Write / Gate#5 实存文件清单 / Gate#6 git clean -fdx 全部在 _run_implementation_agent 内硬编码生效
    # 输出端
    gate_keyword_enabled: bool = True           # Gate#8：关键词命中
    gate_keyword_min_hits: int = 1
    gate_syntax_enabled: bool = True            # Gate#7：语法速检（best-effort）
    gate_llm_judge_enabled: bool = False        # Gate#9：二级 LLM 判官（默认关，开 = 每 PR 多一次 agent 调用成本）
    gate_llm_judge_min_score: int = 7
    # 路由端
    gate_primary_only_enabled: bool = True      # Gate#10：多候选合议为单 primary
    # 闭环端
    # Gate#11：人工 approve 总闸——开启后 PR 不直接落 draft，而是落 needs_review；前端 approve 才推送
    pr_manual_approve_mode: bool = False
    # Gate#12：PR 落地后 CI 反馈
    gate_ci_feedback_enabled: bool = True
    gate_ci_feedback_close_on_fail: bool = True  # CI 失败自动关 PR
    # Gate#14：老 draft 污染自动关闭——draft >N 小时且 diff 含 pubspec / .gen.dart
    # 等污染文件，pr_sync tick 内自动关 PR，等下个 cron 用干净 base 重生。
    # 抓手：#987 stale-base 链遗留的 pubspec bump 类问题，源头治理后兜底清扫。
    gate_draft_pollution_enabled: bool = True
    gate_draft_pollution_min_age_hours: int = 24
    # 可自动修复的平台白名单——不在名单内（如 BROWSER）的 issue 直接跳过 AI 分析和 PR，
    # 避免浪费 token 且永远无法生成 PR（BROWSER 是 JS，无对应 mobile 代码仓库）
    auto_pr_fixable_platforms: List[str] = Field(default_factory=lambda: ["android", "ios", "flutter"])

    # === PR Review 自动响应（Step 3）===
    # 默认关，启用前先在测试 PR 上验过。开启后 pr_sync tick 内会拉每条 open PR 的
    # reviews，对未响应的 review 调 LLM 评判：问题真存在 → 修复 commit；不存在 →
    # 发评论解释。所有 Gate#1-13 闸门复用。
    pr_review_response_enabled: bool = False
    # 每条 PR 最多自动响应 N 轮（防 fix-break-refix 循环）
    pr_review_response_max_iterations: int = 3
    # 同 PR 距上次 dispatch ≤ N min 跳过（cooldown 节流）
    pr_review_response_cooldown_minutes: int = 30
    # 允许响应的 reviewer 白名单——其它 author（特别是 owner 本人 / unknown bot）跳过
    # 默认覆盖 Copilot / Codex / Claude；人工评审走人工 PR 流程，agent 不抢
    pr_review_response_allowed_authors: list[str] = [
        "copilot-pull-request-reviewer",
        "chatgpt-codex-connector",
        "claude",
    ]
    # review body 字符数下限——太短的 review（如 "LGTM" "+1"）信噪比低，直接跳过
    pr_review_response_min_body_chars: int = 50

    # === Top crash 专属自动 PR（解决 Top N 因全局阈值过保守而无 PR 的问题）===
    # 默认 enabled=True，因为这是用户主动需求；threshold 比全局低（0.5 vs 0.7），
    # 抓手是：Top crash 即使 feasibility 一般也优先派人看一眼（开 PR 比无解强）
    top_crash_auto_pr_enabled: bool = True
    # cron 间隔——默认每 2h 一次（pr_drafter 调用本身较重，不必每 5min 扫）
    top_crash_auto_pr_cron: str = "0 */2 * * *"
    # 扫描的 Top N
    top_crash_auto_pr_top_n: int = 20
    # 专属低门槛（全局 feasibility_pr_threshold=0.7，Top 放宽到 0.5）
    top_crash_auto_pr_threshold: float = 0.5
    # 每 tick 最多开 N 个 PR（防一次 spam 出 20 个）
    top_crash_auto_pr_max_per_tick: int = 3
    # 已有 closed PR 的 issue 是否重试——默认 False 防反复开烂 PR
    top_crash_auto_pr_retry_on_closed: bool = False
    # Top 专属 Gate#3 confidence 门槛——其他入口仍卡 high
    # 选项: low / medium / high；默认 medium（high 太严会卡掉大半 Top crash）
    top_crash_min_confidence: str = "medium"

    # === Phase 1 深度诊断 ===
    deep_analysis_enabled: bool = True
    deep_analysis_timeout_seconds: int = 1800          # 30 分钟，可调
    deep_analysis_dedup_hours: int = 6                 # 6h 内不重复跑
    deep_analysis_auto_proceed_threshold: float = 0.9  # 快车道置信度门槛

    model_config = {
        "env_prefix": "CRASHGUARD_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def _backfill_repo_paths_from_jarvis_env(self):
        """老 jarvis 约定 (CODE_REPO_APP / CODE_REPO_PATH) 兜底 + 壳工程自动下钻。

        底层逻辑：jarvis 老约定里 `CODE_REPO_APP` / `CODE_REPO_PATH` 指向的是
        **Plaud-App 壳工程**（含 `.gitmodules`，里面是 plaud-android/plaud-ios/
        plaud-flutter-common 三个子模块）；新约定 `CRASHGUARD_REPO_PATH_*` 直接指子模块。
        两套语义混用，如果直接把壳路径塞进 `repo_path_flutter`，pr_drafter 拿到的就是
        壳本身——AI agent 在壳工程改文件 → PR 进 Plaud-App 而不是子模块 repo。

        owner 意识做法：识别 wrapper（有 `.gitmodules` + 标准子目录），**自动下钻**到
        子模块；下钻失败时**留空**，让 `_platform_repo_path` 走它自己的 sub_default
        路径推导逻辑，绝不把壳路径直接当 direct path。

        优先级：
        - repo_path_flutter 空 → CRASHGUARD_REPO_PATH_FLUTTER → 壳下钻 plaud-flutter-common
        - repo_path_android 空 → CRASHGUARD_REPO_PATH_ANDROID → 壳下钻 plaud-android
        - repo_path_ios     空 → CRASHGUARD_REPO_PATH_IOS → 壳下钻 plaud-ios
        """
        import os
        from pathlib import Path as _P

        def _wrapper_or_sub(raw: str, sub_default: str) -> str:
            """若 raw 是 wrapper（含 .gitmodules + sub_default 子目录），返回子目录。
            否则按 raw 原样返回（包括 raw 本身就是子模块路径的情况）。
            找不到子目录则返回空，让上游 _platform_repo_path 走自己的回落。
            """
            if not raw:
                return ""
            p = os.path.expanduser(raw)
            if not os.path.isdir(p):
                return ""
            # 是否为 wrapper：有 .gitmodules 文件
            if os.path.isfile(os.path.join(p, ".gitmodules")):
                cand = os.path.join(p, sub_default)
                if os.path.exists(os.path.join(cand, ".git")):
                    return cand
                # wrapper 存在但子目录没 init → 留空让上层报错（避免悄悄落到壳）
                return ""
            # 不是 wrapper，本身可能就是子模块 → 原样返回
            return p

        wrapper_env = (
            os.environ.get("CODE_REPO_APP")
            or os.environ.get("CODE_REPO_PATH")
            or ""
        )

        if not self.repo_path_flutter:
            self.repo_path_flutter = _wrapper_or_sub(wrapper_env, "plaud-flutter-common")
        if not self.repo_path_android:
            raw = os.environ.get("CODE_REPO_ANDROID") or wrapper_env
            self.repo_path_android = _wrapper_or_sub(raw, "plaud-android")
        if not self.repo_path_ios:
            raw = os.environ.get("CODE_REPO_IOS") or wrapper_env
            self.repo_path_ios = _wrapper_or_sub(raw, "plaud-ios")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        # 优先级（左 > 右）: init_kwargs > env > dotenv > yaml > defaults
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls),
            file_secret_settings,
        )


def _yaml_overrides() -> Dict[str, Any]:
    """从 config.yaml crashguard 段读取覆盖项"""
    cfg = _load_yaml().get("crashguard") or {}
    flat: Dict[str, Any] = {}
    for k in (
        "enabled", "pr_enabled", "feishu_enabled", "scheduler_enabled",
        "max_top_n", "analyze_top_n",
    ):
        if k in cfg:
            flat[k] = cfg[k]
    if "thresholds" in cfg:
        t = cfg["thresholds"] or {}
        for k_yaml, k_py in [
            ("surge_multiplier", "surge_multiplier"),
            ("surge_min_events", "surge_min_events"),
            ("regression_silent_versions", "regression_silent_versions"),
            ("feasibility_pr_threshold", "feasibility_pr_threshold"),
            ("daily_surge_threshold", "daily_surge_threshold"),
            ("daily_drop_threshold", "daily_drop_threshold"),
            ("daily_attention_min_events", "daily_attention_min_events"),
        ]:
            if k_yaml in t:
                flat[k_py] = t[k_yaml]
    if "datadog" in cfg:
        d = cfg["datadog"] or {}
        if "site" in d:
            flat["datadog_site"] = d["site"]
        if "tracks" in d:
            v = d["tracks"]
            flat["datadog_tracks"] = ",".join(v) if isinstance(v, list) else str(v)
        if "query" in d:
            flat["datadog_query"] = d["query"]
        if "query_fatal" in d:
            flat["datadog_query_fatal"] = d["query_fatal"]
        if "query_nonfatal" in d:
            flat["datadog_query_nonfatal"] = d["query_nonfatal"]
        if "query_non_fatal" in d:
            flat["datadog_query_nonfatal"] = d["query_non_fatal"]
        if "window_hours" in d:
            flat["datadog_window_hours"] = int(d["window_hours"])
    if "feishu" in cfg:
        f = cfg["feishu"] or {}
        if "target_chat_id" in f:
            flat["feishu_target_chat_id"] = f["target_chat_id"]
        if "target_email" in f:
            flat["feishu_target_email"] = f["target_email"]
        if "alert_email" in f:
            flat["feishu_alert_email"] = f["alert_email"]
        if "admin_open_ids" in f:
            flat["feishu_admin_open_ids"] = f["admin_open_ids"]
        if "morning_cron" in f:
            flat["morning_cron"] = f["morning_cron"]
        if "evening_cron" in f:
            flat["evening_cron"] = f["evening_cron"]
        if "evening_window_hours" in f:
            flat["evening_window_hours"] = int(f["evening_window_hours"])
    if "repo_paths" in cfg:
        rp = cfg["repo_paths"] or {}
        if "flutter" in rp:
            flat["repo_path_flutter"] = rp["flutter"]
        if "android" in rp:
            flat["repo_path_android"] = rp["android"]
        if "ios" in rp:
            flat["repo_path_ios"] = rp["ios"]
    if "frontend_base_url" in cfg:
        flat["frontend_base_url"] = cfg["frontend_base_url"]
    if "pr_dedup_days" in cfg:
        flat["pr_dedup_days"] = int(cfg["pr_dedup_days"])
    if "pr_sync_cron" in cfg:
        flat["pr_sync_cron"] = str(cfg["pr_sync_cron"])
    if "warmup_on_startup" in cfg:
        flat["warmup_on_startup"] = bool(cfg["warmup_on_startup"])
    if "pipeline_cron" in cfg:
        flat["pipeline_cron"] = str(cfg["pipeline_cron"])
    if "current_release" in cfg:
        cr = cfg["current_release"] or {}
        if isinstance(cr, dict):
            if "flutter" in cr:
                flat["current_release_flutter"] = str(cr["flutter"] or "")
            if "android" in cr:
                flat["current_release_android"] = str(cr["android"] or "")
            if "ios" in cr:
                flat["current_release_ios"] = str(cr["ios"] or "")
    if "latest_version_min_events" in cfg:
        flat["latest_version_min_events"] = int(cfg["latest_version_min_events"])
    if "analysis_dedup_hours" in cfg:
        flat["analysis_dedup_hours"] = int(cfg["analysis_dedup_hours"])
    if "deep_analysis_enabled" in cfg:
        flat["deep_analysis_enabled"] = bool(cfg["deep_analysis_enabled"])
    if "deep_analysis_timeout_seconds" in cfg:
        flat["deep_analysis_timeout_seconds"] = int(cfg["deep_analysis_timeout_seconds"])
    if "deep_analysis_dedup_hours" in cfg:
        flat["deep_analysis_dedup_hours"] = int(cfg["deep_analysis_dedup_hours"])
    if "deep_analysis_auto_proceed_threshold" in cfg:
        flat["deep_analysis_auto_proceed_threshold"] = float(
            cfg["deep_analysis_auto_proceed_threshold"]
        )
    if "analyze_cron" in cfg:
        flat["analyze_cron"] = str(cfg["analyze_cron"])
    if "analyze_max_per_tick" in cfg:
        flat["analyze_max_per_tick"] = int(cfg["analyze_max_per_tick"])
    if "hourly_alert" in cfg:
        ha = cfg["hourly_alert"] or {}
        if isinstance(ha, dict):
            if "enabled" in ha:
                flat["hourly_alert_enabled"] = bool(ha["enabled"])
            if "cron" in ha:
                flat["hourly_alert_cron"] = str(ha["cron"])
            if "growth_threshold_pct" in ha:
                flat["hourly_alert_growth_threshold_pct"] = float(ha["growth_threshold_pct"])
            if "new_window_days" in ha:
                flat["hourly_alert_new_window_days"] = int(ha["new_window_days"])
            if "min_baseline_events" in ha:
                flat["hourly_alert_min_baseline_events"] = int(ha["min_baseline_events"])
            if "max_items" in ha:
                flat["hourly_alert_max_items"] = int(ha["max_items"])
            if "min_sessions" in ha:
                flat["hourly_alert_min_sessions"] = int(ha["min_sessions"])
            if "min_events_absolute" in ha:
                flat["hourly_alert_min_events_absolute"] = int(ha["min_events_absolute"])
            if "dedup_hours" in ha:
                flat["hourly_alert_dedup_hours"] = int(ha["dedup_hours"])
            # 通道 1 / 3 配置
            new_version = (ha or {}).get("new_version") or {}
            if "enabled" in new_version:
                flat["hourly_alert_new_version_enabled"] = bool(new_version["enabled"])
            if "shadow_mode" in new_version:
                flat["hourly_alert_new_version_shadow_mode"] = bool(new_version["shadow_mode"])
            if "min_events" in new_version:
                flat["hourly_alert_new_version_min_events"] = int(new_version["min_events"])
            if "user_rate_pct" in new_version:
                flat["hourly_alert_new_version_user_rate_pct"] = float(new_version["user_rate_pct"])

            new_crash = (ha or {}).get("new_crash") or {}
            if "enabled" in new_crash:
                flat["hourly_alert_new_crash_enabled"] = bool(new_crash["enabled"])
            if "shadow_mode" in new_crash:
                flat["hourly_alert_new_crash_shadow_mode"] = bool(new_crash["shadow_mode"])
            if "window_hours" in new_crash:
                flat["hourly_alert_new_crash_window_hours"] = int(new_crash["window_hours"])
            if "min_events" in new_crash:
                flat["hourly_alert_new_crash_min_events"] = int(new_crash["min_events"])
            if "min_sessions" in new_crash:
                flat["hourly_alert_new_crash_min_sessions"] = int(new_crash["min_sessions"])
    if "core_metric" in cfg:
        cm = cfg["core_metric"] or {}
        if isinstance(cm, dict):
            if "enabled" in cm:
                flat["core_metric_enabled"] = bool(cm["enabled"])
            if "cron" in cm:
                flat["core_metric_cron"] = str(cm["cron"])
            if "change_threshold_pp" in cm:
                flat["core_metric_change_threshold_pp"] = float(cm["change_threshold_pp"])
            if "min_sessions" in cm:
                flat["core_metric_min_sessions"] = int(cm["min_sessions"])
            if "min_crashed_sessions" in cm:
                flat["core_metric_min_crashed_sessions"] = int(cm["min_crashed_sessions"])
            if "platforms" in cm:
                v = cm["platforms"]
                flat["core_metric_platforms"] = ",".join(v) if isinstance(v, list) else str(v)
    if "job_health_alert" in cfg:
        jha = cfg["job_health_alert"] or {}
        if isinstance(jha, dict):
            if "enabled" in jha:
                flat["job_health_alert_enabled"] = bool(jha["enabled"])
            if "cron" in jha:
                flat["job_health_alert_cron"] = str(jha["cron"])
            if "cooldown_minutes" in jha:
                flat["job_health_alert_cooldown_minutes"] = int(jha["cooldown_minutes"])
    return flat


@lru_cache
def get_crashguard_settings() -> CrashguardSettings:
    """获取 crashguard 配置（cached singleton）

    优先级由 ``settings_customise_sources`` 注册：env > dotenv > yaml > defaults。
    """
    return CrashguardSettings()
