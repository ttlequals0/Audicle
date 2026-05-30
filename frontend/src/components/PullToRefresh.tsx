import { usePullToRefresh } from "../lib/usePullToRefresh";
import type { PullPhase } from "../lib/usePullToRefresh";

const LABELS: Record<PullPhase, string> = {
  idle: "",
  pulling: "pull to refresh",
  armed: "release to refresh",
  refreshing: "refreshing",
};

/**
 * Self-contained pull-to-refresh: owns the gesture hook and renders the
 * visible pill, so the per-frame pull state re-renders only this component and
 * not the app shell. A pill drops from behind the header following the finger,
 * then spins until `onRefresh` settles. Purely decorative, hidden from a11y.
 */
export default function PullToRefresh({
  onRefresh,
}: {
  onRefresh: () => void | Promise<unknown>;
}) {
  const { phase, distance } = usePullToRefresh(onRefresh);
  if (phase === "idle") return null;
  return (
    <div className="ptr-host" aria-hidden="true">
      <div
        className="ptr-pill"
        style={{
          transform: `translateY(${distance}px)`,
          opacity: Math.min(distance / 28, 1),
        }}
      >
        <span className={`ptr-spinner${phase === "refreshing" ? " ptr-spinning" : ""}`} />
        <span className={`ptr-label${phase === "armed" ? " ptr-label-armed" : ""}`}>
          {LABELS[phase]}
        </span>
      </div>
    </div>
  );
}
