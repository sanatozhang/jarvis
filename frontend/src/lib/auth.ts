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

export type AuthConfig = {
  sso_enabled: boolean;
  support_web: boolean;
  support_desktop: boolean;
  support_mcp: boolean;
};

export async function fetchAuthConfig(): Promise<AuthConfig> {
  const fallback: AuthConfig = { sso_enabled: false, support_web: false, support_desktop: false, support_mcp: false };
  try {
    const res = await fetch("/api/auth/config", { cache: "no-store" });
    if (!res.ok) return fallback;
    return { ...fallback, ...(await res.json()) } as AuthConfig;
  } catch {
    return fallback;
  }
}

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

