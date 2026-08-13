// Pure date-window helpers for the analytics natural-week time selector.
// No React, no fetch — deliberately isolated so the calendar-week math can
// be reasoned about (and eventually tested) independently of the page.
//
// All "today" anchors are UTC, matching the backend's `date_window.py`
// (which anchors to `datetime.utcnow()` against naive-UTC `created_at`
// columns). Do NOT use local Date methods (getDay/getMonth/...) here —
// only the UTC variants — or a user west of UTC will see a different
// "today" than the backend does.

export type TimeRange =
  | { kind: "week"; weekStart: string }
  | { kind: "month"; monthStart: string }
  | { kind: "days"; days: number };

export interface ResolvedRange {
  dateFrom: string;
  dateTo: string;
  spanDays: number;
  weekStart?: string; // only set for kind === "week"
  monthStart?: string; // only set for kind === "month"
  inProgress: boolean; // true for the current (not-yet-complete) week/month
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

/** Shift a month-start date (`d.getUTCDate() === 1`) by `months` whole months. */
function addMonthsUTC(d: Date, months: number): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + months, 1));
}

export function monthStartOf(d: Date): string {
  return isoDate(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1)));
}

export function thisMonthStart(today: Date = todayUTC()): string {
  return monthStartOf(today);
}

export function lastMonthStart(today: Date = todayUTC()): string {
  const thisStart = new Date(monthStartOf(today) + "T00:00:00Z");
  return isoDate(addMonthsUTC(thisStart, -1));
}

export function isMonthStartISO(s: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const d = new Date(s + "T00:00:00Z");
  if (Number.isNaN(d.getTime())) return false;
  if (isoDate(d) !== s) return false;
  return d.getUTCDate() === 1;
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
  if (r.kind === "month") {
    const start = r.monthStart;
    const isCurrent = start === thisMonthStart(today);
    if (isCurrent) {
      const spanDays = Math.floor((today.getTime() - new Date(start + "T00:00:00Z").getTime()) / 86400000) + 1;
      return { dateFrom: start, dateTo: todayStr, spanDays, monthStart: start, inProgress: true };
    }
    const nextMonthStart = addMonthsUTC(new Date(start + "T00:00:00Z"), 1);
    const end = isoDate(addDaysUTC(nextMonthStart, -1));
    const spanDays = Math.floor((nextMonthStart.getTime() - new Date(start + "T00:00:00Z").getTime()) / 86400000);
    return { dateFrom: start, dateTo: end, spanDays, monthStart: start, inProgress: false };
  }
  const dateFrom = isoDate(addDaysUTC(today, -(r.days - 1)));
  return { dateFrom, dateTo: todayStr, spanDays: r.days, inProgress: false };
}

/**
 * For a resolved "current week/month" range, the aligned prior-period
 * baseline covering the SAME span (e.g. Mon-Wed vs. the previous Mon-Wed,
 * or Aug 1-12 vs. Jul 1-12) — comparing a partial period-in-progress
 * against a full prior period (weekend days included, or a longer/shorter
 * month) is not a meaningful baseline. Returns null for `days`-kind ranges,
 * where the caller's default (immediately-prior same-length period)
 * already applies.
 */
export function alignedPrevPeriod(r: ResolvedRange): { prevFrom: string; prevTo: string } | null {
  if (r.weekStart) {
    const priorMonday = addDaysUTC(new Date(r.weekStart + "T00:00:00Z"), -7);
    const prevFrom = isoDate(priorMonday);
    const prevTo = isoDate(addDaysUTC(priorMonday, r.spanDays - 1));
    return { prevFrom, prevTo };
  }
  if (r.monthStart) {
    const priorMonthStart = addMonthsUTC(new Date(r.monthStart + "T00:00:00Z"), -1);
    const prevFrom = isoDate(priorMonthStart);
    // In-progress: mirror the same partial span into the prior month. A
    // COMPLETED month must use the prior month's own natural last day
    // (monthStart - 1 day), NOT `priorMonthStart + spanDays - 1` — months
    // have unequal lengths, so that arithmetic mislands (a 31-day July
    // baselined with `+30d` from June 1st lands on July 1st, not June 30th).
    const prevTo = r.inProgress
      ? isoDate(addDaysUTC(priorMonthStart, r.spanDays - 1))
      : isoDate(addDaysUTC(new Date(r.monthStart + "T00:00:00Z"), -1));
    return { prevFrom, prevTo };
  }
  return null;
}
