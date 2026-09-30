import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import OpenModeBanner from "./OpenModeBanner";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock("../lib/api", () => ({ api: apiMock }));
vi.mock("../lib/auth", () => ({
  useAuth: () => ({ status: { password_set: false } }),
}));
vi.mock("../lib/useHealthLive", () => ({
  useHealthLive: () => ({ data: { base_url: "https://legacy-env.example" } }),
}));

afterEach(() => {
  cleanup();
  apiMock.mockReset();
});

describe("OpenModeBanner", () => {
  it("uses the effective runtime base URL rather than the env-only health URL", async () => {
    apiMock.mockResolvedValue({
      allowlist: ["BASE_URL"],
      values: { BASE_URL: "http://localhost:8000" },
      defaults: { BASE_URL: "https://legacy-env.example" },
      feed_url: "",
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <OpenModeBanner />
      </QueryClientProvider>
    );

    expect(await screen.findByText(/Fine on your own machine/)).toBeInTheDocument();
    expect(screen.queryByText(/looks internet-facing/)).not.toBeInTheDocument();
  });
});
