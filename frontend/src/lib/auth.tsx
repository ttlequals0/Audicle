import { createContext, useContext, ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, AuthStatus } from "./api";

interface AuthContextValue {
  status: AuthStatus | null;
  loading: boolean;
  failed: boolean;
  refresh: () => void;
}

const AuthContext = createContext<AuthContextValue>({
  status: null,
  loading: true,
  failed: false,
  refresh: () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const q = useQuery({
    queryKey: ["auth_status"],
    queryFn: () => api<AuthStatus>("/api/v1/auth/status"),
    staleTime: 30_000,
  });
  return (
    <AuthContext.Provider
      value={{
        status: q.data ?? null,
        loading: q.isLoading,
        failed: q.isError && !q.data,
        refresh: () => q.refetch(),
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}
