import { createContext, useContext } from "react";

export const AuthContext = createContext(null);

export const SESSION_EXPIRED_NOTICE = "Your session has expired. Sign in again to continue.";
export const SIGNED_OUT_NOTICE = "You have signed out.";

export function useAuth() {
  return useContext(AuthContext);
}
