import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import SettingsRoute from "./Settings";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock("../lib/api", () => ({
  api: apiMock,
  postForm: vi.fn(),
  readCsrf: vi.fn(() => null),
  ApiError: class ApiError extends Error {
    status = 500;
    detail = "";
  },
}));

const settingKey = "LLM_TIMEOUT_SECONDS";
const settings = {
  allowlist: [settingKey],
  values: {},
  defaults: { [settingKey]: 60 },
  feed_url: "",
};

let sidecarOnline = false;

function sidecarState(available: boolean) {
  return {
    available,
    values: {},
    defaults: available ? { RENDER_NAV_TIMEOUT_MS: 12000 } : {},
    pending: false,
  };
}

function createClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
}

function renderSettings(client = createClient(), category = "services") {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/settings?category=${category}`]}>
        <SettingsRoute />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return client;
}

beforeEach(() => {
  localStorage.clear();
  sidecarOnline = false;
  apiMock.mockImplementation((path: string, options?: RequestInit) => {
    if (options?.method === "PUT" && path === "/api/v1/settings") {
      return Promise.resolve(settings);
    }
    if (path === "/api/v1/settings") return Promise.resolve(settings);
    if (path === "/api/v1/settings/sidecars") {
      const state = sidecarState(sidecarOnline);
      return Promise.resolve({ render: state, tts_wrapper: state });
    }
    if (path === "/api/v1/prompt") return Promise.resolve({ prompt: "" });
    if (path === "/api/v1/corrections") return Promise.resolve({});
    if (path === "/api/v1/source-fallbacks") {
      return Promise.resolve({ default_proxy: "", min_chars: 0, rules: [], available_proxies: [], builtin: [] });
    }
    if (path === "/api/v1/settings/ocr/languages") return Promise.resolve({ languages: ["en"], default: "en" });
    if (path === "/api/v1/tts/models") return Promise.resolve({ active: null, models: [] });
    if (path.startsWith("/api/v1/jobs")) return Promise.resolve([]);
    if (path === "/health/live") return Promise.resolve({ version: "test", uptime_seconds: 0 });
    return Promise.resolve({});
  });
});

afterEach(() => {
  cleanup();
  apiMock.mockReset();
});

describe("SettingsRoute saves", () => {
  it("shows an error instead of treating an empty numeric setting as saved", async () => {
    const user = userEvent.setup();
    renderSettings();

    const field = await screen.findByLabelText("LLM timeout (seconds)");
    fireEvent.change(field, { target: { value: "" } });
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("must contain a number");
    expect(apiMock).not.toHaveBeenCalledWith("/api/v1/settings", expect.objectContaining({ method: "PUT" }));

    fireEvent.change(field, { target: { value: "60" } });
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Save settings" })).not.toBeInTheDocument();
  });

  it("keeps edits made during a save dirty until a later save", async () => {
    const user = userEvent.setup();
    let resolveSave!: (value: typeof settings) => void;
    apiMock.mockImplementation((path: string, options?: RequestInit) => {
      if (path === "/api/v1/settings" && options?.method === "PUT") {
        return new Promise((resolve) => { resolveSave = resolve; });
      }
      if (path === "/api/v1/settings") return Promise.resolve(settings);
      if (path === "/api/v1/prompt") return Promise.resolve({ prompt: "" });
      if (path === "/api/v1/corrections") return Promise.resolve({});
      if (path === "/api/v1/source-fallbacks") return Promise.resolve({ default_proxy: "", min_chars: 0, rules: [], available_proxies: [], builtin: [] });
      if (path === "/api/v1/settings/ocr/languages") return Promise.resolve({ languages: ["en"], default: "en" });
      if (path === "/api/v1/tts/models") return Promise.resolve({ active: null, models: [] });
      if (path.startsWith("/api/v1/jobs")) return Promise.resolve([]);
      return Promise.resolve({});
    });
    renderSettings();

    const field = await screen.findByLabelText("LLM timeout (seconds)");
    fireEvent.change(field, { target: { value: "10" } });
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(resolveSave).toBeDefined());

    fireEvent.change(field, { target: { value: "15" } });
    resolveSave(settings);

    expect(await screen.findByText("1 unsaved change")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save settings" })).toBeEnabled();
  });

  it("loads sidecar defaults when an offline sidecar becomes available", async () => {
    const user = userEvent.setup();
    const client = renderSettings();

    await user.click(await screen.findByRole("button", { name: /sidecar settings/i }));
    expect(await screen.findAllByText(/Sidecar is offline/)).toHaveLength(2);

    sidecarOnline = true;
    await client.invalidateQueries({ queryKey: ["sidecar-settings"] });

    await waitFor(() =>
      expect(screen.getByLabelText("Page navigation timeout (ms)")).toHaveValue(12000)
    );
  });

  it("keeps sidecar edits made while a save is in flight", async () => {
    const user = userEvent.setup();
    sidecarOnline = true;
    let resolveSave!: (value: { render: ReturnType<typeof sidecarState>; tts_wrapper: ReturnType<typeof sidecarState> }) => void;
    apiMock.mockImplementation((path: string, options?: RequestInit) => {
      if (path === "/api/v1/settings/sidecars" && options?.method === "PUT") {
        return new Promise((resolve) => { resolveSave = resolve; });
      }
      if (path === "/api/v1/settings/sidecars") {
        const state = sidecarState(sidecarOnline);
        return Promise.resolve({ render: state, tts_wrapper: state });
      }
      if (path === "/api/v1/settings") return Promise.resolve(settings);
      if (path === "/api/v1/prompt") return Promise.resolve({ prompt: "" });
      if (path === "/api/v1/corrections") return Promise.resolve({});
      if (path === "/api/v1/source-fallbacks") return Promise.resolve({ default_proxy: "", min_chars: 0, rules: [], available_proxies: [], builtin: [] });
      if (path === "/api/v1/settings/ocr/languages") return Promise.resolve({ languages: ["en"], default: "en" });
      if (path === "/api/v1/tts/models") return Promise.resolve({ active: null, models: [] });
      if (path.startsWith("/api/v1/jobs")) return Promise.resolve([]);
      return Promise.resolve({});
    });
    renderSettings();

    await user.click(await screen.findByRole("button", { name: /sidecar settings/i }));
    const field = await screen.findByLabelText("Page navigation timeout (ms)");
    fireEvent.change(field, { target: { value: "13000" } });
    await user.click(screen.getByRole("button", { name: "Save sidecar settings" }));
    await waitFor(() => expect(resolveSave).toBeDefined());

    fireEvent.change(field, { target: { value: "14000" } });
    await waitFor(() => expect(field).toHaveValue(14000));
    await act(async () => resolveSave({ render: sidecarState(true), tts_wrapper: sidecarState(true) }));

    await waitFor(() => expect(screen.getByLabelText("Page navigation timeout (ms)")).toHaveValue(14000));
    expect(screen.getByRole("button", { name: "Save sidecar settings" })).toBeEnabled();
  });

  it("saves and resets chapter density controls in the existing settings form", async () => {
    const user = userEvent.setup();
    const chapterDefaults = {
      CHAPTERS_TARGET_SECONDS: 600,
      CHAPTERS_WINDOW_SECONDS: 2700,
      CHAPTERS_MAX_BOUNDARIES: 40,
      CHAPTERS_MIN_CHAPTER_SECONDS: 180,
    };
    const chapterSettings = {
      ...settings,
      allowlist: Object.keys(chapterDefaults),
      defaults: chapterDefaults,
      values: {} as Record<string, number>,
    };
    const implementation = apiMock.getMockImplementation()!;
    apiMock.mockImplementation((path: string, options?: RequestInit) => {
      if (path === "/api/v1/settings") {
        if (options?.method === "PUT") {
          for (const [key, value] of Object.entries(JSON.parse(String(options.body)))) {
            if (value === null) delete chapterSettings.values[key];
            else chapterSettings.values[key] = value as number;
          }
        }
        return Promise.resolve({ ...chapterSettings, values: { ...chapterSettings.values } });
      }
      return implementation(path, options);
    });
    renderSettings(createClient(), "publishing");
    const sectionButton = await screen.findByRole("button", { name: /^Chapters/ });
    await user.click(sectionButton);
    const target = await screen.findByLabelText("Target chapter length (seconds)");
    expect(target).toHaveValue("600");
    expect(screen.getByLabelText("Analysis window (seconds)")).toHaveValue("2700");
    expect(screen.getByLabelText("Maximum topic changes")).toHaveValue("40");
    expect(screen.getByLabelText("Minimum chapter length (seconds)")).toHaveValue("180");
    fireEvent.change(target, { target: { value: "300" } });
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/api/v1/settings", {
      method: "PUT", body: JSON.stringify({ CHAPTERS_TARGET_SECONDS: 300 }),
    }));
    await waitFor(() => expect(target).toHaveValue("300"));
    const reset = within(sectionButton.closest("section")!).getByRole("button", { name: /reset to defaults/i });
    await waitFor(() => expect(reset).toBeEnabled());
    await user.click(reset);
    expect(target).toHaveValue("600");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/api/v1/settings", {
      method: "PUT", body: JSON.stringify({ CHAPTERS_TARGET_SECONDS: null }),
    }));
  });
});
