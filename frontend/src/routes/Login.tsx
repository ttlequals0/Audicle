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
        if (e.status === 423) setError("account locked - wait and retry");
        else if (e.status === 401) setError("invalid password");
        else if (e.status === 400) setError("no password is set; auth is open");
        else setError(`error ${e.status}`);
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
    <form onSubmit={submit} className="card space-y-4 max-w-md mx-auto">
      <h1 className="font-mono uppercase text-sm text-accent">login</h1>
      <div>
        <label className="label" htmlFor="pw">
          password
        </label>
        <input
          id="pw"
          className="field"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      {error && <p className="text-danger text-xs font-mono">{error}</p>}
      <button
        type="submit"
        className="btn-primary w-full"
        disabled={loginM.isPending || !password}
      >
        {loginM.isPending ? "signing in..." : "sign in"}
      </button>
    </form>
  );
}
