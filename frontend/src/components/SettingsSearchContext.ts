import { createContext, useContext } from "react";

/**
 * Section keys matching the active Settings search, or null when no search is
 * running. Provided only around the Settings page; the default null leaves
 * CollapsibleSection behaving exactly as it does everywhere else.
 *
 * Matching is done on data the Settings page already holds (group titles and
 * their setting keys) rather than by scanning rendered text. CollapsibleSection
 * unmounts its children when closed, so a DOM scan would miss every collapsed
 * section, and force-mounting them all would fire the queries behind the voices,
 * corrections, and site-override widgets on every page load.
 */
export const SettingsSearchContext = createContext<Set<string> | null>(null);

export function useSettingsSearch(): Set<string> | null {
  return useContext(SettingsSearchContext);
}
