import { useState, useEffect, useId, type ReactNode } from "react";
import { useSettingsSearch } from "./SettingsSearchContext";

interface CollapsibleSectionProps {
  title: string;
  description?: string;
  active?: boolean;
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
  description,
  active = true,
  defaultOpen = false,
  children,
  storageKey,
  searchKey,
}: CollapsibleSectionProps) {
  const key = storageKey ?? `settings-section-${title.toLowerCase().replace(/\s+/g, "-")}`;
  const [open, setOpen] = usePersistentOpen(key, defaultOpen);
  const matches = useSettingsSearch();
  const resolvedKey = searchKey ?? title;

  const id = useId();
  const searching = matches !== null;
  const isMatch = !searching || matches.has(resolvedKey);
  const expanded = searching && isMatch ? true : open;
  // Mount children on first open and keep them mounted while hidden so widget
  // queries and drafts survive category switches and search.
  const shouldMount = active && isMatch && expanded;
  const [visited, setVisited] = useState(shouldMount);
  if (shouldMount && !visited) setVisited(true);

  return (
    <section className="section" hidden={!isMatch}>
      <h3>
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="w-full flex items-center justify-between px-5 py-4 text-left"
          aria-expanded={expanded}
          aria-controls={`${id}-body`}
          aria-labelledby={`${id}-title`}
          aria-describedby={description ? `${id}-description` : undefined}
          // While searching the section is already open and its state is driven
          // by the query, so the toggle would only be able to fight it.
          disabled={searching}
        >
          <span className="min-w-0">
            <span id={`${id}-title`} className="section-title">{title}</span>
            {description && <span id={`${id}-description`} className="section-description">{description}</span>}
          </span>
          <span
            aria-hidden="true"
            className={`text-mute text-lg leading-none transition-transform ${
              expanded ? "rotate-90" : ""
            }`}
          >
            &rsaquo;
          </span>
        </button>
      </h3>
      <div id={`${id}-body`} hidden={!expanded} className="px-5 pb-5 space-y-3">
        {(visited || shouldMount) && children}
      </div>
    </section>
  );
}
