import { KeyboardEvent, useEffect, useId, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, Book, BookChapter } from "../lib/api";
import { isBookBusy, useBookPollMs } from "../lib/polling";

const LOCKED = ["done", "queued", "processing"];

const isEnabled = (c: BookChapter) => c.selectable && !LOCKED.includes(c.status);
const defaultTicks = (chapters: BookChapter[]) =>
  new Set(chapters.filter((c) => isEnabled(c) && c.default_selected).map((c) => c.index));

function statusTag(status: string): string {
  switch (status) {
    case "done":
      return "tag-done";
    case "failed":
      return "tag-failed";
    case "cancelled":
    case "removed":
      return "tag-cancelled";
    case "processing":
      return "tag-processing";
    default:
      return "tag-queued";
  }
}

interface Props {
  bookId: string;
  voice: string;
  onClose: () => void;
}

export default function ChapterPicker({ bookId, voice, onClose }: Props) {
  const qc = useQueryClient();
  const pollMs = useBookPollMs();
  const uid = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => headingRef.current?.focus(), []);
  const bookQ = useQuery({
    queryKey: ["books", bookId],
    queryFn: () => api<Book>(`/api/v1/books/${bookId}`),
    refetchInterval: (q) => (q.state.data && isBookBusy(q.state.data) ? pollMs : false),
  });
  // null until the operator edits, so polling never overwrites their ticks.
  const [edited, setEdited] = useState<Set<number> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const book = bookQ.data;
  const chapters = book?.chapters ?? [];
  const byIndex = new Map(chapters.map((c) => [c.index, c]));
  // Drop ticks on chapters that became locked while the picker was open.
  const ticked = [...(edited ?? defaultTicks(chapters))].filter((i) => {
    const c = byIndex.get(i);
    return c !== undefined && isEnabled(c);
  });

  const queueM = useMutation({
    mutationFn: (indexes: number[]) =>
      api<Book>(`/api/v1/books/${bookId}/chapters`, {
        method: "POST",
        body: JSON.stringify(
          voice === "random" ? { chapters: indexes } : { chapters: indexes, voice }
        ),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["books"] });
      qc.invalidateQueries({ queryKey: ["jobs"] });
      onClose();
    },
    onError: (e) =>
      setError(e instanceof ApiError ? `queue failed (HTTP ${e.status})` : (e as Error).message),
  });

  const toggle = (index: number) => {
    const next = new Set(ticked);
    if (next.has(index)) next.delete(index);
    else next.add(index);
    setEdited(next);
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Escape") onClose();
  };

  const selectAll = () => setEdited(new Set(chapters.filter(isEnabled).map((c) => c.index)));
  const selectDefaults = () => setEdited(defaultTicks(chapters));
  const selectNone = () => setEdited(new Set());
  const submitSelection = () => queueM.mutate([...ticked].sort((a, b) => a - b));

  const voiceLabel = voice === "random" ? "random" : voice === "last" ? "last used" : `slot ${voice}`;

  return (
    <section
      role="dialog"
      aria-label={book?.title ?? "Chapters"}
      className="card p-4 mt-3"
      onKeyDown={onKeyDown}
    >
      <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
        <h3 ref={headingRef} tabIndex={-1} className="text-sm font-bold truncate min-w-0">
          {book?.title ?? "Loading..."}
        </h3>
        <div className="flex items-center gap-2">
          <button type="button" className="btn-ghost text-xs" onClick={selectAll}>
            Select all
          </button>
          <button type="button" className="btn-ghost text-xs" onClick={selectDefaults}>
            Defaults
          </button>
          <button type="button" className="btn-ghost text-xs" onClick={selectNone}>
            None
          </button>
        </div>
      </div>
      {bookQ.isError && (
        <p className="text-danger text-sm" role="alert">
          Chapters could not be loaded.
        </p>
      )}
      <ul className="space-y-1 max-h-96 overflow-y-auto">
        {chapters.map((c) => {
          const enabled = isEnabled(c);
          const note = !c.selectable ? "too short" : c.status !== "not_queued" ? c.status : null;
          return (
            <li key={c.index} className={`flex items-center gap-3 text-sm py-1${enabled ? "" : " text-mute"}`}>
              <input
                id={`${uid}-c${c.index}`}
                type="checkbox"
                checked={ticked.includes(c.index)}
                disabled={!enabled}
                onChange={() => toggle(c.index)}
                aria-describedby={`${uid}-d${c.index}`}
              />
              <span className="text-xs text-mute w-6 shrink-0 text-right">{c.index}</span>
              <label
                htmlFor={`${uid}-c${c.index}`}
                className={`flex-1 min-w-0 truncate${enabled ? " cursor-pointer" : ""}`}
              >
                {c.title}
              </label>
              <span id={`${uid}-d${c.index}`} className="text-xs text-mute shrink-0">
                {c.word_count} words
                {note && (
                  <span className={`tag ${c.selectable ? statusTag(c.status) : "tag-cancelled"} ml-2`}>
                    {note}
                  </span>
                )}
              </span>
            </li>
          );
        })}
      </ul>
      {error && (
        <p className="text-danger text-sm mt-2 break-words" role="alert">
          {error}
        </p>
      )}
      <div className="flex items-center justify-between gap-3 mt-3 flex-wrap">
        <span className="text-xs text-mute">Voice: {voiceLabel}</span>
        <div className="flex items-center gap-2">
          <button type="button" className="btn-ghost text-xs" onClick={onClose}>
            Close
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={ticked.length === 0 || queueM.isPending}
            onClick={submitSelection}
          >
            {`Narrate ${ticked.length} chapter${ticked.length === 1 ? "" : "s"}`}
          </button>
        </div>
      </div>
    </section>
  );
}
