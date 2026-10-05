"use client";

import { Fragment } from "react";
import { useT } from "@/lib/i18n";
import { MH_RESUMABLE_FROM, type MhRelease } from "@/lib/api";
import { S, PLATFORM_LABEL, STATE_STYLE, fmtTime, tagUrl } from "./ui";

const linkStyle = { color: S.accent };

function Links({ r }: { r: MhRelease }) {
  const t = useT();
  const tag = tagUrl(r);
  const items: { href: string; label: string }[] = [];
  if (r.buildUrl) items.push({ href: r.buildUrl, label: "Jenkins" });
  if (tag) items.push({ href: tag, label: `tag v${r.version}` });
  for (const p of r.platforms) {
    const pr = r.bumpPrs[p];
    if (pr) items.push({ href: pr, label: `${PLATFORM_LABEL[p]} bump PR` });
  }
  if (r.backportPrUrl) items.push({ href: r.backportPrUrl, label: t("回合 PR") });
  if (items.length === 0) return <span style={{ color: S.text3 }}>—</span>;
  return (
    <div className="flex flex-col gap-0.5">
      {items.map((i) => (
        <a key={i.href + i.label} href={i.href} target="_blank" rel="noreferrer" style={linkStyle} onClick={(e) => e.stopPropagation()}>
          {i.label}
        </a>
      ))}
    </div>
  );
}

function Detail({ r }: { r: MhRelease }) {
  const t = useT();
  return (
    <div className="space-y-2 px-2 py-3 text-xs" style={{ color: S.text2 }}>
      <div>
        {t("仓库")}: <span className="font-mono">{r.repo || "—"}</span>
        {r.gitSha && <> · commit <span className="font-mono">{r.gitSha.slice(0, 10)}</span></>}
        {r.resumeCount > 0 && <> · {t("续跑次数")} {r.resumeCount}</>}
        {r.updatedAt && <> · {t("更新于")} {fmtTime(r.updatedAt)}</>}
      </div>
      {r.error && (
        <div style={{ color: S.danger }}>
          {r.failedFrom && <span className="font-mono">[{r.failedFrom}] </span>}
          {r.error}
        </div>
      )}
      {r.platforms.map((p) => {
        const a = r.artifacts[p];
        const prev = r.previousVersions[p];
        const pr = r.bumpPrs[p];
        return (
          <div key={p}>
            <span className="font-medium" style={{ color: S.text1 }}>{PLATFORM_LABEL[p]}</span>
            {prev !== undefined && <span className="ml-2 font-mono">{prev || "—"} → {r.version}</span>}
            {a?.coordinate && <span className="ml-2 font-mono" style={{ color: S.text3 }}>{a.coordinate}</span>}
            {pr === "" && <span className="ml-2" style={{ color: S.text3 }}>{t("壳工程已固定该版本，无需 bump PR")}</span>}
            {a && a.apiChanges.length > 0 && (
              <ul className="ml-4 mt-1 list-disc font-mono">
                {a.apiChanges.map((c, i) => <li key={i}>{c}</li>)}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function ReleasesTable({
  releases,
  selectedId,
  onSelect,
  onResume,
  resumingId,
}: {
  releases: MhRelease[];
  selectedId: number | null;
  onSelect: (id: number | null) => void;
  onResume: (r: MhRelease) => void;
  resumingId: number | null;
}) {
  const t = useT();
  return (
    <div className="mt-3 overflow-x-auto">
      <table className="min-w-full text-xs">
        <thead>
          <tr style={{ color: S.text2 }}>
            {["ID", "模块", "分支", "平台", "状态", "版本", "链接", "发起人", "发起时间", "操作"].map((h) => (
              <th key={h} className="px-2 py-2 text-left">{t(h)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {releases.length === 0 && (
            <tr>
              <td colSpan={10} className="px-2 py-6 text-center" style={{ color: S.text3 }}>{t("暂无发版记录")}</td>
            </tr>
          )}
          {releases.map((r) => {
            const st = STATE_STYLE[r.state] || STATE_STYLE.pending;
            const selected = r.id === selectedId;
            const resumable = r.kind === "release" && r.state === "failed" && MH_RESUMABLE_FROM.includes(r.failedFrom);
            return (
              <Fragment key={r.id}>
                <tr
                  onClick={() => onSelect(selected ? null : r.id)}
                  className="cursor-pointer"
                  style={{ borderTop: `1px solid ${S.border}`, background: selected ? S.accentBg : "transparent" }}
                >
                  <td className="px-2 py-2 font-mono" style={{ color: S.text2 }}>#{r.id}</td>
                  <td className="px-2 py-2">
                    <span className="font-medium">{r.module}</span>
                    {r.kind === "preview" && (
                      <span className="ml-1 rounded px-1 py-0.5 text-[10px]" style={{ background: S.hover, color: S.text3 }}>{t("预览")}</span>
                    )}
                  </td>
                  <td className="px-2 py-2 font-mono">
                    {r.branch}
                    {r.major && <span className="ml-1 text-[10px]" style={{ color: S.warn }}>major</span>}
                  </td>
                  <td className="px-2 py-2">{r.platforms.map((p) => PLATFORM_LABEL[p]).join(" + ")}</td>
                  <td className="px-2 py-2">
                    <span className="rounded px-2 py-0.5" style={{ background: st.bg, color: st.fg }} title={r.error || ""}>
                      {t(st.label)}
                    </span>
                    {r.state === "failed" && r.failedFrom && (
                      <div className="mt-0.5 font-mono text-[10px]" style={{ color: S.text3 }}>@{r.failedFrom}</div>
                    )}
                  </td>
                  <td className="px-2 py-2 font-mono">{r.version || "—"}</td>
                  <td className="px-2 py-2"><Links r={r} /></td>
                  <td className="px-2 py-2" style={{ color: S.text2 }}>{r.requestedBy || "—"}</td>
                  <td className="px-2 py-2" style={{ color: S.text2 }}>{fmtTime(r.createdAt)}</td>
                  <td className="px-2 py-2">
                    {resumable ? (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onResume(r);
                        }}
                        disabled={resumingId === r.id}
                        className="rounded px-2 py-1 font-medium text-white disabled:opacity-50"
                        style={{ background: S.accent }}
                      >
                        {resumingId === r.id ? t("续跑中…") : t("续跑")}
                      </button>
                    ) : (
                      <span style={{ color: S.text3 }}>—</span>
                    )}
                  </td>
                </tr>
                {selected && (
                  <tr style={{ background: S.accentBg }}>
                    <td colSpan={10}><Detail r={r} /></td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
