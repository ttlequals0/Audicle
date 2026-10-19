import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import UpdateBanner from "./UpdateBanner";
import { offerAppUpdate } from "../lib/appUpdate";
import { useUnsavedChanges } from "../lib/useUnsavedChanges";

function Draft({ dirty = true }: { dirty?: boolean }) {
  useUnsavedChanges(dirty);
  return null;
}

afterEach(() => {
  cleanup();
  offerAppUpdate(null);
});

describe("UpdateBanner", () => {
  it("renders nothing before an update is offered", () => {
    render(<UpdateBanner pageWidth="max-w-2xl" />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("offers a reload that runs the update", async () => {
    const update = vi.fn();
    render(<UpdateBanner pageWidth="max-w-2xl" />);
    act(() => {
      offerAppUpdate(update);
    });

    expect(await screen.findByText("An app update is ready.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reload to update" }));
    expect(update).toHaveBeenCalledTimes(1);
  });

  it("disables the reload while a draft is unsaved", () => {
    render(
      <>
        <Draft />
        <UpdateBanner pageWidth="max-w-2xl" />
      </>
    );
    act(() => {
      offerAppUpdate(vi.fn());
    });

    expect(screen.getByText("Update ready. Save your changes before reloading.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload to update" })).toBeDisabled();
  });

  it("re-enables the reload once the draft is clean again", () => {
    const { rerender } = render(
      <>
        <Draft dirty />
        <UpdateBanner pageWidth="max-w-2xl" />
      </>
    );
    act(() => {
      offerAppUpdate(vi.fn());
    });
    expect(screen.getByRole("button", { name: "Reload to update" })).toBeDisabled();

    rerender(
      <>
        <Draft dirty={false} />
        <UpdateBanner pageWidth="max-w-2xl" />
      </>
    );
    expect(screen.getByRole("button", { name: "Reload to update" })).toBeEnabled();
  });
});
