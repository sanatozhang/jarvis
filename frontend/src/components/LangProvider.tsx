"use client";

import { useState, useEffect } from "react";
import { LangContext, LangToggleContext, type Lang } from "@/lib/i18n";

export default function LangProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLang] = useState<Lang>("cn");

  useEffect(() => {
    const saved = localStorage.getItem("jarvis_lang");
    if (saved === "en" || saved === "cn") setLang(saved);
  }, []);

  const toggle = () => {
    const next = lang === "cn" ? "en" : "cn";
    setLang(next);
    localStorage.setItem("jarvis_lang", next);
  };

  return (
    <LangContext.Provider value={lang}>
      <LangToggleContext.Provider value={toggle}>
        {children}
      </LangToggleContext.Provider>
    </LangContext.Provider>
  );
}
