import { createContext, useContext } from "react";

/**
 * Section keys matching the active Settings search, or null when no search is
 * running. Provided only around the Settings page; the default null leaves
 * CollapsibleSection behaving exactly as it does everywhere else.
 *
 * Matching is done on data the Settings page already holds (group titles and
 * their setting keys) rather than by scanning rendered text. CollapsibleSection
 * mounts children on first expansion and retains them to preserve drafts.
 * A DOM scan would miss unvisited sections; mounting everything up front would
 * trigger queries for widgets the user has not opened.
 */
export const SettingsSearchContext = createContext<Set<string> | null>(null);

export function useSettingsSearch(): Set<string> | null {
  return useContext(SettingsSearchContext);
}
