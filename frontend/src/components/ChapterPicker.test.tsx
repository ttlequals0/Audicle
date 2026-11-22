import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { ApiError } from "../lib/api";
import ChapterPicker from "./ChapterPicker";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock("../lib/api", () => ({
  api: apiMock,
  ApiError: class ApiError extends Error {
    status: number;
    detail: unknown;
    constructor(status: number, detail: unknown) {
      super(`HTTP ${status}`);
      this.status = status;
      this.detail = detail;
    }
  },
}));

const ch = (index: number, over: Record<string, unknown> = {}) => ({
  index,
  title: `Chapter ${index}`,
  word_count: 1000,
  default_selected: false,
  selectable: true,
  requested: false,
  status: "not_queued",
  episode_id: null,
  ...over,
});

const book = {
  book_id: "b1",
  title: "Bad Blood",
  author: null,
  filename: "bb.epub",
  format: "epub",
  season_number: 1,
  has_cover: false,
  voice_id: null,
  created_at: "2026-01-01T00:00:00Z",
  counts: { not_queued: 0, queued: 0, processing: 0, done: 0, failed: 0, cancelled: 0, removed: 0 },
  can_resume: false,
  queued: [],
  skipped: [],
  chapters: [
    ch(1, { title: "Title Page", word_count: 4, selectable: false }),
    ch(2),
    ch(6, { default_selected: true }),
    ch(7, { default_selected: true, status: "failed" }),
    ch(8, { default_selected: true, status: "done" }),
  ],
};

function renderPicker(voice = "random", onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ChapterPicker bookId="b1" voice={voice} onClose={onClose} />
    </QueryClientProvider>
  );
  return onClose;
}

beforeEach(() => {
  apiMock.mockImplementation((path: string, options?: RequestInit) => {
    if (options?.method === "POST") return Promise.resolve({ ...book, queued: [6, 7] });
    if (path === "/api/v1/settings") return Promise.resolve({ values: {}, defaults: {} });
    return Promise.resolve(book);
  });
});

afterEach(() => {
  cleanup();
  apiMock.mockReset();
});

describe("ChapterPicker", () => {
  it("ticks default chapters that are not already done", async () => {
    renderPicker();
    expect(await screen.findByRole("checkbox", { name: /Chapter 6/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Chapter 7/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Chapter 2/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Chapter 8/ })).not.toBeChecked();
  });

  it("disables done and too-short rows", async () => {
    renderPicker();
    expect(await screen.findByRole("checkbox", { name: /Title Page/ })).toBeDisabled();
    expect(screen.getByText("too short")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Chapter 8/ })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /Chapter 7/ })).toBeEnabled();
  });

  it("Select all ticks only enabled rows", async () => {
    const user = userEvent.setup();
    renderPicker();
    await user.click(await screen.findByRole("button", { name: "Select all" }));
    expect(screen.getByRole("checkbox", { name: /Chapter 2/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Title Page/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Chapter 8/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Narrate 3 chapters" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "None" }));
    expect(screen.getByRole("button", { name: "Narrate 0 chapters" })).toBeDisabled();
  });

  it("posts the ticked indexes with the chosen voice and closes", async () => {
    const user = userEvent.setup();
    const onClose = renderPicker("3");
    await user.click(await screen.findByRole("button", { name: "Narrate 2 chapters" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const call = apiMock.mock.calls.find(([, o]) => o?.method === "POST");
    expect(call?.[0]).toBe("/api/v1/books/b1/chapters");
    expect(JSON.parse(call?.[1].body)).toEqual({ chapters: [6, 7], voice: "3" });
  });

  it("omits voice when random", async () => {
    const user = userEvent.setup();
    renderPicker("random");
    await user.click(await screen.findByRole("button", { name: "Narrate 2 chapters" }));
    await waitFor(() => expect(apiMock.mock.calls.some(([, o]) => o?.method === "POST")).toBe(true));
    const call = apiMock.mock.calls.find(([, o]) => o?.method === "POST");
    expect(JSON.parse(call?.[1].body)).toEqual({ chapters: [6, 7] });
  });

  describe("voice label", () => {
    const withVoice = (voiceId: string | null) =>
      apiMock.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/api/v1/settings"
            ? { values: {}, defaults: {} }
            : { ...book, voice_id: voiceId }
        )
      );

    it("shows the book's stored voice when Home is on random", async () => {
      withVoice("2");
      renderPicker("random");
      expect(await screen.findByText("Voice: slot 2 (book's voice)")).toBeInTheDocument();
    });

    it("shows random when the book has no voice yet", async () => {
      withVoice(null);
      renderPicker("random");
      expect(await screen.findByText("Voice: random")).toBeInTheDocument();
    });

    it("shows the Home choice when it is not random", async () => {
      withVoice("2");
      renderPicker("3");
      expect(await screen.findByText("Voice: slot 3")).toBeInTheDocument();
    });
  });

  it("moves focus into the picker when it opens", async () => {
    renderPicker();
    const dialog = await screen.findByRole("dialog", { name: "Bad Blood" });
    await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement));
  });

  it("describes a done chapter with its status", async () => {
    renderPicker();
    expect(await screen.findByRole("checkbox", { name: "Chapter 8" })).toHaveAccessibleDescription(
      /done/
    );
  });

  it("shows an error and re-enables Narrate when queueing fails", async () => {
    const user = userEvent.setup();
    apiMock.mockImplementation((path: string, options?: RequestInit) => {
      if (options?.method === "POST") return Promise.reject(new ApiError(500, "boom"));
      if (path === "/api/v1/settings") return Promise.resolve({ values: {}, defaults: {} });
      return Promise.resolve(book);
    });
    const onClose = renderPicker();
    await user.click(await screen.findByRole("button", { name: "Narrate 2 chapters" }));
    expect(await screen.findByText("queue failed (HTTP 500)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Narrate 2 chapters" })).toBeEnabled();
    expect(onClose).not.toHaveBeenCalled();
  });
});
