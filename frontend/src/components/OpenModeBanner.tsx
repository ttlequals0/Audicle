import { useState } from "react";
import { useAuth } from "../lib/auth";
import { useHealthLive } from "../lib/useHealthLive";

function isLocal(baseUrl: string | undefined): boolean {
  if (!baseUrl) return true;
  try {
    const host = new URL(baseUrl).hostname.toLowerCase();
    return (
      host === "localhost" ||
      host === "127.0.0.1" ||
      host === "::1" ||
      host.endsWith(".localhost")
    );
  } catch {
    return true;
  }
}

// Shown only in convenience mode (no admin password). Dismiss is state-only, so
// it comes back on the next load until a password is set. Turns red when the
// server looks internet-facing (non-localhost BASE_URL).
export default function OpenModeBanner() {
  const { status } = useAuth();
  const health = useHealthLive();
  const [dismissed, setDismissed] = useState(false);

  if (dismissed || !status || status.password_set) return null;

  const exposed = !isLocal(health.data?.base_url);
  const tone = exposed
    ? "border-danger/60 bg-danger/10 text-danger"
    : "border-amber-400/50 bg-amber-400/10 text-amber-300";
  const message = exposed
    ? "No admin password set, and this server looks internet-facing. Every admin action is open to anyone who can reach it. Set a password in Settings now."
    : "No admin password set, so admin actions are open. Fine on your own machine; set a password before you expose this.";

  return (
    <div className={`border-b ${tone}`}>
      <div className="max-w-2xl mx-auto px-4 py-2 flex items-center justify-between gap-3">
        <span className="mono-xs">{message}</span>
        <button
          className="mono-xs underline shrink-0 hover:opacity-80"
          onClick={() => setDismissed(true)}
        >
          dismiss
        </button>
      </div>
    </div>
  );
}
