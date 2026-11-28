import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, Book } from "../lib/api";
import { isBookBusy, useBookPollMs } from "../lib/polling";
import ChapterPicker from "./ChapterPicker";
import { usePersistentOpen } from "./CollapsibleSection";

const SUMMARY_ORDER = [
  "done",
  "processing",
  "queued",
  "failed",
  "cancelled",
  "removed",
];

function summary(b: Book): string {
  const parts = SUMMARY_ORDER.filter((s) => b.counts[s] > 0).map(
    (s) => `${b.counts[s]} ${s}`,
  );
  return parts.length ? parts.join(", ") : "not started";
}

interface Props {
  voice: string;
  openBookId: string | null;
  onOpenBook: (id: string | null) => void;
}

export default function BooksSection({ voice, openBookId, onOpenBook }: Props) {
  const qc = useQueryClient();
  const pollMs = useBookPollMs();
  const booksQ = useQuery({
    queryKey: ["books"],
    queryFn: () => api<Book[]>("/api/v1/books"),
    refetchInterval: (q) => (q.state.data?.some(isBookBusy) ? pollMs : false),
  });
  const [msg, setMsg] = useState<string | null>(null);
  const [open, setOpen] = usePersistentOpen("home.books.open", false);
  const toggleRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const closePicker = (id: string) => {
    onOpenBook(null);
    toggleRefs.current[id]?.focus();
  };

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["books"] });
    qc.invalidateQueries({ queryKey: ["jobs"] });
  };
  const resumeM = useMutation({
    mutationFn: (id: string) =>
      api<Book>(`/api/v1/books/${id}/chapters`, {
        method: "POST",
        body: JSON.stringify({ chapters: "remaining" }),
      }),
    onSuccess: () => {
      setMsg(null);
      refresh();
    },
    onError: (e) =>
      setMsg(
        `resume failed${e instanceof ApiError ? ` (HTTP ${e.status})` : ""}`,
      ),
  });
  const deleteM = useMutation({
    mutationFn: (id: string) =>
      api(`/api/v1/books/${id}`, { method: "DELETE" }),
    onSuccess: (_r, id) => {
      setMsg(null);
      if (openBookId === id) onOpenBook(null);
      refresh();
    },
    onError: (e) =>
      setMsg(
        e instanceof ApiError && e.status === 409
          ? "chapters are still queued or processing"
          : `delete failed${e instanceof ApiError ? ` (HTTP ${e.status})` : ""}`,
      ),
  });

  const books = booksQ.data ?? [];
  if (!books.length) return null;
  // A picker opened by Home (e.g. after an upload) forces the list visible.
  const expanded = open || openBookId !== null;

  return (
    <section className="mt-8" aria-label="Books">
      <div className="mb-3 flex items-center justify-between gap-3">
        <button
          type="button"
          className="text-xs text-mute flex items-center gap-1.5 hover:text-fg"
          onClick={() => {
            if (expanded) {
              setOpen(false);
              onOpenBook(null);
            } else {
              setOpen(true);
            }
          }}
          aria-expanded={expanded}
        >
          <span
            className={`transition-transform ${expanded ? "rotate-90" : ""}`}
          >
            &rsaquo;
          </span>
          Books ({books.length})
        </button>
      </div>
      {msg && (
        <p className="text-xs text-danger mb-2" role="alert">
          {msg}
        </p>
      )}
      {expanded && (
        <ul className="space-y-2">
          {books.map((b) => (
            <li key={b.book_id} className="card p-4">
              <p className="text-sm break-words">{b.title}</p>
              <p className="text-xs text-mute mt-1">
                {b.author ? `${b.author}   ` : ""}Season {b.season_number}
                {"   "}
                <span className="text-dim">{summary(b)}</span>
              </p>
              <div className="flex items-center gap-2 mt-3 flex-wrap">
                <button
                  type="button"
                  ref={(el) => {
                    toggleRefs.current[b.book_id] = el;
                  }}
                  className="btn-ghost text-xs"
                  aria-expanded={openBookId === b.book_id}
                  onClick={() =>
                    openBookId === b.book_id
                      ? closePicker(b.book_id)
                      : onOpenBook(b.book_id)
                  }
                  aria-label={`Choose chapters for ${b.title}`}
                >
                  Choose chapters
                </button>
                {b.can_resume && (
                  <button
                    type="button"
                    className="btn-ghost text-xs"
                    disabled={
                      resumeM.isPending && resumeM.variables === b.book_id
                    }
                    onClick={() => resumeM.mutate(b.book_id)}
                    aria-label={`Resume ${b.title}`}
                  >
                    Resume
                  </button>
                )}
                <button
                  type="button"
                  className="btn-ghost text-xs"
                  disabled={
                    deleteM.isPending && deleteM.variables === b.book_id
                  }
                  onClick={() => {
                    if (
                      confirm(
                        `Delete "${b.title}" and its stored file? Episodes already made stay in the feed.`,
                      )
                    )
                      deleteM.mutate(b.book_id);
                  }}
                  aria-label={`Delete ${b.title}`}
                >
                  Delete
                </button>
              </div>
              {openBookId === b.book_id && (
                <ChapterPicker
                  bookId={b.book_id}
                  voice={voice}
                  onClose={() => closePicker(b.book_id)}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
