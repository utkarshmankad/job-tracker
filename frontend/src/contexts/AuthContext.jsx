import { useCallback, useEffect, useMemo, useState } from "react";
import { auth as authApi, setCsrfToken, setUnauthorizedHandler } from "../api/client";
import { AuthContext, SESSION_EXPIRED_NOTICE, SIGNED_OUT_NOTICE } from "./auth-context";

// setTimeout fires immediately for delays above 2^31-1 ms; cap so long sessions still work.
const MAX_TIMER_MS = 2 ** 31 - 1;

const unauthenticated = (notice = null) => ({
  status: "unauthenticated",
  user: null,
  expiresAt: null,
  notice,
});

/**
 * Owns the signed-in state. Tracker data components are only rendered once `status` is
 * "authenticated", so no tracker request is sent before sign-in succeeds.
 */
export function AuthProvider({ children }) {
  const [state, setState] = useState({ status: "loading", user: null, expiresAt: null, notice: null });

  const applySession = useCallback((session) => {
    setCsrfToken(session.csrf_token);
    setState({
      status: "authenticated",
      user: session.user,
      expiresAt: new Date(session.expires_at).getTime(),
      notice: null,
    });
  }, []);

  const endSession = useCallback((notice) => {
    setCsrfToken(null);
    setState(unauthenticated(notice));
  }, []);

  // Any protected request that comes back 401 means the session expired or was revoked.
  useEffect(() => {
    setUnauthorizedHandler(() => endSession(SESSION_EXPIRED_NOTICE));
    return () => setUnauthorizedHandler(null);
  }, [endSession]);

  // Restore an existing session on load.
  useEffect(() => {
    let cancelled = false;
    authApi
      .getSession()
      .then((session) => {
        if (!cancelled) applySession(session);
      })
      .catch((error) => {
        if (cancelled) return;
        setCsrfToken(null);
        setState(
          unauthenticated(
            error?.status === 401 ? null : "Could not reach the server. Try again shortly.",
          ),
        );
      });
    return () => {
      cancelled = true;
    };
  }, [applySession]);

  // Return to the login screen when the session lifetime runs out, even if idle.
  useEffect(() => {
    if (state.status !== "authenticated" || !state.expiresAt) return undefined;
    const delay = Math.min(Math.max(state.expiresAt - Date.now(), 0), MAX_TIMER_MS);
    const timer = setTimeout(() => endSession(SESSION_EXPIRED_NOTICE), delay);
    return () => clearTimeout(timer);
  }, [state.status, state.expiresAt, endSession]);

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      // The server clears the cookie on its own expiry; locally we sign out regardless.
    }
    window.google?.accounts?.id?.disableAutoSelect?.();
    endSession(SIGNED_OUT_NOTICE);
  }, [endSession]);

  const value = useMemo(
    () => ({ ...state, applySession, logout }),
    [state, applySession, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
