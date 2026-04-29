"use client";

import { useEffect } from "react";

const S = {
  overlay: "#FFFFFF",
  text1: "#111827",
  border: "rgba(0,0,0,0.08)",
};

export function Toast({
  msg,
  type,
  onClose,
  duration,
}: {
  msg: string;
  type?: "success" | "error";
  onClose: () => void;
  duration?: number;
}) {
  const ms = duration ?? (type === "error" ? 4000 : 2500);
  useEffect(() => {
    const id = setTimeout(onClose, ms);
    return () => clearTimeout(id);
  }, [onClose, ms]);

  const isError = type === "error";
  const isSuccess = type === "success";

  const bg = isError
    ? "rgba(239,68,68,0.12)"
    : isSuccess
      ? "rgba(34,197,94,0.12)"
      : S.overlay;
  const color = isError ? "#DC2626" : isSuccess ? "#16A34A" : S.text1;
  const border = isError
    ? "1px solid rgba(239,68,68,0.25)"
    : isSuccess
      ? "1px solid rgba(34,197,94,0.25)"
      : `1px solid ${S.border}`;

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center pointer-events-none">
      <div
        className="pointer-events-auto rounded-2xl px-8 py-5 text-sm font-medium shadow-2xl max-w-md text-center"
        style={{ background: bg, color, border, backdropFilter: "blur(8px)" }}
      >
        {msg}
      </div>
    </div>
  );
}
