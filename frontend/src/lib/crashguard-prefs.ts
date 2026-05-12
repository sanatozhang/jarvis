/**
 * Crashguard 浏览器端偏好（localStorage 持久化）。
 *
 * 底层逻辑：批量分析 Top N 等用户偏好不需要后端持久化（每个工程师都可以有自己的预期），
 * localStorage 本地存就够；要全局生效再升级到后端 config。
 */

const KEY_BATCH_TOP_N = "crashguard.batch_top_n";
const DEFAULT_BATCH_TOP_N = 20;
const MIN_TOP_N = 1;
const MAX_TOP_N = 100;

export function getBatchTopN(): number {
  if (typeof window === "undefined") return DEFAULT_BATCH_TOP_N;
  try {
    const raw = window.localStorage.getItem(KEY_BATCH_TOP_N);
    const n = parseInt(raw || "", 10);
    if (Number.isFinite(n) && n >= MIN_TOP_N && n <= MAX_TOP_N) return n;
  } catch {
    /* SSR / 隐私模式 fallback */
  }
  return DEFAULT_BATCH_TOP_N;
}

export function setBatchTopN(n: number): number {
  const clamped = Math.max(MIN_TOP_N, Math.min(MAX_TOP_N, Math.floor(n) || DEFAULT_BATCH_TOP_N));
  try {
    window.localStorage.setItem(KEY_BATCH_TOP_N, String(clamped));
  } catch {
    /* ignored */
  }
  // 跨页广播（settings 改 → 首页同步）
  try {
    window.dispatchEvent(new CustomEvent("crashguard:batch_top_n_changed", { detail: clamped }));
  } catch {
    /* ignored */
  }
  return clamped;
}

export const BATCH_TOP_N_BOUNDS = { min: MIN_TOP_N, max: MAX_TOP_N, default: DEFAULT_BATCH_TOP_N };
