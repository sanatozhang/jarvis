"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { fetchAuthConfig, fetchAuthMe, readLocalStorageUser, type AuthState, type AuthUser } from "@/lib/auth";
import { loginUser } from "@/lib/api";

type Ctx = {
  state: AuthState;
  ssoActive: boolean;
  supportWeb: boolean;
  supportDesktop: boolean;
  supportMcp: boolean;
  // 非 SSO 部署下的首次注册（用户名 + @plaud.ai 邮箱）。成功后立即把 state 切到
  // authed，这样 Sidebar/FeedbackWidget 等 useCurrentUser() 消费方无需刷新页面
  // 就能拿到新用户名（此前 page.tsx 自己维护 username state，注册后不通知这里，
  // 是历史遗留的一个次要不一致，顺带修掉）。
  registerLegacyUser: (username: string, email?: string) => Promise<void>;
};

const AuthContext = createContext<Ctx>({
  state: { status: "loading" },
  ssoActive: false,
  supportWeb: false,
  supportDesktop: false,
  supportMcp: false,
  registerLegacyUser: async () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [ssoActive, setSsoActive] = useState(false);
  const [supportWeb, setSupportWeb] = useState(false);
  const [supportDesktop, setSupportDesktop] = useState(false);
  const [supportMcp, setSupportMcp] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // ssoActive must reflect the backend's ENABLE_SSO config, not "has this
    // browser ever completed a Feishu login" — /me returns 401 both for a
    // brand-new visitor on an SSO-enabled deployment and for any visitor on
    // an SSO-disabled one, so deriving ssoActive from a successful /me call
    // left first-time SSO visitors ungated (they fell through to the legacy
    // username form instead of being redirected to /login).
    Promise.all([fetchAuthConfig(), fetchAuthMe()]).then(([config, user]) => {
      if (cancelled) return;
      setSsoActive(config.sso_enabled);
      setSupportWeb(config.support_web);
      setSupportDesktop(config.support_desktop);
      setSupportMcp(config.support_mcp);
      if (user) {
        setState({ status: "authed", user });
        return;
      }
      const legacy = readLocalStorageUser();
      if (legacy) {
        setState({ status: "authed", user: legacy });
      } else {
        setState({ status: "anonymous" });
      }
    });
    return () => { cancelled = true; };
  }, []);

  const registerLegacyUser = async (username: string, email?: string) => {
    const v = username.trim().toLowerCase();
    const e = (email || "").trim().toLowerCase();
    const user = await loginUser(v, e || undefined);
    localStorage.setItem("appllo_username", user.username);
    localStorage.setItem("appllo_role", user.role);
    if (user.feishu_email) localStorage.setItem("appllo_feishu_email", user.feishu_email);
    setState({
      status: "authed",
      user: { username: user.username, email: "", role: user.role as "admin" | "user", feishu_email: user.feishu_email || "" },
    });
  };

  return (
    <AuthContext.Provider value={{ state, ssoActive, supportWeb, supportDesktop, supportMcp, registerLegacyUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): Ctx {
  return useContext(AuthContext);
}

export function useCurrentUser(): AuthUser | null {
  const { state } = useAuth();
  return state.status === "authed" ? state.user : null;
}
