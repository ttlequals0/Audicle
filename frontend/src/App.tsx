import { Routes, Route, NavLink, Navigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { AuthProvider, useAuth } from "./lib/auth";
import { api } from "./lib/api";
import { useHealthLive } from "./lib/useHealthLive";
import Home from "./routes/Home";
import Feed from "./routes/Feed";
import SettingsRoute from "./routes/Settings";
import Login from "./routes/Login";

// Audicle mark: a custom "A" with a five-bar audio waveform for the crossbar.
// Geometry from branding/mark-mono.svg (currentColor so it inherits text-accent).
function Mark({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 160 160" width="28" height="28" className={className} aria-label="Audicle">
      <path
        d="M 30 130 L 80 26 L 130 130"
        stroke="currentColor"
        strokeWidth="10"
        fill="none"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      {[
        [56, 100],
        [68, 86],
        [80, 74],
        [92, 86],
        [104, 100],
      ].map(([x, y]) => (
        <line
          key={x}
          x1={x}
          y1={y}
          x2={x}
          y2={112}
          stroke="currentColor"
          strokeWidth="6"
          strokeLinecap="round"
        />
      ))}
    </svg>
  );
}

const TABS: [string, string][] = [
  ["/", "Home"],
  ["/feed", "Feed"],
  ["/settings", "Settings"],
];

function Shell() {
  const { status, refresh } = useAuth();
  const healthQ = useHealthLive();
  const logoutM = useMutation({
    mutationFn: () => api("/api/v1/auth/logout", { method: "POST" }),
    onSuccess: () => refresh(),
  });

  return (
    <div className="min-h-screen flex flex-col">
      <header className="safe-top sticky top-0 z-20 bg-ink/95 backdrop-blur-md border-b border-line">
        <div className="max-w-2xl mx-auto px-4">
          <div className="flex items-center justify-between py-3.5">
            <NavLink to="/" className="flex items-center gap-2.5 text-accent">
              <Mark className="flex-shrink-0" />
              <span className="wordmark text-lg text-fg">audicle</span>
            </NavLink>
            <div className="flex items-center gap-3">
              {status?.password_set && status?.authenticated && (
                <button
                  className="mono-xs text-mute hover:text-fg"
                  disabled={logoutM.isPending}
                  onClick={() => logoutM.mutate()}
                >
                  logout
                </button>
              )}
              {status?.password_set && !status?.authenticated && (
                <NavLink to="/login" className="mono-xs text-mute hover:text-fg">
                  login
                </NavLink>
              )}
              <a
                href="https://github.com/ttlequals0/Audicle"
                target="_blank"
                rel="noreferrer"
                className="mono-xs text-mute hover:text-fg"
              >
                {healthQ.data ? `v${healthQ.data.version}` : ""}
              </a>
            </div>
          </div>
          <nav className="flex">
            {TABS.map(([to, label]) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                className={({ isActive }) => `tab-btn ${isActive ? "active" : ""}`}
              >
                {label}
              </NavLink>
            ))}
          </nav>
        </div>
        <div className="accent-line" />
      </header>

      <main className="flex-1 max-w-2xl mx-auto w-full px-4 py-6 relative z-10 safe-bottom">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/feed" element={<Feed />} />
          <Route path="/settings" element={<SettingsRoute />} />
          <Route path="/login" element={<Login />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  );
}
