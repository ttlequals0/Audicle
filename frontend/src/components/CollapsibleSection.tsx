import { useState, useEffect, type ReactNode } from "react";
import { useSettingsSearch } from "./SettingsSearchContext";

interface CollapsibleSectionProps {
  title: string;
  defaultOpen?: boolean;
  children: ReactNode;
  storageKey?: string;
  /**
   * Identity for the Settings search. Defaults to the title, which is what
   * every caller outside the Settings page relies on. A section whose key is
   * absent from the active match set hides itself; a section in the set opens
   * itself, so a match is never buried behind a collapsed header.
   */
  searchKey?: string;
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
  searchKey,
}: CollapsibleSectionProps) {
  const key = storageKey ?? `settings-section-${title.toLowerCase().replace(/\s+/g, "-")}`;
  const [open, setOpen] = usePersistentOpen(key, defaultOpen);
  const matches = useSettingsSearch();
  const resolvedKey = searchKey ?? title;

  // No search running: behave exactly as before, honouring the stored toggle.
  const searching = matches !== null;
  const isMatch = !searching || matches.has(resolvedKey);
  if (searching && !isMatch) return null;
  // A search result is shown expanded. The stored preference is untouched, so
  // clearing the box restores whatever was open beforehand.
  const expanded = searching ? true : open;

  return (
    <section className="section">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-5 py-4 text-left"
        aria-expanded={expanded}
        // While searching the section is already open and its state is driven
        // by the query, so the toggle would only be able to fight it.
        disabled={searching}
      >
        <span className="section-title">{title}</span>
        <span
          className={`text-mute text-lg leading-none transition-transform ${
            expanded ? "rotate-90" : ""
          }`}
        >
          &rsaquo;
        </span>
      </button>
      {expanded && <div className="px-5 pb-5 space-y-3">{children}</div>}
    </section>
  );
}
