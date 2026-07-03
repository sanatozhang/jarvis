"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { fetchAuthConfig, fetchAuthMe, readLocalStorageUser, type AuthState, type AuthUser } from "@/lib/auth";

type Ctx = {
  state: AuthState;
  ssoActive: boolean;
};

const AuthContext = createContext<Ctx>({ state: { status: "loading" }, ssoActive: false });

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [ssoActive, setSsoActive] = useState(false);

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

  return <AuthContext.Provider value={{ state, ssoActive }}>{children}</AuthContext.Provider>;
}

export function useAuth(): Ctx {
  return useContext(AuthContext);
}

export function useCurrentUser(): AuthUser | null {
  const { state } = useAuth();
  return state.status === "authed" ? state.user : null;
}
