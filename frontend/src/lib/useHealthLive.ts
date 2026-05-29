import { useQuery } from "@tanstack/react-query";
import { api, HealthLive } from "./api";

// Shared liveness query. One queryKey so React Query dedupes the call across
// the header, Feed, and Settings; consistent freshness/poll in one place.
export function useHealthLive() {
  return useQuery({
    queryKey: ["health_live"],
    queryFn: () => api<HealthLive>("/health/live"),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
}
