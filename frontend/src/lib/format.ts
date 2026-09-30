/** Shared display formatters used across routes. */

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const mb = bytes / (1024 * 1024);
  if (mb < 1) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${mb.toFixed(1)} MB`;
}

export function formatEpisodeDuration(secs: number | null): string {
  if (secs === null || !Number.isFinite(secs)) return "-";
  const rounded = Math.max(0, Math.floor(secs + 0.5));
  const h = Math.floor(rounded / 3600);
  const m = Math.floor((rounded % 3600) / 60);
  const s = rounded % 60;
  if (h > 0) return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function formatDateTime(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Lower-cased file extension without the dot ("Report.PDF" -> "pdf"). */
export function fileExt(filename: string): string {
  return filename.split(".").pop()?.toLowerCase() ?? "";
}
