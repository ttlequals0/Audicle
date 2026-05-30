import { useEffect, useRef, useState } from "react";

export type PullPhase = "idle" | "pulling" | "armed" | "refreshing";

// Raw downward drag (px) needed to arm a refresh; the visible travel is damped
// to roughly half that so the gesture feels rubber-banded.
const THRESHOLD = 80;
const MAX_TRAVEL = 64;
const RESIST = 0.5;

export interface PullState {
  phase: PullPhase;
  distance: number;
}

/**
 * Pull-to-refresh: track a downward drag that starts at the top of the
 * document and, past ~80 px, call `onRefresh`. Mobile-only by design; desktop
 * users have the browser refresh button. Wired once at the app shell so a pull
 * on any page refreshes that page's data.
 *
 * Returns live gesture state so a visible indicator can follow the finger and
 * keep spinning until the refresh promise settles. `onRefresh` may be async
 * (e.g. `queryClient.invalidateQueries()`); the spinner stays up until it
 * resolves.
 */
export function usePullToRefresh(onRefresh: () => void | Promise<unknown>): PullState {
  const startY = useRef<number | null>(null);
  const [phase, setPhase] = useState<PullPhase>("idle");
  const [distance, setDistance] = useState(0);
  // Mirror phase into a ref so the listeners read the current value without
  // re-subscribing on every phase change.
  const phaseRef = useRef<PullPhase>("idle");
  phaseRef.current = phase;

  useEffect(() => {
    const onStart = (e: TouchEvent) => {
      if (window.scrollY > 0 || phaseRef.current === "refreshing") {
        startY.current = null;
        return;
      }
      startY.current = e.touches[0].clientY;
    };
    const onMove = (e: TouchEvent) => {
      if (startY.current === null) return;
      const dy = e.touches[0].clientY - startY.current;
      if (dy <= 0) {
        setDistance(0);
        setPhase("idle");
        return;
      }
      setDistance(Math.min(dy * RESIST, MAX_TRAVEL));
      setPhase(dy > THRESHOLD ? "armed" : "pulling");
    };
    const onEnd = () => {
      if (startY.current === null) return;
      const armed = phaseRef.current === "armed";
      startY.current = null;
      if (!armed) {
        setDistance(0);
        setPhase("idle");
        return;
      }
      setPhase("refreshing");
      setDistance(MAX_TRAVEL * 0.7);
      // Swallow before finally so a rejected onRefresh resets the spinner
      // without surfacing an unhandled rejection.
      Promise.resolve(onRefresh())
        .catch(() => {})
        .finally(() => {
          setPhase("idle");
          setDistance(0);
        });
    };
    window.addEventListener("touchstart", onStart, { passive: true });
    window.addEventListener("touchmove", onMove, { passive: true });
    window.addEventListener("touchend", onEnd, { passive: true });
    return () => {
      window.removeEventListener("touchstart", onStart);
      window.removeEventListener("touchmove", onMove);
      window.removeEventListener("touchend", onEnd);
    };
  }, [onRefresh]);

  return { phase, distance };
}
