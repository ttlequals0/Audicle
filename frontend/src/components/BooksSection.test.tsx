import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import BooksSection from "./BooksSection";

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

const makeBook = (over: Record<string, unknown> = {}) => ({
  book_id: "b1",
  title: "Bad Blood",
  author: "John Carreyrou",
  filename: "bb.epub",
  format: "epub",
  season_number: 2,
  has_cover: false,
  created_at: "2026-01-01T00:00:00Z",
  counts: { done: 12, queued: 2, failed: 1 },
  can_resume: true,
  chapters: [],
  queued: [],
  skipped: [],
  ...over,
});

let books: unknown[] = [];
let deleteStatus = 204;

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BooksSection voice="random" openBookId={null} onOpenBook={vi.fn()} />
    </QueryClientProvider>
  );
}

beforeEach(async () => {
  books = [makeBook()];
  deleteStatus = 204;
  const { ApiError } = await import("../lib/api");
  apiMock.mockImplementation((path: string, options?: RequestInit) => {
    if (path === "/api/v1/settings") return Promise.resolve({ values: {}, defaults: {} });
    if (options?.method === "DELETE") {
      return deleteStatus === 204
        ? Promise.resolve(undefined)
        : Promise.reject(new ApiError(deleteStatus, "in flight"));
    }
    if (options?.method === "POST") return Promise.resolve(makeBook({ queued: [3] }));
    if (path === "/api/v1/books") return Promise.resolve(books);
    return Promise.resolve({});
  });
});

afterEach(() => {
  cleanup();
  apiMock.mockReset();
  vi.restoreAllMocks();
});

describe("BooksSection", () => {
  it("renders nothing when there are no books", async () => {
    books = [];
    renderSection();
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/api/v1/books"));
    expect(screen.queryByText("Books")).not.toBeInTheDocument();
  });

  it("renders the title, author, season and status summary", async () => {
    renderSection();
    expect(await screen.findByText("Bad Blood")).toBeInTheDocument();
    expect(screen.getByText(/John Carreyrou/)).toBeInTheDocument();
    expect(screen.getByText(/Season 2/)).toBeInTheDocument();
    expect(screen.getByText("12 done, 2 queued, 1 failed")).toBeInTheDocument();
  });

  it("shows not started when nothing is requested", async () => {
    books = [makeBook({ counts: {}, can_resume: false })];
    renderSection();
    expect(await screen.findByText("not started")).toBeInTheDocument();
  });

  it("hides Resume unless can_resume", async () => {
    books = [makeBook({ can_resume: false })];
    renderSection();
    await screen.findByText("Bad Blood");
    expect(screen.queryByRole("button", { name: /Resume/ })).not.toBeInTheDocument();
  });

  it("Resume posts remaining without a voice", async () => {
    const user = userEvent.setup();
    renderSection();
    await user.click(await screen.findByRole("button", { name: /Resume/ }));
    await waitFor(() =>
      expect(apiMock.mock.calls.some(([, o]) => o?.method === "POST")).toBe(true)
    );
    const call = apiMock.mock.calls.find(([, o]) => o?.method === "POST");
    expect(call?.[0]).toBe("/api/v1/books/b1/chapters");
    expect(JSON.parse(call?.[1].body)).toEqual({ chapters: "remaining" });
  });

  it("shows the processing message when Delete returns 409", async () => {
    const user = userEvent.setup();
    deleteStatus = 409;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderSection();
    await user.click(await screen.findByRole("button", { name: /Delete/ }));
    expect(await screen.findByText("chapters are still processing")).toBeInTheDocument();
  });

  it("does not delete when confirm is declined", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderSection();
    await user.click(await screen.findByRole("button", { name: /Delete/ }));
    expect(apiMock.mock.calls.some(([, o]) => o?.method === "DELETE")).toBe(false);
  });
});
