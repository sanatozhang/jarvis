"use client";

import { useState, useEffect } from "react";
import { LangContext, LangToggleContext, type Lang } from "@/lib/i18n";

// 英文优先策略：每次访问默认 English；切换按钮在当前 session 内有效，
// 但**不持久化**——下次刷新自动回到英文。
export default function LangProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLang] = useState<Lang>("en");

  useEffect(() => {
    // 一次性 migration：清除老用户可能残留的 appllo_lang 偏好
    try {
      localStorage.removeItem("appllo_lang");
    } catch {}
  }, []);

  const toggle = () => {
    setLang((prev) => (prev === "cn" ? "en" : "cn"));
  };

  return (
    <LangContext.Provider value={lang}>
      <LangToggleContext.Provider value={toggle}>
        {children}
      </LangToggleContext.Provider>
    </LangContext.Provider>
  );
}
