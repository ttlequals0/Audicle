import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface MenuAction {
  label: string;
  hint: string;
  run: () => void;
}

/**
 * Row-action menu used by Recents and the Feed.
 *
 * Rendered through a portal on purpose: the rows are `.card`, which sets
 * `overflow: hidden`, so an absolutely-positioned panel inside one gets clipped
 * by its own row. Fixed coordinates off the trigger's rect escape that and any
 * stacking context, and let the panel flip above the button near the viewport
 * bottom.
 */
export default function ActionMenu({
  actions,
  pending,
  label = "Redo",
}: {
  actions: MenuAction[];
  pending?: boolean;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);

  const place = () => {
    const button = trigger.current;
    const box = panel.current;
    if (!button) return;
    const rect = button.getBoundingClientRect();
    const height = box?.offsetHeight ?? 0;
    const width = box?.offsetWidth ?? 0;
    const below = rect.bottom + 4;
    // Flip above the trigger when the panel would run off the bottom.
    const top = height && below + height > window.innerHeight - 8 ? rect.top - height - 4 : below;
    const left = width ? Math.min(rect.left, window.innerWidth - width - 12) : rect.left;
    setPos({ left: Math.max(8, left), top: Math.max(8, top) });
  };

  useLayoutEffect(() => {
    if (open) place();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDocDown = (e: MouseEvent) => {
      if (!panel.current?.contains(e.target as Node) && !trigger.current?.contains(e.target as Node))
        setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onMove = () => place();
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onMove, true);
    window.addEventListener("resize", onMove);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onMove, true);
      window.removeEventListener("resize", onMove);
    };
  }, [open]);

  if (actions.length === 0) return null;

  return (
    <>
      <button
        ref={trigger}
        className="btn-ghost inline-flex items-center gap-1.5"
        disabled={pending}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        &#8635; {label}
        <span
          aria-hidden="true"
          className={`text-mute text-lg leading-none transition-transform motion-reduce:transition-none ${
            open ? "rotate-90" : ""
          }`}
        >
          ›
        </span>
      </button>
      {open &&
        createPortal(
          <div
            ref={panel}
            role="menu"
            className="menu-panel"
            style={{
              position: "fixed",
              left: pos?.left ?? -9999,
              top: pos?.top ?? -9999,
              zIndex: 60,
            }}
          >
            {actions.map((a) => (
              <button
                key={a.label}
                role="menuitem"
                className="menu-item"
                onClick={() => {
                  setOpen(false);
                  a.run();
                }}
              >
                <span className="text-sm text-fg">{a.label}</span>
                <span className="mono-xs text-mute col-start-2">// {a.hint}</span>
              </button>
            ))}
          </div>,
          document.body
        )}
    </>
  );
}
