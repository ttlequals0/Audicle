import { useState, useEffect, useRef, type ReactNode } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, readCsrf, LlmModelsResponse, SettingsPayload, VoiceSlot } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useHealthLive } from "../lib/useHealthLive";
import CollapsibleSection from "../components/CollapsibleSection";

/**
 * Operator-facing setting groups, plus the prompt
 * editor (which talks to /api/v1/prompt), the corrections table (which
 * talks to /api/v1/corrections), and the reference voice widget
 * (preview/test/commit against /api/v1/reference). System info is a
 * read-only block.
 */

const GROUPS: Record<string, string[]> = {
  LLM: [
    "LLM_PROVIDER",
    "LLM_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "OLLAMA_BASE_URL",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "LLM_TIMEOUT_SECONDS",
    "LLM_RETRY_COUNT",
  ],
  Feed: [
    "FEED_TITLE",
    "FEED_DESCRIPTION",
    "FEED_AUTHOR",
    "FEED_EMAIL",
    "FEED_LANGUAGE",
    "FEED_CATEGORY",
    "FEED_EXPLICIT",
    "FEED_ARTWORK_URL",
  ],
  Connections: ["FIRECRAWL_URL", "FIRECRAWL_API_KEY", "TTS_URL", "FLARESOLVERR_URL"],
  Extraction: [
    "EXTRACTION_ENGINE",
    "EXTRACTION_DIRECT_TIMEOUT_SECONDS",
    "EXTRACTION_ARC_ENABLED",
    "ARCHIVE_FALLBACK_ENABLED",
  ],
  Webhooks: ["WEBHOOK_URL"],
  TTS: ["TTS_CHUNK_TARGET_WORDS", "TTS_CHUNK_MAX_WORDS", "TTS_CHUNK_SILENCE_MS"],
  Verification: [
    "WHISPER_VERIFY_ENABLED",
    "WHISPER_DIVERGENCE_THRESHOLD",
    "WHISPER_VERIFY_MIN_WORDS",
  ],
  Cleanup: ["MIN_CLEANUP_CHARS", "MAX_PROMPT_LENGTH_BYTES"],
  Uploads: ["UPLOAD_MAX_MB"],
  Pipeline: ["JOB_TIMEOUT_SECONDS", "JOB_TIMEOUT_PER_CHUNK_SECONDS"],
  Retention: ["RETENTION_DAYS"],
  RSS: ["RSS_CACHE_MAX_AGE_SECONDS"],
};

// Secret fields: rendered as password inputs. The backend masks them on read
// (a sentinel arrives instead of the value) and ignores the sentinel on save.
const MASKED_KEYS = new Set([
  "OPENAI_API_KEY",
  "ANTHROPIC_API_KEY",
  "OPENROUTER_API_KEY",
  "FIRECRAWL_API_KEY",
]);
const PROVIDER_OPTIONS = ["openai-compatible", "anthropic", "openrouter", "ollama"];
// Keep in sync with the EXTRACTION_ENGINE Literal in backend/app/config.py. The
// backend rejects any other value on PUT, so this list only drives the dropdown.
const EXTRACTION_ENGINE_OPTIONS = ["direct", "firecrawl"];

// Which provider-specific keys are relevant per provider. Keys not listed for
// the selected provider are hidden (openrouter's base URL is fixed server-side;
// anthropic has no base URL). Provider-agnostic keys (model, tuning) always show.
const PROVIDER_FIELDS: Record<string, Set<string>> = {
  "openai-compatible": new Set(["OPENAI_BASE_URL", "OPENAI_API_KEY"]),
  anthropic: new Set(["ANTHROPIC_API_KEY"]),
  openrouter: new Set(["OPENROUTER_API_KEY"]),
  ollama: new Set(["OLLAMA_BASE_URL"]),
};
// Union of every provider's keys, derived so it can't drift from PROVIDER_FIELDS.
const PROVIDER_SPECIFIC_KEYS = new Set(
  Object.values(PROVIDER_FIELDS).flatMap((s) => [...s])
);

interface PromptBody {
  prompt: string;
  is_default?: boolean;
}

// Sends a sample payload to the saved WEBHOOK_URL and reports the outcome inline,
// reusing the same btn-ghost + mono-xs message language as the voice audition.
function WebhookTest() {
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [pending, setPending] = useState(false);

  const run = async () => {
    setPending(true);
    setMsg(null);
    try {
      const r = await api<{ delivered: boolean; status_code: number | null; error: string | null }>(
        "/api/v1/webhooks/test",
        { method: "POST" }
      );
      const code = r.status_code ? ` (${r.status_code})` : "";
      setMsg(
        r.delivered
          ? { ok: true, text: `// delivered${code}` }
          : { ok: false, text: `// failed${code}: ${r.error ?? "no response"}` }
      );
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      setMsg({
        ok: false,
        text: status === 409 ? "// save a webhook URL above first" : `// request failed (${status})`,
      });
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="mt-3 flex items-center gap-3 flex-wrap">
      <button type="button" className="btn-ghost" disabled={pending} onClick={run}>
        {pending ? "testing..." : "send test webhook"}
      </button>
      {msg && <span className={`mono-xs ${msg.ok ? "text-accent" : "text-danger"}`}>{msg.text}</span>}
    </div>
  );
}

function Toggle({
  id,
  checked,
  onChange,
}: {
  id: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      type="button"
      id={id}
      role="switch"
      aria-checked={checked}
      aria-label={id}
      className="toggle"
      onClick={() => onChange(!checked)}
    />
  );
}

export default function SettingsRoute() {
  const qc = useQueryClient();
  // Shared app-wide auth status so a password change here also updates the
  // header (a separate query would leave the header's useAuth() stale).
  const { status: authStatus, refresh: refreshAuth } = useAuth();
  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<SettingsPayload>("/api/v1/settings"),
  });
  const promptQ = useQuery({
    queryKey: ["prompt"],
    queryFn: () => api<PromptBody>("/api/v1/prompt"),
  });
  const correctionsQ = useQuery({
    queryKey: ["corrections"],
    queryFn: () => api<Record<string, CorrectionEntry>>("/api/v1/corrections"),
  });
  const fallbacksQ = useQuery({
    queryKey: ["source-fallbacks"],
    queryFn: () => api<SourceFallbacksConfig>("/api/v1/source-fallbacks"),
  });
  const healthQ = useHealthLive();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [savedMsg, setSavedMsg] = useState<string | null>(null);
  const seeded = useRef(false);
  // The seeded values (override-or-default) so save() can send only the keys
  // the operator actually changed -- otherwise every default would be written
  // as an explicit override.
  const baseline = useRef<Record<string, string>>({});

  // Seed the draft once on first arrival; subsequent refetches must not
  // clobber unsaved field edits.
  useEffect(() => {
    if (!seeded.current && settingsQ.data) {
      const next: Record<string, string> = {};
      for (const key of settingsQ.data.allowlist) {
        // Stored override if present, else the effective default, so fields
        // show editable values instead of blank.
        const v = settingsQ.data.values[key] ?? settingsQ.data.defaults[key];
        next[key] = v === undefined || v === null ? "" : String(v);
      }
      setDraft(next);
      baseline.current = { ...next };
      seeded.current = true;
    }
  }, [settingsQ.data]);

  const putM = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      api("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      setSavedMsg("saved");
      setTimeout(() => setSavedMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["settings"] });
      // A saved provider/base-URL change means the model list may differ.
      qc.invalidateQueries({ queryKey: ["llm-models"] });
    },
  });

  const save = () => {
    const payload: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(draft)) {
      // Only persist keys the operator changed from the seeded value, so
      // leaving a field at its default doesn't pin it as an override.
      if (value === (baseline.current[key] ?? "")) continue;
      if (MASKED_KEYS.has(key)) {
        // Secrets are sent verbatim (never number-coerced). The mask sentinel
        // round-trips and the backend ignores it; an empty value clears the
        // stored override (reverts to the .env value).
        payload[key] = value;
        continue;
      }
      if (value === "") continue;
      if (value === "true") payload[key] = true;
      else if (value === "false") payload[key] = false;
      else if (!Number.isNaN(Number(value)) && value.trim() !== "")
        payload[key] = Number(value);
      else payload[key] = value;
    }
    putM.mutate(payload);
  };

  if (settingsQ.isLoading) return <p className="text-mute text-sm">loading...</p>;

  // Keys whose effective default is a real boolean render as a switch, not a
  // text field. The defaults map carries the typed value from the backend.
  const boolKeys = new Set(
    Object.entries(settingsQ.data?.defaults ?? {})
      .filter(([, v]) => typeof v === "boolean")
      .map(([k]) => k)
  );

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black tracking-tight mb-1">Settings</h1>
      {Object.entries(GROUPS).map(([group, keys]) => {
        const provider = draft["LLM_PROVIDER"] ?? "";
        const visible = keys.filter((k) => {
          if (!settingsQ.data?.allowlist.includes(k)) return false;
          // A provider-specific key shows only for its provider; others always show.
          return !PROVIDER_SPECIFIC_KEYS.has(k) || (PROVIDER_FIELDS[provider]?.has(k) ?? false);
        });
        if (visible.length === 0) return null;
        return (
          <CollapsibleSection key={group} title={group} defaultOpen={group === "LLM"}>
            {group === "Feed" && (
              <p className="mono-xs text-mute mb-3">
                // applies on the next podcast-app refresh
              </p>
            )}
            {group === "Connections" && (
              <p className="mono-xs text-mute mb-3">
                // firecrawl api key optional -- blank for self-hosted
              </p>
            )}
            {group === "Webhooks" && (
              <p className="mono-xs text-mute mb-3">
                // POSTs episode.processed / episode.failed to this URL on every finished or
                failed job. blank disables. the test sends a sample to the saved URL -- save first
              </p>
            )}
            {group === "Verification" && (
              <p className="mono-xs text-mute mb-3">
                // regenerates chunks when audio drifts from the text. needs
                WHISPER_ENABLED on the wrapper. threshold 0-1, higher = stricter
              </p>
            )}
            {group === "Uploads" && (
              <p className="mono-xs text-mute mb-3">
                // max direct-upload size in MB -- applies immediately, no restart
              </p>
            )}
            {group === "Pipeline" && (
              <p className="mono-xs text-mute mb-3">
                // per-job time = max(JOB_TIMEOUT_SECONDS, chunks x per-chunk). raise per-chunk on
                slower hardware. applies to the next job
              </p>
            )}
            {visible.map((key) => {
              const isBool = boolKeys.has(key);
              return (
                <div
                  key={key}
                  className={isBool ? "flex items-center justify-between gap-3 py-1" : undefined}
                >
                  <label className={`label ${isBool ? "mb-0" : ""}`} htmlFor={key}>
                    {key}
                  </label>
                  {key === "LLM_PROVIDER" || key === "EXTRACTION_ENGINE" ? (
                    <select
                      id={key}
                      className="field"
                      value={draft[key] ?? ""}
                      onChange={(e) =>
                        setDraft((p) => ({ ...p, [key]: e.target.value }))
                      }
                    >
                      {(key === "EXTRACTION_ENGINE"
                        ? EXTRACTION_ENGINE_OPTIONS
                        : PROVIDER_OPTIONS
                      ).map((opt) => (
                        <option key={opt} value={opt}>
                          {opt}
                        </option>
                      ))}
                    </select>
                  ) : key === "LLM_MODEL" ? (
                    <ModelField
                      value={draft[key] ?? ""}
                      provider={draft["LLM_PROVIDER"] ?? ""}
                      onChange={(v) => setDraft((p) => ({ ...p, [key]: v }))}
                    />
                  ) : isBool ? (
                    <Toggle
                      id={key}
                      checked={draft[key] === "true"}
                      onChange={(v) => setDraft((p) => ({ ...p, [key]: v ? "true" : "false" }))}
                    />
                  ) : (
                    <input
                      id={key}
                      className="field"
                      type={MASKED_KEYS.has(key) ? "password" : "text"}
                      autoComplete={MASKED_KEYS.has(key) ? "off" : undefined}
                      value={draft[key] ?? ""}
                      onChange={(e) =>
                        setDraft((p) => ({ ...p, [key]: e.target.value }))
                      }
                    />
                  )}
                  {key === "FEED_ARTWORK_URL" && (
                    <ArtworkPreview value={draft[key] ?? ""} />
                  )}
                </div>
              );
            })}
            {group === "Webhooks" && <WebhookTest />}
          </CollapsibleSection>
        );
      })}

      <div className="flex items-center gap-3 sticky bottom-2 z-10">
        <button className="btn-primary" disabled={putM.isPending} onClick={save}>
          {putM.isPending ? "saving..." : "save all"}
        </button>
        {savedMsg && (
          <span className="font-mono text-xs text-accent">{savedMsg}</span>
        )}
      </div>

      {promptQ.data !== undefined && (
        <CollapsibleSection title="cleanup prompt">
          <PromptEditor initial={promptQ.data.prompt} />
        </CollapsibleSection>
      )}
      {correctionsQ.data !== undefined && (
        <CollapsibleSection title="pronunciation corrections">
          <CorrectionsTable initial={correctionsQ.data} />
        </CollapsibleSection>
      )}
      {fallbacksQ.data !== undefined && (
        <CollapsibleSection title="paywall sites">
          <SourceFallbacksTable initial={fallbacksQ.data} />
        </CollapsibleSection>
      )}
      <CollapsibleSection title="voices">
        <VoicesWidget />
      </CollapsibleSection>

      {authStatus && (
        <CollapsibleSection title="security">
          <SecuritySection passwordSet={authStatus.password_set} onChanged={refreshAuth} />
        </CollapsibleSection>
      )}

      <CollapsibleSection title="system info" defaultOpen>
        <ReadOnlyRow label="version" value={healthQ.data?.version ?? "loading"} />
        <ReadOnlyRow label="uptime" value={formatUptime(healthQ.data?.uptime_seconds)} />
        <ReadOnlyRow label="password_set" value={String(authStatus?.password_set ?? "loading")} />
        <ReadOnlyRow label="authenticated" value={String(authStatus?.authenticated ?? "loading")} />
        <div className="flex justify-between font-mono text-[11px] pt-1">
          <span className="text-dim uppercase">api docs</span>
          <a
            href="/api/v1/docs"
            target="_blank"
            rel="noreferrer"
            className="text-accent hover:underline"
          >
            /api/v1/docs
          </a>
        </div>
      </CollapsibleSection>
    </div>
  );
}

// Live preview of the feed cover. An empty value previews the locally-seeded
// /media/default.jpg branding art. The published feed falls back to the same
// branding cover served from DEFAULT_ARTWORK_URL (a stable external .jpg) when
// no FEED_ARTWORK_URL is set; the preview uses the local copy so it always
// renders in-app without depending on the external URL.
function ArtworkPreview({ value }: { value: string }) {
  const src = value.trim() || "/media/default.jpg";
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  return (
    <div className="mt-2 flex items-center gap-3">
      {failed ? (
        <div
          className="artwork-preview grid place-items-center mono-xs text-mute"
          style={{ background: "#15151a" }}
        >
          no image
        </div>
      ) : (
        <img
          src={src}
          alt="feed cover preview"
          className="artwork-preview"
          onError={() => setFailed(true)}
        />
      )}
      <span className="mono-xs text-mute">
        {value.trim() ? "custom cover" : "branding default (/media/default.jpg)"}
      </span>
    </div>
  );
}

function formatUptime(seconds: number | undefined): string {
  if (seconds === undefined) return "loading";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const parts = [];
  if (d) parts.push(`${d}d`);
  if (h) parts.push(`${h}h`);
  parts.push(`${m}m`);
  return parts.join(" ");
}

function ModelField({
  value,
  provider,
  onChange,
}: {
  value: string;
  provider: string;
  onChange: (v: string) => void;
}) {
  const qc = useQueryClient();
  // The backend lists models for the SAVED provider/base URL (it resolves them
  // from runtime settings, not the request), so the key is provider-only.
  // Saving settings invalidates this query; "refresh" flushes the server cache.
  const modelsQ = useQuery({
    queryKey: ["llm-models", provider],
    queryFn: () =>
      api<LlmModelsResponse>(
        `/api/v1/llm/models?provider=${encodeURIComponent(provider)}`
      ),
    enabled: provider !== "",
    staleTime: 60_000,
  });
  const refreshM = useMutation({
    mutationFn: () =>
      api<LlmModelsResponse>(
        `/api/v1/llm/models/refresh?provider=${encodeURIComponent(provider)}`,
        { method: "POST" }
      ),
    onSuccess: (data) => qc.setQueryData(["llm-models", provider], data),
  });

  const models = modelsQ.data?.models ?? [];
  const ids = models.map((m) => m.id);
  // Stored value not in the live list: keep it selectable (orphan option) so a
  // saved model that the endpoint no longer reports isn't silently dropped.
  const isOrphan = value !== "" && !ids.includes(value);
  const [freeText, setFreeText] = useState(false);

  if (freeText) {
    return (
      <div className="flex gap-2">
        <input
          id="LLM_MODEL"
          className="field flex-1"
          value={value}
          placeholder="model id"
          onChange={(e) => onChange(e.target.value)}
        />
        <button type="button" className="btn-ghost" onClick={() => setFreeText(false)}>
          list
        </button>
      </div>
    );
  }

  return (
    <div className="flex gap-2">
      <select
        id="LLM_MODEL"
        className="field flex-1"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">(none)</option>
        {isOrphan && <option value={value}>{value} (saved)</option>}
        {models.map((m) => (
          <option key={m.id} value={m.id}>
            {m.name}
          </option>
        ))}
      </select>
      <button
        type="button"
        className="btn-ghost"
        disabled={refreshM.isPending || provider === ""}
        onClick={() => refreshM.mutate()}
        title="refresh model list from provider"
      >
        {refreshM.isPending ? "..." : "refresh"}
      </button>
      <button type="button" className="btn-ghost" onClick={() => setFreeText(true)}>
        custom
      </button>
    </div>
  );
}

function SecuritySection({
  passwordSet,
  onChanged,
}: {
  passwordSet: boolean;
  onChanged: () => void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const m = useMutation({
    mutationFn: (clear: boolean) => {
      const body: Record<string, string> = { new_password: clear ? "" : next };
      if (passwordSet) body.current_password = current;
      return api("/api/v1/auth/password", { method: "PUT", body: JSON.stringify(body) });
    },
    onSuccess: (_data, clear) => {
      setMsg(clear ? "password removed" : "password saved");
      setCurrent("");
      setNext("");
      onChanged();
      setTimeout(() => setMsg(null), 2500);
    },
    onError: (e) => {
      if (e instanceof ApiError) {
        if (e.status === 401) setMsg("current password is incorrect");
        else if (e.status === 400)
          setMsg(
            typeof e.detail === "object" && e.detail && "detail" in e.detail
              ? String((e.detail as { detail: unknown }).detail)
              : "invalid request"
          );
        else setMsg(`error ${e.status}`);
      } else {
        setMsg((e as Error).message);
      }
    },
  });

  return (
    <section className="space-y-3">
      {!passwordSet && (
        <p className="text-danger text-xs font-mono">
          no password set - the admin UI and API are open to anyone. set one below.
        </p>
      )}
      {passwordSet && (
        <div>
          <label className="label" htmlFor="cur-pw">
            current password
          </label>
          <input
            id="cur-pw"
            className="field"
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </div>
      )}
      <div>
        <label className="label" htmlFor="new-pw">
          {passwordSet ? "new password (min 8 chars)" : "password (min 8 chars)"}
        </label>
        <input
          id="new-pw"
          className="field"
          type="password"
          autoComplete="new-password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
        />
      </div>
      <div className="flex items-center gap-3 flex-wrap">
        <button
          className="btn-primary"
          disabled={m.isPending || next.length < 8}
          onClick={() => m.mutate(false)}
        >
          {passwordSet ? "change password" : "set password"}
        </button>
        {passwordSet && (
          <button
            className="btn-ghost text-danger"
            disabled={m.isPending}
            onClick={() => {
              if (confirm("Remove the password and reopen the admin UI to anyone?"))
                m.mutate(true);
            }}
          >
            remove password
          </button>
        )}
        {msg && <span className="font-mono text-xs text-accent">{msg}</span>}
      </div>
    </section>
  );
}

function ReadOnlyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between font-mono text-[11px]">
      <span className="text-dim uppercase">{label}</span>
      <span className="text-fg">{value}</span>
    </div>
  );
}

function PromptEditor({ initial }: { initial: string }) {
  const qc = useQueryClient();
  // Seed from the initial prop via lazy initializer so the editor is
  // pre-populated on mount; subsequent refetches arrive as a new
  // PromptEditor instance only when the parent unmounts/remounts (we
  // gate at the parent on data !== undefined), so in-progress edits are
  // never clobbered.
  const [text, setText] = useState(initial);
  const [msg, setMsg] = useState<string | null>(null);

  const m = useMutation({
    mutationFn: () =>
      api("/api/v1/prompt", {
        method: "PUT",
        body: JSON.stringify({ prompt: text }),
      }),
    onSuccess: () => {
      setMsg("saved");
      setTimeout(() => setMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["prompt"] });
    },
  });

  // Reset clears the DB override; the response carries the packaged default,
  // which we load back into the editor.
  const reset = useMutation({
    mutationFn: () => api<PromptBody>("/api/v1/prompt", { method: "DELETE" }),
    onSuccess: (data) => {
      setText(data.prompt);
      setMsg("reset to default");
      setTimeout(() => setMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["prompt"] });
    },
  });

  return (
    <section className="space-y-3">
      <textarea
        className="field min-h-[200px] font-mono text-xs"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="flex items-center gap-3">
        <button className="btn-primary" disabled={m.isPending} onClick={() => m.mutate()}>
          {m.isPending ? "saving..." : "save prompt"}
        </button>
        <button
          className="btn-ghost"
          disabled={reset.isPending}
          onClick={() => reset.mutate()}
        >
          {reset.isPending ? "resetting..." : "reset to default"}
        </button>
        {msg && <span className="font-mono text-xs text-accent">{msg}</span>}
      </div>
    </section>
  );
}

type Mode = "spell" | "word" | "override";

interface CorrectionEntry {
  mode: Mode;
  spoken: string;
  ipa: string | null;
  case_sensitive: boolean;
}

interface Row {
  id: number;
  k: string;
  v: string; // spoken
  mode: Mode;
  ipa: string;
  caseSensitive: boolean;
}

let _rowCounter = 0;
const newRow = (k = "", entry?: Partial<CorrectionEntry>): Row => ({
  id: ++_rowCounter,
  k,
  v: entry?.spoken ?? "",
  mode: entry?.mode ?? "override",
  ipa: entry?.ipa ?? "",
  caseSensitive: entry?.case_sensitive ?? false,
});

function CorrectionsTable({ initial }: { initial: Record<string, CorrectionEntry> }) {
  const qc = useQueryClient();
  // Lazy initializer: seeded from the initial prop. Parent only mounts
  // this component once the query has data, so refetches arriving as
  // new identical props won't reset rows.
  const [rows, setRows] = useState<Row[]>(() =>
    Object.entries(initial).map(([k, entry]) => newRow(k, entry))
  );
  const [msg, setMsg] = useState<string | null>(null);

  const m = useMutation({
    mutationFn: () => {
      const obj: Record<string, Partial<CorrectionEntry>> = {};
      for (const row of rows) {
        if (!row.k.trim()) continue;
        obj[row.k.trim()] = {
          mode: row.mode,
          spoken: row.v,
          ipa: row.ipa.trim() || null,
          case_sensitive: row.caseSensitive,
        };
      }
      return api("/api/v1/corrections", {
        method: "PUT",
        body: JSON.stringify(obj),
      });
    },
    onSuccess: () => {
      setMsg("saved");
      setTimeout(() => setMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["corrections"] });
    },
  });

  // Clear all user corrections (built-in fixes are unaffected).
  const reset = useMutation({
    mutationFn: () => api("/api/v1/corrections", { method: "DELETE" }),
    onSuccess: () => {
      setRows([]);
      setMsg("cleared");
      setTimeout(() => setMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["corrections"] });
    },
  });

  return (
    <section className="space-y-3">
      <div className="builtin-note">
        <span className="builtin-note-tag">built-in</span>
        <p className="builtin-note-body">
          Built-in pronunciation fixes apply to every episode; your rules below
          override them. Full set:{" "}
          <code className="builtin-note-path">GET /api/v1/corrections/seed</code>.
        </p>
      </div>
      <p className="text-mute text-xs">
        ipa is optional and only feeds the PLS export, not narration.
      </p>
      <LexiconLookup />
      <div className="space-y-2">
        {rows.map((row) => {
          const patch = (p: Partial<Row>) =>
            setRows((rs) => rs.map((r) => (r.id === row.id ? { ...r, ...p } : r)));
          return (
            <div key={row.id} className="flex flex-wrap items-center gap-2">
              <input
                className="field flex-1 min-w-[8rem]"
                placeholder="word"
                value={row.k}
                onChange={(e) => patch({ k: e.target.value })}
              />
              <input
                className="field flex-1 min-w-[8rem]"
                placeholder="spoken"
                value={row.v}
                onChange={(e) => patch({ v: e.target.value })}
              />
              <select
                className="field w-28"
                value={row.mode}
                onChange={(e) => patch({ mode: e.target.value as Mode })}
              >
                <option value="override">override</option>
                <option value="word">word</option>
                <option value="spell">spell</option>
              </select>
              <input
                className="field w-28"
                placeholder="ipa (opt)"
                value={row.ipa}
                onChange={(e) => patch({ ipa: e.target.value })}
              />
              <label className="mono-xs text-mute flex items-center gap-1" title="case-sensitive">
                <input
                  type="checkbox"
                  checked={row.caseSensitive}
                  onChange={(e) => patch({ caseSensitive: e.target.checked })}
                />
                Aa
              </label>
              <button
                className="text-mute hover:text-danger flex items-center justify-center w-8"
                onClick={() => setRows((rs) => rs.filter((r) => r.id !== row.id))}
              >
                &times;
              </button>
            </div>
          );
        })}
        <button className="btn-ghost" onClick={() => setRows((r) => [...r, newRow()])}>
          add row
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="mono-xs text-mute">export:</span>
        <a className="btn-ghost" href="/api/v1/corrections/export?format=json&scope=user">
          JSON
        </a>
        <a className="btn-ghost" href="/api/v1/corrections/export?format=pls&scope=user">
          PLS
        </a>
        <a className="btn-ghost" href="/api/v1/corrections/export?format=json&scope=all">
          full lexicon (JSON)
        </a>
      </div>
      <div className="flex items-center gap-3">
        <button className="btn-primary" disabled={m.isPending} onClick={() => m.mutate()}>
          {m.isPending ? "saving..." : "save corrections"}
        </button>
        <button
          className="btn-ghost"
          disabled={reset.isPending || rows.length === 0}
          onClick={() => reset.mutate()}
        >
          {reset.isPending ? "clearing..." : "clear all"}
        </button>
        {msg && <span className="font-mono text-xs text-accent">{msg}</span>}
      </div>
    </section>
  );
}

interface ProxyOption {
  key: string;
  label: string;
}

interface FallbackRule {
  host: string;
  proxy: string;
  custom_template: string;
  cookies: string;
}

interface SourceFallbacksConfig {
  default_proxy: string;
  min_chars: number;
  rules: FallbackRule[];
  available_proxies: ProxyOption[];
  builtin: { host: string; proxy: string }[];
}

interface FallbackRow {
  id: number;
  host: string;
  proxy: string; // "" -> use the global default
  customTemplate: string;
  cookies: string; // sentinel ("********") when a jar is stored; "" clears it
}

let _fbRowCounter = 0;
const newFallbackRow = (rule?: FallbackRule): FallbackRow => ({
  id: ++_fbRowCounter,
  host: rule?.host ?? "",
  proxy: rule?.proxy ?? "",
  customTemplate: rule?.custom_template ?? "",
  cookies: rule?.cookies ?? "",
});

function SourceFallbacksTable({ initial }: { initial: SourceFallbacksConfig }) {
  const qc = useQueryClient();
  // Lazy init from the initial prop; parent mounts only once the query resolves.
  const [defaultProxy, setDefaultProxy] = useState(initial.default_proxy);
  const [minChars, setMinChars] = useState(String(initial.min_chars));
  const [rows, setRows] = useState<FallbackRow[]>(() =>
    initial.rules.map((r) => newFallbackRow(r))
  );
  const [msg, setMsg] = useState<string | null>(null);

  const proxies = initial.available_proxies;
  const proxyLabel = (key: string) =>
    proxies.find((p) => p.key === key)?.label ?? key;

  const m = useMutation({
    mutationFn: () =>
      api("/api/v1/source-fallbacks", {
        method: "PUT",
        body: JSON.stringify({
          default_proxy: defaultProxy,
          min_chars: Number(minChars) || 0,
          rules: rows
            .filter((r) => r.host.trim())
            .map((r) => ({
              host: r.host.trim(),
              proxy: r.proxy,
              custom_template: r.customTemplate.trim(),
              // Cookies only apply to the flaresolverr strategy; switching away clears the
              // jar so the session secret isn't silently retained on a rule that won't use it.
              cookies: r.proxy === "flaresolverr" ? r.cookies.trim() : "",
            })),
        }),
      }),
    onSuccess: () => {
      setMsg("saved");
      setTimeout(() => setMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["source-fallbacks"] });
    },
    onError: (e) => {
      const detail =
        e instanceof ApiError ? (e.detail as { detail?: string })?.detail : null;
      setMsg(detail || "save failed");
    },
  });

  // Run the saved rules against one URL so the operator can confirm a rule (and its
  // cookie jar) actually fetches the article. Never returns the cookie value.
  const [testUrl, setTestUrl] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);
  const testM = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; chars: number; strategy: string | null; title?: string; detail?: string }>(
        "/api/v1/source-fallbacks/test",
        { method: "POST", body: JSON.stringify({ url: testUrl.trim() }) }
      ),
    onSuccess: (r) =>
      setTestResult(
        r.ok
          ? `ok: ${r.chars.toLocaleString()} chars via ${r.strategy ?? "direct scrape"}` +
              (r.title ? ` -- ${r.title}` : "")
          : `no full article: ${r.detail || "came back below the threshold"}`
      ),
    onError: (e) =>
      setTestResult(
        (e instanceof ApiError ? (e.detail as { detail?: string })?.detail : null) || "test failed"
      ),
  });

  return (
    <section className="space-y-3">
      <div className="builtin-note">
        <span className="builtin-note-tag">built-in</span>
        <p className="builtin-note-body">
          Built-in:{" "}
          {initial.builtin.map((b) => `${b.host} -> ${b.proxy}`).join(", ")}. Your rules
          below override these.
        </p>
      </div>
      <p className="text-mute text-xs">
        List a paywalled host and how to bypass it. The default applies to any host
        that scrapes near-empty.
      </p>
      <p className="text-mute text-xs">
        Cookie jar (flaresolverr only): paste a logged-in Cookie header to fetch as a
        subscriber. Stored masked.
      </p>

      <div className="flex flex-wrap items-end gap-4">
        <div>
          <label className="label" htmlFor="fb-default-proxy">
            default strategy
          </label>
          <select
            id="fb-default-proxy"
            className="field w-56"
            value={defaultProxy}
            onChange={(e) => setDefaultProxy(e.target.value)}
          >
            {proxies.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="fb-min-chars">
            teaser threshold (chars)
          </label>
          <input
            id="fb-min-chars"
            className="field w-36"
            type="number"
            min={1}
            value={minChars}
            onChange={(e) => setMinChars(e.target.value)}
          />
        </div>
      </div>

      <div className="space-y-2">
        {rows.map((row) => {
          const patch = (p: Partial<FallbackRow>) =>
            setRows((rs) => rs.map((r) => (r.id === row.id ? { ...r, ...p } : r)));
          return (
            <div key={row.id} className="flex flex-wrap items-center gap-2">
              <input
                className="field flex-1 min-w-[10rem]"
                placeholder="domain"
                value={row.host}
                onChange={(e) => patch({ host: e.target.value })}
              />
              <select
                className="field w-48"
                value={row.proxy}
                onChange={(e) => patch({ proxy: e.target.value })}
              >
                <option value="">use default ({proxyLabel(defaultProxy)})</option>
                {proxies.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.label}
                  </option>
                ))}
              </select>
              <button
                className="text-mute hover:text-danger flex items-center justify-center w-8"
                onClick={() => setRows((rs) => rs.filter((r) => r.id !== row.id))}
              >
                &times;
              </button>
              {row.proxy === "custom" && (
                <input
                  className="field basis-full min-w-[12rem]"
                  placeholder="https://reader.example/{url}"
                  value={row.customTemplate}
                  onChange={(e) => patch({ customTemplate: e.target.value })}
                />
              )}
              {row.proxy === "flaresolverr" && (
                <input
                  className="field basis-full min-w-[12rem] font-mono"
                  type="password"
                  autoComplete="off"
                  placeholder="cookie jar (optional): name=value; name2=value2"
                  value={row.cookies}
                  onChange={(e) => patch({ cookies: e.target.value })}
                />
              )}
            </div>
          );
        })}
        <button
          className="btn-ghost"
          onClick={() => setRows((r) => [...r, newFallbackRow()])}
        >
          add site
        </button>
      </div>

      <div className="flex items-center gap-3">
        <button className="btn-primary" disabled={m.isPending} onClick={() => m.mutate()}>
          {m.isPending ? "saving..." : "save paywall sites"}
        </button>
        {msg && <span className="font-mono text-xs text-accent">{msg}</span>}
      </div>

      <div className="space-y-1 pt-2">
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="field flex-1 min-w-[12rem]"
            placeholder="test a URL"
            value={testUrl}
            onChange={(e) => setTestUrl(e.target.value)}
          />
          <button
            className="btn-ghost"
            disabled={testM.isPending || !testUrl.trim()}
            onClick={() => {
              setTestResult(null);
              testM.mutate();
            }}
          >
            {testM.isPending ? "testing..." : "test"}
          </button>
        </div>
        {testResult && <p className="text-mute text-xs font-mono">{testResult}</p>}
      </div>
    </section>
  );
}

function LexiconLookup() {
  const [q, setQ] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const search = async () => {
    const term = q.trim();
    if (!term) return;
    const body = await api<{ entry: (CorrectionEntry & { origin: string }) | null }>(
      `/api/v1/corrections/lookup?q=${encodeURIComponent(term)}`
    );
    setResult(
      body.entry
        ? `${body.entry.origin}: "${body.entry.spoken}" (${body.entry.mode})` +
            (body.entry.ipa ? ` /${body.entry.ipa}/` : "")
        : "no match in the built-in lexicon"
    );
  };
  return (
    <div className="flex flex-wrap items-center gap-2">
      <input
        className="field flex-1 min-w-[8rem]"
        placeholder="look up a word"
        value={q}
        onChange={(e) => {
          setResult(null);
          setQ(e.target.value);
        }}
        onKeyDown={(e) => e.key === "Enter" && search()}
      />
      <button className="btn-ghost" onClick={search}>
        look up
      </button>
      {result && <span className="mono-xs text-mute">{result}</span>}
    </div>
  );
}

const VOICE_ACCEPT = ".wav,.mp3,.m4a,.aac,.flac,.ogg,.oga,.opus,audio/*";

function VoiceUploadButton({
  replace,
  onPick,
}: {
  replace: boolean;
  onPick: (file: File) => void;
}) {
  return (
    <label className="btn-ghost cursor-pointer">
      {replace ? "Replace" : "Upload"}
      <input
        type="file"
        accept={VOICE_ACCEPT}
        className="sr-only"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onPick(f);
          e.target.value = "";
        }}
      />
    </label>
  );
}

// One card shared by the Default fallback and every slot: badge, name (a static
// label or a rename input), duration, an inline player for the stored clip, then
// Replace / Audition / Clear. Clear is omitted (onClear undefined) for the
// fallback, which can be replaced but not emptied.
function VoiceRow({
  badge,
  name,
  filled,
  durationSecs,
  previewUrl,
  onUpload,
  onAudition,
  auditioning,
  onClear,
}: {
  badge: string;
  name: ReactNode;
  filled: boolean;
  durationSecs: number | null;
  previewUrl: string;
  onUpload: (file: File) => void;
  onAudition: () => void;
  auditioning: boolean;
  onClear?: () => void;
}) {
  return (
    <div className="card p-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className="format-badge">{badge}</span>
        {name}
        <span className="mono-xs text-mute shrink-0">
          {filled ? `${durationSecs ?? "?"}s` : "empty"}
        </span>
      </div>
      {filled && <audio controls src={previewUrl} className="w-full" />}
      <div className="flex flex-wrap gap-2">
        <VoiceUploadButton replace={filled} onPick={onUpload} />
        {filled && (
          <button className="btn-ghost" disabled={auditioning} onClick={onAudition}>
            {auditioning ? "auditioning..." : "Audition"}
          </button>
        )}
        {filled && onClear && (
          <button className="btn-ghost btn-danger" onClick={onClear}>
            Clear
          </button>
        )}
      </div>
    </div>
  );
}

// Merged voices section: the fallback voice.wav ("Default") sits as row 0
// alongside the 5 slots, each with the same controls -- inline preview of the
// stored clip, audition (a TTS sample), and replace. Uploads accept any common
// audio format; the backend transcodes non-WAV to WAV with ffmpeg.
function VoicesWidget() {
  const qc = useQueryClient();
  const slotsQ = useQuery({
    queryKey: ["voice-slots"],
    queryFn: () => api<VoiceSlot[]>("/api/v1/reference/slots"),
    staleTime: 60_000,
  });
  const defaultQ = useQuery({
    queryKey: ["voice-default"],
    queryFn: () =>
      api<{ filled: boolean; duration_secs: number | null }>("/api/v1/reference/status"),
    staleTime: 60_000,
  });
  const [msg, setMsg] = useState<string | null>(null);
  const [auditionUrl, setAuditionUrl] = useState<string | null>(null);
  const [auditioning, setAuditioning] = useState<string | null>(null);
  const [sample, setSample] = useState(
    "But I must explain to you how all this mistaken idea of denouncing of a pleasure and praising pain was born and I will give you a complete account of the system, and expound the actual teachings of the great explorer of the truth, the master-builder of human happiness.",
  );
  // Bumped after any upload/clear so the inline preview <audio> refetches the
  // changed clip instead of replaying the browser-cached one.
  const [bust, setBust] = useState(0);

  useEffect(
    () => () => {
      if (auditionUrl) URL.revokeObjectURL(auditionUrl);
    },
    [auditionUrl]
  );

  const sendForm = async (path: string, method: string, fd?: FormData): Promise<Response> => {
    const headers: Record<string, string> = {};
    const csrf = readCsrf();
    if (csrf) headers["X-CSRF-Token"] = csrf;
    return fetch(path, { method, body: fd, credentials: "include", headers });
  };

  const invalidate = () => {
    setBust((b) => b + 1);
    qc.invalidateQueries({ queryKey: ["voice-slots"] });
    qc.invalidateQueries({ queryKey: ["voice-default"] });
  };

  const upload = async (path: string, f: File, ok: string) => {
    setMsg("uploading...");
    const fd = new FormData();
    fd.append("voice", f);
    const r = await sendForm(path, "POST", fd);
    setMsg(r.ok ? ok : `upload failed (${r.status})`);
    if (r.ok) invalidate();
  };

  const audition = async (key: string, path: string) => {
    if (sample.trim().length < 4) {
      setMsg("audition sample text must be at least 4 characters");
      return;
    }
    setAuditioning(key);
    setMsg(null);
    const fd = new FormData();
    fd.append("sample_text", sample);
    const r = await sendForm(path, "POST", fd);
    setAuditioning(null);
    if (!r.ok) {
      setMsg(r.status === 503 ? "no voice committed yet" : `audition failed (${r.status})`);
      return;
    }
    setAuditionUrl(URL.createObjectURL(await r.blob()));
  };

  const clearSlot = async (slot: number) => {
    if (!confirm(`Clear voice slot ${slot}?`)) return;
    try {
      await api(`/api/v1/reference/slots/${slot}`, { method: "DELETE" });
      invalidate();
    } catch {
      setMsg(`slot ${slot} clear failed`);
    }
  };

  const renameSlot = async (slot: number, label: string) => {
    const fd = new FormData();
    fd.append("label", label);
    const r = await sendForm(`/api/v1/reference/slots/${slot}/label`, "PUT", fd);
    setMsg(r.ok ? `slot ${slot} renamed` : `rename failed (${r.status})`);
    if (r.ok) qc.invalidateQueries({ queryKey: ["voice-slots"] });
  };

  const slots = slotsQ.data ?? [];
  const defaultVoice = defaultQ.data;

  return (
    <div className="space-y-3">
      <p className="mono-xs text-mute">
        // default is the fallback; a random filled slot is used per episode unless you pick one
        at submit. uploads take wav/mp3/m4a/flac/ogg -- converted to wav on the server
      </p>
      <div>
        <label className="label" htmlFor="voice-sample">
          audition sample text
        </label>
        <textarea
          id="voice-sample"
          className="field min-h-[120px] resize-y"
          value={sample}
          onChange={(e) => setSample(e.target.value)}
        />
      </div>
      {msg && <p className="mono-xs text-accent">{msg}</p>}
      {auditionUrl && <audio src={auditionUrl} controls autoPlay className="w-full" />}

      <VoiceRow
        badge="D"
        name={<span className="flex-1 font-mono text-sm">Default voice</span>}
        filled={!!defaultVoice?.filled}
        durationSecs={defaultVoice?.duration_secs ?? null}
        previewUrl={`/api/v1/reference/preview?v=${bust}`}
        onUpload={(f) => {
          // The default is the global fallback and can't be cleared, so guard the
          // destructive replace (slots are one of five and stay confirm-less).
          if (confirm("Replace the default fallback voice?")) {
            upload("/api/v1/reference/commit", f, "default voice replaced");
          }
        }}
        onAudition={() => audition("default", "/api/v1/reference/audition")}
        auditioning={auditioning === "default"}
      />

      {slots.map((s) => (
        <VoiceRow
          key={s.slot}
          badge={String(s.slot)}
          name={
            <input
              className="field flex-1"
              defaultValue={s.label ?? ""}
              placeholder={`Slot ${s.slot} label`}
              onBlur={(e) => {
                if ((e.target.value ?? "") !== (s.label ?? "")) renameSlot(s.slot, e.target.value);
              }}
            />
          }
          filled={s.filled}
          durationSecs={s.duration_secs}
          previewUrl={`/api/v1/reference/slots/${s.slot}/preview?v=${bust}`}
          onUpload={(f) => upload(`/api/v1/reference/slots/${s.slot}`, f, `slot ${s.slot} saved`)}
          onAudition={() =>
            audition(`slot-${s.slot}`, `/api/v1/reference/slots/${s.slot}/audition`)
          }
          auditioning={auditioning === `slot-${s.slot}`}
          onClear={() => clearSlot(s.slot)}
        />
      ))}
    </div>
  );
}
