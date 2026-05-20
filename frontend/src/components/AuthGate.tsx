"use client";

import { useRouter, usePathname } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { useAuth } from "./AuthProvider";

export function AuthGate({ children }: { children: ReactNode }) {
  const { state, ssoActive } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!ssoActive) return;
    if (state.status !== "anonymous") return;
    if (pathname === "/login") return;
    const next = encodeURIComponent(pathname || "/");
    router.replace(`/login?next=${next}`);
  }, [state.status, ssoActive, pathname, router]);

  if (state.status === "loading") {
    return <div className="flex items-center justify-center h-screen text-j-fg/60">Loading…</div>;
  }
  if (ssoActive && state.status === "anonymous" && pathname !== "/login") {
    return null;
  }
  return <>{children}</>;
}
