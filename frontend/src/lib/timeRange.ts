// Pure date-window helpers for the analytics natural-week time selector.
// No React, no fetch — deliberately isolated so the calendar-week math can
// be reasoned about (and eventually tested) independently of the page.
//
// All "today" anchors are UTC, matching the backend's `date_window.py`
// (which anchors to `datetime.utcnow()` against naive-UTC `created_at`
// columns). Do NOT use local Date methods (getDay/getMonth/...) here —
// only the UTC variants — or a user west of UTC will see a different
// "today" than the backend does.

export type TimeRange = { kind: "week"; weekStart: string } | { kind: "days"; days: number };

export interface ResolvedRange {
  dateFrom: string;
  dateTo: string;
  spanDays: number;
  weekStart?: string; // only set for kind === "week"
  inProgress: boolean; // true for the current (not-yet-complete) week
}

export const PRESET_DAYS = [30, 90, 180, 365] as const;

function todayUTC(): Date {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
}

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function addDaysUTC(d: Date, days: number): Date {
  const copy = new Date(d.getTime());
  copy.setUTCDate(copy.getUTCDate() + days);
  return copy;
}

/**
 * The Monday (UTC) of the calendar week containing `d`.
 *
 * getUTCDay() returns 0 for Sunday, 1 for Monday, ..., 6 for Saturday —
 * NOT the same convention as Python's date.weekday() (0=Monday). The
 * `(getUTCDay() + 6) % 7` remaps Sunday to 6 and Monday to 0, matching
 * Python. Writing this as `getUTCDay() - 1` (the naive translation) is
 * wrong for Sunday — it produces -1, off by a full week.
 */
export function mondayOf(d: Date): string {
  const wd = (d.getUTCDay() + 6) % 7;
  return isoDate(addDaysUTC(d, -wd));
}

export function thisMonday(today: Date = todayUTC()): string {
  return mondayOf(today);
}

export function lastMonday(today: Date = todayUTC()): string {
  const thisWeek = new Date(mondayOf(today) + "T00:00:00Z");
  return isoDate(addDaysUTC(thisWeek, -7));
}

export function isMondayISO(s: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const d = new Date(s + "T00:00:00Z");
  if (Number.isNaN(d.getTime())) return false;
  // Reject non-canonical inputs date parsing would silently normalize
  // (e.g. "2026-02-30" -> March 2) by round-tripping back to ISO.
  if (isoDate(d) !== s) return false;
  return d.getUTCDay() === 1;
}

export function resolveRange(r: TimeRange, today: Date = todayUTC()): ResolvedRange {
  const todayStr = isoDate(today);
  if (r.kind === "week") {
    const start = r.weekStart;
    const isCurrent = start === thisMonday(today);
    if (isCurrent) {
      const spanDays = Math.floor((today.getTime() - new Date(start + "T00:00:00Z").getTime()) / 86400000) + 1;
      return { dateFrom: start, dateTo: todayStr, spanDays, weekStart: start, inProgress: true };
    }
    const end = isoDate(addDaysUTC(new Date(start + "T00:00:00Z"), 6));
    return { dateFrom: start, dateTo: end, spanDays: 7, weekStart: start, inProgress: false };
  }
  const dateFrom = isoDate(addDaysUTC(today, -(r.days - 1)));
  return { dateFrom, dateTo: todayStr, spanDays: r.days, inProgress: false };
}

/**
 * For a resolved "current week" range, the aligned prior-week baseline
 * covering the SAME weekday span (e.g. Mon-Wed vs. the previous Mon-Wed) —
 * comparing partial-week-in-progress against a full prior week (Fri-Sun
 * included) is comparing weekday traffic to weekend traffic, which is not
 * a meaningful baseline. Returns null for non-week ranges, where the
 * caller's default (immediately-prior same-length period) already applies.
 */
export function alignedPrevWeek(r: ResolvedRange): { prevFrom: string; prevTo: string } | null {
  if (!r.weekStart) return null;
  const priorMonday = addDaysUTC(new Date(r.weekStart + "T00:00:00Z"), -7);
  const prevFrom = isoDate(priorMonday);
  const prevTo = isoDate(addDaysUTC(priorMonday, r.spanDays - 1));
  return { prevFrom, prevTo };
}
