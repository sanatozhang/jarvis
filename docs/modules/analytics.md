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

| Method | Path | 用途 |
|--------|------|------|
| `POST` | `/api/analytics/track` | 前端打点上报（用户行为） |
| `GET`  | `/api/analytics/dashboard` | 主仪表盘聚合数据 |
| `GET`  | `/api/analytics/problem-types` | 问题类型分布统计 |
| `GET`  | `/api/analytics/classification-stats` | 工单分类准确率统计 |
| `POST` | `/api/analytics/backfill-classifications` | 历史工单回填分类（一次性 job） |
| `GET`  | `/api/analytics/rule-accuracy` | 规则命中准确度（按规则、按时间） |

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
- 所有 API 调用走 `src/lib/api.ts` wrappers（找 `fetchAnalytics*` 前缀）
- 大时间窗口聚合走后端，前端不要在浏览器里 reduce 几万条原始记录
- 仪表盘默认展示最近 30 天，时间选择器 query 走 URL（刷新 / 分享保留状态）
