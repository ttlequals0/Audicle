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

  it("notes a fallback to one episode on the file row", async () => {
    const user = userEvent.setup();
    postFormMock.mockResolvedValue({ job_id: "j", episode_id: "e", chapters_detected: false });
    renderHome();
    await chooseFile(user, new File(["x"], "book.pdf"));
    await user.click(screen.getByLabelText("Split into chapters"));
    await user.click(screen.getByRole("button", { name: "Submit" }));
    expect(
      await screen.findByText("no chapters found, queued as one episode")
    ).toBeInTheDocument();
  });
});
