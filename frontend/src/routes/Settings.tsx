import { useState, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  readCsrf,
  LlmModelsResponse,
  SettingsPayload,
} from "../lib/api";
import { useAuth } from "../lib/auth";

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
  Connections: ["FIRECRAWL_URL", "TTS_URL"],
  TTS: ["TTS_CHUNK_TARGET_WORDS", "TTS_CHUNK_MAX_WORDS", "TTS_CHUNK_SILENCE_MS"],
  Cleanup: ["MIN_CLEANUP_CHARS", "MAX_PROMPT_LENGTH_BYTES"],
  Retention: ["RETENTION_DAYS"],
  RSS: ["RSS_CACHE_MAX_AGE_SECONDS"],
};

// Secret fields: rendered as password inputs. The backend masks them on read
// (a sentinel arrives instead of the value) and ignores the sentinel on save.
const MASKED_KEYS = new Set(["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]);
const PROVIDER_OPTIONS = ["openai-compatible", "anthropic"];

interface PromptBody {
  prompt: string;
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
    queryFn: () => api<Record<string, string>>("/api/v1/corrections"),
  });
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [savedMsg, setSavedMsg] = useState<string | null>(null);
  const seeded = useRef(false);

  // Seed the draft once on first arrival; subsequent refetches must not
  // clobber unsaved field edits.
  useEffect(() => {
    if (!seeded.current && settingsQ.data) {
      const next: Record<string, string> = {};
      for (const key of settingsQ.data.allowlist) {
        const v = settingsQ.data.values[key];
        next[key] = v === undefined || v === null ? "" : String(v);
      }
      setDraft(next);
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
    <div className="space-y-8">
      {Object.entries(GROUPS).map(([group, keys]) => {
        const visible = keys.filter((k) => settingsQ.data?.allowlist.includes(k));
        if (visible.length === 0) return null;
        return (
          <section key={group} className="space-y-3">
            <h2 className="font-mono uppercase text-xs text-dim">{group}</h2>
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
              </div>
            ))}
          </section>
        );
      })}

      <div className="flex items-center gap-3 sticky bottom-2">
        <button className="btn-primary" disabled={putM.isPending} onClick={save}>
          {putM.isPending ? "saving..." : "save all"}
        </button>
        {savedMsg && (
          <span className="font-mono text-xs text-accent">{savedMsg}</span>
        )}
      </div>

      {promptQ.data !== undefined && <PromptEditor initial={promptQ.data.prompt} />}
      {correctionsQ.data !== undefined && <CorrectionsTable initial={correctionsQ.data} />}
      <ReferenceVoiceWidget />

      {authStatus && (
        <SecuritySection passwordSet={authStatus.password_set} onChanged={refreshAuth} />
      )}

      <section className="space-y-2 border-t border-line pt-6">
        <h2 className="font-mono uppercase text-xs text-dim">system info</h2>
        <ReadOnlyRow label="password_set" value={String(authStatus?.password_set ?? "loading")} />
        <ReadOnlyRow label="authenticated" value={String(authStatus?.authenticated ?? "loading")} />
        <ReadOnlyRow
          label="allowlist_keys"
          value={String(settingsQ.data?.allowlist.length ?? 0)}
        />
      </section>
    </div>
  );
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
    <section className="space-y-3 border-t border-line pt-6">
      <h2 className="font-mono uppercase text-xs text-dim">security</h2>
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

  return (
    <section className="space-y-3 border-t border-line pt-6">
      <h2 className="font-mono uppercase text-xs text-dim">cleanup prompt</h2>
      <textarea
        className="field min-h-[200px] font-mono text-xs"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="flex items-center gap-3">
        <button className="btn-primary" disabled={m.isPending} onClick={() => m.mutate()}>
          {m.isPending ? "saving..." : "save prompt"}
        </button>
        {msg && <span className="font-mono text-xs text-accent">{msg}</span>}
      </div>
    </section>
  );
}

interface Row {
  id: number;
  k: string;
  v: string;
}

let _rowCounter = 0;
const newRow = (k = "", v = ""): Row => ({ id: ++_rowCounter, k, v });

function CorrectionsTable({ initial }: { initial: Record<string, string> }) {
  const qc = useQueryClient();
  // Lazy initializer: seeded from the initial prop. Parent only mounts
  // this component once the query has data, so refetches arriving as
  // new identical props won't reset rows.
  const [rows, setRows] = useState<Row[]>(() =>
    Object.entries(initial).map(([k, v]) => newRow(k, v))
  );
  const [msg, setMsg] = useState<string | null>(null);

  const m = useMutation({
    mutationFn: () => {
      const obj: Record<string, string> = {};
      for (const row of rows) {
        if (row.k.trim()) obj[row.k.trim()] = row.v;
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

  return (
    <section className="space-y-3 border-t border-line pt-6">
      <h2 className="font-mono uppercase text-xs text-dim">pronunciation corrections</h2>
      <p className="text-mute text-xs">
        left column: source word; right column: the spelling the TTS should narrate.
      </p>
      <div className="space-y-2">
        {rows.map((row) => (
          <div key={row.id} className="flex gap-2">
            <input
              className="field flex-1"
              placeholder="word"
              value={row.k}
              onChange={(e) =>
                setRows((rs) =>
                  rs.map((r) => (r.id === row.id ? { ...r, k: e.target.value } : r))
                )
              }
            />
            <input
              className="field flex-1"
              placeholder="replacement"
              value={row.v}
              onChange={(e) =>
                setRows((rs) =>
                  rs.map((r) => (r.id === row.id ? { ...r, v: e.target.value } : r))
                )
              }
            />
            <button
              className="btn-ghost text-danger"
              onClick={() => setRows((rs) => rs.filter((r) => r.id !== row.id))}
            >
              &times;
            </button>
          </div>
        ))}
        <button
          className="btn-ghost"
          onClick={() => setRows((r) => [...r, newRow()])}
        >
          add row
        </button>
      </div>
      <div className="flex items-center gap-3">
        <button className="btn-primary" disabled={m.isPending} onClick={() => m.mutate()}>
          {m.isPending ? "saving..." : "save corrections"}
        </button>
        {msg && <span className="font-mono text-xs text-accent">{msg}</span>}
      </div>
    </section>
  );
}

function ReferenceVoiceWidget() {
  const [candidate, setCandidate] = useState<File | null>(null);
  const [sample, setSample] = useState("The quick brown fox jumps over the lazy dog.");
  const [testAudioUrl, setTestAudioUrl] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const previewUrl = "/api/v1/reference/preview";

  useEffect(() => {
    return () => {
      if (testAudioUrl) URL.revokeObjectURL(testAudioUrl);
    };
  }, [testAudioUrl]);

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
    const fd = new FormData();
    fd.append("voice", candidate);
    fd.append("sample_text", sample);
    const r = await postForm("/api/v1/reference/test", fd);
    if (!r) {
      setMsg("test failed (network error)");
      return;
    }
    if (!r.ok) {
      setMsg(`test failed (${r.status})`);
      return;
    }
    const blob = await r.blob();
    setTestAudioUrl(URL.createObjectURL(blob));
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
    <section className="space-y-3 border-t border-line pt-6">
      <h2 className="font-mono uppercase text-xs text-dim">reference voice</h2>
      <audio controls src={previewUrl} className="w-full" />
      <div>
        <label className="label" htmlFor="ref-file">
          upload candidate WAV (3-60s, &lt;= 5 MB)
        </label>
        <input
          id="ref-file"
          ref={fileRef}
          type="file"
          accept="audio/wav"
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
          sample text (for test only)
        </label>
        <input
          id="ref-sample"
          className="field"
          value={sample}
          onChange={(e) => setSample(e.target.value)}
        />
      </div>
      <div className="flex gap-2 items-center flex-wrap">
        <button className="btn-ghost" disabled={!candidate} onClick={test}>
          test
        </button>
        <button className="btn-primary" disabled={!candidate} onClick={commit}>
          commit
        </button>
        {msg && <span className="font-mono text-xs text-accent">{msg}</span>}
      </div>
      {testAudioUrl && (
        <div>
          <p className="label">audition</p>
          <audio controls src={testAudioUrl} className="w-full" />
        </div>
      )}
    </section>
  );
}
