import { KeyboardEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface MenuAction {
  label: string;
  hint: string;
  /** Click handler. Mutually exclusive with `href`. */
  run?: () => void;
  /** Renders the row as a real link, so middle-click and copy-address work. */
  href?: string;
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
  context,
}: {
  actions: MenuAction[];
  pending?: boolean;
  label?: string;
  context?: string;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const itemIndex = useRef(0);
  const menuLabel = context ? `${label} actions for ${context}` : `${label} actions`;

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
    if (!open) return;
    place();
    const items = panel.current?.querySelectorAll<HTMLElement>("[role='menuitem']");
    if (items?.length) items[Math.min(itemIndex.current, items.length - 1)].focus();
  }, [open]);

  const close = (restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus();
  };

  const onMenuKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(panel.current?.querySelectorAll<HTMLElement>("[role='menuitem']") ?? []);
    if (!items.length) return;
    const current = items.indexOf(document.activeElement as HTMLElement);
    let next = current;
    if (e.key === "ArrowDown") next = (current + 1 + items.length) % items.length;
    else if (e.key === "ArrowUp") next = (current - 1 + items.length) % items.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    else if (e.key === "Escape") {
      e.preventDefault();
      close(true);
      return;
    } else if (e.key === "Tab") {
      const candidates = Array.from(
        document.querySelectorAll<HTMLElement>(
          "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
        )
      ).filter((el) => {
        if (panel.current?.contains(el)) return false;
        for (let node: HTMLElement | null = el; node; node = node.parentElement) {
          const style = window.getComputedStyle(node);
          if (node.hidden || style.display === "none" || style.visibility === "hidden") return false;
        }
        return true;
      });
      const triggerIndex = candidates.indexOf(trigger.current!);
      const target = candidates[triggerIndex + (e.shiftKey ? -1 : 1)];
      close();
      if (target) {
        e.preventDefault();
        target.focus();
      }
      return;
    } else return;
    e.preventDefault();
    itemIndex.current = next;
    items[next].focus();
  };

  useEffect(() => {
    if (!open) return;
    const onDocDown = (e: MouseEvent) => {
      if (!panel.current?.contains(e.target as Node) && !trigger.current?.contains(e.target as Node))
        setOpen(false);
    };
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape" && panel.current?.contains(document.activeElement)) {
        e.preventDefault();
        close(true);
      }
    };
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
        aria-label={context ? `${label} actions for ${context}` : label}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            itemIndex.current = e.key === "ArrowUp" ? actions.length - 1 : 0;
            setOpen(true);
          }
        }}
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
            aria-label={menuLabel}
            className="menu-panel"
            style={{
              position: "fixed",
              left: pos?.left ?? -9999,
              top: pos?.top ?? -9999,
              zIndex: 60,
            }}
            onKeyDown={onMenuKeyDown}
          >
            {actions.map((a) => {
              const body = (
                <>
                  <span className="text-sm text-fg">{a.label}</span>
                  <span className="mono-xs text-mute col-start-2">// {a.hint}</span>
                </>
              );
              return a.href ? (
                <a
                  key={a.label}
                  role="menuitem"
                  tabIndex={-1}
                  className="menu-item"
                  href={a.href}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => close(true)}
                >
                  {body}
                </a>
              ) : (
                <button
                  key={a.label}
                  role="menuitem"
                  tabIndex={-1}
                  className="menu-item"
                  onClick={() => {
                    close(true);
                    a.run?.();
                  }}
                >
                  {body}
                </button>
              );
            })}
          </div>,
          document.body
        )}
    </>
  );
}
