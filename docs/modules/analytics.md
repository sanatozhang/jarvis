# 数据统计模块

工单分类分布、规则命中准确度、各种 KPI 仪表盘。

## 后端

### 代码位置

| 文件 | 职责 |
|------|------|
| `backend/app/api/analytics.py` | 全部 API 端点 |
| `backend/app/services/rule_accuracy.py` | 规则准确度计算（人工标注 vs AI 结果对比） |
| `backend/app/services/golden_samples.py` | 黄金样本管理（评估基准） |

### API 端点

| Method | Path | 用途 | 时间窗口参数 |
|--------|------|------|------|
| `POST` | `/api/analytics/track` | 前端打点上报（用户行为） | — |
| `GET`  | `/api/analytics/dashboard` | 主仪表盘聚合数据 | `days` 或 `date_from`/`date_to`，默认近 7 天 |
| `GET`  | `/api/analytics/problem-types` | 问题类型分布统计 | 同上，默认近 30 天 |
| `GET`  | `/api/analytics/classification-stats` | 工单分类准确率统计 | 同上，默认近 30 天 |
| `POST` | `/api/analytics/backfill-classifications` | 历史工单回填分类（一次性 job） | — |
| `GET`  | `/api/analytics/rule-accuracy` | 规则命中准确度（按规则、按时间） | 同上，默认近 30 天 |
| `GET`  | `/api/analytics/engineer-label-accuracy` | AI `needs_engineer` 标签准确性混淆矩阵 | 同上，默认近 30 天 |
| `GET`  | `/api/analytics/fallback-extraction` | Markdown 兜底提取占比监控 | 同上，默认近 7 天 |

**时间窗口参数**（`backend/app/api/_window.py` 的 `window_params()` 共享依赖，`backend/app/services/date_window.py` 做实际解析）：
- `date_from`/`date_to`（`YYYY-MM-DD`，inclusive）优先级最高，必须成对给出；未给出时回退 `days`；两者都没给用各 endpoint 自己的默认值。
- 窗口统一是 inclusive 日历日 `[date_from 00:00:00, date_to 23:59:59]`，`days=N` 换算成 `[today-(N-1), today]`（含今天）。
- 周边界计算一律用 `date.weekday()`（Monday=0），锚点是 **UTC**（`date_window.today_utc()`），不要用 `isocalendar()` 或本地时区——ISO 周号会把某些 1 月初的日期归到上一年，且本地时区在跨 UTC 天边界时会和后端 `datetime.utcnow()` 的判断错位。
- ⚠️ Oncall 模块另有一套**非自然周**定义（`database.py` 的 `OncallWeekAssignmentRecord`，按值班起始日 + 周数偏移计算），语义与这里完全不同，**不要混用**。

### 数据口径

- **问题类型分布**：以 issue 表的 `problem_type` 字段聚合
- **分类准确率**：人工 confirm 的 issue 中，AI 预测一致的比例（需要前端有「确认」操作触发标签）
- **规则准确度**：`services/rule_accuracy.py` 计算每条规则的 precision / recall（基于 golden_samples）

### 与其它模块的耦合

- 读 jarvis 主表（issues / feedbacks / rules / golden_samples）做聚合
- 不读 `crash_*` 表（Crashguard 自有 audit log，互不交叉）

## 前端

### 页面入口

- `/analytics`（`frontend/src/app/analytics/page.tsx`）

### 主要视图

| 视图 | 数据源 |
|------|--------|
| 工单数趋势 | `/api/analytics/dashboard` 时间序列 |
| 问题类型饼图 / 柱图 | `/api/analytics/problem-types` |
| 规则命中 Top N | `/api/analytics/rule-accuracy` |
| 分类准确率仪表 | `/api/analytics/classification-stats` |

### 约定

- 图表用站点金调 `#B8922E` 为主色，辅色用同色系阶梯
- 所有 API 调用走 `src/lib/api.ts` wrappers（`fetchAnalyticsDashboard` / `fetchRuleAccuracy` / `fetchProblemTypeStats` / `fetchClassificationStats` / `fetchVocClassificationStats` / `fetchVocTrend` / `fetchVocMovers` 等），组件不直接 `fetch`
- 大时间窗口聚合走后端，前端不要在浏览器里 reduce 几万条原始记录
- **时间窗口默认展示上一个完整自然周（周一~周日）**，档位顺序：本周（进行中）、上周、近 1/3/6 个月、近 1 年，外加自定义天数输入。周计算在 `frontend/src/lib/timeRange.ts`（纯函数，UTC 锚点，`mondayOf()`/`resolveRange()`），后端只做校验、不重复推导，避免前后端两套周计算漂移
- 时间选择器状态走 URL query 深链：`?week=YYYY-MM-DD`（周一）或 `?days=N`，二者互斥；等于默认值（上周）时不写 query，刷新 / 分享自动保留窗口
- VOC 周报卡片（weekly digest）只在主窗口选中「上周」或任意历史完整周时与之同步；选「本周（进行中）」或非周窗口（月/季/年/自定义天数）时固定展示最近一个已完成的自然周，卡片上会打「上周汇总」标记说明不同步
