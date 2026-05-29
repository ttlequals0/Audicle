import { useState, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, Episode } from "../lib/api";

/**
 * Pull-to-refresh: track touchstart at the top of the document, and if
 * the user drags down ~80 px before lifting, invalidate the episodes
 * query so React Query refetches. Mobile-only by design; desktop users
 * have the browser refresh button.
 */
function usePullToRefresh(onRefresh: () => void) {
  const startY = useRef<number | null>(null);
  useEffect(() => {
    const onStart = (e: TouchEvent) => {
      if (window.scrollY > 0) {
        startY.current = null;
        return;
      }
      startY.current = e.touches[0].clientY;
    };
    const onEnd = (e: TouchEvent) => {
      if (startY.current === null) return;
      const dy = e.changedTouches[0].clientY - startY.current;
      startY.current = null;
      if (dy > 80) onRefresh();
    };
    window.addEventListener("touchstart", onStart, { passive: true });
    window.addEventListener("touchend", onEnd, { passive: true });
    return () => {
      window.removeEventListener("touchstart", onStart);
      window.removeEventListener("touchend", onEnd);
    };
  }, [onRefresh]);
}

export default function Feed() {
  const [copied, setCopied] = useState(false);
  const feedUrl = `${window.location.origin}/rss/rss.xml`;
  const qc = useQueryClient();

  const episodesQ = useQuery({
    queryKey: ["episodes"],
    queryFn: () => api<Episode[]>("/api/v1/episodes?per_page=50"),
  });
  usePullToRefresh(() => {
    qc.invalidateQueries({ queryKey: ["episodes"] });
  });

  const deleteM = useMutation({
    mutationFn: (id: string) =>
      api(`/api/v1/episodes/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["episodes"] }),
  });

  const reprocessM = useMutation({
    mutationFn: (url: string) =>
      api("/api/v1/submit", {
        method: "POST",
        body: JSON.stringify({ url, reprocess: true }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["episodes"] }),
  });

  const copy = async () => {
    await navigator.clipboard.writeText(feedUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="space-y-6">
      <section className="card">
        <p className="label">feed url</p>
        <div className="flex gap-2 items-center">
          <code className="flex-1 font-mono text-xs text-fg bg-surface px-3 py-2 rounded border border-line truncate">
            {feedUrl}
          </code>
          <button className="btn-ghost" onClick={copy}>
            {copied ? "copied" : "copy"}
          </button>
        </div>
        <p className="text-mute text-xs mt-2">
          paste into any podcast client (Pocket Casts, Overcast, Apple Podcasts) to subscribe.
        </p>
      </section>

      <section>
        <h2 className="font-mono uppercase text-xs text-dim mb-3">episodes</h2>
        {episodesQ.isLoading && <p className="text-mute text-sm">loading…</p>}
        {episodesQ.data && episodesQ.data.length === 0 && (
          <p className="text-mute text-sm">no episodes yet.</p>
        )}
        <ul className="space-y-2">
          {(episodesQ.data ?? []).map((ep) => (
            <li key={ep.id} className="card">
              <div className="flex items-start gap-3">
                {ep.artwork_path ? (
                  <img
                    src={`/media/${ep.id}.jpg`}
                    alt=""
                    className="h-16 w-16 flex-none rounded border border-line object-cover"
                  />
                ) : (
                  <div className="h-16 w-16 flex-none rounded border border-line bg-surface" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[10px] uppercase text-accent border border-line rounded px-1.5 py-0.5">
                      done
                    </span>
                    <span className="font-mono text-[11px] text-mute truncate">
                      {ep.id} &middot; {formatDuration(ep.duration_secs)}
                    </span>
                  </div>
                  <a
                    href={ep.original_url}
                    target="_blank"
                    rel="noreferrer"
                    className="block text-sm mt-1 line-clamp-2 hover:text-accent"
                  >
                    {ep.title ?? ep.original_url}
                  </a>
                  <p className="font-mono text-[11px] text-mute truncate mt-1">
                    {ep.author ? `${ep.author} · ` : ""}
                    {sourceDomain(ep.original_url)} &middot; {ep.pub_date}
                  </p>
                  <div className="flex flex-wrap gap-1 mt-2">
                    <a
                      className="btn-ghost"
                      href={`/media/${ep.id}.mp3`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      mp3
                    </a>
                    <a
                      className="btn-ghost"
                      href={`/media/${ep.id}.vtt`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      transcript
                    </a>
                    <button
                      className="btn-ghost"
                      disabled={reprocessM.isPending}
                      onClick={() => {
                        if (confirm(`Reprocess "${ep.title ?? ep.original_url}"?`))
                          reprocessM.mutate(ep.original_url);
                      }}
                    >
                      reprocess
                    </button>
                    <button
                      className="btn-ghost btn-danger text-danger border-line"
                      disabled={deleteM.isPending}
                      onClick={() => {
                        if (confirm(`Delete episode ${ep.id}?`)) deleteM.mutate(ep.id);
                      }}
                    >
                      delete
                    </button>
                  </div>
                </div>
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function sourceDomain(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
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
