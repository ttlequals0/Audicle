import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, SettingsPayload } from "../lib/api";

export default function SettingsRoute() {
  const qc = useQueryClient();
  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<SettingsPayload>("/api/v1/settings"),
  });

  const [draft, setDraft] = useState<Record<string, string>>({});
  const [savedMsg, setSavedMsg] = useState<string | null>(null);

  useEffect(() => {
    if (settingsQ.data) {
      const next: Record<string, string> = {};
      for (const key of settingsQ.data.allowlist) {
        const v = settingsQ.data.values[key];
        next[key] = v === undefined || v === null ? "" : String(v);
      }
      setDraft(next);
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
      // Best-effort type coercion. Server does its own coercion via the
      // Settings field annotation; this is just so booleans / numbers
      // travel as the right JSON shape.
      if (value === "true") payload[key] = true;
      else if (value === "false") payload[key] = false;
      else if (!Number.isNaN(Number(value)) && value.trim() !== "")
        payload[key] = Number(value);
      else payload[key] = value;
    }
    putM.mutate(payload);
  };

  return (
    <div className="space-y-6">
      {settingsQ.isLoading && <p className="text-mute text-sm">loading…</p>}
      {settingsQ.data && (
        <section className="space-y-4">
          {settingsQ.data.allowlist.map((key) => (
            <div key={key}>
              <label className="label" htmlFor={key}>
                {key}
              </label>
              <input
                id={key}
                className="field"
                value={draft[key] ?? ""}
                onChange={(e) =>
                  setDraft((prev) => ({ ...prev, [key]: e.target.value }))
                }
              />
            </div>
          ))}
          <div className="flex items-center gap-3">
            <button className="btn-primary" disabled={putM.isPending} onClick={save}>
              {putM.isPending ? "saving…" : "save"}
            </button>
            {savedMsg && (
              <span className="font-mono text-xs text-accent">{savedMsg}</span>
            )}
          </div>
          <p className="font-mono text-[10px] uppercase text-mute mt-4">
            settings stored in runtime_settings table; apply on next read.
          </p>
        </section>
      )}
    </div>
  );
}
