import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, Episode, SettingsPayload } from "../lib/api";
import AudioPlayer from "../components/AudioPlayer";

export default function Feed() {
  const [copied, setCopied] = useState(false);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const qc = useQueryClient();
  // The subscribe URL is slug-derived from FEED_TITLE and built server-side
  // (against the configured BASE_URL, the public feed host), so the client
  // never reimplements slugify and always shows the real feed URL.
  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<SettingsPayload>("/api/v1/settings"),
  });
  const feedUrl = settingsQ.data?.feed_url ?? "";

  const onActionError = (verb: string) => (err: unknown) => {
    const status = (err as { status?: number })?.status;
    setActionMsg(
      status === 409
        ? "already queued or processing for that URL"
        : `${verb} failed${status ? ` (HTTP ${status})` : ""}`,
    );
    setTimeout(() => setActionMsg(null), 4000);
  };

  const episodesQ = useQuery({
    queryKey: ["episodes"],
    queryFn: () => api<Episode[]>("/api/v1/episodes?per_page=50"),
  });

  const deleteM = useMutation({
    mutationFn: (id: string) =>
      api(`/api/v1/episodes/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["episodes"] }),
    onError: onActionError("delete"),
  });

  const reprocessM = useMutation({
    mutationFn: (url: string) =>
      api("/api/v1/submit", {
        method: "POST",
        body: JSON.stringify({ url, reprocess: true }),
      }),
    onSuccess: () => {
      setActionMsg("reprocess queued");
      setTimeout(() => setActionMsg(null), 4000);
      qc.invalidateQueries({ queryKey: ["episodes"] });
    },
    onError: onActionError("reprocess"),
  });

  const copy = async () => {
    await navigator.clipboard.writeText(feedUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const episodes = episodesQ.data ?? [];

  return (
    <div>
      <div className="flex items-baseline justify-between mb-3">
        <h1 className="text-2xl font-black tracking-tight">Feed</h1>
        <div className="mono-xs text-mute">
          {episodes.length} episode{episodes.length === 1 ? "" : "s"}
        </div>
      </div>

      <button
        className="btn-ghost w-full mb-2 flex items-center justify-center gap-2"
        onClick={copy}
        disabled={!feedUrl}
      >
        {copied ? "✓ Copied" : "⧉ Copy feed URL"}
      </button>
      <p className="mono-xs text-mute truncate mb-5" title={feedUrl}>
        {feedUrl || (settingsQ.isError ? "feed URL unavailable" : "loading...")}
      </p>

      {actionMsg && <p className="mono-xs text-accent mb-3">{actionMsg}</p>}
      {episodesQ.isLoading && <p className="text-mute text-sm">loading...</p>}
      {episodes.length === 0 && !episodesQ.isLoading && (
        <p className="text-mute text-sm">no episodes yet.</p>
      )}

      <div className="space-y-3">
        {episodes.map((ep) => (
          <article key={ep.id} className="card p-4">
            <div className="flex gap-3">
              <EpisodeArtwork ep={ep} />
              <div className="flex-1 min-w-0">
                <div className="flex items-start gap-2 mb-1">
                  <a
                    href={ep.original_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-sm font-bold leading-snug hover:text-accent line-clamp-2 flex-1"
                  >
                    {ep.title ?? ep.original_url}
                  </a>
                  <span className="tag tag-done">done</span>
                </div>
                <div className="mono-xs text-mute">
                  {ep.author ? `${ep.author} · ` : ""}
                  {sourceDomain(ep.original_url)}
                </div>
                <div className="mono-xs text-mute mt-0.5">
                  {ep.pub_date} &middot; {formatDuration(ep.duration_secs)}
                  {ep.audio_size_bytes ? ` · ${formatBytes(ep.audio_size_bytes)}` : ""}
                </div>
              </div>
            </div>
            <div className="mt-3 pt-3 border-t border-line">
              <AudioPlayer src={`/media/${ep.id}.mp3`} />
            </div>
            <div className="flex flex-wrap gap-2 mt-3">
              <a className="btn-ghost" href={`/media/${ep.id}.vtt`} target="_blank" rel="noreferrer">
                Transcript
              </a>
              {ep.has_cleaned_text && (
                <a className="btn-ghost" href={`/media/${ep.id}.txt`} target="_blank" rel="noreferrer">
                  Cleaned text
                </a>
              )}
              <button
                className="btn-ghost"
                disabled={reprocessM.isPending}
                onClick={() => {
                  if (confirm(`Reprocess "${ep.title ?? ep.original_url}"?`))
                    reprocessM.mutate(ep.original_url);
                }}
              >
                &#8635; Reprocess
              </button>
              <button
                className="btn-ghost btn-danger ml-auto"
                disabled={deleteM.isPending}
                onClick={() => {
                  if (confirm(`Delete episode ${ep.id}?`)) deleteM.mutate(ep.id);
                }}
              >
                Delete
              </button>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

/**
 * Per-episode artwork: the episode's own JPG when present, else the seeded
 * default podcast art at /media/default.jpg. Falls back to a gradient tile only
 * if that image fails to load (e.g. default not seeded yet).
 */
function EpisodeArtwork({ ep }: { ep: Episode }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div
        className="artwork"
        style={{ background: "linear-gradient(135deg, #1ce783, #16d076)" }}
      />
    );
  }
  const src = ep.artwork_path ? `/media/${ep.id}.jpg` : "/media/default.jpg";
  return <img src={src} alt="" className="artwork" onError={() => setFailed(true)} />;
}

function sourceDomain(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const mb = bytes / (1024 * 1024);
  if (mb < 1) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${mb.toFixed(1)} MB`;
}

function formatDuration(secs: number | null): string {
  if (!secs) return "-";
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0)
    return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  return `${m}:${s.toString().padStart(2, "0")}`;
}
