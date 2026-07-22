"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
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
  const pathname = usePathname();

  useEffect(() => {
    let cancelled = false;
    fetchAuthMe().then((user) => {
      if (cancelled) return;
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

  // 平台支持开关（web/desktop/mcp）在 /settings 页可随时改，且是全站共享的开关，
  // 不是"只影响当前标签页"的东西——不能只在应用首次挂载时拉一次就再也不刷新。
  // 之前只在 mount 时 fetch 一次：管理员在 /settings 打开某个开关保存成功后，
  // 不刷新整个页面（Next.js 客户端路由切页面不会重新挂载 AuthProvider）就切去
  // /feedback，看到的还是挂载时那份旧配置，开关"看起来没生效"。按路由变化重新
  // 拉一次配置（无需鉴权，开销很小），覆盖这种滞后。
  useEffect(() => {
    let cancelled = false;
    // ssoActive must reflect the backend's ENABLE_SSO config, not "has this
    // browser ever completed a Feishu login" — /me returns 401 both for a
    // brand-new visitor on an SSO-enabled deployment and for any visitor on
    // an SSO-disabled one, so deriving ssoActive from a successful /me call
    // left first-time SSO visitors ungated (they fell through to the legacy
    // username form instead of being redirected to /login).
    fetchAuthConfig().then((config) => {
      if (cancelled) return;
      setSsoActive(config.sso_enabled);
      setSupportWeb(config.support_web);
      setSupportDesktop(config.support_desktop);
      setSupportMcp(config.support_mcp);
    });
    return () => { cancelled = true; };
  }, [pathname]);

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
