# Crashguard PR 自动指派 Reviewer 设计

**Goal**: Crashguard 自动产生的 PR 创建后，通过 git blame 找到改动行的原作者，自动通过飞书 IM 通知他来 review；找不到原作者时通知 sanato；未 review 的 PR 每日滚动提醒，直到 review 或 PR 关闭。

**Author**: sanato
**Date**: 2026-05-21
**Status**: Draft — pending approval

---

## 1. 背景与目标

### 1.1 现状
Crashguard 已能从 Datadog 崩溃数据自动生成 PR（见 `app/crashguard/services/pr_drafter.py`），近 24 小时已有 PR 落地 plaud-flutter-global 等仓。但目前 PR 创建后**没有任何人被通知**，导致：
- PR 静默堆积，工程师不知道有 AI PR 需要 review
- 没有 reviewer 指派，PR 长期 open
- 即使知道，由谁 review 不明确

### 1.2 目标
- PR 创建后 30 秒内，自动定位"最了解被改动代码的人"作为推荐 reviewer
- 通过飞书私聊主动 ping 他/她："请 review crashguard 自动 PR #{n}"
- 找不到/通知不到 → fallback 给 sanato 兜底，**不打扰 oncall**
- 每日 09:30 cron 扫所有未 reviewed 的 PR，重发提醒（防止漏看）
- review 完成（任意 GitHub review record）或 PR closed/merged → 立刻停止提醒

### 1.3 非目标
- 不做 PR 优先级排序、不做工作量平衡（v1 简单粗暴）
- 不做 reviewer 拒绝/转派流程（v1 通知到位即可）
- 不做飞书 mapping 表 UI（v1 依赖飞书自身的 email→open_id 解析）

---

## 2. 架构与组件

### 2.1 顶层流程
```
PR 创建成功 (pr_drafter.py)
   ↓ fire-and-forget
pr_reviewer_service.resolve_and_notify(pr_id)
   ├─ git blame diff 改动行 → 候选 author emails
   ├─ 排除 bot / 已不在职 / 自己 → 行数排序 Top 2
   ├─ feishu_cli._emails_to_open_id_map(emails) → 解析飞书账号
   ├─ 成功解析的 → send_card(reviewer) "请你 review"
   ├─ 全部解析失败 / blame 为空 → send_card(sanato) "需手动指派"
   └─ 写回 CrashPullRequest.{reviewer_emails, reviewer_assigned_at, last_reminder_at}

每日 09:30 cron (pipeline_scheduler_loop 复用)
   ↓
pr_reviewer_service.daily_reminder_sweep()
   └─ for each open PR where reviewed_at IS NULL AND last_reminder_at < today:
        ├─ check_review_status(pr) → 拉 GH 看是否有 review record
        ├─ 已 reviewed → 写 reviewed_at, 跳过
        └─ 未 reviewed → 重发提醒（同一份通知逻辑）
```

### 2.2 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| GH↔飞书映射 | 不建独立表，复用 `_emails_to_open_id_map` | 飞书 SDK 已支持 email→open_id，多数工程师飞书账号就用 corp email |
| 找不到 owner 时谁兜底 | `feishu_admin_open_ids[0]`（即 sanato） | 用户明确要求"不发 oncall，发我自己" |
| reviewer 数量 | Top 2 by blame 行数（>= 20% 占比） | Top 1 单点失败风险（休假/离职），3+ 噪音 |
| "已 reviewed" 判定 | GitHub 上该 PR 有任何 review record（任意 state） | APPROVED 太严，COMMENTED 也表明他看过了 |
| Bot author 排除 | config 配 `pr_reviewer_blocked_authors` 列表 | 防止 blame 出 jarvis-bot 自己 |
| 提醒幂等 | `last_reminder_at` 字段 + 当日 date 比较 | 同一天 cron 多次跑也只发一次 |
| 触发时机 | PR 创建后 fire-and-forget + 每日 cron | 双层兜底；创建时即时通知 + cron 兜未 review |

---

## 3. 数据模型变更

### 3.1 `CrashPullRequest` 表新增字段
```python
# app/crashguard/models.py
class CrashPullRequest(Base):
    # ... 既有字段 ...

    # === reviewer auto-assign (2026-05-21) ===
    reviewer_emails = Column(JSON, nullable=True)
    # ↑ blame 出的候选 reviewer email 列表（脱敏后存 ["alice@plaud.ai", ...]）

    reviewer_open_ids = Column(JSON, nullable=True)
    # ↑ 飞书解析成功的 open_ids（用于后续提醒 / 审计）

    reviewer_assigned_at = Column(DateTime, nullable=True)
    # ↑ 首次成功通知时间

    last_reminder_at = Column(DateTime, nullable=True)
    # ↑ 最近一次提醒时间（每日幂等）

    reviewed_at = Column(DateTime, nullable=True)
    # ↑ GitHub 上检测到任何 review record 的时间；非 NULL 则停止提醒

    reviewer_fallback_reason = Column(String(64), nullable=True)
    # ↑ 兜底原因：blame_empty / all_unresolved / blocked_only / bot_only
    # 用于 sanato 收到兜底提醒时看到为什么
```

### 3.2 Migration
- 走现有 `app/crashguard/migrations.py` 模式（运行时检查 column 存在则跳过、不存在则 ADD COLUMN）
- 6 个字段全 nullable，无 default → 老数据不需要 backfill

---

## 4. 新增 / 改动文件

### 4.1 新增
- `app/crashguard/services/pr_reviewer.py` — 核心服务
  - `resolve_reviewers_by_blame(pr_id) -> ReviewerResolution`
  - `notify_reviewers(pr_id, resolution) -> NotificationResult`
  - `check_review_status_from_gh(pr_id) -> bool`
  - `daily_reminder_sweep() -> SweepStats`
- `backend/tests/crashguard/test_pr_reviewer.py` — 单测（blame mock + 各路径覆盖）

### 4.2 改动
- `app/crashguard/models.py` — 加 6 字段
- `app/crashguard/migrations.py` — 加这 6 列的 ADD COLUMN
- `app/crashguard/config.py`：
  ```python
  # PR reviewer auto-assign
  pr_reviewer_enabled: bool = True
  pr_reviewer_top_n: int = 2
  pr_reviewer_min_lines_pct: float = 0.20  # < 20% 行数占比则忽略
  pr_reviewer_blocked_authors: List[str] = Field(
      default_factory=lambda: [
          "jarvis-bot@plaud.ai",
          "noreply@github.com",
          "sanato.zhang@plaud.ai",  # 用户要求自己排除在 reviewer 候选外（同时也是 fallback 接收人）
      ]
  )
  pr_reviewer_daily_cron: str = "30 9 * * *"  # 09:30 每天
  ```
- `app/crashguard/services/pr_drafter.py` — PR 创建成功后的尾端 fire-and-forget：
  ```python
  if pr_record:
      asyncio.create_task(
          pr_reviewer.resolve_and_notify(pr_record.id)
      )
  ```
- `app/crashguard/workers/scheduler.py`（或 warmup.py 里的 pipeline loop） — 加 daily reminder tick

---

## 5. 关键算法细节

### 5.1 blame 算法
```python
def resolve_reviewers_by_blame(pr_id) -> ReviewerResolution:
    pr = db.get(CrashPullRequest, pr_id)
    if not pr.pr_url:
        return ReviewerResolution(emails=[], reason="pr_url_missing")

    # 1. 用 gh pr diff 远端拉 diff（CrashPullRequest 不存本地 diff_path）
    diff_text = fetch_pr_diff_via_gh(pr.pr_url)
    if not diff_text:
        return ReviewerResolution(emails=[], reason="diff_empty")
    targets = parse_diff_target_lines(diff_text)
    if not targets:
        return ReviewerResolution(emails=[], reason="blame_empty")

    # 2. 对每个文件跑 git blame，只关心改动行
    author_lines = Counter()  # email -> 行数
    repo_path = resolve_repo_path_for_pr(pr)
    for fpath, lines in targets.items():
        for ln in lines:
            r = subprocess.run(
                ["git", "blame", "-L", f"{ln},{ln}", "--porcelain", fpath],
                cwd=repo_path, capture_output=True, text=True, timeout=10,
            )
            email = parse_blame_author_email(r.stdout)
            if email and email not in s.pr_reviewer_blocked_authors:
                author_lines[email] += 1

    if not author_lines:
        return ReviewerResolution(emails=[], reason="bot_only")

    # 3. 排序，过滤行数占比 < 20%
    total = sum(author_lines.values())
    sorted_authors = sorted(author_lines.items(), key=lambda x: -x[1])
    filtered = [
        (email, n) for email, n in sorted_authors
        if n / total >= s.pr_reviewer_min_lines_pct
    ][: s.pr_reviewer_top_n]

    return ReviewerResolution(
        emails=[e for e, _ in filtered],
        line_counts={e: n for e, n in filtered},
        reason="ok",
    )
```

### 5.2 通知卡片（飞书 interactive card）
- 标题：`🔍 请你 review crashguard 自动 PR`
- 内容：
  - PR 链接（GitHub URL）
  - 原始 crash issue 名称 + Datadog 链接
  - 你被选中的原因：`你贡献了被修改代码的 N 行（占总改动 M%）`
  - PR 改动文件列表
  - 操作按钮：「打开 PR」（v1 仅一个按钮，简化卡片）
- 兜底卡片（发给 sanato）：
  - 标题：`⚠️ 需手动指派 reviewer`
  - 内容：PR 链接 + 兜底原因（reason 字段）+ 建议下一步

### 5.3 review 状态检测
```python
def check_review_status_from_gh(pr_url) -> bool:
    r = subprocess.run(
        ["gh", "pr", "view", pr_url, "--json", "reviews,state,mergedAt,closedAt"],
        capture_output=True, text=True, timeout=20,
    )
    data = json.loads(r.stdout)
    if data.get("state") in ("MERGED", "CLOSED"):
        return True
    if data.get("mergedAt") or data.get("closedAt"):
        return True
    reviews = data.get("reviews") or []
    return len(reviews) > 0
```

---

## 6. 错误处理 & 边界

| 场景 | 行为 |
|------|------|
| blame 命令超时（10s） | 该文件该行跳过，继续其他 |
| repo_path 不存在（容器外） | log warning + reason=`repo_missing` + 兜底 sanato |
| `_emails_to_open_id_map` 全部失败 | reason=`all_unresolved` + 兜底 sanato（附原始 emails 列表） |
| 飞书 API 失败 | 重试一次，仍失败则记 audit + 不更新 `last_reminder_at`（下次 cron 再试） |
| GH API rate limit | check_review_status 间隔 0.5s，达上限则 abort sweep（明天再扫） |
| force-pushed PR | blame 命中的 commit hash 可能已不存在 → fallback 当前 HEAD blame |
| 同一天 cron 跑多次 | `last_reminder_at::date == today` → 跳过 |

---

## 7. 测试策略

### 7.1 单元测试（mock subprocess + DB）
- `test_resolve_reviewers_blame_top_n` — 多文件多行 blame 正确聚合排序
- `test_resolve_reviewers_filters_blocked_authors` — bot author 被排除
- `test_resolve_reviewers_min_lines_pct` — 占比过低的 author 被过滤
- `test_resolve_reviewers_blame_empty_returns_reason` — blame 空时 reason 正确
- `test_notify_fallback_to_sanato_when_unresolved` — emails→open_ids 全部失败时 fallback
- `test_notify_fallback_to_sanato_when_bot_only` — blame 全是 bot 时 fallback
- `test_daily_sweep_idempotent_same_day` — 同日多次 cron 只发一次
- `test_daily_sweep_skips_reviewed_prs` — 已 reviewed 的不再提醒
- `test_check_review_status_merged_closed_counts_as_reviewed`

### 7.2 集成验证（手动 / 部署后）
- 在 102 找一个真实 PR（如 #145），手动调 `resolve_and_notify(145)`，验证：
  1. blame 出真实 author email
  2. 飞书私聊 sanato 或 reviewer 真的收到卡片
  3. DB 字段写入正确
  4. 第二次调用同日不重发

---

## 8. 上线节奏

1. **开发阶段**：本地实现 + 单测全绿（无外部依赖）
2. **本地 dry-run**：把 102 的一个 PR 用 SSH 拉到本地数据，跑 resolve_reviewers_by_blame 看输出（不发飞书）
3. **灰度**：先在 102 server 上开启，但 `pr_reviewer_enabled=False` → 手动触发一个 PR 验证 → 确认无误后 `enabled=True`
4. **观察**：开启后 24h，看 audit log，确认没有滥发

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 飞书私聊轰炸 | 工程师反感 | v1 同人同 PR 每日最多 1 次；批合并（一条卡片列 N 个 PR） |
| blame 错误指向（cherry-pick / revert） | 通知错人 | 卡片内容显示"你贡献了 N 行"，附行号；让被通知人能判断 |
| GH 拉取 reviews 频繁 | rate limit | 复用现有 `pr_sync_cron`（已是 30min/次）— 把 review 状态读到 DB，sweep 时只读 DB |
| email 不一致（GH 用私人邮箱，飞书用 corp） | 通知不到 | fallback sanato 时附上原始 email，sanato 知道是谁，手动转派 |
| sanato 收到太多兜底 | 噪音 | 同人同 PR 每日 1 次；卡片标题加 fallback reason 便于分类 |

---

## 10. 后续 v2 候选（不在本次范围）

- 在 GH PR 上自动 `gh pr edit --add-reviewer`（需要 GH ↔ GH login 映射，v1 暂不做）
- 工程师"我休假中"反馈通道
- PR review 完成率 / 平均响应时间 看板
- mapping 表 UI（如果 v1 fallback 噪音大）

---

## 附录 A：用户原始需求

> "我们现在已经创建了一些 PR，但是这些 PR 需要被对应的工程师 review，我想把这些 PR 修改点的 git blame owner（谁写的代码）谁来 reviewer，找到了这个 owner，就给他发送 PR 的消息，提示他需要他 review，没有 reviewer 的话，每天发送一次"
>
> 澄清 Q5：找不到 owner 时单独发给 sanato，不发 oncall

