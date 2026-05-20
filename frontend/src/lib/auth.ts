"use client";

export type AuthUser = {
  username: string;
  email: string;
  role: "admin" | "user";
  feishu_email: string;
};

export type AuthState =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "authed"; user: AuthUser };

export async function fetchAuthMe(): Promise<AuthUser | null> {
  try {
    const res = await fetch("/api/auth/me", {
      credentials: "include",
      cache: "no-store",
    });
    if (res.status === 401) return null;
    if (res.status === 404) return null;
    if (!res.ok) return null;
    return (await res.json()) as AuthUser;
  } catch {
    return null;
  }
}

export function readLocalStorageUser(): AuthUser | null {
  if (typeof window === "undefined") return null;
  const username = window.localStorage.getItem("appllo_username") || "";
  if (!username) return null;
  return {
    username,
    email: "",
    role: (window.localStorage.getItem("appllo_role") || "user") as "admin" | "user",
    feishu_email: window.localStorage.getItem("appllo_feishu_email") || "",
  };
}

const BIND_PROMPTED_KEY = "appllo_feishu_bind_prompted_date";
const BIND_FEISHU_EMAIL_KEY = "appllo_feishu_email";

export function shouldShowBindPrompt(user: AuthUser | null, ssoActive: boolean): boolean {
  if (typeof window === "undefined") return false;
  if (ssoActive) return false;             // SSO mode already has email via cookie
  if (!user || !user.username) return false;
  if (user.feishu_email) return false;     // already bound
  const today = new Date().toISOString().slice(0, 10);  // YYYY-MM-DD
  const last = window.localStorage.getItem(BIND_PROMPTED_KEY);
  return last !== today;
}

export function markBindPromptedToday(): void {
  if (typeof window === "undefined") return;
  const today = new Date().toISOString().slice(0, 10);
  window.localStorage.setItem(BIND_PROMPTED_KEY, today);
}

export function persistBoundEmail(email: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(BIND_FEISHU_EMAIL_KEY, email);
}
