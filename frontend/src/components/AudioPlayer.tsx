import { useEffect, useRef, useState } from "react";
import { formatEpisodeDuration } from "../lib/format";

const SPEEDS = [0.75, 1, 1.25, 1.5, 2];
const POSITION_SAVE_INTERVAL_MS = 5000;

interface SavedPlayback {
  rate: number;
  current: number;
}

export default function AudioPlayer({
  src,
  context,
  persistenceKey = "",
}: {
  src: string;
  context?: string;
  persistenceKey?: string;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const lastSavedAt = useRef(0);
  const completed = useRef(false);
  const restored = useRef(false);
  const pendingPosition = useRef<{ storageKey: string; value: number } | null>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(0);
  const [rate, setRate] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const storageKey = `audicle:player:${encodeURIComponent(src)}:${encodeURIComponent(persistenceKey)}`;

  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    let saved: SavedPlayback = { rate: 1, current: 0 };
    try {
      const raw = localStorage.getItem(storageKey);
      const parsed = raw ? (JSON.parse(raw) as Partial<SavedPlayback>) : null;
      if (parsed && SPEEDS.includes(parsed.rate ?? NaN)) saved.rate = parsed.rate!;
      if (parsed && Number.isFinite(parsed.current) && parsed.current! > 0) {
        saved.current = parsed.current!;
      }
    } catch {
      // Storage can be disabled or contain stale data.
    }
    el.playbackRate = saved.rate;
    setRate(saved.rate);
    setCurrent(0);
    setDuration(0);
    setPlaying(false);
    setError(null);
    lastSavedAt.current = 0;
    completed.current = false;
    restored.current = false;
    pendingPosition.current = null;

    const save = (position: number) => {
      try {
        localStorage.setItem(storageKey, JSON.stringify({ rate: el.playbackRate, current: position }));
        lastSavedAt.current = Date.now();
        pendingPosition.current = null;
      } catch {
        // Storage can be disabled or full.
      }
    };
    const restorePosition = () => {
      if (restored.current) return;
      restored.current = true;
      const nextDuration = Number.isFinite(el.duration) ? el.duration : 0;
      setDuration(nextDuration);
      if (saved.current > 0 && nextDuration > 0 && saved.current < nextDuration) {
        el.currentTime = saved.current;
        setCurrent(saved.current);
      } else {
        setCurrent(el.currentTime);
      }
    };
    const onTime = () => {
      setCurrent(el.currentTime);
      if (!completed.current && Date.now() - lastSavedAt.current >= POSITION_SAVE_INTERVAL_MS) {
        save(el.currentTime);
      }
    };
    const onPlay = () => {
      completed.current = false;
      setPlaying(true);
      setError(null);
    };
    const onPause = () => {
      setPlaying(false);
      save(completed.current ? 0 : el.currentTime);
    };
    const onEnded = () => {
      completed.current = true;
      setPlaying(false);
      setCurrent(0);
      save(0);
    };
    const onError = () => {
      setPlaying(false);
      setError(`Unable to play${context ? ` ${context}` : " this episode"}.`);
    };
    if (el.readyState >= 1) restorePosition();
    el.addEventListener("timeupdate", onTime);
    el.addEventListener("loadedmetadata", restorePosition);
    el.addEventListener("play", onPlay);
    el.addEventListener("pause", onPause);
    el.addEventListener("ended", onEnded);
    el.addEventListener("error", onError);
    return () => {
      if (restored.current) save(completed.current ? 0 : el.currentTime);
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("loadedmetadata", restorePosition);
      el.removeEventListener("play", onPlay);
      el.removeEventListener("pause", onPause);
      el.removeEventListener("ended", onEnded);
      el.removeEventListener("error", onError);
    };
  }, [src, storageKey, context]);

  const toggle = () => {
    const el = audioRef.current;
    if (!el) return;
    if (el.paused) {
      document.querySelectorAll("audio[data-audicle-player]").forEach((other) => {
        if (other !== el) (other as HTMLAudioElement).pause();
      });
      setError(null);
      void el.play().catch(() => {
        setPlaying(false);
        setError(`Unable to play${context ? ` ${context}` : " this episode"}.`);
      });
    } else {
      el.pause();
    }
  };

  const seek = (value: number) => {
    const el = audioRef.current;
    if (!el) return;
    el.currentTime = value;
    completed.current = false;
    restored.current = true;
    setCurrent(value);
    pendingPosition.current = { storageKey, value };
    if (Date.now() - lastSavedAt.current >= POSITION_SAVE_INTERVAL_MS) flushSeek();
  };

  const flushSeek = () => {
    const el = audioRef.current;
    const pending = pendingPosition.current;
    if (!el || !pending) return;
    if (pending.storageKey !== storageKey) {
      pendingPosition.current = null;
      return;
    }
    try {
      const raw = localStorage.getItem(storageKey);
      const saved = raw ? (JSON.parse(raw) as Partial<SavedPlayback>) : {};
      localStorage.setItem(
        storageKey,
        JSON.stringify({ rate: saved.rate ?? el.playbackRate, current: el.currentTime })
      );
      lastSavedAt.current = Date.now();
      pendingPosition.current = null;
    } catch {
      // Storage can be disabled or contain stale data.
    }
  };

  const skip = (delta: number) => {
    const el = audioRef.current;
    if (!el) return;
    seek(Math.min(Math.max(0, el.currentTime + delta), duration || Number.MAX_SAFE_INTEGER));
    flushSeek();
  };

  const cycleRate = () => {
    const el = audioRef.current;
    if (!el) return;
    const next = SPEEDS[(SPEEDS.indexOf(rate) + 1) % SPEEDS.length];
    el.playbackRate = next;
    setRate(next);
    try {
      const raw = localStorage.getItem(storageKey);
      const saved = raw ? (JSON.parse(raw) as Partial<SavedPlayback>) : {};
      localStorage.setItem(
        storageKey,
        JSON.stringify({
          ...saved,
          rate: next,
          current: restored.current ? el.currentTime : saved.current ?? 0,
        })
      );
      lastSavedAt.current = Date.now();
    } catch {
      // Storage can be disabled or stale.
    }
  };

  const pct = duration ? (current / duration) * 100 : 0;
  const accessibleContext = context ? ` ${context}` : "";

  return (
    <div className="audio-player">
      <audio key={`${src}:${persistenceKey}`} ref={audioRef} src={src} preload="metadata" data-audicle-player />
      <button
        className="audio-toggle"
        onClick={toggle}
        aria-label={`${playing ? "Pause" : "Play"}${accessibleContext}`}
      >
        {playing ? (
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <rect x="6" y="5" width="4" height="14" rx="1" fill="currentColor" />
            <rect x="14" y="5" width="4" height="14" rx="1" fill="currentColor" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path d="M7 5l12 7-12 7z" fill="currentColor" />
          </svg>
        )}
      </button>
      <button className="audio-skip" onClick={() => skip(-15)} aria-label={`Back 15 seconds${accessibleContext}`}>
        -15
      </button>
      <input
        type="range"
        className="audio-scrub"
        min={0}
        max={duration || 0}
        step={0.1}
        value={current}
        onChange={(e) => seek(Number(e.target.value))}
        onPointerUp={flushSeek}
        onKeyUp={flushSeek}
        onBlur={flushSeek}
        style={{ "--pct": `${pct}%` } as React.CSSProperties}
        aria-label={`Seek${accessibleContext}`}
        aria-valuetext={`${formatEpisodeDuration(current)} of ${formatEpisodeDuration(duration)}`}
      />
      <button className="audio-skip" onClick={() => skip(15)} aria-label={`Forward 15 seconds${accessibleContext}`}>
        +15
      </button>
      <span className="mono-xs text-mute audio-time">
        {formatEpisodeDuration(current)}/{formatEpisodeDuration(duration)}
      </span>
      <button
        className="audio-rate mono-xs"
        onClick={cycleRate}
        aria-label={`Playback speed ${rate} times${accessibleContext}. Change speed`}
      >
        {rate}x
      </button>
      {error && <p className="audio-error" role="alert">{error}</p>}
    </div>
  );
}
