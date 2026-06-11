/** Shared display formatters used across routes. */

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const mb = bytes / (1024 * 1024);
  if (mb < 1) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${mb.toFixed(1)} MB`;
}

/** Lower-cased file extension without the dot ("Report.PDF" -> "pdf"). */
export function fileExt(filename: string): string {
  return filename.split(".").pop()?.toLowerCase() ?? "";
}
