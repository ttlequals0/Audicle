import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, JobRow } from "../lib/api";

interface SubmitResponse {
  job_id: string;
  episode_id: string;
}

export default function Home() {
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();

  const jobsQ = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<JobRow[]>("/api/v1/jobs?per_page=20"),
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
          typeof e.detail === "object" && e.detail
            ? JSON.stringify(e.detail)
            : String(e.detail)
        );
      } else {
        setError((e as Error).message);
      }
    },
  });

  return (
    <div className="space-y-6">
      <section className="card">
        <label className="label" htmlFor="url-input">
          submit an article
        </label>
        <input
          id="url-input"
          className="field"
          placeholder="https://example.com/article"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && url) submitM.mutate(url);
          }}
        />
        <div className="flex gap-2 items-center mt-3">
          <button
            className="btn-primary"
            disabled={!url || submitM.isPending}
            onClick={() => submitM.mutate(url)}
          >
            {submitM.isPending ? "enqueuing..." : "enqueue"}
          </button>
          {error && (
            <span className="text-danger text-xs font-mono">{error}</span>
          )}
        </div>
      </section>

      <section>
        <h2 className="font-mono uppercase text-xs text-dim mb-3">
          recent jobs
        </h2>
        {jobsQ.isLoading && (
          <p className="text-mute text-sm">loading...</p>
        )}
        {jobsQ.data && jobsQ.data.length === 0 && (
          <p className="text-mute text-sm">no jobs yet - submit a URL above.</p>
        )}
        <ul className="space-y-2">
          {(jobsQ.data ?? []).map((j) => (
            <li key={j.id} className="card flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="font-mono text-[11px] text-mute truncate">{j.url}</p>
                <p className="text-sm mt-1 truncate">
                  {j.episode_id} &middot; {j.stage ?? "-"}
                  {j.error && <span className="text-danger"> &middot; {j.error}</span>}
                </p>
              </div>
              <span className={`tag ${statusColor(j.status)}`}>{j.status}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function statusColor(status: string): string {
  switch (status) {
    case "done":
      return "text-accent";
    case "failed":
      return "text-danger";
    case "processing":
      return "text-fg";
    default:
      return "text-mute";
  }
}
