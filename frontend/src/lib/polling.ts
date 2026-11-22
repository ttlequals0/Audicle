import { useQuery } from "@tanstack/react-query";
import { api, Book, SettingsPayload } from "./api";

const FALLBACK_POLL_MS = 5000;

/** Queue poll interval in ms from the QUEUE_POLL_INTERVAL_SECONDS setting. */
export function pollIntervalMs(settings: SettingsPayload | undefined): number {
  const seconds = Number(
    settings?.values.QUEUE_POLL_INTERVAL_SECONDS ??
      settings?.defaults.QUEUE_POLL_INTERVAL_SECONDS ??
      5
  );
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : FALLBACK_POLL_MS;
}

export function useBookPollMs(): number {
  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<SettingsPayload>("/api/v1/settings"),
    staleTime: 60_000,
  });
  return pollIntervalMs(settingsQ.data);
}

export const isBookBusy = (b: Pick<Book, "counts">) =>
  (b.counts.queued ?? 0) > 0 || (b.counts.processing ?? 0) > 0;
