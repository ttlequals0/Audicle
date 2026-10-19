import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, SettingsPayload } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useHealthLive } from "../lib/useHealthLive";
import Banner from "./Banner";

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
export default function OpenModeBanner({ pageWidth = "max-w-2xl" }: { pageWidth?: string }) {
  const { status } = useAuth();
  const health = useHealthLive();
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<SettingsPayload>("/api/v1/settings"),
  });
  const [dismissed, setDismissed] = useState(false);

  if (dismissed || !status || status.password_set) return null;

  const baseUrl = settings.data?.values.BASE_URL ?? settings.data?.defaults.BASE_URL ?? health.data?.base_url;
  const exposed = !isLocal(typeof baseUrl === "string" ? baseUrl : undefined);
  const tone = exposed
    ? "border-danger/60 bg-danger/10 text-danger"
    : "border-warning/50 bg-warning/10 text-warning";
  const message = exposed
    ? "No password set and this server looks internet-facing. Anyone who can reach it has full admin access. Set a password in Settings."
    : "No password set, so admin actions are open. Fine on your own machine; set one before you expose this.";

  return (
    <Banner
      tone={tone}
      pageWidth={pageWidth}
      action={
        <button
          className="text-sm underline shrink-0 hover:opacity-80"
          onClick={() => setDismissed(true)}
        >
          Dismiss
        </button>
      }
    >
      {message}
    </Banner>
  );
}
