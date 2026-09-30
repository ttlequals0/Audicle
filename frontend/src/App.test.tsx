import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import App from "./App";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock("./lib/api", () => ({ api: apiMock }));
vi.mock("./lib/useHealthLive", () => ({ useHealthLive: () => ({ data: { version: "test" } }) }));
vi.mock("./components/PullToRefresh", () => ({ default: () => null }));
vi.mock("./components/OpenModeBanner", () => ({ default: () => null }));
vi.mock("./routes/Home", () => ({ default: () => <h1>Home page</h1> }));
vi.mock("./routes/Feed", () => ({ default: () => <h1>Feed page</h1> }));
vi.mock("./routes/Settings", () => ({ default: () => <h1>Settings page</h1> }));

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <App />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  apiMock.mockImplementation((path: string) => {
    if (path === "/api/v1/auth/status") return Promise.resolve({ password_set: true, authenticated: true });
    return Promise.resolve({});
  });
});

afterEach(() => {
  cleanup();
  apiMock.mockReset();
  vi.restoreAllMocks();
});

describe("App main focus", () => {
  it("returns to the top before focusing the main landmark after route changes", async () => {
    const user = userEvent.setup();
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    renderApp();

    await screen.findByRole("heading", { name: "Home page" });
    await user.click(screen.getByRole("link", { name: "Settings" }));

    const main = screen.getByRole("main");
    await waitFor(() => expect(main).toHaveFocus());
    expect(scroll).toHaveBeenCalledWith({ top: 0, behavior: "auto" });
  });

  it("keeps the skip-link main focus below the sticky header", async () => {
    const user = userEvent.setup();
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    renderApp();

    await screen.findByRole("heading", { name: "Home page" });
    await user.click(screen.getByRole("link", { name: "Skip to main content" }));

    expect(screen.getByRole("main")).toHaveFocus();
    expect(scroll).toHaveBeenCalledWith({ top: 0, behavior: "auto" });
  });
});
