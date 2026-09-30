import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import AudioPlayer from "./AudioPlayer";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(function (this: HTMLMediaElement) {
    this.dispatchEvent(new Event("pause"));
  });
});

function keyFor(src: string, revision: string): string {
  return `audicle:player:${encodeURIComponent(src)}:${encodeURIComponent(revision)}`;
}

function setMetadata(audio: HTMLAudioElement, duration: number): void {
  Object.defineProperty(audio, "duration", { configurable: true, value: duration });
  Object.defineProperty(audio, "readyState", { configurable: true, value: 1 });
  Object.defineProperty(audio, "currentTime", { configurable: true, writable: true, value: 0 });
  fireEvent.loadedMetadata(audio);
}

describe("AudioPlayer", () => {
  it("restores a saved position and speed, skips by 15 seconds, and clears position on completion", async () => {
    const user = userEvent.setup();
    const src = "/media/42.mp3";
    const revision = "42:updated-at-1";
    localStorage.setItem(keyFor(src, revision), JSON.stringify({ rate: 1.5, current: 20 }));
    const { container, unmount } = render(
      <AudioPlayer src={src} context="Morning news" persistenceKey={revision} />
    );
    const audio = container.querySelector("audio")!;
    setMetadata(audio, 120);
    expect(audio.currentTime).toBe(20);

    await user.click(screen.getByRole("button", { name: "Playback speed 1.5 times Morning news. Change speed" }));
    expect(audio.playbackRate).toBe(2);
    expect(JSON.parse(localStorage.getItem(keyFor(src, revision))!).rate).toBe(2);

    await user.click(screen.getByRole("button", { name: "Forward 15 seconds Morning news" }));
    expect(audio.currentTime).toBe(35);
    expect(JSON.parse(localStorage.getItem(keyFor(src, revision))!).current).toBe(35);

    fireEvent.ended(audio);
    expect(JSON.parse(localStorage.getItem(keyFor(src, revision))!).current).toBe(0);
    unmount();
    expect(JSON.parse(localStorage.getItem(keyFor(src, revision))!).current).toBe(0);
  });

  it("uses separate playback state for a new episode revision", () => {
    const src = "/media/42.mp3";
    const firstRevision = "42:updated-at-1";
    const secondRevision = "42:updated-at-2";
    localStorage.setItem(keyFor(src, firstRevision), JSON.stringify({ rate: 1.5, current: 20 }));
    localStorage.setItem(keyFor(src, secondRevision), JSON.stringify({ rate: 0.75, current: 55 }));
    const { container, rerender } = render(
      <AudioPlayer src={src} persistenceKey={firstRevision} />
    );
    const audio = container.querySelector("audio")!;
    setMetadata(audio, 120);
    expect(audio.currentTime).toBe(20);

    rerender(<AudioPlayer src={src} persistenceKey={secondRevision} />);
    const nextAudio = container.querySelector("audio")!;
    expect(nextAudio).not.toBe(audio);
    setMetadata(nextAudio, 120);
    expect(nextAudio.currentTime).toBe(55);
    expect(nextAudio.playbackRate).toBe(0.75);
  });

  it("does not carry an old revision's loaded position into a revision with no saved state", () => {
    const src = "/media/42.mp3";
    const firstRevision = "42:updated-at-1";
    const secondRevision = "42:updated-at-2";
    const { container, rerender } = render(
      <AudioPlayer src={src} persistenceKey={firstRevision} />
    );
    const firstAudio = container.querySelector("audio")!;
    setMetadata(firstAudio, 120);
    Object.defineProperty(firstAudio, "currentTime", { configurable: true, writable: true, value: 48 });

    rerender(<AudioPlayer src={src} persistenceKey={secondRevision} />);

    const secondAudio = container.querySelector("audio")!;
    expect(secondAudio).not.toBe(firstAudio);
    setMetadata(secondAudio, 120);
    expect(secondAudio.currentTime).toBe(0);
  });

  it("preserves the saved resume point when unmounted or changing speed before metadata", async () => {
    const user = userEvent.setup();
    const src = "/media/42.mp3";
    const revision = "42:updated-at-1";
    localStorage.setItem(keyFor(src, revision), JSON.stringify({ rate: 1, current: 32 }));
    const { unmount } = render(
      <AudioPlayer src={src} persistenceKey={revision} />
    );

    await user.click(screen.getByRole("button", { name: /Playback speed/ }));
    expect(JSON.parse(localStorage.getItem(keyFor(src, revision))!).current).toBe(32);
    unmount();
    expect(JSON.parse(localStorage.getItem(keyFor(src, revision))!).current).toBe(32);
  });

  it("throttles slider storage writes and flushes the final seek on blur", () => {
    const src = "/media/42.mp3";
    const revision = "42:updated-at-1";
    const { container } = render(<AudioPlayer src={src} persistenceKey={revision} />);
    const audio = container.querySelector("audio")!;
    setMetadata(audio, 120);
    const writes = vi.spyOn(Storage.prototype, "setItem");
    const seek = screen.getByRole("slider");

    fireEvent.change(seek, { target: { value: "40" } });
    fireEvent.change(seek, { target: { value: "50" } });
    expect(writes).toHaveBeenCalledTimes(1);
    Object.defineProperty(audio, "currentTime", { configurable: true, writable: true, value: 51 });
    fireEvent.blur(seek);

    expect(writes).toHaveBeenCalledTimes(2);
    expect(JSON.parse(localStorage.getItem(keyFor(src, revision))!).current).toBe(51);
  });

  it("does not flush a pending seek into the next revision's storage", async () => {
    const user = userEvent.setup();
    const src = "/media/42.mp3";
    const firstRevision = "42:updated-at-1";
    const secondRevision = "42:updated-at-2";
    localStorage.setItem(keyFor(src, firstRevision), JSON.stringify({ rate: 1, current: 20 }));
    localStorage.setItem(keyFor(src, secondRevision), JSON.stringify({ rate: 1, current: 10 }));
    const { container, rerender } = render(
      <AudioPlayer src={src} persistenceKey={firstRevision} />
    );
    let audio = container.querySelector("audio")!;
    setMetadata(audio, 120);
    await user.click(screen.getByRole("button", { name: /Playback speed/ }));
    fireEvent.change(screen.getByRole("slider"), { target: { value: "45" } });

    rerender(<AudioPlayer src={src} persistenceKey={secondRevision} />);
    audio = container.querySelector("audio")!;
    setMetadata(audio, 120);
    fireEvent.blur(screen.getByRole("slider"));

    expect(JSON.parse(localStorage.getItem(keyFor(src, secondRevision))!).current).toBe(10);
  });

  it("announces play rejection and keeps working if browser storage is blocked", async () => {
    const user = userEvent.setup();
    vi.spyOn(HTMLMediaElement.prototype, "play").mockRejectedValueOnce(new Error("blocked"));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Storage disabled", "SecurityError");
    });
    render(<AudioPlayer src="/media/42.mp3" context="Morning news" />);

    await user.click(screen.getByRole("button", { name: "Play Morning news" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to play Morning news.");
    await user.click(screen.getByRole("button", { name: "Forward 15 seconds Morning news" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Playback speed/ })).toBeEnabled());
  });
});
