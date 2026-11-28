import { ReactNode } from "react";

export default function SectionToggle({
  expanded,
  onToggle,
  children,
}: {
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className="text-xs text-mute flex items-center gap-1.5 hover:text-fg"
      onClick={onToggle}
      aria-expanded={expanded}
    >
      <span className={`transition-transform ${expanded ? "rotate-90" : ""}`}>
        &rsaquo;
      </span>
      {children}
    </button>
  );
}
