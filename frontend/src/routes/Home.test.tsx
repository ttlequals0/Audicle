import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import Home from "./Home";

const { apiMock, postFormMock } = vi.hoisted(() => ({ apiMock: vi.fn(), postFormMock: vi.fn() }));

vi.mock("../lib/api", () => ({
  api: apiMock,
  postForm: postFormMock,
  ApiError: class ApiError extends Error {
    status = 500;
    detail = "";
  },
}));

function renderHome() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Home />
    </QueryClientProvider>
  );
}

async function chooseFile(user: ReturnType<typeof userEvent.setup>, file: File) {
  await user.click(screen.getByRole("button", { name: "File" }));
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await user.upload(input, file);
}

beforeEach(() => {
  localStorage.clear();
  apiMock.mockImplementation((path: string) => {
    if (path === "/api/v1/settings") return Promise.resolve({ values: {}, defaults: {} });
    if (path === "/api/v1/reference/slots") {
      return Promise.resolve([{ slot: 1, filled: true, label: "A", duration_secs: 5 }]);
    }
    if (path.startsWith("/api/v1/jobs")) return Promise.resolve([]);
    if (path === "/api/v1/books") return Promise.resolve([]);
    return Promise.resolve({});
  });
  postFormMock.mockResolvedValue({ job_id: "j", episode_id: "e", chapters_detected: null });
});

afterEach(() => {
  cleanup();
  apiMock.mockReset();
  postFormMock.mockReset();
});

describe("Home split option", () => {
  it("hides the checkbox for a .txt selection", async () => {
    const user = userEvent.setup();
    renderHome();
    await chooseFile(user, new File(["hello"], "a.txt", { type: "text/plain" }));
    expect(screen.queryByLabelText("Split into chapters")).not.toBeInTheDocument();
  });

  it("shows the checkbox for .epub and sends split_chapters when ticked", async () => {
    const user = userEvent.setup();
    renderHome();
    await chooseFile(user, new File(["x"], "book.epub"));
    const box = screen.getByLabelText("Split into chapters");
    expect(box).not.toBeChecked();
    await user.click(box);
    await user.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(postFormMock).toHaveBeenCalled());
    const fd = postFormMock.mock.calls[0][1] as FormData;
    expect(fd.get("split_chapters")).toBe("true");
  });

  it("sends no split field when unticked", async () => {
    const user = userEvent.setup();
    renderHome();
    await chooseFile(user, new File(["x"], "book.epub"));
    await user.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(postFormMock).toHaveBeenCalled());
    const fd = postFormMock.mock.calls[0][1] as FormData;
    expect(fd.has("split_chapters")).toBe(false);
  });

  it("reports a split fallback and clears the file from the list", async () => {
    const user = userEvent.setup();
    postFormMock.mockResolvedValue({ job_id: "j", episode_id: "e", chapters_detected: false });
    renderHome();
    await chooseFile(user, new File(["x"], "book.pdf"));
    await user.click(screen.getByLabelText("Split into chapters"));
    await user.click(screen.getByRole("button", { name: "Submit" }));
    expect(
      await screen.findByText("book.pdf: no chapters found, queued as one episode")
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove book.pdf" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit" })).toBeDisabled();
  });

  it("posts a mixed batch and splits only the epub", async () => {
    const user = userEvent.setup();
    renderHome();
    await user.click(screen.getByRole("button", { name: "File" }));
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, [new File(["x"], "book.epub"), new File(["y"], "a.txt")]);
    await user.click(screen.getByLabelText("Split into chapters"));
    await user.click(screen.getByRole("button", { name: "Upload 2 files" }));
    await waitFor(() => expect(postFormMock).toHaveBeenCalledTimes(2));
    const sent = postFormMock.mock.calls.map(([, fd]) => fd as FormData);
    const epub = sent.find((fd) => (fd.get("file") as File).name === "book.epub");
    const txt = sent.find((fd) => (fd.get("file") as File).name === "a.txt");
    expect(epub?.get("split_chapters")).toBe("true");
    expect(txt?.has("split_chapters")).toBe(false);
  });

  it("starts unticked on a later epub selection after a split upload", async () => {
    const user = userEvent.setup();
    renderHome();
    await chooseFile(user, new File(["x"], "book.epub"));
    await user.click(screen.getByLabelText("Split into chapters"));
    await user.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(postFormMock).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.queryByLabelText("Split into chapters")).not.toBeInTheDocument()
    );
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, new File(["z"], "other.epub"));
    expect(screen.getByLabelText("Split into chapters")).not.toBeChecked();
  });
});

describe("Home book responses", () => {
  const book = {
    book_id: "nb",
    title: "My Book",
    author: null,
    filename: "book.epub",
    format: "epub",
    season_number: 1,
    has_cover: false,
    created_at: "2026-01-01T00:00:00Z",
    counts: {},
    can_resume: false,
    chapters: [],
    queued: [],
    skipped: [],
  };
  let booksFetches = 0;

  beforeEach(() => {
    booksFetches = 0;
    apiMock.mockImplementation((path: string) => {
      if (path === "/api/v1/settings") return Promise.resolve({ values: {}, defaults: {} });
      if (path === "/api/v1/reference/slots") {
        return Promise.resolve([{ slot: 1, filled: true, label: "A", duration_secs: 5 }]);
      }
      if (path.startsWith("/api/v1/jobs")) return Promise.resolve([]);
      if (path === "/api/v1/books") {
        booksFetches += 1;
        return Promise.resolve([book]);
      }
      if (path === "/api/v1/books/nb") return Promise.resolve(book);
      return Promise.resolve({});
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  async function uploadWithHttpStatus(status: number) {
    const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
    postFormMock.mockImplementation(actual.postForm);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(book), {
          status,
          headers: { "Content-Type": "application/json" },
        })
      )
    );
    const user = userEvent.setup();
    renderHome();
    await chooseFile(user, new File(["x"], "book.epub"));
    await user.click(screen.getByLabelText("Split into chapters"));
    await user.click(screen.getByRole("button", { name: "Submit" }));
    expect(await screen.findByRole("dialog", { name: "My Book" })).toBeInTheDocument();
    await waitFor(() => expect(booksFetches).toBeGreaterThanOrEqual(2));
  }

  it("opens the picker and refetches books on a 201 book response", () =>
    uploadWithHttpStatus(201));

  it("does the same when the book already existed (200)", () => uploadWithHttpStatus(200));
});
