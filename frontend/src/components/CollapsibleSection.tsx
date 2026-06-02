import { useState, useEffect, type ReactNode } from "react";

interface CollapsibleSectionProps {
  title: string;
  defaultOpen?: boolean;
  children: ReactNode;
  storageKey?: string;
}

export function usePersistentOpen(
  key: string,
  defaultOpen: boolean,
): [boolean, (v: boolean) => void] {
  const [open, setOpen] = useState<boolean>(() => {
    try {
      const stored = localStorage.getItem(key);
      return stored === null ? defaultOpen : stored === "true";
    } catch {
      return defaultOpen;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, String(open));
    } catch {
      /* ignore quota/availability errors */
    }
  }, [key, open]);
  return [open, setOpen];
}

export default function CollapsibleSection({
  title,
  defaultOpen = false,
  children,
  storageKey,
}: CollapsibleSectionProps) {
  const key = storageKey ?? `settings-section-${title.toLowerCase().replace(/\s+/g, "-")}`;
  const [open, setOpen] = usePersistentOpen(key, defaultOpen);

  return (
    <section className="section">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-5 py-4 text-left"
        aria-expanded={open}
      >
        <span className="section-title">{title}</span>
        <span
          className={`text-mute text-lg leading-none transition-transform ${
            open ? "rotate-90" : ""
          }`}
        >
          &rsaquo;
        </span>
      </button>
      {open && <div className="px-5 pb-5 space-y-3">{children}</div>}
    </section>
  );
}
