import { useEffect, useId, useSyncExternalStore } from "react";

const drafts = new Set<string>();
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};
const setDraft = (id: string, dirty: boolean) => {
  if (dirty) drafts.add(id);
  else drafts.delete(id);
  listeners.forEach((listener) => listener());
};
export const useAnyUnsavedChanges = () => useSyncExternalStore(subscribe, () => drafts.size > 0);

export function useUnsavedChanges(dirty: boolean) {
  const id = useId();
  useEffect(() => {
    if (!dirty) return;
    setDraft(id, true);
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => {
      setDraft(id, false);
      window.removeEventListener("beforeunload", beforeUnload);
    };
  }, [dirty, id]);
}
