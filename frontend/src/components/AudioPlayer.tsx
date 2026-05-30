import { useEffect, useRef, useState } from "react";

const SPEEDS = [0.75, 1, 1.25, 1.5, 2];

/**
 * Dependency-free inline player for an episode mp3: play/pause, a seek
 * scrubber bound to currentTime/duration, current/total time, and a
 * playback-speed cycle. Built on the HTML5 `<audio>` element + React refs.
 *
 * `pauseOthers` pauses every other Audicle player when this one starts, so
 * a feed of cards never plays two at once.
 */
export default function AudioPlayer({ src }: { src: string }) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(0);
  const [rate, setRate] = useState(1);

  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const onTime = () => setCurrent(el.currentTime);
    const onMeta = () => setDuration(el.duration || 0);
    // A cached mp3 can fire loadedmetadata before this effect attaches the
    // listener, leaving duration stuck at 0 and a dead scrubber. Sync from the
    // element if metadata (readyState >= HAVE_METADATA) is already available.
    if (el.readyState >= 1) {
      setDuration(el.duration || 0);
      setCurrent(el.currentTime);
    }
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onEnded = () => {
      setPlaying(false);
      setCurrent(0);
    };
    el.addEventListener("timeupdate", onTime);
    el.addEventListener("loadedmetadata", onMeta);
    el.addEventListener("play", onPlay);
    el.addEventListener("pause", onPause);
    el.addEventListener("ended", onEnded);
    return () => {
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("loadedmetadata", onMeta);
      el.removeEventListener("play", onPlay);
      el.removeEventListener("pause", onPause);
      el.removeEventListener("ended", onEnded);
    };
  }, []);

  const toggle = () => {
    const el = audioRef.current;
    if (!el) return;
    if (el.paused) {
      // Pause any other feed player going so only one card plays at a time.
      // Scoped to feed players so it never touches Settings' preview/test audio.
      document.querySelectorAll("audio[data-audicle-player]").forEach((other) => {
        if (other !== el) (other as HTMLAudioElement).pause();
      });
      void el.play();
    } else {
      el.pause();
    }
  };

  const seek = (value: number) => {
    const el = audioRef.current;
    if (!el) return;
    el.currentTime = value;
    setCurrent(value);
  };

  const cycleRate = () => {
    const el = audioRef.current;
    if (!el) return;
    const next = SPEEDS[(SPEEDS.indexOf(rate) + 1) % SPEEDS.length];
    el.playbackRate = next;
    setRate(next);
  };

  const pct = duration ? (current / duration) * 100 : 0;

  return (
    <div className="audio-player">
      <audio ref={audioRef} src={src} preload="metadata" data-audicle-player />
      <button
        className="audio-toggle"
        onClick={toggle}
        aria-label={playing ? "Pause" : "Play"}
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
      <input
        type="range"
        className="audio-scrub"
        min={0}
        max={duration || 0}
        step={0.1}
        value={current}
        onChange={(e) => seek(Number(e.target.value))}
        style={{ "--pct": `${pct}%` } as React.CSSProperties}
        aria-label="Seek"
      />
      <span className="mono-xs text-mute audio-time">
        {fmt(current)}/{fmt(duration)}
      </span>
      <button className="audio-rate mono-xs" onClick={cycleRate} aria-label="Playback speed">
        {rate}x
      </button>
    </div>
  );
}

function fmt(secs: number): string {
  if (!isFinite(secs) || secs <= 0) return "0:00";
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}
