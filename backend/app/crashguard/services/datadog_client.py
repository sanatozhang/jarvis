"""
Datadog Error Tracking client (基于官方 SDK datadog-api-client)。

API 文档: https://docs.datadoghq.com/api/latest/error-tracking/
SDK: https://github.com/DataDog/datadog-api-client-python

外层签名 (DatadogClient.list_issues / normalize_issue) 与早期 httpx 版本兼容，
内部改为官方 SDK 调用 POST /api/v2/error-tracking/issues/search。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("crashguard.datadog")

_RETRY_STATUSES = {500, 502, 503, 504}
_RATE_LIMIT_STATUS = 429

# 进程内 5min 缓存：按 (start_ms, end_ms, tracks, query) 去重；
# C 路线下 daily_report 双窗口 × 双 fatality = 4 次拉取，preview/send 又会再走一遍 → 8 次，
# 缓存能把同口径的重复调用合并为 1 次。
_LIST_CACHE_TTL_SEC = 300
_list_cache: Dict[str, Any] = {}


class DatadogRateLimitError(Exception):
    """Datadog 触发限流"""


class CircuitBreakerOpen(Exception):
    """限流熔断器开启中"""


class DatadogClient:
    """异步 Datadog Error Tracking client（内部用官方 SDK 同步调用 + asyncio.to_thread）"""

    def __init__(
        self,
        api_key: str,
        app_key: str,
        site: str = "datadoghq.com",
        timeout: float = 30.0,
        service_filter: str = "service:plaud-flutter",
    ):
        self.api_key = api_key
        self.app_key = app_key
        self.site = site
        self.timeout = timeout
        # 顶层设计：只看 app 侧数据。
        # 历史 bug 现场：早晚报「最新版本」选到 3.32.4 这种 web 端版本号；iOS 桶混入 ~5.7%
        # plaud-web Safari sessions（Android ~1.5%）。所有 RUM / Issue Search query 入口
        # 统一前置 service:plaud-flutter（默认）保证 app-only 颗粒度。
        # native iOS/Android 真实 service = plaud_ios / plaud_android（下划线，2026-06-30 实测），
        # 共存期 filter 已扩成 "(service:plaud-flutter OR service:plaud_android OR service:plaud_ios)"。
        # 空串 = 不注入（兜底逃生口；仅 debug 用）。
        self.service_filter = (service_filter or "").strip()
        self._rate_limit_events: Deque[float] = deque(maxlen=10)
        self._circuit_open_until: float = 0.0
        self._circuit_threshold: int = 5
        self._circuit_window_sec: int = 600     # 10 分钟
        self._circuit_open_sec: int = 1800      # 30 分钟

    def _inject_service(self, query: str) -> str:
        """所有 RUM / Issue Search query 注入 service filter（app-only 抓手）。

        - 若 self.service_filter 为空 → 直接返回原 query（debug 逃生口）
        - 若 query 已显式包含 'service:'（外部已自带过滤）→ 不重复注入
        - 否则前置 self.service_filter（用空格连接）
        - query=='*' 直接替换为纯 service filter（避免 '* service:xxx' 引发歧义）
        """
        if not self.service_filter:
            return query
        q = (query or "").strip()
        if "service:" in q:
            return q  # 调用方已明确 service，尊重之
        if not q or q == "*":
            return self.service_filter
        return f"{self.service_filter} {q}"

    def _build_configuration(self):
        from datadog_api_client import Configuration

        conf = Configuration()
        conf.api_key["apiKeyAuth"] = self.api_key
        conf.api_key["appKeyAuth"] = self.app_key
        conf.server_variables["site"] = self.site
        return conf

    async def list_issues(
        self,
        window_hours: int = 24,
        page_size: int = 100,
        tracks: str = "rum",
        query: str = "*",
    ) -> List[Dict[str, Any]]:
        """
        拉取最近 N 小时内的 error tracking issue（按 total_count 降序）。

        Args:
            window_hours: 时间窗口（小时）
            page_size: 单 track 最多返回（SDK 上限 100）
            tracks: 单/多 track，逗号分隔。例 "rum"、"rum,logs"。
            query: event search 语法 query；"*" = 全量。

        多 track 时按 issue id 去重合并，相同 id 取 events_count 较大的一条。
        失败重试 3 次指数退避（1s/2s/4s），429 抛 DatadogRateLimitError，
        10min 内 5 次 429 触发 30min 熔断。

        Returns:
            List[Dict] — 与 fixture 兼容的 raw issue dict（id / type / attributes）。
        """
        now = time.time()
        if now < self._circuit_open_until:
            raise CircuitBreakerOpen(
                f"Datadog 熔断中，将于 {int(self._circuit_open_until - now)}s 后恢复"
            )

        end_ms = int(time.time() * 1000)
        start_ms = end_ms - window_hours * 3600 * 1000
        return await self.list_issues_for_window(
            start_ms=start_ms, end_ms=end_ms, tracks=tracks, query=query,
        )

    async def list_issues_for_window(
        self,
        start_ms: int,
        end_ms: int,
        tracks: str = "rum",
        query: str = "*",
        use_cache: bool = True,
    ) -> List[Dict[str, Any]]:
        """与 list_issues 等价，但接受显式时间窗口（C 路线 + 方案 A 同窗口对齐用）。

        - start_ms / end_ms: 毫秒时间戳
        - 5min 进程内缓存（同 cache key 复用，避免 dual-window × dual-fatality 重复调用）
        """
        now = time.time()
        if now < self._circuit_open_until:
            raise CircuitBreakerOpen(
                f"Datadog 熔断中，将于 {int(self._circuit_open_until - now)}s 后恢复"
            )

        track_list = [t.strip().lower() for t in (tracks or "rum").split(",") if t.strip()]
        if not track_list:
            track_list = ["rum"]
        cache_key = f"{start_ms}|{end_ms}|{','.join(track_list)}|{query}"
        if use_cache:
            entry = _list_cache.get(cache_key)
            if entry and (now - entry[0]) < _LIST_CACHE_TTL_SEC:
                return entry[1]

        merged: Dict[str, Dict[str, Any]] = {}
        for track in track_list:
            body = self._build_search_request(start_ms, end_ms, track=track, query=query)
            try:
                response = await self._search_with_retry(body)
            except CircuitBreakerOpen:
                raise
            for item in self._response_to_issue_dicts(response):
                key = item.get("id") or ""
                if not key:
                    continue
                prev = merged.get(key)
                if prev is None or (
                    item["attributes"].get("events_count", 0)
                    > prev["attributes"].get("events_count", 0)
                ):
                    merged[key] = item

        result = list(merged.values())
        if use_cache:
            _list_cache[cache_key] = (now, result)
        return result

    def _build_search_request(
        self,
        start_ms: int,
        end_ms: int,
        track: str = "rum",
        query: str = "*",
    ):
        from datadog_api_client.v2.model.issues_search_request import IssuesSearchRequest
        from datadog_api_client.v2.model.issues_search_request_data import (
            IssuesSearchRequestData,
        )
        from datadog_api_client.v2.model.issues_search_request_data_attributes import (
            IssuesSearchRequestDataAttributes,
        )
        from datadog_api_client.v2.model.issues_search_request_data_attributes_track import (
            IssuesSearchRequestDataAttributesTrack,
        )
        from datadog_api_client.v2.model.issues_search_request_data_type import (
            IssuesSearchRequestDataType,
        )
        from datadog_api_client.v2.model.issues_search_request_data_attributes_order_by import (
            IssuesSearchRequestDataAttributesOrderBy,
        )

        track_enum = self._resolve_track(track, IssuesSearchRequestDataAttributesTrack)
        # 注入 service filter：app-only 抓手，过滤 plaud-web / plaud-desktop 污染源
        injected_query = self._inject_service(query or "*")
        return IssuesSearchRequest(
            data=IssuesSearchRequestData(
                attributes=IssuesSearchRequestDataAttributes(
                    query=injected_query,
                    _from=start_ms,
                    to=end_ms,
                    track=track_enum,
                    order_by=IssuesSearchRequestDataAttributesOrderBy.TOTAL_COUNT,
                ),
                type=IssuesSearchRequestDataType.SEARCH_REQUEST,
            ),
        )

    @staticmethod
    def _resolve_track(track: str, track_cls):
        mapping = {
            "rum": getattr(track_cls, "RUM", None),
            "logs": getattr(track_cls, "LOGS", None),
            "trace": getattr(track_cls, "TRACE", None),
        }
        return mapping.get(track.lower()) or track_cls("rum")

    async def _search_with_retry(self, body, max_retries: int = 3):
        last_error: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                return await asyncio.to_thread(self._sync_search, body)
            except DatadogRateLimitError:
                raise
            except _RetryableSDKError as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise e.original
        raise last_error if last_error else RuntimeError("Datadog SDK 调用未知错误")

    def _sync_search(self, body):
        """同步执行，由 asyncio.to_thread 包装"""
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.error_tracking_api import ErrorTrackingApi
        from datadog_api_client.v2.model.search_issues_include_query_parameter_item import (
            SearchIssuesIncludeQueryParameterItem,
        )

        conf = self._build_configuration()
        try:
            with ApiClient(conf) as api_client:
                api = ErrorTrackingApi(api_client)
                return api.search_issues(
                    body=body,
                    include=[SearchIssuesIncludeQueryParameterItem.ISSUE],
                )
        except ApiException as exc:
            status = getattr(exc, "status", None)
            if status == _RATE_LIMIT_STATUS:
                self._record_rate_limit_event()
                raise DatadogRateLimitError(
                    f"Datadog 限流 (429), reason={exc.reason}"
                ) from exc
            if status in _RETRY_STATUSES:
                raise _RetryableSDKError(exc) from exc
            raise

    def _record_rate_limit_event(self) -> None:
        now = time.time()
        while self._rate_limit_events and self._rate_limit_events[0] < now - self._circuit_window_sec:
            self._rate_limit_events.popleft()
        self._rate_limit_events.append(now)
        if len(self._rate_limit_events) >= self._circuit_threshold:
            self._circuit_open_until = now + self._circuit_open_sec
            logger.error(
                "Datadog 熔断开启 — %d 次 429 in %ds，暂停 %ds",
                self._circuit_threshold, self._circuit_window_sec, self._circuit_open_sec,
            )

    @staticmethod
    def _response_to_issue_dicts(response) -> List[Dict[str, Any]]:
        """
        合并 search_result（聚合指标）+ included Issue（基础属性）为 list of dict。

        输出 dict 形状与 normalize_issue 期待的 raw 格式一致：
            {"id": str, "type": "error_tracking_issue",
             "attributes": {title, service, platform,
                            first_seen_timestamp, last_seen_timestamp,
                            first_seen_version, last_seen_version,
                            events_count, users_affected, stack_trace, tags}}
        """
        issues_by_id: Dict[str, Any] = {}
        for inc in (_safe_attr(response, "included", []) or []):
            inc_id = _safe_attr(inc, "id", None)
            inc_type = str(_safe_attr(inc, "type", "") or "").lower()
            if inc_id and "issue" in inc_type:
                issues_by_id[inc_id] = inc

        out: List[Dict[str, Any]] = []
        for result in (_safe_attr(response, "data", []) or []):
            issue_obj = _resolve_linked_issue(result, issues_by_id)
            issue_id = getattr(issue_obj, "id", None) or getattr(result, "id", "")
            issue_attrs = getattr(issue_obj, "attributes", None) if issue_obj else None
            metric_attrs = getattr(result, "attributes", None)

            error_message = _attr(issue_attrs, "error_message", "") or ""
            error_type = _attr(issue_attrs, "error_type", "") or ""
            file_path = _attr(issue_attrs, "file_path", "") or ""
            function_name = _attr(issue_attrs, "function_name", "") or ""

            title = _build_title(error_type, function_name, error_message)
            stack_trace = _build_stack_trace(error_message, function_name, file_path)

            out.append({
                "id": issue_id,
                "type": "error_tracking_issue",
                "attributes": {
                    "title": title,
                    "service": _attr(issue_attrs, "service", "") or "",
                    "platform": _coerce_str(_attr(issue_attrs, "platform", "")),
                    "first_seen_timestamp": _attr(issue_attrs, "first_seen", None),
                    "last_seen_timestamp": _attr(issue_attrs, "last_seen", None),
                    "first_seen_version": _attr(issue_attrs, "first_seen_version", "") or "",
                    "last_seen_version": _attr(issue_attrs, "last_seen_version", "") or "",
                    "events_count": int(_attr(metric_attrs, "total_count", 0) or 0),
                    # batched Issues API 仍只稳定返 sessions 维度；用 attr 直读做兜底。
                    # 全局 distinct user 走 cardinality(@usr.id)（见 count_users_by_platform / count_crash_users_by_platform）；
                    # 2026-05-25 实测 @usr.id 填充率 92.7%，旧"Plan 2.5"路径已落地。
                    "users_affected": int(_attr(metric_attrs, "impacted_users", 0) or 0),
                    "sessions_affected": int(_attr(metric_attrs, "impacted_sessions", 0) or 0),
                    "stack_trace": stack_trace,
                    "tags": {},
                },
            })
        return out


    async def get_issue_detail(self, issue_id: str, lookback_days: int = 7, max_events: int = 500) -> Optional[Dict[str, Any]]:
        """
        拉单 issue 的富化信息（C 任务）：完整堆栈 + 设备/OS/版本分布 + 视图 + 网络。

        策略：用 RUMApi.search_rum_events 按 `@issue.id:{id}` 拉最近 N 条事件（默认 100），
        在同一组样本上：
            ① 选 stack 最长 / 符号化 的那条作为代表
            ② Counter 统计 device.model / os.version / application.version 分布
        返回 None 表示无 RUM 事件。
        """
        now = time.time()
        if now < self._circuit_open_until:
            return None

        try:
            events = await asyncio.to_thread(self._sync_search_rum_events, issue_id, lookback_days, max_events)
        except Exception as exc:
            logger.warning("get_issue_detail RUM search failed for %s: %s", issue_id, exc)
            return None

        if not events:
            return None

        from collections import Counter

        device_counter: Counter = Counter()
        os_counter: Counter = Counter()
        version_counter: Counter = Counter()
        view_counter: Counter = Counter()
        country_counter: Counter = Counter()
        # frame bucket → 桶内事件列表 [(score, stack, inner, tags, app_ver, view, ev)]
        frame_buckets: Dict[str, List[tuple]] = {}

        scanned_events = 0
        for ev in events:
            inner = self._extract_inner_attrs(ev)
            if not inner:
                continue
            tags = self._extract_event_tags(ev)
            stack = self._extract_path(inner, "error", "stack") or ""
            score = self._score_stack(stack)
            scanned_events += 1

            # bucket: 机型 / OS 版本 / app 版本 / 页面 / 国家
            dev = self._extract_device(inner)
            if dev:
                device_counter[dev] += 1
            osv = self._extract_os(inner)
            if osv:
                os_counter[osv] += 1
            ver = self._extract_app_version(inner) or _tag_value(tags, "version")
            if ver:
                version_counter[ver] += 1
            view = self._extract_path(inner, "view", "url_path_group") or self._extract_path(inner, "view", "name") or ""
            if view:
                view_counter[view] += 1
            geo = self._extract_path(inner, "geo", "country") or ""
            if geo:
                country_counter[geo] += 1

            # 顶帧分桶（Layer over Datadog grouping）
            fk = self._top_frame_key(stack)
            frame_buckets.setdefault(fk, []).append((score, stack, inner, tags, ver or "", view or "", ev))

        if not frame_buckets:
            return None

        # bucket 按事件数排序；每桶内挑得分最高的当代表
        sorted_buckets = sorted(
            frame_buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )

        def _pick_best(bucket_evs: List[tuple]) -> tuple:
            return max(bucket_evs, key=lambda t: t[0])

        # 主桶（占比最高）
        main_key, main_evs = sorted_buckets[0]
        main_best = _pick_best(main_evs)
        _, best_stack, inner, tags, _, _, ev = main_best
        total = scanned_events

        # 构造 stack_variants：每桶取代表，附 count + pct + 样本 app_version/view
        stack_variants: List[Dict[str, Any]] = []
        for fk, bucket_evs in sorted_buckets[:6]:  # 上限 6 个桶
            best = _pick_best(bucket_evs)
            _score, b_stack, _b_inner, _b_tags, b_ver, b_view, _ = best
            cnt = len(bucket_evs)
            stack_variants.append({
                "top_frame": fk,
                "count": cnt,
                "pct": round(cnt * 100.0 / total, 1) if total else 0.0,
                "representative_stack": (b_stack or "")[:32000],
                "sample_app_version": b_ver,
                "sample_view": b_view,
                "stack_quality": self._stack_quality_label(b_stack),
                "is_main": fk == main_key,
            })

        # Plan A/B/C: 符号化增强（容错，失败原样保留）
        platform_str = self._extract_path(inner, "os", "name") or ""
        binary_images = self._extract_path(inner, "error", "binary_images") or []
        app_ver = self._extract_app_version(inner) or _tag_value(tags, "version") or ""
        try:
            from app.crashguard.services.symbolication import symbolicate_stack
            from app.config import get_repo_routing
            from app.services import repo_router
            _res = repo_router.resolve(platform_str, app_ver, get_repo_routing())
            symbolicated = await symbolicate_stack(
                best_stack, binary_images, platform_str, app_ver,
                symbol_profile=(_res.symbol_profile if _res else ""),
                github_repo=(_res.github_repo if _res else ""),
            )
        except Exception:
            symbolicated = best_stack

        return {
            "full_stack": symbolicated,
            "stack_quality": self._stack_quality_label(best_stack),
            "error_message": self._extract_path(inner, "error", "message") or "",
            "error_type": self._extract_path(inner, "error", "type") or "",
            "error_source_type": self._extract_path(inner, "error", "source_type") or "",
            "device": self._extract_device(inner),
            "os": self._extract_os(inner),
            "app_version": self._extract_app_version(inner) or _tag_value(tags, "version"),
            "view": self._extract_path(inner, "view", "url") or self._extract_path(inner, "view", "name") or "",
            "connectivity": self._extract_connectivity(inner),
            "geo": self._extract_geo(inner),
            "session_id": self._extract_path(inner, "session", "id") or "",
            "context_source": self._extract_path(inner, "context", "source") or "",
            "build_number": self._extract_path(inner, "context", "build_number_int"),
            "events_scanned": total,
            "timestamp": str(getattr(getattr(ev, "attributes", None), "timestamp", "") or ""),
            # 分布（top-5 + 占比）
            "device_distribution": _top_with_pct(device_counter, total, 5),
            "os_distribution": _top_with_pct(os_counter, total, 5),
            "version_distribution": _top_with_pct(version_counter, total, 5),
            "view_distribution": _top_with_pct(view_counter, total, 5),
            "country_distribution": _top_with_pct(country_counter, total, 5),
            # 顶帧 bucket（Datadog issue 内不同代码路径的分布；点最大桶 = 主代表）
            "stack_variants": stack_variants,
            "stack_bucket_count": len(frame_buckets),
            "main_bucket_pct": round(len(main_evs) * 100.0 / total, 1) if total else 0.0,
        }

    @staticmethod
    def _extract_app_version(inner: Dict[str, Any]) -> str:
        app = inner.get("application") or {}
        if isinstance(app, dict):
            v = app.get("version")
            if v:
                return str(v)
        return ""

    @staticmethod
    def _extract_event_tags(event: Any) -> List[str]:
        ea = getattr(event, "attributes", None)
        if ea is None:
            return []
        try:
            ds = getattr(ea, "_data_store", {}) or {}
            t = ds.get("tags") or []
            if isinstance(t, list):
                return [str(x) for x in t]
        except Exception:
            pass
        return []

    @staticmethod
    def _score_stack(stack: str) -> int:
        """打分：符号化栈（含 file:line / package: / .dart 等）远高于 AOT 指针栈。"""
        if not stack:
            return -1
        score = len(stack)
        s = stack.lower()
        # 含符号信息的强烈加分
        if "package:" in s or ".dart:" in s or ".kt:" in s or ".swift:" in s or ".java:" in s:
            score += 50000
        if "<asynchronous suspension>" in s and "package:" in s:
            score += 5000
        # 纯 AOT 指针栈降权
        if "_kdartisolatesnapshotinstructions" in s and "package:" not in s:
            score -= 30000
        return score

    @staticmethod
    def _stack_quality_label(stack: str) -> str:
        if not stack:
            return "empty"
        s = stack.lower()
        if "package:" in s or ".dart:" in s:
            return "symbolicated_dart"
        if ".kt:" in s or ".java:" in s or "(sourcefile:" in s:
            return "symbolicated_jvm"
        if ".swift:" in s or ".m:" in s:
            return "symbolicated_native"
        if "_kdartisolatesnapshotinstructions" in s:
            return "aot_pointers_unsymbolicated"
        return "raw"

    @staticmethod
    def _top_frame_key(stack: str) -> str:
        """从堆栈第一帧抽出 bucket key（去掉 :line / `#N` 前缀 / 多余空格）。

        Datadog 端 fingerprint 粒度过粗时（同 error+message 全归一个 issue），
        用归一化顶帧把不同代码路径的事件分桶。
        """
        if not stack:
            return "(empty)"
        first = stack.split("\n", 1)[0].strip()
        if not first:
            return "(empty)"
        # 未符号化 AOT 指针：abs 0x... / _kDartIsolateSnapshotInstructions
        low = first.lower()
        if "abs 0000" in low or "_kdartisolatesnapshotinstructions" in low:
            return "(unsymbolicated_native)"
        # 剥 "#N " 索引前缀
        import re as _re
        m = _re.match(r"^#\d+\s+", first)
        if m:
            first = first[m.end():].strip()
        # 剥掉行号尾巴: ":1234)" -> ")"
        first = _re.sub(r":\d+\)", ")", first)
        return first[:300] or "(empty)"

    async def count_sessions_in_window(
        self,
        start_ms: int,
        end_ms: int,
    ) -> Dict[str, int]:
        """绝对时间窗口版 sessions by platform。

        给日报双窗口对照用——需要拉 7 天前的 10h 段，不能用相对 'now-Nh'。
        失败返回 {} 不致命。
        """
        try:
            return await asyncio.to_thread(self._sync_count_sessions_in_window, start_ms, end_ms)
        except Exception as exc:
            logger.warning("count_sessions_in_window failed: %s", exc)
            return {}

    def _sync_count_sessions_in_window(self, start_ms: int, end_ms: int) -> Dict[str, int]:
        from datadog_api_client import ApiClient
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_aggregate_request import RUMAggregateRequest
        from datadog_api_client.v2.model.rum_aggregation_function import RUMAggregationFunction
        from datadog_api_client.v2.model.rum_compute import RUMCompute
        from datadog_api_client.v2.model.rum_compute_type import RUMComputeType
        from datadog_api_client.v2.model.rum_group_by import RUMGroupBy
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter

        body = RUMAggregateRequest(
            filter=RUMQueryFilter(
                query=self._inject_service("@type:session"),
                _from=str(start_ms),
                to=str(end_ms),
            ),
            compute=[RUMCompute(aggregation=RUMAggregationFunction.COUNT, type=RUMComputeType.TOTAL)],
            group_by=[RUMGroupBy(facet="@os.name", limit=20)],
        )
        with ApiClient(self._build_configuration()) as api_client:
            rum = RUMApi(api_client)
            resp = rum.aggregate_rum_events(body=body)
        out: Dict[str, int] = {}
        buckets = getattr(getattr(resp, "data", None), "buckets", None) or []
        for b in buckets:
            by = getattr(b, "by", {}) or {}
            os_name = (by.get("@os.name") or "").strip().lower()
            comp = getattr(b, "computes", {}) or {}
            try:
                count = int(next(iter(comp.values())))
            except (StopIteration, TypeError, ValueError):
                count = 0
            if not os_name or count <= 0:
                continue
            if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
                key = "ios"
            elif os_name.startswith("android"):
                key = "android"
            else:
                key = "other"
            out[key] = out.get(key, 0) + count
        return out

    async def count_sessions_by_platform(
        self,
        window_hours: int = 24,
    ) -> Dict[str, int]:
        """
        统计最近 N 小时内 RUM session 总数，按平台分桶（含 active + inactive）。
        返回 {"android": int, "ios": int, ...}；失败返回 {} 不致命。
        """
        try:
            return await asyncio.to_thread(self._sync_count_sessions_by_platform, window_hours, False)
        except Exception as exc:
            logger.warning("count_sessions_by_platform failed: %s", exc)
            return {}

    async def count_inactive_sessions_by_platform(
        self,
        window_hours: int = 24,
    ) -> Dict[str, int]:
        """
        统计最近 N 小时内已结束（inactive）RUM session 数，按平台分桶。
        口径对齐 Firebase / Datadog 官方 Crash-free Sessions 分母（`@session.is_active:false`）。
        失败返回 {} 不致命。
        """
        try:
            return await asyncio.to_thread(self._sync_count_sessions_by_platform, window_hours, True)
        except Exception as exc:
            logger.warning("count_inactive_sessions_by_platform failed: %s", exc)
            return {}

    def _sync_count_sessions_by_platform(self, window_hours: int, inactive_only: bool = False) -> Dict[str, int]:
        """同步实现：用 aggregate_rum_events 按 @os.name group_by。"""
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_aggregate_request import RUMAggregateRequest
        from datadog_api_client.v2.model.rum_aggregate_sort import RUMAggregateSort
        from datadog_api_client.v2.model.rum_aggregation_function import RUMAggregationFunction
        from datadog_api_client.v2.model.rum_compute import RUMCompute
        from datadog_api_client.v2.model.rum_compute_type import RUMComputeType
        from datadog_api_client.v2.model.rum_group_by import RUMGroupBy
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter

        q = "@type:session"
        if inactive_only:
            q += " @session.is_active:false"

        body = RUMAggregateRequest(
            filter=RUMQueryFilter(
                query=self._inject_service(q),
                _from=f"now-{max(1, int(window_hours))}h",
                to="now",
            ),
            compute=[RUMCompute(aggregation=RUMAggregationFunction.COUNT, type=RUMComputeType.TOTAL)],
            group_by=[RUMGroupBy(facet="@os.name", limit=20)],
        )
        conf = self._build_configuration()
        with ApiClient(conf) as api_client:
            rum = RUMApi(api_client)
            try:
                resp = rum.aggregate_rum_events(body=body)
            except ApiException as exc:
                if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                    self._record_rate_limit_event()
                raise
        # 聚合 @os.name → 归类到 ANDROID / IOS（含 iPadOS）；其他 OS 进 OTHER
        out: Dict[str, int] = {}
        buckets = getattr(getattr(resp, "data", None), "buckets", None) or []
        for b in buckets:
            by = getattr(b, "by", {}) or {}
            os_name = (by.get("@os.name") or "").strip().lower()
            comp = getattr(b, "computes", {}) or {}
            try:
                count = int(next(iter(comp.values())))
            except (StopIteration, TypeError, ValueError):
                count = 0
            if not os_name or count <= 0:
                continue
            if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
                key = "ios"
            elif os_name.startswith("android"):
                key = "android"
            else:
                key = "other"
            out[key] = out.get(key, 0) + count
        return out

    async def count_distinct_crash_sessions_by_platform(
        self,
        window_hours: int = 24,
    ) -> Dict[str, int]:
        """
        统计最近 N 小时内有崩溃的 session 数（含 active），按平台分桶。
        崩溃定义：`@session.crash.count:>0`（含 NDK crash + ANR + iOS crash + App Hang）。
        返回 {"android": int, "ios": int, "other": int}；失败返回 {} 不致命。
        """
        try:
            return await asyncio.to_thread(
                self._sync_count_distinct_crash_sessions_by_platform, window_hours, False
            )
        except Exception as exc:
            logger.warning("count_distinct_crash_sessions_by_platform failed: %s", exc)
            return {}

    async def count_inactive_crash_sessions_by_platform(
        self,
        window_hours: int = 24,
    ) -> Dict[str, int]:
        """
        统计最近 N 小时内已结束（inactive）且有崩溃的 session 数，按平台分桶。
        口径：`@type:session @session.is_active:false @session.crash.count:>0`
        对齐 Firebase / Datadog 官方 Crash-free Sessions 的分子口径（崩溃 session 数）。
        失败返回 {} 不致命。
        """
        try:
            return await asyncio.to_thread(
                self._sync_count_distinct_crash_sessions_by_platform, window_hours, True
            )
        except Exception as exc:
            logger.warning("count_inactive_crash_sessions_by_platform failed: %s", exc)
            return {}

    def _sync_count_distinct_crash_sessions_by_platform(self, window_hours: int, inactive_only: bool = False) -> Dict[str, int]:
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_aggregate_request import RUMAggregateRequest
        from datadog_api_client.v2.model.rum_aggregation_function import RUMAggregationFunction
        from datadog_api_client.v2.model.rum_compute import RUMCompute
        from datadog_api_client.v2.model.rum_compute_type import RUMComputeType
        from datadog_api_client.v2.model.rum_group_by import RUMGroupBy
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter

        q = "@type:session @session.crash.count:>0"
        if inactive_only:
            q += " @session.is_active:false"

        body = RUMAggregateRequest(
            filter=RUMQueryFilter(
                query=self._inject_service(q),
                _from=f"now-{max(1, int(window_hours))}h",
                to="now",
            ),
            compute=[RUMCompute(
                aggregation=RUMAggregationFunction.COUNT,
                type=RUMComputeType.TOTAL,
            )],
            group_by=[RUMGroupBy(facet="@os.name", limit=20)],
        )
        conf = self._build_configuration()
        with ApiClient(conf) as api_client:
            rum = RUMApi(api_client)
            try:
                resp = rum.aggregate_rum_events(body=body)
            except ApiException as exc:
                if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                    self._record_rate_limit_event()
                raise
        out: Dict[str, int] = {}
        buckets = getattr(getattr(resp, "data", None), "buckets", None) or []
        for b in buckets:
            by = getattr(b, "by", {}) or {}
            os_name = (by.get("@os.name") or "").strip().lower()
            comp = getattr(b, "computes", {}) or {}
            try:
                count = int(next(iter(comp.values())))
            except (StopIteration, TypeError, ValueError):
                count = 0
            if not os_name or count <= 0:
                continue
            if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
                key = "ios"
            elif os_name.startswith("android"):
                key = "android"
            else:
                key = "other"
            out[key] = out.get(key, 0) + count
        return out

    async def count_sessions_for_platform_versions(
        self,
        versions_by_plat: Dict[str, str],
        window_hours: int = 24,
    ) -> Dict[str, int]:
        """版本过滤版 count_sessions_by_platform（含 active + inactive）。"""
        if not versions_by_plat:
            return {}
        try:
            return await asyncio.to_thread(
                self._sync_count_sessions_for_platform_versions,
                versions_by_plat, window_hours,
            )
        except Exception as exc:
            logger.warning("count_sessions_for_platform_versions failed: %s", exc)
            return {}

    def _sync_count_sessions_for_platform_versions(
        self, versions_by_plat: Dict[str, str], window_hours: int,
    ) -> Dict[str, int]:
        return self._versioned_count(versions_by_plat, window_hours, base_query="@type:session")

    async def count_inactive_sessions_for_platform_versions(
        self,
        versions_by_plat: Dict[str, str],
        window_hours: int = 24,
    ) -> Dict[str, int]:
        """版本过滤版 count_inactive_sessions_by_platform（已结束会话，CF 率分母）。"""
        if not versions_by_plat:
            return {}
        try:
            return await asyncio.to_thread(
                lambda: self._versioned_count(
                    versions_by_plat, window_hours,
                    base_query="@type:session @session.is_active:false",
                )
            )
        except Exception as exc:
            logger.warning("count_inactive_sessions_for_platform_versions failed: %s", exc)
            return {}

    async def count_distinct_crash_sessions_for_platform_versions(
        self,
        versions_by_plat: Dict[str, str],
        window_hours: int = 24,
    ) -> Dict[str, int]:
        """版本过滤版 crashed sessions count（含 active，含 ANR + App Hang）。"""
        if not versions_by_plat:
            return {}
        try:
            return await asyncio.to_thread(
                self._sync_count_distinct_crash_sessions_for_platform_versions,
                versions_by_plat, window_hours,
            )
        except Exception as exc:
            logger.warning("count_distinct_crash_sessions_for_platform_versions failed: %s", exc)
            return {}

    def _sync_count_distinct_crash_sessions_for_platform_versions(
        self, versions_by_plat: Dict[str, str], window_hours: int,
    ) -> Dict[str, int]:
        return self._versioned_count(
            versions_by_plat, window_hours,
            base_query="@type:session @session.crash.count:>0",
        )

    async def count_inactive_crash_sessions_for_platform_versions(
        self,
        versions_by_plat: Dict[str, str],
        window_hours: int = 24,
    ) -> Dict[str, int]:
        """版本过滤版 inactive crashed sessions count（已结束 + 有崩溃，CF 率分子补集）。"""
        if not versions_by_plat:
            return {}
        try:
            return await asyncio.to_thread(
                lambda: self._versioned_count(
                    versions_by_plat, window_hours,
                    base_query="@type:session @session.is_active:false @session.crash.count:>0",
                )
            )
        except Exception as exc:
            logger.warning("count_inactive_crash_sessions_for_platform_versions failed: %s", exc)
            return {}

    # ── User 维度（@usr.id）口径 — Datadog Formulas & Functions Scalar API ──────
    #
    # 底层逻辑：SDK aggregate_rum_events 对 CARDINALITY(@usr.id) 不收敛 filter
    # （实测无论 query 怎么写都返 ~490k 全局 distinct user）。必须走
    # /api/v2/query/scalar（Dashboard query_value widget 同款），filter 才生效。
    #
    # 口径与 sessions 系列严格对齐——fatal 含 ANR + App Hang：
    #   crashed user filter = @type:error @session.type:user (
    #       @error.is_crash:true OR @error.category:ANR OR @error.category:"App Hang"
    #   )
    #   total user filter   = @type:session @session.type:user
    #
    # 与 dashboard widget(`@error.is_crash:true -@error.category:ANR`) 的差异：
    # 我们口径含 ANR + App Hang，与 Plaud 内部 sessions/告警一致；不严格复刻 widget。
    #
    # 不区分 active/inactive：user 是 session 聚合，无法 per-user 归类——这是
    # session 维度独有的概念。

    _USER_FATAL_FILTER = (
        '@type:error @session.type:user '
        '(@error.is_crash:true OR @error.category:ANR OR @error.category:"App Hang")'
    )
    _USER_TOTAL_FILTER = "@type:session @session.type:user"

    async def count_users_by_platform(
        self, window_hours: int = 24, offset_hours: int = 0,
    ) -> Dict[str, int]:
        """最近 N 小时内 distinct user 数，按平台分桶。

        口径：cardinality(@usr.id) WHERE @type:session @session.type:user
        offset_hours：窗口整体往前平移的小时数；0=今日窗 [now-Nh, now]，
        168=上周同 weekday 同段（SHoW，供日报用户同比用）。
        返回 {"android": int, "ios": int}；失败返回 {} 不致命。
        """
        try:
            return await asyncio.to_thread(
                self._sync_user_cardinality_by_platform,
                self._USER_TOTAL_FILTER, window_hours, offset_hours,
            )
        except Exception as exc:
            logger.warning("count_users_by_platform failed: %s", exc)
            return {}

    async def count_crash_users_by_platform(
        self, window_hours: int = 24, offset_hours: int = 0,
    ) -> Dict[str, int]:
        """最近 N 小时内 fatal-affected distinct user 数，按平台分桶。

        口径（与 sessions 系列对齐，含 ANR + App Hang）：
            cardinality(@usr.id) WHERE @type:error @session.type:user
                                  (is_crash OR ANR OR App Hang)
        offset_hours：同 count_users_by_platform，168=SHoW 上周同段基线。
        返回 {"android": int, "ios": int}；失败返回 {} 不致命。
        """
        try:
            return await asyncio.to_thread(
                self._sync_user_cardinality_by_platform,
                self._USER_FATAL_FILTER, window_hours, offset_hours,
            )
        except Exception as exc:
            logger.warning("count_crash_users_by_platform failed: %s", exc)
            return {}

    async def count_users_for_platform_versions(
        self,
        versions_by_plat: Dict[str, str],
        window_hours: int = 24,
    ) -> Dict[str, int]:
        """版本过滤版 count_users_by_platform（每平台单独一次 scalar 调用）。"""
        if not versions_by_plat:
            return {}
        try:
            return await asyncio.to_thread(
                self._sync_versioned_user_cardinality,
                self._USER_TOTAL_FILTER, versions_by_plat, window_hours,
            )
        except Exception as exc:
            logger.warning("count_users_for_platform_versions failed: %s", exc)
            return {}

    async def count_crash_users_for_platform_versions(
        self,
        versions_by_plat: Dict[str, str],
        window_hours: int = 24,
    ) -> Dict[str, int]:
        """版本过滤版 count_crash_users_by_platform。"""
        if not versions_by_plat:
            return {}
        try:
            return await asyncio.to_thread(
                self._sync_versioned_user_cardinality,
                self._USER_FATAL_FILTER, versions_by_plat, window_hours,
            )
        except Exception as exc:
            logger.warning("count_crash_users_for_platform_versions failed: %s", exc)
            return {}

    def _sync_user_cardinality_by_platform(
        self, filter_query: str, window_hours: int, offset_hours: int = 0,
    ) -> Dict[str, int]:
        """走 F&F scalar API，group_by @os.name，归并 ANDROID/IOS 桶。

        offset_hours：窗口右端从 now 往前平移的小时数（168=上周同段 SHoW）。
        窗口 = [end-Nh, end]，end = now-offset_hours。
        """
        now_ms = int(time.time() * 1000)
        end_ms = now_ms - max(0, int(offset_hours)) * 3600 * 1000
        start_ms = end_ms - max(1, int(window_hours)) * 3600 * 1000
        rows = self._scalar_user_cardinality(
            filter_query=filter_query,
            start_ms=start_ms, end_ms=end_ms,
            group_by_facets=["@os.name"],
        )
        out: Dict[str, int] = {}
        for key_tuple, count in rows.items():
            os_name = (key_tuple if isinstance(key_tuple, str) else key_tuple[0]) or ""
            os_name = os_name.strip().lower()
            if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
                key = "ios"
            elif os_name.startswith("android"):
                key = "android"
            else:
                continue
            out[key] = out.get(key, 0) + max(0, int(count))
        return out

    def _sync_versioned_user_cardinality(
        self,
        base_filter: str,
        versions_by_plat: Dict[str, str],
        window_hours: int,
    ) -> Dict[str, int]:
        """每平台 + 版本单独跑一次 scalar 调用（与 _versioned_count 同模式）。"""
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - max(1, int(window_hours)) * 3600 * 1000
        out: Dict[str, int] = {}
        for plat, ver in versions_by_plat.items():
            if not ver:
                continue
            ver_safe = str(ver).replace('"', '\\"')
            # 与 _versioned_count 同口径：用短名 `version`（Plaud RUM facet 别名）
            filter_query = f'{base_filter} version:"{ver_safe}"'
            rows = self._scalar_user_cardinality(
                filter_query=filter_query,
                start_ms=start_ms, end_ms=now_ms,
                group_by_facets=["@os.name"],
            )
            expected = (plat or "").strip().lower()
            for key_tuple, count in rows.items():
                os_name = (key_tuple if isinstance(key_tuple, str) else key_tuple[0]) or ""
                os_name = os_name.strip().lower()
                if int(count) <= 0:
                    continue
                if expected == "ios" and (
                    os_name.startswith("ipados")
                    or os_name.startswith("ios")
                    or "iphone" in os_name
                ):
                    out[expected] = out.get(expected, 0) + int(count)
                elif expected == "android" and os_name.startswith("android"):
                    out[expected] = out.get(expected, 0) + int(count)
                # 平台对不上号的桶忽略（与 _versioned_count 一致）
        return out

    def _scalar_user_cardinality(
        self,
        *,
        filter_query: str,
        start_ms: int,
        end_ms: int,
        group_by_facets: Optional[List[str]] = None,
    ) -> Dict[Any, int]:
        """通用 F&F scalar API + cardinality(@usr.id) 查询。

        Returns: {group_key: count}
        - 无 group_by → {"total": count}
        - 单 facet   → {facet_val_str: count}
        - 多 facet   → {(v1, v2, ...): count}

        异常透传给 caller（async 层 except Exception 兜底）。429 → 记 rate-limit。
        """
        body = {
            "data": {
                "type": "scalar_request",
                "attributes": {
                    "formulas": [{"formula": "query1"}],
                    "queries": [{
                        "name": "query1",
                        "data_source": "rum",
                        "search": {"query": self._inject_service(filter_query)},
                        "indexes": ["*"],
                        "group_by": [
                            {
                                "facet": f,
                                "limit": 30,
                                "sort": {
                                    "order": "desc",
                                    "aggregation": "cardinality",
                                    "metric": "@usr.id",
                                },
                            }
                            for f in (group_by_facets or [])
                        ],
                        "compute": {"aggregation": "cardinality", "metric": "@usr.id"},
                    }],
                    "from": int(start_ms),
                    "to": int(end_ms),
                },
            }
        }
        url = f"https://api.{self.site}/api/v2/query/scalar"
        # F&F scalar API 在大窗口（>24h）+ group_by 时单次可能 ~30-60s。给到 90s 留 buffer。
        # 429 触发熔断；HTTP 错误透传给 caller 兜底（async 层 try/except 转 {}）。
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "DD-API-KEY": self.api_key,
                "DD-APPLICATION-KEY": self.app_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        last_exc: Optional[Exception] = None
        for attempt in range(2):  # 1 retry on transient timeout
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    payload = json.loads(r.read())
                last_exc = None
                break
            except urllib.error.HTTPError as exc:
                if getattr(exc, "code", None) == _RATE_LIMIT_STATUS:
                    self._record_rate_limit_event()
                    raise
                if getattr(exc, "code", None) in _RETRY_STATUSES and attempt == 0:
                    last_exc = exc
                    time.sleep(2.0)
                    continue
                raise
            except (TimeoutError, OSError) as exc:
                if attempt == 0:
                    last_exc = exc
                    time.sleep(2.0)
                    continue
                raise
        if last_exc is not None:
            raise last_exc

        cols = (
            payload.get("data", {})
            .get("attributes", {})
            .get("columns", [])
        ) or []
        val_col = next((c for c in cols if c.get("type") == "number"), None)
        if not val_col:
            return {}
        group_cols = [c for c in cols if c.get("type") == "group"]
        values = val_col.get("values") or []
        if not group_cols:
            if not values:
                return {}
            try:
                return {"total": int(values[0])}
            except (TypeError, ValueError):
                return {}
        out: Dict[Any, int] = {}
        for i, raw_val in enumerate(values):
            keys: List[str] = []
            for gc in group_cols:
                gv = gc.get("values") or []
                cell = gv[i] if i < len(gv) else None
                if isinstance(cell, list):
                    keys.append(cell[0] if cell else "")
                else:
                    keys.append(cell or "")
            key: Any = keys[0] if len(keys) == 1 else tuple(keys)
            try:
                out[key] = int(raw_val)
            except (TypeError, ValueError):
                continue
        return out

    def _versioned_count(
        self,
        versions_by_plat: Dict[str, str],
        window_hours: int,
        base_query: str,
    ) -> Dict[str, int]:
        """共用 per-platform 版本过滤计数实现：base_query + @application.version 二级过滤。

        抓手：每平台一次 Datadog API 调用；group_by @os.name 自动桶 ANDROID/IOS，
        平台对不上号（如查 ios 版本结果落到 android 桶）的桶丢弃。
        """
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_aggregate_request import RUMAggregateRequest
        from datadog_api_client.v2.model.rum_aggregation_function import RUMAggregationFunction
        from datadog_api_client.v2.model.rum_compute import RUMCompute
        from datadog_api_client.v2.model.rum_compute_type import RUMComputeType
        from datadog_api_client.v2.model.rum_group_by import RUMGroupBy
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter

        conf = self._build_configuration()
        out: Dict[str, int] = {}
        with ApiClient(conf) as api_client:
            rum = RUMApi(api_client)
            for plat, ver in versions_by_plat.items():
                if not ver:
                    continue
                ver_safe = str(ver).replace('"', '\\"')
                # Plaud RUM SDK 上报到 Datadog 的版本 facet 字段名是 `version`（短名），
                # 不是 `@application.version`（后者在 Plaud 数据下未索引→ 永远空桶）。
                # 与 top_user_version_by_platform 同步保持口径一致。
                query = f'{base_query} version:"{ver_safe}"'
                body = RUMAggregateRequest(
                    filter=RUMQueryFilter(
                        query=self._inject_service(query),
                        _from=f"now-{max(1, int(window_hours))}h",
                        to="now",
                    ),
                    compute=[RUMCompute(
                        aggregation=RUMAggregationFunction.COUNT,
                        type=RUMComputeType.TOTAL,
                    )],
                    group_by=[RUMGroupBy(facet="@os.name", limit=20)],
                )
                try:
                    resp = rum.aggregate_rum_events(body=body)
                except ApiException as exc:
                    if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                        self._record_rate_limit_event()
                    logger.warning("versioned_count failed for %s=%s: %s", plat, ver, exc)
                    continue

                buckets = getattr(getattr(resp, "data", None), "buckets", None) or []
                expected = (plat or "").strip().lower()
                for b in buckets:
                    by = getattr(b, "by", {}) or {}
                    os_name = (by.get("@os.name") or "").strip().lower()
                    comp = getattr(b, "computes", {}) or {}
                    try:
                        count = int(next(iter(comp.values())))
                    except (StopIteration, TypeError, ValueError):
                        count = 0
                    if count <= 0:
                        continue
                    if expected == "ios" and (
                        os_name.startswith("ipados")
                        or os_name.startswith("ios")
                        or "iphone" in os_name
                    ):
                        out[expected] = out.get(expected, 0) + count
                    elif expected == "android" and os_name.startswith("android"):
                        out[expected] = out.get(expected, 0) + count
                    # 平台对不上号的桶忽略（比如 ios 版本号居然出现在 Android 上 → 数据噪声）
        return out

    async def fetch_crash_breakdown_by_platform(
        self,
        window_hours: int = 24,
    ) -> Dict[str, Dict[str, int]]:
        """
        最近 N 小时内错误事件按类型分布。返回:
            {"android": {"native_crash": int, "anr": int}, "ios": {"native_crash": int, "app_hang": int}}

        说明：
        - Android `native_crash` 用 `@error.is_crash:true`（NDK + Java crash）
        - Android `anr` 用 `@error.category:ANR`
        - iOS `native_crash` 同样用 `@error.is_crash:true`
        - iOS `app_hang` 用 `@error.category:"App Hang"`
        - 这是 error 事件计数（非 session 计数），用于展示分类比例，不参与 crash-free 率计算
        失败返回 {} 不致命。
        """
        try:
            return await asyncio.to_thread(
                self._sync_fetch_crash_breakdown_by_platform, window_hours
            )
        except Exception as exc:
            logger.warning("fetch_crash_breakdown_by_platform failed: %s", exc)
            return {}

    def _sync_fetch_crash_breakdown_by_platform(self, window_hours: int) -> Dict[str, Dict[str, int]]:
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_aggregate_request import RUMAggregateRequest
        from datadog_api_client.v2.model.rum_aggregation_function import RUMAggregationFunction
        from datadog_api_client.v2.model.rum_compute import RUMCompute
        from datadog_api_client.v2.model.rum_compute_type import RUMComputeType
        from datadog_api_client.v2.model.rum_group_by import RUMGroupBy
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter

        # 一次查询，按 (@os.name, @error.category) 二维分组
        body = RUMAggregateRequest(
            filter=RUMQueryFilter(
                query=self._inject_service("@type:error"),
                _from=f"now-{max(1, int(window_hours))}h",
                to="now",
            ),
            compute=[RUMCompute(aggregation=RUMAggregationFunction.COUNT, type=RUMComputeType.TOTAL)],
            group_by=[
                RUMGroupBy(facet="@os.name", limit=20),
                RUMGroupBy(facet="@error.category", limit=20),
            ],
        )
        conf = self._build_configuration()
        with ApiClient(conf) as api_client:
            rum = RUMApi(api_client)
            try:
                resp = rum.aggregate_rum_events(body=body)
            except ApiException as exc:
                if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                    self._record_rate_limit_event()
                raise
        out: Dict[str, Dict[str, int]] = {"android": {}, "ios": {}, "other": {}}
        buckets = getattr(getattr(resp, "data", None), "buckets", None) or []
        # 同时再查一次 is_crash:true（不在 category 里）
        body_crash = RUMAggregateRequest(
            filter=RUMQueryFilter(
                query=self._inject_service("@type:error @error.is_crash:true"),
                _from=f"now-{max(1, int(window_hours))}h",
                to="now",
            ),
            compute=[RUMCompute(aggregation=RUMAggregationFunction.COUNT, type=RUMComputeType.TOTAL)],
            group_by=[RUMGroupBy(facet="@os.name", limit=20)],
        )
        with ApiClient(conf) as api_client:
            rum = RUMApi(api_client)
            try:
                resp_crash = rum.aggregate_rum_events(body=body_crash)
            except ApiException as exc:
                raise
        # 解析 native_crash
        for b in (getattr(getattr(resp_crash, "data", None), "buckets", None) or []):
            by = getattr(b, "by", {}) or {}
            os_name = (by.get("@os.name") or "").strip().lower()
            comp = getattr(b, "computes", {}) or {}
            try:
                count = int(next(iter(comp.values())))
            except (StopIteration, TypeError, ValueError):
                count = 0
            if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
                key = "ios"
            elif os_name.startswith("android"):
                key = "android"
            else:
                key = "other"
            out[key]["native_crash"] = out[key].get("native_crash", 0) + count
        # 解析 ANR / App Hang
        for b in buckets:
            by = getattr(b, "by", {}) or {}
            os_name = (by.get("@os.name") or "").strip().lower()
            cat = (by.get("@error.category") or "").strip()
            comp = getattr(b, "computes", {}) or {}
            try:
                count = int(next(iter(comp.values())))
            except (StopIteration, TypeError, ValueError):
                count = 0
            if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
                key = "ios"
            elif os_name.startswith("android"):
                key = "android"
            else:
                key = "other"
            if cat == "ANR":
                out[key]["anr"] = out[key].get("anr", 0) + count
            elif cat == "App Hang":
                out[key]["app_hang"] = out[key].get("app_hang", 0) + count
        return out

    async def top_user_version_by_platform(
        self,
        window_hours: int = 24,
    ) -> Dict[str, Dict[str, Any]]:
        """
        最近 N 小时内，每个平台「用户量最大」的 app 版本。

        口径（session 维度——历史保留，业务上各告警 SLA 对齐使用 session 颗粒度；
              `@usr.id` 2026-05-25 实测填充率 92.7%，与 session ratio≈1.14，两口径可互换）：
            filter   = @type:session
            compute  = CARDINALITY(@session.id)
            group_by = @os.name × @application.version
            window   = now-{window_hours}h → now

        返回:
            {
              "android": {"version": "3.16.0-634", "users": 12345},
              "ios":     {"version": "3.17.0-712", "users": 9876}
            }
            ↑ "users" 字段在 session-代理 口径下实际是 distinct session 数。
            某平台无数据时该 key 缺失。失败返回 {}。
        """
        try:
            return await asyncio.to_thread(
                self._sync_top_user_version_by_platform, window_hours
            )
        except Exception as exc:
            logger.warning("top_user_version_by_platform failed: %s", exc)
            return {}

    def _sync_top_user_version_by_platform(
        self, window_hours: int
    ) -> Dict[str, Dict[str, Any]]:
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_aggregate_request import RUMAggregateRequest
        from datadog_api_client.v2.model.rum_aggregation_function import RUMAggregationFunction
        from datadog_api_client.v2.model.rum_compute import RUMCompute
        from datadog_api_client.v2.model.rum_compute_type import RUMComputeType
        from datadog_api_client.v2.model.rum_group_by import RUMGroupBy
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter

        body = RUMAggregateRequest(
            filter=RUMQueryFilter(
                query=self._inject_service("@type:session"),
                _from=f"now-{max(1, int(window_hours))}h",
                to="now",
            ),
            compute=[RUMCompute(
                aggregation=RUMAggregationFunction.CARDINALITY,
                metric="@session.id",
                type=RUMComputeType.TOTAL,
            )],
            group_by=[
                RUMGroupBy(facet="@os.name", limit=30),
                RUMGroupBy(facet="version", limit=50),
            ],
        )
        conf = self._build_configuration()
        with ApiClient(conf) as api_client:
            rum = RUMApi(api_client)
            try:
                resp = rum.aggregate_rum_events(body=body)
            except ApiException as exc:
                if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                    self._record_rate_limit_event()
                raise

        # bucket: { platform: { version: users } }
        agg: Dict[str, Dict[str, int]] = {"android": {}, "ios": {}}
        for b in (getattr(getattr(resp, "data", None), "buckets", None) or []):
            by = getattr(b, "by", {}) or {}
            os_name = (by.get("@os.name") or "").strip().lower()
            # RUM session 的 app 版本 facet 在 Plaud 数据下叫 `version`（短名）。
            # `@application.version` 在 Datadog UI / 文档里有提，但 Plaud RUM SDK 上报到的字段是 `version`。
            version = (by.get("version") or "").strip()
            if not version:
                continue
            comp = getattr(b, "computes", {}) or {}
            try:
                users = int(next(iter(comp.values())))
            except (StopIteration, TypeError, ValueError):
                users = 0
            if users <= 0:
                continue
            if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
                key = "ios"
            elif os_name.startswith("android"):
                key = "android"
            else:
                continue  # 只关心 android / ios（Flutter 也跑在这俩上）
            agg[key][version] = agg[key].get(version, 0) + users

        out: Dict[str, Dict[str, Any]] = {}
        for platform, versions in agg.items():
            if not versions:
                continue
            top_ver, top_users = max(versions.items(), key=lambda kv: kv[1])
            out[platform] = {"version": top_ver, "users": top_users}
        return out

    async def version_distribution_by_platform(
        self,
        window_hours: int = 24,
        top_n: int = 10,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        最近 N 小时内，每个平台各版本的 session 分布（Top N）。

        返回:
            {
              "android": [{"version": "3.16.0-634", "sessions": 12345, "pct": 45.3}, ...],
              "ios":     [{"version": "3.17.0-712", "sessions": 9876, "pct": 40.1}, ...]
            }
        """
        try:
            return await asyncio.to_thread(
                self._sync_version_distribution_by_platform, window_hours, top_n
            )
        except Exception as exc:
            logger.warning("version_distribution_by_platform failed: %s", exc)
            return {}

    def _sync_version_distribution_by_platform(
        self, window_hours: int, top_n: int
    ) -> Dict[str, List[Dict[str, Any]]]:
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_aggregate_request import RUMAggregateRequest
        from datadog_api_client.v2.model.rum_aggregation_function import RUMAggregationFunction
        from datadog_api_client.v2.model.rum_compute import RUMCompute
        from datadog_api_client.v2.model.rum_compute_type import RUMComputeType
        from datadog_api_client.v2.model.rum_group_by import RUMGroupBy
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter

        def _build_body(query: str) -> RUMAggregateRequest:
            return RUMAggregateRequest(
                filter=RUMQueryFilter(
                    query=self._inject_service(query),
                    _from=f"now-{max(1, int(window_hours))}h",
                    to="now",
                ),
                compute=[RUMCompute(
                    aggregation=RUMAggregationFunction.CARDINALITY,
                    metric="@session.id",
                    type=RUMComputeType.TOTAL,
                )],
                group_by=[
                    RUMGroupBy(facet="@os.name", limit=30),
                    RUMGroupBy(facet="version", limit=50),
                ],
            )

        def _collect(resp: Any) -> Dict[str, Dict[str, int]]:
            agg: Dict[str, Dict[str, int]] = {"android": {}, "ios": {}}
            for b in (getattr(getattr(resp, "data", None), "buckets", None) or []):
                by = getattr(b, "by", {}) or {}
                os_name = (by.get("@os.name") or "").strip().lower()
                version = (by.get("version") or "").strip()
                if not version:
                    continue
                comp = getattr(b, "computes", {}) or {}
                try:
                    cnt = int(next(iter(comp.values())))
                except (StopIteration, TypeError, ValueError):
                    cnt = 0
                if cnt <= 0:
                    continue
                if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
                    key = "ios"
                elif os_name.startswith("android"):
                    key = "android"
                else:
                    continue
                agg[key][version] = agg[key].get(version, 0) + cnt
            return agg

        conf = self._build_configuration()
        with ApiClient(conf) as api_client:
            rum = RUMApi(api_client)
            # 第 1 次：所有 session（含活跃 + 已结束），用于 sessions 总数 + pct 母数
            try:
                resp_total = rum.aggregate_rum_events(body=_build_body("@type:session"))
            except ApiException as exc:
                if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                    self._record_rate_limit_event()
                raise
            sessions_agg = _collect(resp_total)

            # 第 2 次：只查崩溃 session（与 count_distinct_crash_sessions_* 同口径）
            # 失败不影响 sessions 主路径，crashes 字段直接缺省，前端兼容
            crashes_agg: Dict[str, Dict[str, int]] = {"android": {}, "ios": {}}
            try:
                resp_crash = rum.aggregate_rum_events(
                    body=_build_body("@type:session @session.crash.count:>0")
                )
                crashes_agg = _collect(resp_crash)
            except ApiException as exc:
                if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                    self._record_rate_limit_event()
                logger.warning(
                    "version_distribution: crashed-by-version query failed (sessions still ok): %s", exc
                )
            except Exception as exc:
                logger.warning(
                    "version_distribution: crashed-by-version query failed (sessions still ok): %s", exc
                )

        out: Dict[str, List[Dict[str, Any]]] = {}
        for platform, versions in sessions_agg.items():
            if not versions:
                continue
            total = sum(versions.values())
            sorted_vers = sorted(versions.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
            platform_crashes = crashes_agg.get(platform, {})
            out[platform] = [
                {
                    "version": v,
                    "sessions": s,
                    "crashes": int(platform_crashes.get(v, 0)),
                    "pct": round(s / total * 100, 1),
                }
                for v, s in sorted_vers
            ]
        return out

    async def os_version_distribution_by_platform(
        self,
        window_hours: int = 24,
        top_n: int = 8,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """最近 N 小时内各平台 OS 版本分布（Top N，按 session 数）。

        返回:
            {
              "android": [{"version": "14", "sessions": 1234, "pct": 38.2}, ...],
              "ios":     [{"version": "17.5.1", "sessions": 987, "pct": 35.3}, ...]
            }
        """
        try:
            return await asyncio.to_thread(
                self._sync_os_version_distribution_by_platform, window_hours, top_n
            )
        except Exception as exc:
            logger.warning("os_version_distribution_by_platform failed: %s", exc)
            return {}

    def _sync_os_version_distribution_by_platform(
        self, window_hours: int, top_n: int
    ) -> Dict[str, List[Dict[str, Any]]]:
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_aggregate_request import RUMAggregateRequest
        from datadog_api_client.v2.model.rum_aggregation_function import RUMAggregationFunction
        from datadog_api_client.v2.model.rum_compute import RUMCompute
        from datadog_api_client.v2.model.rum_compute_type import RUMComputeType
        from datadog_api_client.v2.model.rum_group_by import RUMGroupBy
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter

        body = RUMAggregateRequest(
            filter=RUMQueryFilter(
                query=self._inject_service("@type:session"),
                _from=f"now-{max(1, int(window_hours))}h",
                to="now",
            ),
            compute=[RUMCompute(
                aggregation=RUMAggregationFunction.CARDINALITY,
                metric="@session.id",
                type=RUMComputeType.TOTAL,
            )],
            group_by=[
                RUMGroupBy(facet="@os.name", limit=10),
                RUMGroupBy(facet="@os.version", limit=50),
            ],
        )
        conf = self._build_configuration()
        with ApiClient(conf) as api_client:
            rum = RUMApi(api_client)
            try:
                resp = rum.aggregate_rum_events(body=body)
            except ApiException as exc:
                if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                    self._record_rate_limit_event()
                raise

        agg: Dict[str, Dict[str, int]] = {"android": {}, "ios": {}}
        for b in (getattr(getattr(resp, "data", None), "buckets", None) or []):
            by = getattr(b, "by", {}) or {}
            os_name = (by.get("@os.name") or "").strip().lower()
            os_ver = (by.get("@os.version") or "").strip()
            if not os_ver:
                continue
            comp = getattr(b, "computes", {}) or {}
            try:
                sessions = int(next(iter(comp.values())))
            except (StopIteration, TypeError, ValueError):
                sessions = 0
            if sessions <= 0:
                continue
            if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
                key = "ios"
            elif os_name.startswith("android"):
                key = "android"
            else:
                continue
            agg[key][os_ver] = agg[key].get(os_ver, 0) + sessions

        out: Dict[str, List[Dict[str, Any]]] = {}
        for platform, versions in agg.items():
            if not versions:
                continue
            total = sum(versions.values())
            sorted_vers = sorted(versions.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
            out[platform] = [
                {"version": v, "sessions": s, "pct": round(s / total * 100, 1)}
                for v, s in sorted_vers
            ]
        return out

    async def device_distribution_by_platform(
        self,
        window_hours: int = 24,
        top_n: int = 8,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """最近 N 小时内各平台机型分布（Top N，按 session 数）。

        返回:
            {
              "android": [{"model": "Pixel 7", "sessions": 1234, "pct": 18.2}, ...],
              "ios":     [{"model": "iPhone 14 Pro", "sessions": 987, "pct": 15.3}, ...]
            }
        """
        try:
            return await asyncio.to_thread(
                self._sync_device_distribution_by_platform, window_hours, top_n
            )
        except Exception as exc:
            logger.warning("device_distribution_by_platform failed: %s", exc)
            return {}

    def _sync_device_distribution_by_platform(
        self, window_hours: int, top_n: int
    ) -> Dict[str, List[Dict[str, Any]]]:
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_aggregate_request import RUMAggregateRequest
        from datadog_api_client.v2.model.rum_aggregation_function import RUMAggregationFunction
        from datadog_api_client.v2.model.rum_compute import RUMCompute
        from datadog_api_client.v2.model.rum_compute_type import RUMComputeType
        from datadog_api_client.v2.model.rum_group_by import RUMGroupBy
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter

        body = RUMAggregateRequest(
            filter=RUMQueryFilter(
                query=self._inject_service("@type:session"),
                _from=f"now-{max(1, int(window_hours))}h",
                to="now",
            ),
            compute=[RUMCompute(
                aggregation=RUMAggregationFunction.CARDINALITY,
                metric="@session.id",
                type=RUMComputeType.TOTAL,
            )],
            group_by=[
                RUMGroupBy(facet="@os.name", limit=10),
                RUMGroupBy(facet="@device.model", limit=30),
            ],
        )
        conf = self._build_configuration()
        with ApiClient(conf) as api_client:
            rum = RUMApi(api_client)
            try:
                resp = rum.aggregate_rum_events(body=body)
            except ApiException as exc:
                if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                    self._record_rate_limit_event()
                raise

        agg: Dict[str, Dict[str, int]] = {"android": {}, "ios": {}}
        for b in (getattr(getattr(resp, "data", None), "buckets", None) or []):
            by = getattr(b, "by", {}) or {}
            os_name = (by.get("@os.name") or "").strip().lower()
            model = (by.get("@device.model") or "").strip()
            if not model or model in ("", "N/A", "unknown"):
                continue
            comp = getattr(b, "computes", {}) or {}
            try:
                sessions = int(next(iter(comp.values())))
            except (StopIteration, TypeError, ValueError):
                sessions = 0
            if sessions <= 0:
                continue
            if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
                key = "ios"
            elif os_name.startswith("android"):
                key = "android"
            else:
                continue
            agg[key][model] = agg[key].get(model, 0) + sessions

        out: Dict[str, List[Dict[str, Any]]] = {}
        for platform, models in agg.items():
            if not models:
                continue
            total = sum(models.values())
            sorted_models = sorted(models.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
            out[platform] = [
                {"model": m, "sessions": s, "pct": round(s / total * 100, 1)}
                for m, s in sorted_models
            ]
        return out

    async def crash_free_sessions_by_version(
        self,
        start_ms: int,
        end_ms: int,
        versions_by_plat: Dict[str, str],
    ) -> Dict[str, Dict[str, Any]]:
        """版本过滤版 crash_free_sessions_by_platform.

        versions_by_plat = {"ios": "3.17.0-701", "android": "3.17.0-702"}
        每平台加 version:"<ver>" filter，对齐 _versioned_count 口径（RUM `version` 短 facet）。
        返回结构同 crash_free_sessions_by_platform：
            {"android": {"total_sessions": ..., "crashed_sessions": ..., "crash_free_pct": ...}}
        """
        if not versions_by_plat:
            return {}
        try:
            return await asyncio.to_thread(
                self._sync_crash_free_sessions_by_version, start_ms, end_ms, versions_by_plat,
            )
        except Exception as exc:
            logger.warning("crash_free_sessions_by_version failed: %s", exc)
            return {}

    def _sync_crash_free_sessions_by_version(
        self, start_ms: int, end_ms: int, versions_by_plat: Dict[str, str],
    ) -> Dict[str, Dict[str, Any]]:
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_aggregate_request import RUMAggregateRequest
        from datadog_api_client.v2.model.rum_aggregation_function import RUMAggregationFunction
        from datadog_api_client.v2.model.rum_compute import RUMCompute
        from datadog_api_client.v2.model.rum_compute_type import RUMComputeType
        from datadog_api_client.v2.model.rum_group_by import RUMGroupBy
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter

        conf = self._build_configuration()
        result: Dict[str, Dict[str, Any]] = {}
        with ApiClient(conf) as api_client:
            rum = RUMApi(api_client)
            for plat, ver in versions_by_plat.items():
                if not ver:
                    continue
                ver_safe = str(ver).replace('"', '\\"')
                def _agg(base_q: str) -> int:
                    body = RUMAggregateRequest(
                        filter=RUMQueryFilter(
                            query=self._inject_service(f'{base_q} version:"{ver_safe}"'),
                            _from=str(int(start_ms)),
                            to=str(int(end_ms)),
                        ),
                        compute=[RUMCompute(
                            aggregation=RUMAggregationFunction.CARDINALITY,
                            metric="@session.id",
                            type=RUMComputeType.TOTAL,
                        )],
                        group_by=[RUMGroupBy(facet="@os.name", limit=20)],
                    )
                    try:
                        resp = rum.aggregate_rum_events(body=body)
                    except ApiException as exc:
                        if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                            self._record_rate_limit_event()
                        raise
                    buckets = getattr(getattr(resp, "data", None), "buckets", None) or []
                    total = 0
                    expected = (plat or "").strip().lower()
                    for b in buckets:
                        by = getattr(b, "by", {}) or {}
                        os_name = (by.get("@os.name") or "").strip().lower()
                        is_match = (
                            (expected == "ios" and (os_name.startswith("ios") or os_name.startswith("ipados") or "iphone" in os_name))
                            or (expected == "android" and os_name.startswith("android"))
                        )
                        if not is_match:
                            continue
                        comp = getattr(b, "computes", {}) or {}
                        try:
                            total += int(next(iter(comp.values())))
                        except (StopIteration, TypeError, ValueError):
                            pass
                    return total

                try:
                    t = _agg("@type:session")
                    c = _agg("@type:session @session.crash.count:>0")
                    if t <= 0:
                        continue
                    result[plat] = {
                        "total_sessions": t,
                        "crashed_sessions": c,
                        "crash_free_pct": round((1.0 - c / t) * 100.0, 4),
                    }
                except Exception as exc:
                    logger.warning("crash_free_sessions_by_version %s=%s: %s", plat, ver, exc)
        return result

    async def crash_free_sessions_by_platform(
        self,
        start_ms: int,
        end_ms: int,
    ) -> Dict[str, Dict[str, Any]]:
        """
        指定时间窗口内每个平台的 crash-free sessions 比例（Datadog Mobile RUM 原生口径）。

        口径：
            total_sessions   = CARDINALITY(@session.id) WHERE @type:session
            crashed_sessions = CARDINALITY(@session.id) WHERE @type:session @session.crash.count:>0
            crash_free_pct   = (1 - crashed/total) * 100   （total=0 时返回 100.0）

        返回:
            {
              "android": {"total_sessions": 1234, "crashed_sessions": 5, "crash_free_pct": 99.59},
              "ios":     {"total_sessions": 987,  "crashed_sessions": 3, "crash_free_pct": 99.70}
            }
            某平台无数据时该 key 缺失。失败返回 {}。
        """
        try:
            return await asyncio.to_thread(
                self._sync_crash_free_sessions, start_ms, end_ms,
            )
        except Exception as exc:
            logger.warning("crash_free_sessions_by_platform failed: %s", exc)
            return {}

    def _sync_crash_free_sessions(
        self, start_ms: int, end_ms: int,
    ) -> Dict[str, Dict[str, Any]]:
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_aggregate_request import RUMAggregateRequest
        from datadog_api_client.v2.model.rum_aggregation_function import RUMAggregationFunction
        from datadog_api_client.v2.model.rum_compute import RUMCompute
        from datadog_api_client.v2.model.rum_compute_type import RUMComputeType
        from datadog_api_client.v2.model.rum_group_by import RUMGroupBy
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter

        def _agg(query: str) -> Dict[str, int]:
            body = RUMAggregateRequest(
                filter=RUMQueryFilter(
                    query=self._inject_service(query),
                    _from=str(int(start_ms)),
                    to=str(int(end_ms)),
                ),
                compute=[RUMCompute(
                    aggregation=RUMAggregationFunction.CARDINALITY,
                    metric="@session.id",
                    type=RUMComputeType.TOTAL,
                )],
                group_by=[RUMGroupBy(facet="@os.name", limit=30)],
            )
            conf = self._build_configuration()
            with ApiClient(conf) as api_client:
                rum = RUMApi(api_client)
                try:
                    resp = rum.aggregate_rum_events(body=body)
                except ApiException as exc:
                    if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                        self._record_rate_limit_event()
                    raise
            out: Dict[str, int] = {}
            for b in (getattr(getattr(resp, "data", None), "buckets", None) or []):
                by = getattr(b, "by", {}) or {}
                os_name = (by.get("@os.name") or "").strip().lower()
                if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
                    key = "ios"
                elif os_name.startswith("android"):
                    key = "android"
                else:
                    continue
                comp = getattr(b, "computes", {}) or {}
                try:
                    val = int(next(iter(comp.values())))
                except (StopIteration, TypeError, ValueError):
                    val = 0
                out[key] = out.get(key, 0) + max(0, val)
            return out

        total = _agg("@type:session")
        crashed = _agg("@type:session @session.crash.count:>0")

        result: Dict[str, Dict[str, Any]] = {}
        for plat in set(total.keys()) | set(crashed.keys()):
            t = total.get(plat, 0)
            c = crashed.get(plat, 0)
            if t <= 0:
                continue
            cf_pct = (1.0 - (c / t)) * 100.0
            result[plat] = {
                "total_sessions": t,
                "crashed_sessions": c,
                "crash_free_pct": round(cf_pct, 4),
            }
        return result

    def _sync_search_rum_events(self, issue_id: str, lookback_days: int, limit: int):
        """单页拉，limit 上限 1000（Datadog RUM API 单页最大）。"""
        from datadog_api_client import ApiClient
        from datadog_api_client.exceptions import ApiException
        from datadog_api_client.v2.api.rum_api import RUMApi
        from datadog_api_client.v2.model.rum_search_events_request import RUMSearchEventsRequest
        from datadog_api_client.v2.model.rum_query_filter import RUMQueryFilter
        from datadog_api_client.v2.model.rum_query_page_options import RUMQueryPageOptions
        from datadog_api_client.v2.model.rum_sort import RUMSort

        body = RUMSearchEventsRequest(
            filter=RUMQueryFilter(
                query=self._inject_service(f"@issue.id:{issue_id}"),
                _from=f"now-{max(1, int(lookback_days))}d",
                to="now",
            ),
            sort=RUMSort.TIMESTAMP_DESCENDING,
            page=RUMQueryPageOptions(limit=max(1, min(int(limit), 1000))),
        )
        conf = self._build_configuration()
        with ApiClient(conf) as api_client:
            rum = RUMApi(api_client)
            try:
                resp = rum.search_rum_events(body=body)
            except ApiException as exc:
                if getattr(exc, "status", None) == _RATE_LIMIT_STATUS:
                    self._record_rate_limit_event()
                raise
        return list(resp.data or [])

    @staticmethod
    def _extract_inner_attrs(event: Any) -> Dict[str, Any]:
        ea = getattr(event, "attributes", None)
        if ea is None:
            return {}
        inner = getattr(ea, "attributes", None)
        if isinstance(inner, dict):
            return inner
        return {}

    @staticmethod
    def _extract_path(d: Dict[str, Any], *path: str) -> Any:
        cur: Any = d
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None
        return cur

    @classmethod
    def _extract_device(cls, inner: Dict[str, Any]) -> str:
        dev = inner.get("device") or {}
        if not isinstance(dev, dict):
            return ""
        brand = dev.get("brand") or ""
        model = dev.get("model") or dev.get("name") or ""
        arch = dev.get("architecture") or ""
        parts = [p for p in (brand, model, arch) if p]
        return " / ".join(parts)

    @classmethod
    def _extract_os(cls, inner: Dict[str, Any]) -> str:
        os_ = inner.get("os") or {}
        if not isinstance(os_, dict):
            return ""
        name = os_.get("name") or ""
        version = os_.get("version") or ""
        return f"{name} {version}".strip()

    @classmethod
    def _extract_connectivity(cls, inner: Dict[str, Any]) -> str:
        conn = inner.get("connectivity") or {}
        if not isinstance(conn, dict):
            return ""
        ifs = conn.get("interfaces") or []
        if isinstance(ifs, list):
            ifs = ",".join(str(x) for x in ifs)
        return f"{conn.get('status') or ''} ({ifs})".strip()

    @classmethod
    def _extract_geo(cls, inner: Dict[str, Any]) -> str:
        geo = inner.get("geo") or {}
        if not isinstance(geo, dict):
            return ""
        country = geo.get("country") or ""
        city = geo.get("city") or ""
        return ", ".join(p for p in (city, country) if p)


def _tag_value(tags: List[str], key: str) -> str:
    """从 ['version:3.15.1-630', 'env:production', ...] 里取某个 key 的 value。"""
    prefix = f"{key}:"
    for t in tags or []:
        if isinstance(t, str) and t.startswith(prefix):
            return t[len(prefix):]
    return ""


def _top_with_pct(counter, total: int, n: int):
    """Counter -> [{value, count, pct}]，按 count 降序取前 n。"""
    if not counter or total <= 0:
        return []
    out = []
    for value, count in counter.most_common(n):
        out.append({
            "value": value,
            "count": count,
            "pct": round(count * 100.0 / total, 1),
        })
    return out


class _RetryableSDKError(Exception):
    """5xx 等可重试错误"""

    def __init__(self, original: Exception):
        super().__init__(str(original))
        self.original = original


def _attr(obj: Any, name: str, default: Any) -> Any:
    if obj is None:
        return default
    val = _safe_attr(obj, name, None)
    return default if val is None else val


def _safe_attr(obj: Any, name: str, default: Any) -> Any:
    """SDK model 在缺字段时会抛 ApiAttributeError，统一包装。"""
    try:
        val = getattr(obj, name, default)
    except Exception:
        return default
    return val


def _coerce_str(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value)


def _resolve_linked_issue(search_result: Any, issues_by_id: Dict[str, Any]) -> Any:
    rel = _safe_attr(search_result, "relationships", None)
    if rel is None:
        return None
    issue_rel = _safe_attr(rel, "issue", None)
    if issue_rel is None:
        return None
    data = _safe_attr(issue_rel, "data", None)
    issue_id = _safe_attr(data, "id", None) if data else None
    if not issue_id:
        return None
    return issues_by_id.get(issue_id)


def _build_title(error_type: str, function_name: str, error_message: str) -> str:
    if error_type and function_name:
        return f"{error_type} @ {function_name}"
    return error_type or error_message[:120] or "Untitled error"


def _build_stack_trace(error_message: str, function_name: str, file_path: str) -> str:
    """search_issues 不返回完整 stack，先用 message + 顶帧凑一个最小标识。

    需要全栈时改用 ErrorTrackingApi.get_issue 单独拉。
    """
    parts = []
    if error_message:
        parts.append(error_message.strip())
    if function_name or file_path:
        loc = f"  at {function_name or '?'} ({file_path or '?'})"
        parts.append(loc)
    return "\n".join(parts) if parts else ""


def normalize_issue(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Datadog raw issue → 统一字段名结构（喂给上游 dedup/classifier 等）。

    所有字段缺失时给安全默认值，避免 KeyError 中断流水线。
    """
    attrs = raw.get("attributes") or {}

    def _ts_to_dt(ms: Any) -> Optional[datetime]:
        if not ms:
            return None
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)

    return {
        "datadog_issue_id": raw.get("id", ""),
        "title": attrs.get("title", ""),
        "service": attrs.get("service", ""),
        "platform": attrs.get("platform", ""),
        "first_seen_at": _ts_to_dt(attrs.get("first_seen_timestamp")),
        "last_seen_at": _ts_to_dt(attrs.get("last_seen_timestamp")),
        "first_seen_version": attrs.get("first_seen_version", ""),
        "last_seen_version": attrs.get("last_seen_version", ""),
        "events_count": int(attrs.get("events_count") or 0),
        "users_affected": int(attrs.get("users_affected") or 0),
        "sessions_affected": int(attrs.get("sessions_affected") or 0),
        "stack_trace": attrs.get("stack_trace", ""),
        "tags": attrs.get("tags") or {},
    }
