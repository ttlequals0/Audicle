import { useEffect, useRef, useState, DragEvent, FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, JobRow, JobStatus, postForm, SettingsPayload, VoiceSlot } from "../lib/api";
import { fileExt, formatBytes } from "../lib/format";
import { usePersistentOpen } from "../components/CollapsibleSection";

interface SubmitResponse {
  job_id: string;
  episode_id: string;
}

type Mode = "url" | "file";

// Kept in sync with file_extraction.ALLOWED_EXTENSIONS on the backend.
const ACCEPT = ".pdf,.docx,.md,.txt,.html,.htm";
const ALLOWED_EXTS = ["pdf", "docx", "md", "txt", "html", "htm"];
// Fallback only until the live UPLOAD_MAX_MB setting loads.
const DEFAULT_MAX_UPLOAD_MB = 50;

export default function Home() {
  const [mode, setMode] = useState<Mode>("url");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Reference-voice choice for this submission: "random" (default), "last", or a slot id.
  const [voice, setVoice] = useState("random");
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Recents and the per-submission voice picker are collapsed by default; the
  // toggles are remembered across reloads.
  const [recentOpen, setRecentOpen] = usePersistentOpen("home.recents.open", false);
  const [voiceOpen, setVoiceOpen] = usePersistentOpen("home.voice.open", false);
  const qc = useQueryClient();

  const jobsQ = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<JobRow[]>("/api/v1/jobs?per_page=50"),
    refetchInterval: 5000,
  });

  // Effective upload cap (MB) from the operator-tunable UPLOAD_MAX_MB setting, so
  // the client-side guard and the dropzone copy track what the server enforces.
  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<SettingsPayload>("/api/v1/settings"),
    staleTime: 60_000,
  });
  // Filled reference-voice slots drive the optional per-submission voice picker.
  const slotsQ = useQuery({
    queryKey: ["voice-slots"],
    queryFn: () => api<VoiceSlot[]>("/api/v1/reference/slots"),
    staleTime: 60_000,
  });
  const filledSlots = (slotsQ.data ?? []).filter((s) => s.filled);
  // Slots-only: the backend rejects submit/upload with 400 when no voice is loaded.
  // Gate on isSuccess so the button isn't disabled while the slots query is in flight.
  const noVoiceLoaded = slotsQ.isSuccess && filledSlots.length === 0;
  const voiceChoiceLabel =
    voice === "random"
      ? "random"
      : voice === "last"
        ? "last used"
        : (filledSlots.find((s) => String(s.slot) === voice)?.label ?? `slot ${voice}`);
  // If the picked slot is later cleared, fall back to random so the label, the
  // select, and the submitted value can't disagree. Depend on the stable query
  // data (not the per-render filledSlots array) to avoid re-running every render.
  useEffect(() => {
    if (voice === "random" || voice === "last") return;
    const stillFilled = (slotsQ.data ?? []).some((s) => s.filled && String(s.slot) === voice);
    if (!stillFilled) setVoice("random");
  }, [slotsQ.data, voice]);

  const configuredMb = Number(
    settingsQ.data?.values.UPLOAD_MAX_MB ?? settingsQ.data?.defaults.UPLOAD_MAX_MB
  );
  // isFinite (not ||) so a legitimate value isn't masked by the fallback.
  const maxUploadMb = Number.isFinite(configuredMb) ? configuredMb : DEFAULT_MAX_UPLOAD_MB;
  const maxUploadBytes = maxUploadMb * 1024 * 1024;

  const onError = (e: unknown) => {
    if (e instanceof ApiError) {
      setError(
        typeof e.detail === "object" && e.detail ? JSON.stringify(e.detail) : String(e.detail)
      );
    } else {
      setError((e as Error).message);
    }
  };

  const submitM = useMutation({
    mutationFn: (input: string) =>
      api<SubmitResponse>("/api/v1/submit", {
        method: "POST",
        body: JSON.stringify({ url: input, voice }),
      }),
    onSuccess: () => {
      setUrl("");
      setError(null);
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError,
  });

  const uploadM = useMutation({
    mutationFn: (f: File) => {
      const fd = new FormData();
      fd.append("file", f);
      fd.append("voice", voice);
      return postForm<SubmitResponse>("/api/v1/upload", fd);
    },
    onSuccess: () => {
      clearFile();
      setError(null);
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError,
  });

  // Reprocess a terminal job from Recents (uniform for url + upload; the backend
  // branches on the job url and reads the stored upload file when needed).
  const [recentMsg, setRecentMsg] = useState<string | null>(null);
  const requeueM = useMutation({
    mutationFn: (jobId: string) => api(`/api/v1/jobs/${jobId}/requeue`, { method: "POST" }),
    onSuccess: () => {
      setRecentMsg(null);
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e) => {
      const status = e instanceof ApiError ? e.status : undefined;
      setRecentMsg(
        status === 409
          ? "can't reprocess: already queued, or the uploaded file is gone -- re-upload it"
          : `reprocess failed${status ? ` (HTTP ${status})` : ""}`
      );
      setTimeout(() => setRecentMsg(null), 5000);
    },
  });

  // Cancel a queued or processing job from the queue. A processing job stops at the
  // worker's next checkpoint; a queued job is never started.
  const [cancelMsg, setCancelMsg] = useState<string | null>(null);
  const cancelM = useMutation({
    mutationFn: (jobId: string) => api(`/api/v1/jobs/${jobId}/cancel`, { method: "POST" }),
    onSuccess: () => {
      setCancelMsg(null);
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e) => {
      const status = e instanceof ApiError ? e.status : undefined;
      setCancelMsg(
        status === 409
          ? "can't cancel: the job already finished"
          : `cancel failed${status ? ` (HTTP ${status})` : ""}`
      );
      setTimeout(() => setCancelMsg(null), 5000);
    },
  });

  const clearFile = () => {
    setFile(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const pickFile = (f: File | null) => {
    setError(null);
    if (!f) return;
    const ext = fileExt(f.name);
    if (!ALLOWED_EXTS.includes(ext)) {
      setError(`unsupported file type .${ext || "(none)"}; allowed: ${ALLOWED_EXTS.join(", ")}`);
      return;
    }
    if (f.size > maxUploadBytes) {
      setError(`file is too large (max ${maxUploadMb} MB)`);
      return;
    }
    setFile(f);
  };

  const onDrop = (e: DragEvent<HTMLLabelElement>) => {
    e.preventDefault();
    setDragging(false);
    pickFile(e.dataTransfer.files?.[0] ?? null);
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (mode === "url") {
      if (url) submitM.mutate(url);
    } else if (file) {
      uploadM.mutate(file);
    }
  };

  const pending = submitM.isPending || uploadM.isPending;

  const jobs = jobsQ.data ?? [];
  // FIFO queue order: oldest first, so the job actually processing leads.
  const active = jobs
    .filter((j) => j.status === "queued" || j.status === "processing")
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const history = jobs.filter((j) => j.status !== "queued" && j.status !== "processing");

  return (
    <div>
      <div className="pt-10 pb-2">
        <div className="mono-xs text-accent mb-3">// SUBMIT_ARTICLE</div>
        <h1 className="text-4xl font-black tracking-tight mb-2 leading-tight">
          {mode === "url" ? "Drop a link." : "Drop a file."}
          <br />
          <span className="text-accent">Get a podcast.</span>
        </h1>
        <p className="text-dim text-sm mb-6 leading-relaxed">
          {mode === "url"
            ? "Audicle reads articles aloud. Paste a URL and it joins your feed."
            : "Audicle reads documents aloud. Upload a file and it joins your feed."}
        </p>

        <div className="flex border-b border-line mb-5">
          <button
            type="button"
            className={`tab-btn${mode === "url" ? " active" : ""}`}
            onClick={() => {
              setMode("url");
              setError(null);
            }}
          >
            Link
          </button>
          <button
            type="button"
            className={`tab-btn${mode === "file" ? " active" : ""}`}
            onClick={() => {
              setMode("file");
              setError(null);
            }}
          >
            File
          </button>
        </div>

        <form onSubmit={submit} className="space-y-3">
          {mode === "url" ? (
            <input
              type="url"
              className="hero-input"
              placeholder="https://"
              autoComplete="off"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
          ) : (
            <label
              className={`dropzone block cursor-pointer${dragging ? " dropzone-drag" : ""}`}
              onDragEnter={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={(e) => {
                e.preventDefault();
                setDragging(false);
              }}
              onDrop={onDrop}
              aria-label="Upload a document (PDF, DOCX, Markdown, text, or HTML), up to 50 MB"
            >
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPT}
                className="sr-only"
                onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
              />
              {file ? (
                <div className="file-chip">
                  <span className="format-badge">{fileExt(file.name).toUpperCase()}</span>
                  <span className="flex-1 min-w-0 truncate text-sm text-left">{file.name}</span>
                  <span className="mono-xs text-mute">{formatBytes(file.size)}</span>
                  <button
                    type="button"
                    className="btn-ghost"
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      clearFile();
                      setError(null);
                    }}
                    aria-label="Remove selected file"
                  >
                    &times;
                  </button>
                </div>
              ) : (
                <>
                  <div className="mono-xs text-accent mb-2">// DROP_FILE</div>
                  <div className="text-sm text-dim">Drag a document here, or browse</div>
                  <div className="mono-xs text-mute mt-2">
                    PDF · DOCX · MD · TXT · HTML &nbsp;&mdash;&nbsp; up to {maxUploadMb} MB
                  </div>
                </>
              )}
            </label>
          )}
          <button
            type="submit"
            className="btn-primary w-full"
            disabled={pending || noVoiceLoaded || (mode === "url" ? !url : !file)}
          >
            {pending ? "Submitting..." : "Submit"}
          </button>
          {noVoiceLoaded && (
            <p className="mono-xs text-mute mt-2">
              // no voice loaded -- add a voice slot in settings before submitting
            </p>
          )}
          <div>
            <button
              type="button"
              className="mono-xs text-mute flex items-center gap-1.5 hover:text-fg"
              onClick={() => setVoiceOpen(!voiceOpen)}
              aria-expanded={voiceOpen}
            >
              <span className={`transition-transform ${voiceOpen ? "rotate-90" : ""}`}>
                &rsaquo;
              </span>
              // voice: {voiceChoiceLabel}
            </button>
            {voiceOpen && (
              <>
                <select
                  className="field mt-2"
                  value={voice}
                  onChange={(e) => setVoice(e.target.value)}
                  aria-label="Reference voice"
                >
                  <option value="random">Voice: Random</option>
                  <option value="last">Voice: Last used</option>
                  {filledSlots.map((s) => (
                    <option key={s.slot} value={String(s.slot)}>
                      Voice: {s.label ?? `Slot ${s.slot}`}
                    </option>
                  ))}
                </select>
                {filledSlots.length === 0 && (
                  <p className="mono-xs text-mute mt-1">
                    // no voice loaded -- add a slot in settings before submitting
                  </p>
                )}
              </>
            )}
          </div>
        </form>
        {error && <p className="text-danger text-xs font-mono mt-2 break-words">{error}</p>}

        {!jobs.length && jobsQ.data && (
          <p className="mono-xs text-mute mt-8">// no submissions yet</p>
        )}
      </div>

      {active.length > 0 && (
        <section className="mt-8">
          <div className="mono-xs text-accent mb-3">// QUEUE ({active.length})</div>
          {cancelMsg && <p className="mono-xs text-danger mb-2">{cancelMsg}</p>}
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
                <div className="flex flex-col items-end gap-1 flex-shrink-0">
                  <span className={`tag ${statusTag(j.status)}`}>{j.status}</span>
                  <button
                    className="btn-ghost text-xs"
                    disabled={cancelM.isPending}
                    onClick={() => cancelM.mutate(j.id)}
                    aria-label="Cancel this job"
                  >
                    &times; cancel
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {history.length > 0 && (
        <section className="mt-8">
          <button
            className="mono-xs text-mute mb-3 flex items-center gap-1.5 hover:text-fg"
            onClick={() => setRecentOpen(!recentOpen)}
            aria-expanded={recentOpen}
          >
            <span className={`transition-transform ${recentOpen ? "rotate-90" : ""}`}>
              &rsaquo;
            </span>
            // RECENT ({history.length})
          </button>
          {recentMsg && <p className="mono-xs text-danger mb-2">{recentMsg}</p>}
          {recentOpen && (
            <ul className="space-y-2">
              {history.map((j) => {
                const duration = formatDuration(j.started_at, j.updated_at);
                return (
                <li key={j.id} className="card p-4 flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="mono-xs text-mute truncate">{j.source_filename ?? j.url}</p>
                    <p className="text-sm mt-1 truncate text-dim">
                      {j.episode_id} &middot; {j.stage ?? "-"}
                      {progressSuffix(j)}
                    </p>
                    {/* Own line, wrapping (not truncated), so the failure reason and
                        its fix stay readable. */}
                    {j.error && <p className="text-sm mt-1 text-danger break-words">{j.error}</p>}
                    {j.status === "failed" && (
                      <button
                        className="btn-ghost mt-2"
                        disabled={requeueM.isPending}
                        onClick={() => requeueM.mutate(j.id)}
                      >
                        &#8635; Reprocess
                      </button>
                    )}
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
    case "cancelled":
      return "tag-cancelled";
    case "processing":
      return "tag-processing";
    default:
      return "tag-queued";
  }
}
