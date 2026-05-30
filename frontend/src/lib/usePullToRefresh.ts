import { useEffect, useRef } from "react";

/**
 * Pull-to-refresh: track touchstart at the top of the document, and if the
 * user drags down ~80 px before lifting, call `onRefresh`. Mobile-only by
 * design; desktop users have the browser refresh button. Wired once at the
 * app shell so a pull on any page refreshes that page's data.
 */
export function usePullToRefresh(onRefresh: () => void) {
  const startY = useRef<number | null>(null);
  useEffect(() => {
    const onStart = (e: TouchEvent) => {
      if (window.scrollY > 0) {
        startY.current = null;
        return;
      }
      startY.current = e.touches[0].clientY;
    };
    const onEnd = (e: TouchEvent) => {
      if (startY.current === null) return;
      const dy = e.changedTouches[0].clientY - startY.current;
      startY.current = null;
      if (dy > 80) onRefresh();
    };
    window.addEventListener("touchstart", onStart, { passive: true });
    window.addEventListener("touchend", onEnd, { passive: true });
    return () => {
      window.removeEventListener("touchstart", onStart);
      window.removeEventListener("touchend", onEnd);
    };
  }, [onRefresh]);
}
