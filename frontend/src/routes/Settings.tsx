import { useState, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, readCsrf, SettingsPayload } from "../lib/api";

/**
 * Five operator-facing groups per build-plan Phase 11, plus the prompt
 * editor (which talks to /api/v1/prompt), the corrections table (which
 * talks to /api/v1/corrections), and the reference voice widget
 * (preview/test/commit against /api/v1/reference). System info is a
 * read-only block.
 */

const GROUPS: Record<string, string[]> = {
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
  TTS: ["TTS_CHUNK_TARGET_WORDS", "TTS_CHUNK_MAX_WORDS", "TTS_CHUNK_SILENCE_MS"],
  Cleanup: ["MIN_CLEANUP_CHARS", "MAX_PROMPT_LENGTH_BYTES"],
  Retention: ["RETENTION_DAYS"],
  RSS: ["RSS_CACHE_MAX_AGE_SECONDS"],
};

interface PromptBody {
  prompt: string;
}

interface AuthStatus {
  auth_enabled: boolean;
  logged_in: boolean;
  username: string | null;
}

export default function SettingsRoute() {
  const qc = useQueryClient();
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
  const authQ = useQuery({
    queryKey: ["auth_status_settings"],
    queryFn: () => api<AuthStatus>("/api/v1/auth/status"),
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
    },
  });

  const save = () => {
    const payload: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(draft)) {
      if (value === "") continue;
      if (value === "true") payload[key] = true;
      else if (value === "false") payload[key] = false;
      else if (!Number.isNaN(Number(value)) && value.trim() !== "")
        payload[key] = Number(value);
      else payload[key] = value;
    }
    putM.mutate(payload);
  };

  if (settingsQ.isLoading) return <p className="text-mute text-sm">loading…</p>;

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
                <input
                  id={key}
                  className="field"
                  value={draft[key] ?? ""}
                  onChange={(e) =>
                    setDraft((p) => ({ ...p, [key]: e.target.value }))
                  }
                />
              </div>
            ))}
          </section>
        );
      })}

      <div className="flex items-center gap-3 sticky bottom-2">
        <button className="btn-primary" disabled={putM.isPending} onClick={save}>
          {putM.isPending ? "saving…" : "save all"}
        </button>
        {savedMsg && (
          <span className="font-mono text-xs text-accent">{savedMsg}</span>
        )}
      </div>

      {promptQ.data !== undefined && <PromptEditor initial={promptQ.data.prompt} />}
      {correctionsQ.data !== undefined && <CorrectionsTable initial={correctionsQ.data} />}
      <ReferenceVoiceWidget />

      <section className="space-y-2 border-t border-line pt-6">
        <h2 className="font-mono uppercase text-xs text-dim">system info</h2>
        <ReadOnlyRow label="auth_enabled" value={String(authQ.data?.auth_enabled ?? "loading")} />
        <ReadOnlyRow label="logged_in" value={String(authQ.data?.logged_in ?? "loading")} />
        <ReadOnlyRow
          label="allowlist_keys"
          value={String(settingsQ.data?.allowlist.length ?? 0)}
        />
      </section>
    </div>
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
          {m.isPending ? "saving…" : "save prompt"}
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
              ×
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
          {m.isPending ? "saving…" : "save corrections"}
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
