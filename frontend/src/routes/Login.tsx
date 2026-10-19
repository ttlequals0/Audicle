import { useState, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";

interface LoginResponse {
  authenticated: boolean;
  password_set: boolean;
  csrf_token: string;
}

export default function Login() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const { refresh } = useAuth();
  const navigate = useNavigate();

  const loginM = useMutation({
    mutationFn: () =>
      api<LoginResponse>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ password }),
      }),
    onSuccess: () => {
      setError(null);
      refresh();
      navigate("/", { replace: true });
    },
    onError: (e) => {
      if (e instanceof ApiError) {
        if (e.status === 423) setError("Account locked. Wait and try again.");
        else if (e.status === 401) setError("Invalid password. Try again.");
        else if (e.status === 400) setError("Sign-in could not be completed. Check your password or reload the page.");
        else setError(`Sign-in failed (HTTP ${e.status}). Try again.`);
      } else {
        setError((e as Error).message);
      }
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    loginM.mutate();
  };

  return (
    <form onSubmit={submit} className="card p-6 space-y-4 max-w-md mx-auto mt-10">
      <h1 className="text-2xl font-black tracking-tight">Sign in</h1>
      <div>
        <label className="label" htmlFor="pw">
          Password
        </label>
        <input
          id="pw"
          className="field"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => {
            setPassword(e.target.value);
            setError(null);
          }}
        />
      </div>
      {error && <p className="text-danger text-sm" role="alert">{error}</p>}
      <button
        type="submit"
        className="btn-primary w-full"
        disabled={loginM.isPending || !password}
      >
        {loginM.isPending ? "Signing in..." : "Sign in"}
      </button>
    </form>
  );
}
