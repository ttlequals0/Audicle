import { useState, FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, JobRow, JobStatus } from "../lib/api";

interface SubmitResponse {
  job_id: string;
  episode_id: string;
}

export default function Home() {
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  // null = follow the smart default (open when the queue is empty so finished
  // and failed jobs stay visible); a bool means the user toggled it explicitly.
  const [recentManual, setRecentManual] = useState<boolean | null>(null);
  const qc = useQueryClient();

  const jobsQ = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<JobRow[]>("/api/v1/jobs?per_page=50"),
    refetchInterval: 5000,
  });

  const submitM = useMutation({
    mutationFn: (input: string) =>
      api<SubmitResponse>("/api/v1/submit", {
        method: "POST",
        body: JSON.stringify({ url: input }),
      }),
    onSuccess: () => {
      setUrl("");
      setError(null);
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e) => {
      if (e instanceof ApiError) {
        setError(
          typeof e.detail === "object" && e.detail ? JSON.stringify(e.detail) : String(e.detail)
        );
      } else {
        setError((e as Error).message);
      }
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (url) submitM.mutate(url);
  };

  const jobs = jobsQ.data ?? [];
  // FIFO queue order: oldest first, so the job actually processing leads.
  const active = jobs
    .filter((j) => j.status === "queued" || j.status === "processing")
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const history = jobs.filter((j) => j.status !== "queued" && j.status !== "processing");
  // Default open when nothing is active, so a finished or failed job (and its
  // error) is visible without a click; collapsed while a queue is running.
  const recentOpen = recentManual ?? active.length === 0;

  return (
    <div>
      <div className="pt-10 pb-2">
        <div className="mono-xs text-accent mb-3">// SUBMIT_ARTICLE</div>
        <h1 className="text-4xl font-black tracking-tight mb-2 leading-tight">
          Drop a link.
          <br />
          <span className="text-accent">Get a podcast.</span>
        </h1>
        <p className="text-dim text-sm mb-8 leading-relaxed">
          Audicle reads articles aloud. Paste a URL and it joins your feed.
        </p>
        <form onSubmit={submit} className="space-y-3">
          <input
            type="url"
            className="hero-input"
            placeholder="https://"
            autoComplete="off"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <button type="submit" className="btn-primary w-full" disabled={!url || submitM.isPending}>
            {submitM.isPending ? "Submitting..." : "Submit"}
          </button>
        </form>
        {error && <p className="text-danger text-xs font-mono mt-2 break-words">{error}</p>}

        {!jobs.length && jobsQ.data && (
          <p className="mono-xs text-mute mt-8">// no submissions yet -- paste a URL above</p>
        )}
      </div>

      {active.length > 0 && (
        <section className="mt-8">
          <div className="mono-xs text-accent mb-3">// QUEUE ({active.length})</div>
          <ul className="space-y-2">
            {active.map((j, i) => (
              <li
                key={j.id}
                className={`card p-4 flex items-start gap-3${j.status === "processing" ? " queue-row-active" : ""}`}
              >
                <span className="queue-index">{String(i + 1).padStart(2, "0")}</span>
                <div className="min-w-0 flex-1">
                  <p className="mono text-dim truncate">{j.url}</p>
                  <p className="mono-xs text-mute mt-1 truncate">
                    stage: {j.stage ?? "-"}
                    {progressSuffix(j)}
                  </p>
                </div>
                <span className={`tag ${statusTag(j.status)}`}>{j.status}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {history.length > 0 && (
        <section className="mt-8">
          <button
            className="mono-xs text-mute mb-3 flex items-center gap-1.5 hover:text-fg"
            onClick={() => setRecentManual(!recentOpen)}
            aria-expanded={recentOpen}
          >
            <span className={`transition-transform ${recentOpen ? "rotate-90" : ""}`}>
              &rsaquo;
            </span>
            // RECENT ({history.length})
          </button>
          {recentOpen && (
            <ul className="space-y-2">
              {history.map((j) => {
                const duration = formatDuration(j.started_at, j.updated_at);
                return (
                <li key={j.id} className="card p-4 flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="mono-xs text-mute truncate">{j.url}</p>
                    <p className="text-sm mt-1 truncate text-dim">
                      {j.episode_id} &middot; {j.stage ?? "-"}
                      {progressSuffix(j)}
                      {j.error && <span className="text-danger"> &middot; {j.error}</span>}
                    </p>
                  </div>
                  <div className="flex flex-col items-end gap-1 flex-shrink-0">
                    <span className={`tag ${statusTag(j.status)}`}>{j.status}</span>
                    <time className="mono-xs text-mute" dateTime={j.updated_at}>
                      {formatJobTime(j.updated_at)}
                    </time>
                    {duration && (
                      <span className="mono-xs text-mute">took {duration}</span>
                    )}
                  </div>
                </li>
                );
              })}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}

function formatJobTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

// Processing time = claim (started_at) to last update. Null/invalid/negative
// (queued jobs, pre-0.11.0 rows) renders nothing.
function formatDuration(start: string | null, end: string): string {
  if (!start) return "";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (isNaN(ms) || ms < 0) return "";
  const secs = Math.round(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}m ${s}s`;
}

function progressSuffix(j: JobRow): string {
  if (j.progress_current != null && j.progress_total != null) {
    return ` [${j.progress_current}/${j.progress_total}]`;
  }
  return "";
}

function statusTag(status: JobStatus): string {
  switch (status) {
    case "done":
      return "tag-done";
    case "failed":
      return "tag-failed";
    case "processing":
      return "tag-processing";
    default:
      return "tag-queued";
  }
}
