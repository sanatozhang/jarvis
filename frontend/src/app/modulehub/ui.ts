// Shared look and helpers of the modulehub page (split across files to keep each one small).
import { formatLocalTime, type MhPlatform, type MhRelease, type MhReleaseState } from "@/lib/api";

export const S = {
  surface: "var(--j-surface)",
  overlay: "var(--j-panel)",
  hover: "var(--j-hover)",
  border: "var(--j-border)",
  accent: "var(--j-accent)",
  accentBg: "var(--j-accent-soft)",
  text1: "var(--j-ink)",
  text2: "var(--j-graphite)",
  text3: "var(--j-faint)",
  danger: "#DC2626",
  dangerBg: "#FEE2E2",
  warn: "#B45309",
  warnBg: "#FEF3C7",
};

export const inputStyle = {
  background: S.overlay,
  border: `1px solid ${S.border}`,
  color: S.text1,
  outline: "none",
};

export const PLATFORMS: MhPlatform[] = ["android", "ios"];
export const PLATFORM_LABEL: Record<MhPlatform, string> = { android: "Android", ios: "iOS" };

// label = Chinese i18n key (pass through t())
export const STATE_STYLE: Record<MhReleaseState, { bg: string; fg: string; label: string }> = {
  pending: { bg: "#F1F5F9", fg: "#475569", label: "待触发" },
  building: { bg: "#DBEAFE", fg: "#1D4ED8", label: "构建中" },
  tagged: { bg: "#E0E7FF", fg: "#3730A3", label: "已打 tag" },
  pr_opened: { bg: "#E0E7FF", fg: "#3730A3", label: "已开 bump PR" },
  backport_opened: { bg: "#E0E7FF", fg: "#3730A3", label: "已开回合 PR" },
  done: { bg: "#DCFCE7", fg: "#15803D", label: "完成" },
  failed: { bg: "#FEE2E2", fg: "#B91C1C", label: "失败" },
};

/** `main` or `release/<something>` — mirrors backend release_rules.validate_request. */
export function isValidBranch(branch: string): boolean {
  return branch === "main" || (branch.startsWith("release/") && branch.length > "release/".length);
}

export function isReleaseBranch(branch: string): boolean {
  return branch.startsWith("release/");
}

/** GitHub tag page of a released version (the publish job tags `v<version>`). */
export function tagUrl(r: MhRelease): string | null {
  if (r.kind !== "release" || !r.repo || !r.version) return null;
  if (r.state === "building" || r.state === "pending") return null;
  if (r.state === "failed" && !["tagged", "pr_opened"].includes(r.failedFrom)) return null;
  return `https://github.com/${r.repo}/releases/tag/v${r.version}`;
}

export function fmtTime(iso: string | null): string {
  return formatLocalTime(iso);
}

export function sinceSeconds(iso: string | null): number {
  if (!iso) return 0;
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  return Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
}
