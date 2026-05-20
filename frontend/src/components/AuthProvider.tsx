"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { fetchAuthMe, readLocalStorageUser, type AuthState, type AuthUser } from "@/lib/auth";

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
    fetchAuthMe().then((user) => {
      if (cancelled) return;
      if (user) {
        setState({ status: "authed", user });
        setSsoActive(true);
        return;
      }
      const legacy = readLocalStorageUser();
      if (legacy) {
        setState({ status: "authed", user: legacy });
        setSsoActive(false);
      } else {
        setState({ status: "anonymous" });
        setSsoActive(false);
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
