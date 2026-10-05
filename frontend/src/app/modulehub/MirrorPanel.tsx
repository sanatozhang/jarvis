"use client";

import { useState } from "react";
import { useT } from "@/lib/i18n";
import { mhErrorDetail, syncMhMirror } from "@/lib/api";
import { S } from "./ui";

// Lines the backend flags for a human (MirrorService._execute); everything else is informational.
function lineColor(line: string): string {
  if (/DIVERGED|MISMATCH|: error: /.test(line)) return S.danger;
  if (/: ok$|: created from /.test(line)) return S.text1;
  return S.text2;
}

export function MirrorPanel({ notify }: { notify: (msg: string, type: "success" | "error") => void }) {
  const t = useT();
  const [results, setResults] = useState<string[] | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [syncedAt, setSyncedAt] = useState<Date | null>(null);

  const handleSync = async () => {
    setConfirming(false);
    setSyncing(true);
    try {
      const r = await syncMhMirror();
      setResults(r.results);
      setSyncedAt(new Date());
    } catch (e) {
      notify(mhErrorDetail(e), "error");
    } finally {
      setSyncing(false);
    }
  };

  const flagged = (results || []).filter((l) => lineColor(l) === S.danger).length;

  return (
    <section className="rounded-lg p-5" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold">{t("分支对账")}</h2>
        {!confirming ? (
          <button
            onClick={() => setConfirming(true)}
            disabled={syncing}
            className="rounded px-3 py-1.5 text-xs font-medium disabled:opacity-50"
            style={{ border: `1px solid ${S.accent}`, color: S.accent }}
          >
            {syncing ? t("对账中…") : t("立即对账")}
          </button>
        ) : (
          <div className="flex items-center gap-2 text-xs">
            <span style={{ color: S.warn }}>{t("会为缺失的模块 release 分支自动建分支，继续？")}</span>
            <button onClick={handleSync} className="rounded px-3 py-1.5 font-medium text-white" style={{ background: S.accent }}>
              {t("确认")}
            </button>
            <button onClick={() => setConfirming(false)} className="rounded px-3 py-1.5" style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
              {t("取消")}
            </button>
          </div>
        )}
      </div>
      <p className="mt-1 text-xs" style={{ color: S.text2 }}>
        {t("对比两个壳工程 release/* 上固定的版本与模块仓库的 release/* 分支：缺失则从最新 tag 建分支，已有则校验；DIVERGED / MISMATCH 需人工处理。后台每隔几分钟也会自动跑（仅启用时）。")}
      </p>
      {results && (
        <div className="mt-3 text-xs">
          <div style={{ color: S.text3 }}>
            {syncedAt?.toLocaleTimeString()} · {results.length} {t("条结果")}
            {flagged > 0 && <span style={{ color: S.danger }}> · {flagged} {t("条需人工处理")}</span>}
          </div>
          {results.length === 0 ? (
            <div className="mt-2" style={{ color: S.text3 }}>{t("没有需要对账的模块")}</div>
          ) : (
            <ul className="mt-2 space-y-1 font-mono">
              {results.map((l, i) => <li key={i} style={{ color: lineColor(l) }}>{l}</li>)}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
