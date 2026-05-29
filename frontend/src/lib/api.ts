/**
 * Thin fetch wrapper that carries the CSRF token from the
 * `audicle_csrf` cookie on every mutating request.
 *
 * Authentication state lives in HTTP-only cookies (`audicle_session`),
 * managed by Starlette's SessionMiddleware. The CSRF cookie is
 * intentionally NOT HTTP-only so this helper can read it and echo it
 * into the `X-CSRF-Token` header per the double-submit pattern.
 */

const CSRF_COOKIE = "audicle_csrf";

function readCsrf(): string | null {
  const prefix = `${CSRF_COOKIE}=`;
  for (const piece of document.cookie.split(";")) {
    const trimmed = piece.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

type ApiOptions = Omit<RequestInit, "headers"> & {
  headers?: Record<string, string>;
};

export async function api<T = unknown>(
  path: string,
  options: ApiOptions = {}
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(options.headers ?? {}),
  };
  const method = (options.method ?? "GET").toUpperCase();
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = readCsrf();
    if (csrf) {
      headers["X-CSRF-Token"] = csrf;
    }
  }
  const response = await fetch(path, {
    ...options,
    method,
    headers,
    credentials: "include",
  });
  if (!response.ok) {
    let detail: unknown = await response.text();
    try {
      detail = JSON.parse(detail as string);
    } catch {
      /* keep text */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  const ct = response.headers.get("Content-Type") ?? "";
  if (ct.includes("application/json")) {
    return (await response.json()) as T;
  }
  return (await response.text()) as T;
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(`HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

export interface AuthStatus {
  auth_enabled: boolean;
  logged_in: boolean;
  username: string | null;
  csrf_token: string | null;
}

export interface Episode {
  id: string;
  title: string | null;
  author: string | null;
  original_url: string;
  audio_path: string | null;
  artwork_path: string | null;
  duration_secs: number | null;
  pub_date: string;
  updated_at: string;
}

export interface JobRow {
  id: string;
  url: string;
  episode_id: string;
  status: string;
  stage: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface SettingsPayload {
  allowlist: string[];
  values: Record<string, unknown>;
}
