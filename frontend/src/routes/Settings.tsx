import { useState, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, readCsrf, LlmModelsResponse, SettingsPayload } from "../lib/api";
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
  TTS: ["TTS_CHUNK_TARGET_WORDS", "TTS_CHUNK_MAX_WORDS", "TTS_CHUNK_SILENCE_MS"],
  Verification: [
    "WHISPER_VERIFY_ENABLED",
    "WHISPER_DIVERGENCE_THRESHOLD",
    "WHISPER_VERIFY_MIN_WORDS",
  ],
  Cleanup: ["MIN_CLEANUP_CHARS", "MAX_PROMPT_LENGTH_BYTES"],
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
                // saved feed changes apply on the next podcast-app refresh -- no rebuild
              </p>
            )}
            {group === "Connections" && (
              <p className="mono-xs text-mute mb-3">
                // firecrawl api key is optional -- leave blank for an open self-hosted instance
              </p>
            )}
            {group === "Verification" && (
              <p className="mono-xs text-mute mb-3">
                // re-transcribes each chunk and regenerates it when the audio drifts from
                the text. needs WHISPER_ENABLED on the tts wrapper (loads the model); these
                toggle the policy live. enabled=true/false, threshold 0-1 (higher = stricter)
              </p>
            )}
            {visible.map((key) => (
              <div key={key}>
                <label className="label" htmlFor={key}>
                  {key}
                </label>
                {key === "LLM_PROVIDER" ? (
                  <select
                    id={key}
                    className="field"
                    value={draft[key] ?? ""}
                    onChange={(e) =>
                      setDraft((p) => ({ ...p, [key]: e.target.value }))
                    }
                  >
                    {PROVIDER_OPTIONS.map((opt) => (
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
            ))}
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
        <CollapsibleSection title="article proxy / paywall sites">
          <SourceFallbacksTable initial={fallbacksQ.data} />
        </CollapsibleSection>
      )}
      <CollapsibleSection title="reference voice">
        <ReferenceVoiceWidget />
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
          no password set - the admin UI and API are open to anyone who can reach
          this server. set a password below.
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
          Audicle applies a built-in set of pronunciation fixes on every episode.
          They are not listed here, and anything you add below overrides them. The
          full set is available from the API at{" "}
          <code className="builtin-note-path">GET /api/v1/corrections/seed</code>.
        </p>
      </div>
      <p className="text-mute text-xs">
        word: the source term. spoken: how the TTS should narrate it. mode: spell
        (letter by letter), word (read as written), or override (use spoken). ipa is
        optional and used only by the phoneme engine. Case-sensitive matches the
        exact casing only.
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
}

let _fbRowCounter = 0;
const newFallbackRow = (rule?: FallbackRule): FallbackRow => ({
  id: ++_fbRowCounter,
  host: rule?.host ?? "",
  proxy: rule?.proxy ?? "",
  customTemplate: rule?.custom_template ?? "",
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

  return (
    <section className="space-y-3">
      <div className="builtin-note">
        <span className="builtin-note-tag">built-in</span>
        <p className="builtin-note-body">
          Built-in:{" "}
          {initial.builtin.map((b) => `${b.host} -> ${b.proxy}`).join(", ")}. Your rules
          below win on a host collision; a "use default" row uses the strategy above.
        </p>
      </div>
      <p className="text-mute text-xs">
        When a listed host scrapes below the threshold, Audicle retries with its strategy
        before failing the job. domain: the host to bypass. strategy: googlebot (re-fetch
        as Googlebot), freedium (Medium mirror), custom (your own {"{url}"} template), none
        (skip the retry and fail rather than narrate the stub). Cloudflare/bot-challenge
        pages are handled automatically via FlareSolverr when FLARESOLVERR_URL is set.
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
        placeholder="look up a word in the built-in lexicon"
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

function ReferenceVoiceWidget() {
  const [candidate, setCandidate] = useState<File | null>(null);
  const [sample, setSample] = useState(
    "But I must explain to you how all this mistaken idea of denouncing of a pleasure and praising pain was born and I will give you a complete account of the system, and expound the actual teachings of the great explorer of the truth, the master-builder of human happiness.",
  );
  const [testAudioUrl, setTestAudioUrl] = useState<string | null>(null);
  const [auditionUrl, setAuditionUrl] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [testPending, setTestPending] = useState(false);
  const [auditionPending, setAuditionPending] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const previewUrl = "/api/v1/reference/preview";

  useEffect(() => {
    return () => {
      if (testAudioUrl) URL.revokeObjectURL(testAudioUrl);
    };
  }, [testAudioUrl]);

  useEffect(() => {
    return () => {
      if (auditionUrl) URL.revokeObjectURL(auditionUrl);
    };
  }, [auditionUrl]);

  const postForm = async (path: string, fd: FormData): Promise<Response | null> => {
    const headers: Record<string, string> = {};
    const csrf = readCsrf();
    if (csrf) headers["X-CSRF-Token"] = csrf;
    try {
      return await fetch(path, {
        method: "POST",
        body: fd,
        credentials: "include",
        headers,
      });
    } catch {
      return null;
    }
  };

  const test = async () => {
    if (!candidate) return;
    setMsg(null);
    setTestPending(true);
    try {
      const fd = new FormData();
      fd.append("voice", candidate);
      fd.append("sample_text", sample);
      const r = await postForm("/api/v1/reference/test", fd);
      if (!r) {
        setMsg("preview failed (network error)");
        return;
      }
      if (!r.ok) {
        setMsg(`preview failed (${r.status})`);
        return;
      }
      const blob = await r.blob();
      setTestAudioUrl(URL.createObjectURL(blob));
      setMsg("preview ready");
    } finally {
      setTestPending(false);
    }
  };

  const audition = async () => {
    setMsg(null);
    setAuditionPending(true);
    try {
      const fd = new FormData();
      fd.append("sample_text", sample);
      const r = await postForm("/api/v1/reference/audition", fd);
      if (!r) {
        setMsg("audition failed (network error)");
        return;
      }
      if (!r.ok) {
        setMsg(r.status === 503 ? "no voice committed yet" : `audition failed (${r.status})`);
        return;
      }
      const blob = await r.blob();
      setAuditionUrl(URL.createObjectURL(blob));
      setMsg("audition ready");
    } finally {
      setAuditionPending(false);
    }
  };

  const commit = async () => {
    if (!candidate) return;
    if (!confirm("Replace the current reference voice?")) return;
    setMsg(null);
    const fd = new FormData();
    fd.append("voice", candidate);
    const r = await postForm("/api/v1/reference/commit", fd);
    if (!r) {
      setMsg("commit failed (network error)");
      return;
    }
    setMsg(r.ok ? "committed; TTS reloaded" : `commit failed (${r.status})`);
    if (r.ok) {
      setCandidate(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <section className="space-y-3">
      <audio controls src={previewUrl} className="w-full" />
      <div className="dropzone">
        <label className="label" htmlFor="ref-file">
          upload candidate WAV (3-60s, &lt;= 5 MB)
        </label>
        <input
          id="ref-file"
          ref={fileRef}
          type="file"
          accept=".wav,audio/wav,audio/x-wav,audio/wave,audio/vnd.wave"
          className="field"
          onChange={(e) => {
            setCandidate(e.target.files?.[0] ?? null);
            setTestAudioUrl(null);
            setMsg(null);
          }}
        />
      </div>
      <div>
        <label className="label" htmlFor="ref-sample">
          sample text (for preview / audition)
        </label>
        <textarea
          id="ref-sample"
          className="field min-h-[120px] resize-y"
          value={sample}
          onChange={(e) => setSample(e.target.value)}
        />
        <div className="flex gap-2 items-center flex-wrap mt-2">
          <button className="btn-ghost" onClick={audition} disabled={auditionPending}>
            {auditionPending ? "auditioning..." : "play current voice"}
          </button>
          <span className="label">synthesize the sample with the saved voice</span>
        </div>
        {auditionUrl && (
          <div className="mt-2">
            <p className="label">current voice saying the sample</p>
            <audio controls src={auditionUrl} className="w-full" />
          </div>
        )}
      </div>
      <div className="flex gap-2 items-center flex-wrap">
        <button
          className="btn-ghost"
          disabled={!candidate || testPending}
          onClick={test}
          title={candidate ? undefined : "upload a candidate WAV first"}
        >
          {testPending ? "previewing..." : "preview this upload"}
        </button>
        <button className="btn-primary" disabled={!candidate} onClick={commit}>
          commit
        </button>
        {msg && <span className="font-mono text-xs text-accent">{msg}</span>}
      </div>
      {testAudioUrl && (
        <div>
          <p className="label">candidate upload saying the sample</p>
          <audio controls src={testAudioUrl} className="w-full" />
        </div>
      )}
    </section>
  );
}
