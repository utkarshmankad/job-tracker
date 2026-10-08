import { useCallback, useEffect, useRef, useState } from "react";
import { Sun, Moon, Loader2 } from "lucide-react";
import { auth } from "../api/client";
import { useAuth } from "../contexts/auth-context";
import { useTheme } from "../contexts/ThemeContext";
import BrandMark from "./BrandMark";

const GOOGLE_IDENTITY_SRC = "https://accounts.google.com/gsi/client";
let googleIdentityPromise = null;

// Loads Google Identity Services once. Resolves immediately if it is already present.
function loadGoogleIdentity() {
  if (window.google?.accounts?.id) return Promise.resolve(window.google);
  if (!googleIdentityPromise) {
    googleIdentityPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = GOOGLE_IDENTITY_SRC;
      script.async = true;
      script.defer = true;
      script.onload = () =>
        window.google?.accounts?.id ? resolve(window.google) : reject(new Error("unavailable"));
      script.onerror = () => {
        googleIdentityPromise = null;
        reject(new Error("load failed"));
      };
      document.head.appendChild(script);
    });
  }
  return googleIdentityPromise;
}

function signInErrorMessage(error) {
  if (error?.status === 403 && error.code === "account_not_allowed") {
    return "That Google account isn't allowed to use Job Tracker. Sign in with the owner's account.";
  }
  if (error?.status === 403) return "Sign-in isn't permitted from here.";
  if (error?.status === 429) return "Too many sign-in attempts. Wait a few minutes and try again.";
  if (error?.status === 503) return error.detail ?? "Sign-in is temporarily unavailable. Try again shortly.";
  if (error?.status === 401) return "Google sign-in could not be verified. Try again.";
  return "Sign-in failed. Check your connection and try again.";
}

export default function LoginScreen() {
  const { applySession, notice } = useAuth();
  const { dark, toggle } = useTheme();
  const [config, setConfig] = useState(null);
  const [configError, setConfigError] = useState(null);
  const [signInError, setSignInError] = useState(null);
  const [busy, setBusy] = useState(false);
  // Bumped after a failed attempt: each Google sign-in needs a fresh single-use nonce.
  const [attempt, setAttempt] = useState(0);
  const googleButtonRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    auth
      .getConfig()
      .then((result) => {
        if (cancelled) return;
        setConfigError(null);
        setConfig(result);
      })
      .catch(() => {
        if (!cancelled) setConfigError("Could not reach the server. Try again shortly.");
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const completeSignIn = useCallback(
    async (signIn) => {
      setBusy(true);
      setSignInError(null);
      try {
        applySession(await signIn());
      } catch (error) {
        setSignInError(signInErrorMessage(error));
        setAttempt((n) => n + 1);
      } finally {
        setBusy(false);
      }
    },
    [applySession],
  );

  const googleReady = config?.mode === "google" && config.configured && config.google_client_id;

  useEffect(() => {
    if (!googleReady) return undefined;
    let cancelled = false;
    loadGoogleIdentity()
      .then((google) => {
        if (cancelled || !googleButtonRef.current) return;
        google.accounts.id.initialize({
          client_id: config.google_client_id,
          nonce: config.nonce,
          auto_select: false,
          cancel_on_tap_outside: true,
          callback: ({ credential }) => completeSignIn(() => auth.loginWithGoogle(credential)),
        });
        googleButtonRef.current.replaceChildren();
        google.accounts.id.renderButton(googleButtonRef.current, {
          type: "standard",
          theme: dark ? "filled_black" : "outline",
          size: "large",
          text: "signin_with",
          shape: "rectangular",
          logo_alignment: "left",
          width: 280,
        });
      })
      .catch(() => {
        if (!cancelled) {
          setConfigError(
            "Google sign-in could not be loaded. Check your connection or content blockers.",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [googleReady, config, dark, completeSignIn]);

  const loading = !config && !configError;

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-950 transition-colors flex flex-col">
      <div className="flex justify-end px-4 pt-4">
        <button
          onClick={toggle}
          title={dark ? "Switch to light mode" : "Switch to dark mode"}
          aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
          className="p-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
        >
          {dark ? <Sun size={16} aria-hidden="true" /> : <Moon size={16} aria-hidden="true" />}
        </button>
      </div>
      <main className="flex-1 flex items-center justify-center px-4 pb-16">
        <section
          aria-labelledby="login-heading"
          className="w-full max-w-sm rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 shadow-sm p-8"
        >
          <div className="flex items-center gap-2.5 mb-6">
            <BrandMark />
            <span className="text-2xl font-bold text-gray-900 dark:text-gray-100">Job Tracker</span>
          </div>
          <h1 id="login-heading" className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            Sign in
          </h1>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
            This tracker is private. Sign in with the Google account that owns it.
          </p>

          {notice && (
            <p
              role="status"
              className="mt-4 text-sm rounded-lg px-3 py-2 bg-blue-50 text-blue-800 border border-blue-200 dark:bg-blue-900/30 dark:text-blue-200 dark:border-blue-800"
            >
              {notice}
            </p>
          )}
          {(signInError || configError) && (
            <p
              role="alert"
              className="mt-4 text-sm rounded-lg px-3 py-2 bg-red-50 text-red-700 border border-red-200 dark:bg-red-900/30 dark:text-red-300 dark:border-red-800"
            >
              {signInError ?? configError}
            </p>
          )}

          <div className="mt-6 min-h-[44px]" aria-busy={loading || busy}>
            {loading && (
              <p role="status" className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
                <Loader2 size={16} className="animate-spin" aria-hidden="true" />
                Loading sign-in…
              </p>
            )}
            {config && !config.configured && (
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Sign-in isn&apos;t configured on the server yet. See docs/authentication.md.
              </p>
            )}
            {googleReady && <div ref={googleButtonRef} data-testid="google-signin-button" />}
            {config?.mode === "local" && config.configured && (
              <>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => completeSignIn(auth.loginLocal)}
                  className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white rounded-lg text-sm font-medium transition-colors"
                >
                  Continue as local developer
                </button>
                <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                  Local development sign-in. Only available on this machine.
                </p>
              </>
            )}
            {busy && (
              <p role="status" className="mt-3 text-sm text-gray-500 dark:text-gray-400">
                Signing in…
              </p>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
