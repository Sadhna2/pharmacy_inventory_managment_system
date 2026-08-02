import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, setAccessToken, setAuthLostHandler } from "./api";
import type { LoginResponse, User } from "./types";

interface AuthState {
  user: User | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  can: (...permissions: string[]) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // On first load, try the refresh cookie so a page reload keeps the session.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/v1/auth/refresh", {
          method: "POST",
          credentials: "include",
        });
        if (!cancelled && res.ok) {
          const body: LoginResponse = await res.json();
          setAccessToken(body.access_token);
          setUser(body.user);
        }
      } catch {
        /* no valid session — land on the sign-in screen */
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setAuthLostHandler(() => {
      setAccessToken(null);
      setUser(null);
    });
  }, []);

  const signIn = useCallback(async (email: string, password: string) => {
    const body = await api.post<LoginResponse>("/api/v1/auth/login", {
      email,
      password,
    });
    setAccessToken(body.access_token);
    setUser(body.user);
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api.post("/api/v1/auth/logout");
    } finally {
      setAccessToken(null);
      setUser(null);
    }
  }, []);

  const can = useCallback(
    (...permissions: string[]) =>
      !!user && permissions.every((p) => user.permissions.includes(p)),
    [user],
  );

  const value = useMemo(
    () => ({ user, loading, signIn, signOut, can }),
    [user, loading, signIn, signOut, can],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside <AuthProvider>");
  return context;
}
