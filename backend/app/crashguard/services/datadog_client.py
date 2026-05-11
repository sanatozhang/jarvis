"""
Datadog Error Tracking client (基于官方 SDK datadog-api-client)。

API 文档: https://docs.datadoghq.com/api/latest/error-tracking/
SDK: https://github.com/DataDog/datadog-api-client-python

外层签名 (DatadogClient.list_issues / normalize_issue) 与早期 httpx 版本兼容，
内部改为官方 SDK 调用 POST /api/v2/error-tracking/issues/search。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

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
    ):
        self.api_key = api_key
        self.app_key = app_key
        self.site = site
        self.timeout = timeout
        self._rate_limit_events: Deque[float] = deque(maxlen=10)
        self._circuit_open_until: float = 0.0
        self._circuit_threshold: int = 5
        self._circuit_window_sec: int = 600     # 10 分钟
        self._circuit_open_sec: int = 1800      # 30 分钟

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
        return IssuesSearchRequest(
            data=IssuesSearchRequestData(
                attributes=IssuesSearchRequestDataAttributes(
                    query=query or "*",
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
                    # Datadog Error Tracking 只返回 sessions 维度，不返回 users。
                    # users_affected 走单独的 RUM Events API（Plan 2.5），目前置 0。
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

        best_event = None
        best_score = float("-inf")
        best_stack = ""
        device_counter: Counter = Counter()
        os_counter: Counter = Counter()
        version_counter: Counter = Counter()
        view_counter: Counter = Counter()
        country_counter: Counter = Counter()

        for ev in events:
            inner = self._extract_inner_attrs(ev)
            if not inner:
                continue
            tags = self._extract_event_tags(ev)
            stack = self._extract_path(inner, "error", "stack") or ""
            score = self._score_stack(stack)
            if score > best_score:
                best_score = score
                best_event = (ev, inner, tags)
                best_stack = stack

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

        if best_event is None:
            return None
        ev, inner, tags = best_event
        total = len(events)
        return {
            "full_stack": best_stack,
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

    async def count_sessions_by_platform(
        self,
        window_hours: int = 24,
    ) -> Dict[str, int]:
        """
        统计最近 N 小时内 RUM session 总数，按平台分桶。
        返回 {"android": int, "ios": int, "flutter": int, ...}
        失败返回 {} 不致命。
        """
        try:
            return await asyncio.to_thread(self._sync_count_sessions_by_platform, window_hours)
        except Exception as exc:
            logger.warning("count_sessions_by_platform failed: %s", exc)
            return {}

    def _sync_count_sessions_by_platform(self, window_hours: int) -> Dict[str, int]:
        """同步实现：用 aggregate_rum_events 按 service.platform group_by。"""
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

        body = RUMAggregateRequest(
            filter=RUMQueryFilter(
                query="@type:session",
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
        统计最近 N 小时内 crash sessions 数，按平台分桶。

        与 Datadog 官方 RUM Mobile "Crash-free sessions" 面板对齐：
            filter   = @type:session AND @session.crash.count:>0
            agg      = COUNT (每个 session 是一条 event，自带 crash.count 字段)
            group_by = @os.name → 归桶 ANDROID / IOS / OTHER

        说明：Datadog Mobile RUM SDK 在 session 上注入 `session.crash.count` 字段，
        包含 NDK crash + ANR + iOS crash + App Hang 等所有崩溃类型。
        直接按 session 维度 count 即可，无需通过 error 事件 distinct 反推。

        返回 {"android": int, "ios": int, "other": int}；失败返回 {} 不致命。
        """
        try:
            return await asyncio.to_thread(
                self._sync_count_distinct_crash_sessions_by_platform, window_hours
            )
        except Exception as exc:
            logger.warning("count_distinct_crash_sessions_by_platform failed: %s", exc)
            return {}

    def _sync_count_distinct_crash_sessions_by_platform(self, window_hours: int) -> Dict[str, int]:
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
                query="@type:session @session.crash.count:>0",
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
                query="@type:error",
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
                query="@type:error @error.is_crash:true",
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
                query=f"@issue.id:{issue_id}",
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
