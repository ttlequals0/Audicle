import { Routes, Route, NavLink, Navigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { AuthProvider, useAuth } from "./lib/auth";
import { api } from "./lib/api";
import Home from "./routes/Home";
import Feed from "./routes/Feed";
import SettingsRoute from "./routes/Settings";
import Login from "./routes/Login";

function Shell() {
  const { status, refresh } = useAuth();
  const logoutM = useMutation({
    mutationFn: () => api("/api/v1/auth/logout", { method: "POST" }),
    onSuccess: () => refresh(),
  });
  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-line">
        <div className="max-w-3xl mx-auto px-4 py-3 flex items-center justify-between">
          <NavLink to="/" className="font-mono uppercase tracking-wider text-accent">
            audicle
          </NavLink>
          {status?.password_set && status?.authenticated ? (
            <button
              className="btn-ghost"
              disabled={logoutM.isPending}
              onClick={() => logoutM.mutate()}
            >
              logout
            </button>
          ) : status?.password_set ? (
            <NavLink to="/login" className="btn-ghost">
              login
            </NavLink>
          ) : (
            <span className="font-mono text-[11px] text-dim">
              no password set
            </span>
          )}
        </div>
        <nav className="border-t border-line">
          <div className="max-w-3xl mx-auto flex">
            {[
              ["/", "home"],
              ["/feed", "feed"],
              ["/settings", "settings"],
            ].map(([to, label]) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                className={({ isActive }) =>
                  `flex-1 text-center py-3 font-mono uppercase text-[11px] tracking-wider transition ${
                    isActive
                      ? "text-accent border-b-2 border-accent"
                      : "text-mute"
                  }`
                }
              >
                {label}
              </NavLink>
            ))}
          </div>
        </nav>
      </header>
      <main className="flex-1 max-w-3xl mx-auto w-full px-4 py-6">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/feed" element={<Feed />} />
          <Route path="/settings" element={<SettingsRoute />} />
          <Route path="/login" element={<Login />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <footer className="border-t border-line py-4">
        <div className="max-w-3xl mx-auto px-4 font-mono text-[10px] uppercase text-mute">
          audicle &middot; self-hosted podcast pipeline
        </div>
      </footer>
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
