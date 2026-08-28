import { useEffect, useRef } from "react";

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Minimal dialog accessibility: focus trap, initial focus, Escape-to-close,
 * and focus restoration to whatever had focus before the dialog opened.
 * Attach the returned ref to the dialog's outer element (the one carrying
 * role="dialog"/aria-modal).
 */
export function useModalA11y(onClose) {
  const containerRef = useRef(null);

  useEffect(() => {
    const previouslyFocused = document.activeElement;
    const container = containerRef.current;

    const getFocusable = () =>
      container ? [...container.querySelectorAll(FOCUSABLE_SELECTOR)] : [];

    const first = getFocusable()[0];
    (first ?? container)?.focus();

    const handleKeyDown = (e) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const focusable = getFocusable();
      if (focusable.length === 0) return;
      const firstEl = focusable[0];
      const lastEl = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      if (previouslyFocused instanceof HTMLElement) previouslyFocused.focus();
    };
  }, [onClose]);

  return containerRef;
}
