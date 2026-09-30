import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import ActionMenu from "./ActionMenu";

afterEach(cleanup);

describe("ActionMenu keyboard access", () => {
  it("moves focus through menu items and restores it after Escape", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    render(
      <>
        <button>Before</button>
        <ActionMenu
          label="View"
          context="Morning news"
          actions={[
            { label: "Transcript", hint: "timed text", run: onRun },
            { label: "Chapters", hint: "chapter data", href: "/chapters" },
          ]}
        />
        <button>After</button>
      </>
    );

    const trigger = screen.getByRole("button", { name: "View actions for Morning news" });
    await user.click(trigger);
    const items = screen.getAllByRole("menuitem");
    expect(items[0]).toHaveFocus();

    await user.keyboard("{ArrowDown}");
    expect(items[1]).toHaveFocus();
    await user.keyboard("{Home}");
    expect(items[0]).toHaveFocus();
    await user.keyboard("{End}");
    expect(items[1]).toHaveFocus();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(onRun).not.toHaveBeenCalled();
  });

  it("dismisses on Tab and focuses the next control after the trigger", async () => {
    const user = userEvent.setup();
    render(
      <>
        <button>Before</button>
        <ActionMenu actions={[{ label: "Reprocess", hint: "run again", run: vi.fn() }]} />
        <button>After</button>
      </>
    );

    const trigger = screen.getByRole("button", { name: "Redo" });
    await user.click(trigger);
    await user.keyboard("{Tab}");

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "After" })).toHaveFocus();
  });
});
