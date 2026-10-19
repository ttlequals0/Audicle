import { useState, useEffect, useRef, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  postForm,
  readCsrf,
  FeedAuthStatus,
  LlmConnectionTestResponse,
  LlmModelsResponse,
  SettingsPayload,
  SidecarSettingsPayload,
  VoiceSlot,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { useHealthLive } from "../lib/useHealthLive";
import { useUnsavedChanges } from "../lib/useUnsavedChanges";
import CollapsibleSection from "../components/CollapsibleSection";
import { SettingsSearchContext } from "../components/SettingsSearchContext";

/**
 * Operator-facing setting groups, plus the prompt
 * editor (which talks to /api/v1/prompt), the corrections table (which
 * talks to /api/v1/corrections), and the voice-slots widget
 * (against /api/v1/reference/slots). System info is a
 * read-only block.
 */

// Allowlisted runtime keys rendered as the generic settings form, grouped. Most
// booleans render here as a Toggle (see the render loop below). The exception is
// CHIME_ENABLED: it lives in the dedicated ChimeWidget so its toggle sits next to
// the clip upload/preview and the enabled/disabled status -- keep it OUT of this map.
const GROUPS: Record<string, string[]> = {
  LLM: [
    "LLM_PROVIDER",
    "LLM_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "OLLAMA_BASE_URL",
    "LLM_TEMPERATURE",
    "LLM_REASONING_EFFORT",
    "LLM_MAX_TOKENS",
    "LLM_TIMEOUT_SECONDS",
    "LLM_RETRY_COUNT",
    "LLM_PRONUNCIATION_CONCURRENCY",
    "PRONUNCIATION_SCOPE",
  ],
  Feed: [
    "FEED_TITLE",
    "FEED_DESCRIPTION",
    "FEED_AUTHOR",
    "FEED_EMAIL",
    "FEED_LANGUAGE",
    "FEED_CATEGORY",
    "FEED_EXPLICIT",
    "FEED_ARTWORK_URL",
  ],
  Connections: [
    "BASE_URL",
    "UI_BASE_URL",
    "FIRECRAWL_URL",
    "FIRECRAWL_API_KEY",
    "TTS_URL",
    "FLARESOLVERR_URL",
    "RENDER_URL",
    "READER_PROXY_TEMPLATE",
    "READER_API_KEY",
  ],
  // Per-request budgets for the extraction cascade and the webhook. Opened to
  // runtime in 0.55.0: a slow upstream is exactly when these need changing.
  Timeouts: [
    "FIRECRAWL_TIMEOUT_SECONDS",
    "FIRECRAWL_RETRY_COUNT",
    "FIRECRAWL_BACKOFF_BASE_SECONDS",
    "RENDER_TIMEOUT_SECONDS",
    "FLARESOLVERR_MAX_TIMEOUT_MS",
    "WAYBACK_TIMEOUT_SECONDS",
    "WEBHOOK_TIMEOUT_SECONDS",
  ],
  Extraction: [
    "EXTRACTION_ENGINE",
    "EXTRACTION_DIRECT_TIMEOUT_SECONDS",
    "EXTRACTION_DIRECT_USER_AGENT",
    "EXTRACTION_ARC_ENABLED",
    "EXTRACTION_FALLBACKS_ENABLED",
    "READER_AUTO_ENABLED",
    "ARCHIVE_FALLBACK_ENABLED",
    "MIN_EXTRACTION_CHARS",
    "REGISTRATION_EMAIL",
    "FIRECRAWL_ONLY_MAIN_CONTENT",
    "FIRECRAWL_REMOVE_BASE64_IMAGES",
    "FIRECRAWL_EXCLUDE_TAGS",
  ],
  Webhooks: ["WEBHOOK_URL"],
  TTS: [
    "TTS_MODEL",
    "TTS_LANGUAGE",
    "TTS_CHUNK_TARGET_WORDS",
    "TTS_CHUNK_MAX_WORDS",
    "TTS_CHUNK_MAX_CHARS",
    "TTS_CHUNK_SILENCE_MS",
  ],
  "TTS generation": [
    "CHATTERBOX_TEMPERATURE",
    "CHATTERBOX_REPETITION_PENALTY",
    "CHATTERBOX_TOP_P",
    "CHATTERBOX_TOP_K",
    "CHATTERBOX_SEED",
    "CHATTERBOX_MAX_CHARS",
    "TTS_CHUNK_CACHE_ENABLED",
    "TTS_CACHE_RETENTION_DAYS",
    "TTS_ADAPTIVE_MAX_CHARS_ENABLED",
  ],
  // Wrapper resilience: reachable at runtime since 0.55.0 so an operator can
  // widen the budget during an incident instead of redeploying.
  // Which TTS implementation runs, and where ASR verification happens. The two
  // are independent: local synthesis with remote ASR is a valid pairing.
  "TTS backend": [
    "TTS_BACKEND",
    "TTS_API_BASE_URL",
    "TTS_API_MODEL",
    "TTS_API_VOICE",
    "TTS_API_KEY",
    "WHISPER_BACKEND",
    "WHISPER_API_BASE_URL",
    "WHISPER_API_MODEL",
    "WHISPER_API_KEY",
    "WHISPER_API_TIMEOUT_SECONDS",
  ],
  "TTS delivery": [
    "TTS_RETRY_COUNT",
    "TTS_CONNECT_RETRY_MAX_SECONDS",
    "TTS_HTTP_TIMEOUT_SECONDS",
    "TTS_REACHABILITY_GRACE_SECONDS",
    "TTS_REACHABILITY_PROBE_TIMEOUT",
    "TTS_IDLE_RESTART_ENABLED",
  ],
  Verification: [
    "WHISPER_VERIFY_ENABLED",
    "WHISPER_DIVERGENCE_THRESHOLD",
    "WHISPER_MAX_DIVERGENT_RUN",
    "WHISPER_VERIFY_MIN_WORDS",
    "WHISPER_SHORT_CHUNK_DIVERGENCE",
    "WHISPER_API_STRICT",
  ],
  "Audio analysis": [
    "AUDIO_ANALYSIS_ENABLED",
    "AUDIO_ANALYSIS_MAX_REGEN",
    "AUDIO_ANALYSIS_MIN_RMS_CV",
    "AUDIO_ANALYSIS_MIN_CREST",
    "AUDIO_ANALYSIS_MAX_ZCR",
    "AUDIO_ANALYSIS_MAX_F0_SEMITONES",
    "AUDIO_ANALYSIS_MAX_SILENT_FRACTION",
    "AUDIO_ANALYSIS_WORDS_PER_SEC",
    "AUDIO_ANALYSIS_DURATION_OVERHEAD_SECS",
    "AUDIO_ANALYSIS_MAX_DURATION_RATIO",
    "AUDIO_ANALYSIS_MIN_DURATION_RATIO",
    "AUDIO_ANALYSIS_REGEN_CHARS_FACTOR",
    "AUDIO_ANALYSIS_REGEN_MIN_CHARS",
    "AUDIO_ANALYSIS_REGEN_PENALTY_STEP",
    "AUDIO_ANALYSIS_FRAME_MS",
    "AUDIO_ANALYSIS_HOP_MS",
    "AUDIO_ANALYSIS_WINDOW_SECS",
    "AUDIO_ANALYSIS_F0_WARMUP_CHUNKS",
  ],
  // Output encode + silence handling, opened to runtime in 0.55.0.
  "Audio output": [
    "MP3_BITRATE",
    "MP3_SAMPLE_RATE",
    "MP3_CHANNELS",
    "LOUDNORM_TARGET_LUFS",
    "LOUDNORM_TRUE_PEAK_DB",
    "LOUDNORM_LRA",
    "AUDIO_SILENCE_THRESHOLD",
    "AUDIO_SILENCE_BUFFER_MS",
    "AUDIO_MAX_INTERNAL_SILENCE_MS",
    "AUDIO_INTERNAL_SILENCE_KEEP_MS",
    "INTRO_READ_ENABLED",
  ],
  Chapters: [
    "CHAPTERS_ENABLED",
    "CHAPTERS_MIN_DURATION_SECS",
    "CHAPTERS_TARGET_SECONDS",
    "CHAPTERS_WINDOW_SECONDS",
    "CHAPTERS_MAX_BOUNDARIES",
    "CHAPTERS_MIN_CHAPTER_SECONDS",
  ],
  Artwork: [
    "DEFAULT_ARTWORK_URL",
    "ARTWORK_SIZE_PX",
    "EMBED_ARTWORK_SIZE_PX",
    "ARTWORK_JPG_QUALITY",
    "ARTWORK_FETCH_TIMEOUT_SECONDS",
    "ARTWORK_MIN_SOURCE_PX",
    "ARTWORK_MAX_DOWNLOAD_BYTES",
  ],
  Cleanup: [
    "LLM_CLEANUP_WINDOW_CHARS",
    "MIN_CLEANUP_CHARS",
    "CLEANUP_MIN_RETENTION_RATIO",
    "MAX_PROMPT_LENGTH_BYTES",
    "MAX_CORRECTIONS_ENTRIES",
    "LEXICON_AGGRESSIVE",
  ],
  Uploads: [
    "UPLOAD_MAX_MB",
    "OCR_ENABLED",
    "OCR_MAX_PAGES",
    "OCR_DPI",
    "OCR_MIN_CONFIDENCE",
    "OCR_LANGUAGE",
  ],
  "Job timeouts": [
    "JOB_STALL_SECONDS",
    "JOB_TIMEOUT_SECONDS",
    "JOB_TIMEOUT_PER_CHUNK_SECONDS",
    "JOB_TIMEOUT_CEILING_MULTIPLIER",
  ],
  Retention: ["RETENTION_DAYS", "RETENTION_SWEEP_HOUR_UTC", "MIGRATION_BACKUP_RETENTION_DAYS"],
  Logging: ["LOG_LEVEL", "LOG_FORMAT"],
  RSS: ["RSS_CACHE_MAX_AGE_SECONDS"],
  Operations: ["QUEUE_POLL_INTERVAL_SECONDS"],
  Security: [
    "LOGIN_RATE_LIMIT",
    "LOCKOUT_MAX_FAILED_ATTEMPTS",
    "LOCKOUT_WINDOW_SECONDS",
    "TRUST_PROXY_HEADERS",
    "TRUSTED_PROXY_HOPS",
    "SESSION_COOKIE_SECURE",
    "SESSION_COOKIE_MAX_AGE_SECONDS",
  ],
};

// Groups arranged into categories, ordered by how often an operator touches
// them. Presentation only: no key changes group, so nothing about how a setting
// is stored or read is affected. Every GROUPS entry must appear exactly once
// here (asserted below) so a new group can't be added and silently not render.
// The hand-written sections below the generic form. Listed so the search can
// match them by title; their bodies are widgets, not settings keys.
const STANDALONE_SECTIONS = [
  "Cleanup prompt",
  "Pronunciation corrections",
  "Site overrides",
  "Voices",
  "End chime",
  "Authenticated feeds",
  "Admin access",
  "System info",
  "Sidecar settings",
];

// Settings arranged by the question an operator is answering, not by which
// mechanism happens to store them. Entries name either a GROUPS key or one of
// the hand-written widget sections, and both render inline here: leaving the
// widgets to trail the page was what made "Voices" impossible to find under
// Voice and turned the last category into a junk drawer.
const CATEGORIES: { name: string; description: string; entries: string[] }[] = [
  {
    // Getting the article and preparing its text.
    name: "Content",
    description: "Article extraction, cleanup and uploads",
    entries: ["Extraction", "Site overrides", "Cleanup", "Cleanup prompt", "Uploads"],
  },
  {
    // Everything that shapes how it sounds, in the order you set it up:
    // pick a voice, choose the engine, tune it, check the result, shape output.
    name: "Voice",
    description: "Narration, pronunciation and audio quality",
    entries: [
      "Voices",
      "TTS",
      "TTS backend",
      "TTS generation",
      "Pronunciation corrections",
      "Verification",
      "Audio analysis",
      "Audio output",
      "End chime",
      "TTS delivery",
    ],
  },
  {
    // The podcast as subscribers receive it.
    name: "Publishing",
    description: "Podcast details, artwork and subscribers",
    entries: ["Feed", "Artwork", "Chapters", "RSS", "Authenticated feeds", "Retention"],
  },
  {
    // The services this app talks to. Named "Services" rather than
    // "Integrations" because one of its own groups is called Connections, and
    // two near-synonyms at adjacent levels of the same hierarchy just make the
    // reader work out whether they mean different things. "Services" is also
    // what a self-hoster already calls these.
    name: "Services",
    description: "AI providers, connections and integrations",
    entries: ["LLM", "Connections", "Timeouts", "Webhooks", "Sidecar settings"],
  },
  {
    // Running the app itself. Small on purpose: anything that belongs to a
    // subject above lives there instead.
    name: "System",
    description: "Admin access, job limits and diagnostics",
    entries: ["Job timeouts", "Operations", "Logging", "Security", "Admin access", "System info"],
  },
];

const SECTION_SUMMARIES: Record<string, string> = {
  Extraction: "Choose how articles are fetched and when to try a fallback.",
  "Site overrides": "Set extraction rules for individual websites.",
  Cleanup: "Control text cleanup and minimum article length.",
  "Cleanup prompt": "Edit the instructions used to prepare articles for narration.",
  Uploads: "Set file size limits and text recognition for scanned documents.",
  Voices: "Upload and preview the reference voices used for narration.",
  TTS: "Choose a speech model, language and chunk size.",
  "TTS backend": "Choose where speech generation and verification run.",
  "TTS generation": "Tune Chatterbox sampling, caching and chunk sizing.",
  "Pronunciation corrections": "Teach the narrator how to say names and acronyms.",
  Verification: "Compare narration with the source text and retry mismatches.",
  "Audio analysis": "Detect noise, silence and pacing problems.",
  "Audio output": "Set MP3 encoding, loudness and silence handling.",
  "End chime": "Upload or preview the sound at the end of each episode.",
  "TTS delivery": "Set retry budgets and wrapper restart behavior.",
  Feed: "Set your podcast title, description, author and cover image.",
  Artwork: "Configure fallback artwork, image sizes and quality.",
  Chapters: "Control chapter detection and target chapter length.",
  RSS: "Set how long podcast apps can cache the feed.",
  "Authenticated feeds": "Manage the access key used by podcast subscribers.",
  Retention: "Choose when old episodes and backups are deleted.",
  LLM: "Choose your AI provider, model and API credentials.",
  Connections: "Set public URLs and connect extraction and speech services.",
  Timeouts: "Set request time limits for upstream services.",
  Webhooks: "Send episode notifications to another service.",
  "Sidecar settings": "Configure the browser renderer and speech wrapper.",
  "Job timeouts": "Set limits for stalled jobs and total processing time.",
  Operations: "Set how often the worker checks the queue.",
  Logging: "Choose the log level and output format.",
  Security: "Configure login limits, trusted proxies and session cookies.",
  "Admin access": "Set or change the password for the admin interface.",
  "System info": "View the app version, uptime and API documentation.",
};

// A setting that belongs to the rule row above it: indented, tied to it by an accent line.
const RULE_DETAIL = "block ml-3 pl-3.5 border-l-2 border-accent/35";
const RULE_DETAIL_LABEL = "block mb-1.5 text-mute text-[0.8125rem]";

const SETTING_LABELS: Record<string, string> = {
  LLM_CLEANUP_WINDOW_CHARS: "Cleanup window (characters)",
  CHAPTERS_MIN_DURATION_SECS: "Minimum episode length (seconds)",
  CHAPTERS_TARGET_SECONDS: "Target chapter length (seconds)",
  CHAPTERS_WINDOW_SECONDS: "Analysis window (seconds)",
  CHAPTERS_MAX_BOUNDARIES: "Maximum topic changes",
  CHAPTERS_MIN_CHAPTER_SECONDS: "Minimum chapter length (seconds)",
  EXTRACTION_DIRECT_TIMEOUT_SECONDS: "Direct fetch timeout (seconds)",
  EXTRACTION_ARC_TIMEOUT_SECONDS: "Archive fetch timeout (seconds)",
  EXTRACTION_RENDER_TIMEOUT_SECONDS: "Browser render timeout (seconds)",
  UPLOAD_MAX_MB: "Maximum upload size (MB)",
  QUEUE_POLL_INTERVAL_SECONDS: "Queue polling interval (seconds)",
  RETENTION_SWEEP_HOUR_UTC: "Retention cleanup hour (UTC)",
  MIGRATION_BACKUP_RETENTION_DAYS: "Migration backup retention (days)",
  LOGIN_RATE_LIMIT: "Login rate limit (attempts per minute)",
  LOCKOUT_MAX_FAILED_ATTEMPTS: "Failed login attempts before lockout",
  LOCKOUT_WINDOW_SECONDS: "Login lockout window (seconds)",
  TRUST_PROXY_HEADERS: "Trust proxy headers",
  TRUSTED_PROXY_HOPS: "Trusted proxy hops",
  SESSION_COOKIE_SECURE: "Secure session cookie",
  SESSION_COOKIE_MAX_AGE_SECONDS: "Session lifetime (seconds)",
  BASE_URL: "Public feed base URL",
  UI_BASE_URL: "Admin interface base URL",
  DEFAULT_ARTWORK_URL: "Default feed artwork URL",
  LOG_FORMAT: "Log format",
};

const ACRONYMS: Record<string, string> = {
  API: "API",
  CPU: "CPU",
  GPU: "GPU",
  HTTP: "HTTP",
  ID: "ID",
  LLM: "LLM",
  MB: "MB",
  OCR: "OCR",
  RSS: "RSS",
  TTS: "TTS",
  URL: "URL",
  UTC: "UTC",
};

function settingLabel(key: string): string {
  if (SETTING_LABELS[key]) return SETTING_LABELS[key];
  const rawWords = key.split("_");
  const unit = ["SECONDS", "MINUTES", "HOURS", "DAYS", "MB", "GB", "KB", "BYTES", "PERCENT"].includes(
    rawWords[rawWords.length - 1]
  )
    ? rawWords.pop()
    : null;
  const words = rawWords.map((word) => ACRONYMS[word] ?? word.toLowerCase());
  return (
    words.map((word, index) => (index === 0 ? word[0].toUpperCase() + word.slice(1) : word)).join(" ") +
    (unit ? ` (${unit.toLowerCase()})` : "")
  );
}

function feedbackRole(message: string): "alert" | "status" {
  return /failed|error|cannot|incorrect|unable|invalid|must be/i.test(message) ? "alert" : "status";
}

function FeedbackMsg({ msg, as: Tag = "span" }: { msg: string | null; as?: "span" | "p" }) {
  if (!msg) return null;
  const role = feedbackRole(msg);
  return <Tag className={`text-sm ${role === "alert" ? "text-danger" : "text-accent"}`} role={role}>{msg}</Tag>;
}

const SIDECAR_GROUPS = [
  {
    key: "render",
    title: "Render sidecar",
    keys: [
      "RENDER_NAV_TIMEOUT_MS",
      "RENDER_CLICK_TIMEOUT_MS",
      "RENDER_GROW_WAIT_MS",
      "RENDER_ATTEMPTS",
      "RENDER_SETTLE_POLL_MS",
      "RENDER_SETTLE_QUIET_POLLS",
      "RENDER_SETTLE_MAX_MS",
      "RENDER_BUDGET_SECONDS",
      "LOG_LEVEL",
      "LOG_FORMAT",
    ],
    note: "Browser extraction settings apply to the next article. Logging changes apply live.",
  },
  {
    key: "tts_wrapper",
    title: "TTS wrapper",
    keys: [
      "TTS_MEMORY_SOFT_LIMIT_MB",
      "TTS_MEMORY_HARD_LIMIT_MB",
      "TTS_IDLE_UNLOAD_SECONDS",
      "WHISPER_ENABLED",
      "WHISPER_MODEL",
      "WHISPER_DEVICE",
      "WHISPER_COMPUTE_TYPE",
      "TTS_REQUEST_TIMEOUT_SECONDS",
      "LOG_LEVEL",
      "LOG_FORMAT",
    ],
    note: "Memory, timeout, and logging changes apply live. Whisper model changes load lazily on the next verification.",
  },
] as const;

const SIDECAR_LABELS: Record<string, string> = {
  RENDER_NAV_TIMEOUT_MS: "Page navigation timeout (ms)",
  RENDER_CLICK_TIMEOUT_MS: "Gate click timeout (ms)",
  RENDER_GROW_WAIT_MS: "Content growth wait (ms)",
  RENDER_ATTEMPTS: "Render attempts",
  RENDER_SETTLE_POLL_MS: "Page settle poll interval (ms)",
  RENDER_SETTLE_QUIET_POLLS: "Quiet polls before complete",
  RENDER_SETTLE_MAX_MS: "Maximum page settle wait (ms)",
  RENDER_BUDGET_SECONDS: "Maximum time per article (seconds)",
  TTS_MEMORY_SOFT_LIMIT_MB: "Soft memory limit (MB)",
  TTS_MEMORY_HARD_LIMIT_MB: "Hard memory limit (MB)",
  TTS_IDLE_UNLOAD_SECONDS: "Unload models after idle (seconds)",
  WHISPER_ENABLED: "Enable speech verification",
  WHISPER_MODEL: "Speech verification model",
  WHISPER_DEVICE: "Speech verification device",
  WHISPER_COMPUTE_TYPE: "Speech verification precision",
  TTS_REQUEST_TIMEOUT_SECONDS: "TTS request timeout (seconds)",
  LOG_LEVEL: "Log detail",
  LOG_FORMAT: "Log format",
};

const WIDGET_SEARCH_TERMS: Record<string, string[]> = {
  "Sidecar settings": SIDECAR_GROUPS.flatMap(group => [group.title, ...group.keys, ...group.keys.map(key => SIDECAR_LABELS[key])]),
  "Site overrides": ["domain", "default strategy", "teaser threshold", "proxy template", "cookie jar", "subscriber login", "test URL"],
  Voices: ["reference voice slots", "audition sample text", "upload", "replace", "rename"],
  "End chime": ["CHIME_ENABLED", "upload clip", "preview"],
  "Authenticated feeds": ["FEED_AUTH_ENABLED", "FEED_AUTH_KEY", "feed URL", "regenerate key", "copy"],
  "Pronunciation corrections": ["word", "spoken", "override", "spell", "case sensitive", "lexicon", "export"],
  "Admin access": ["current password", "new password", "change password", "remove password"],
};

// Runs once at import. An entry that matches neither a GROUPS key nor a widget
// section would never render, and a duplicate would render twice. Both are easy
// to miss by eye and this is the only thing that keeps the lists in step; the
// same goes for a section without a summary or search terms for a name that
// does not exist.
{
  const placed = CATEGORIES.flatMap((c) => c.entries);
  const known = [...Object.keys(GROUPS), ...STANDALONE_SECTIONS];
  const missing = known.filter((g) => !placed.includes(g));
  const duplicated = placed.filter((g, i) => placed.indexOf(g) !== i);
  const unknown = placed.filter((g) => !known.includes(g));
  const unsummarized = known.filter((g) => !(g in SECTION_SUMMARIES));
  const strayTerms = Object.keys(WIDGET_SEARCH_TERMS).filter((g) => !known.includes(g));
  if (missing.length || duplicated.length || unknown.length || unsummarized.length || strayTerms.length) {
    console.error("CATEGORIES mismatch", { missing, duplicated, unknown, unsummarized, strayTerms });
  }
}

const SIDECAR_NUMERIC_KEYS = new Set(
  SIDECAR_GROUPS.flatMap(({ keys }) => keys).filter((key) =>
    /_MS$|_SECONDS$|_POLLS$|_ATTEMPTS$|_LIMIT_MB$/.test(key)
  )
);
const SIDECAR_BOOLEAN_KEYS = new Set(["WHISPER_ENABLED"]);

// Site-override strategies that drive a real browser, so a subscriber cookie jar applies.
const acceptsCookies = (proxy: string) => proxy === "flaresolverr" || proxy === "render";

// A masked text field keeps password managers away from the cookie jar. Where the
// browser can't mask text (no -webkit-text-security), fall back to a password field.
const SECRET_INPUT_TYPE =
  typeof CSS !== "undefined" && CSS.supports("-webkit-text-security", "disc") ? "text" : "password";
// Attributes 1Password, LastPass, and Bitwarden read to skip a field.
const PASSWORD_MANAGER_IGNORE = {
  "data-1p-ignore": true,
  "data-lpignore": "true",
  "data-bwignore": true,
  "data-form-type": "other",
};

// One terse help line per group, rendered above the group's fields.
const GROUP_NOTES: Record<string, string> = {
  Feed: "Changes appear when the podcast app next refreshes the feed.",
  "TTS backend":
    "The wrapper runs locally. The openai-api backend uses another speech server and its voices. " +
    "Choose its voice with TTS API voice. Remote speech needs remote verification or verification turned off.",
  "TTS delivery":
    "Set how long to retry a slow or restarting wrapper. The connection retry budget must outlast model loading after a restart.",
  "Job timeouts":
    "The total time limit grows with the chunk count. The stall limit stops a job that no longer makes progress.",
  Timeouts:
    "Set request time limits for extraction services. Increase a limit when that service needs more time.",
  Connections:
    "Self-hosted Firecrawl can run without a key. The reader key is for Jina Reader; requests without a key are rate limited.",
  Extraction:
    "Automatic reader fallback sends the article URL to the reader proxy (Jina by default). Turn it off to keep URLs local. " +
    "Registration email lets the render service submit your address to free registration forms. Clear it to stop these submissions.",
  Webhooks:
    "Send episode.processed and episode.failed events to this URL. Leave it blank to disable webhooks. Save before sending a test.",
  TTS:
    "Model and language apply to the next episode and are locked while a job runs. Other controls set chunk sizes and silence between chunks.",
  "TTS generation":
    "Sampling changes apply to the next job. Lower temperature reduces variation. Raise the repetition penalty if words loop. Seed 0 chooses a random seed.",
  Verification:
    "Regenerate chunks when narration differs from the requested text. Wrapper verification needs Whisper enabled. A lower threshold is stricter.",
  "Audio analysis":
    "Check each chunk for noise, silence and pacing problems. Failed checks retry synthesis within the regeneration budget shared with speech verification.",
  Uploads:
    "The upload size limit applies immediately. OCR reads scanned PDFs and images. The minimum confidence rejects unreadable scans.",
};

// Secret fields: rendered as password inputs. The backend masks them on read
// (a sentinel arrives instead of the value) and ignores the sentinel on save.
const MASKED_KEYS = new Set([
  "OPENAI_API_KEY",
  "ANTHROPIC_API_KEY",
  "OPENROUTER_API_KEY",
  "FIRECRAWL_API_KEY",
  "READER_API_KEY",
  "TTS_API_KEY",
  "WHISPER_API_KEY",
]);
// Allowlisted so the backend overlay reads them, but written only through the
// dedicated AuthenticatedFeedsWidget -- keep them out of the generic form's
// draft so "save all" never PUTs them (the backend rejects that path).
const ENDPOINT_MANAGED_KEYS = new Set(["FEED_AUTH_ENABLED", "FEED_AUTH_KEY"]);
const PROVIDER_OPTIONS = ["openai-compatible", "anthropic", "openrouter", "ollama"];
// Keep in sync with the EXTRACTION_ENGINE Literal in backend/app/config.py. The
// backend rejects any other value on PUT, so this list only drives the dropdown.
const EXTRACTION_ENGINE_OPTIONS = ["direct", "firecrawl"];
// Keep in sync with the TTS_BACKEND / WHISPER_BACKEND Literals in
// backend/app/config.py (issue #117).
const TTS_BACKEND_OPTIONS = ["wrapper", "openai-api"];
const WHISPER_BACKEND_OPTIONS = ["wrapper", "openai-api", "off"];
const PRONUNCIATION_SCOPE_OPTIONS = ["chunk", "sentence"];

// Remote-backend keys only mean anything under their own backend, so they are
// hidden otherwise -- the same treatment LLM provider keys already get.
const TTS_REMOTE_KEYS = new Set([
  "TTS_API_BASE_URL",
  "TTS_API_KEY",
  "TTS_API_MODEL",
  "TTS_API_VOICE",
]);
const WHISPER_REMOTE_KEYS = new Set([
  "WHISPER_API_BASE_URL",
  "WHISPER_API_KEY",
  "WHISPER_API_MODEL",
  "WHISPER_API_TIMEOUT_SECONDS",
  // Strict verification only exists for the remote ASR path: the wrapper
  // backend has no separate ASR call to fail.
  "WHISPER_API_STRICT",
]);

// Which provider-specific keys are relevant per provider. Keys not listed for
// the selected provider are hidden (openrouter's base URL is fixed server-side;
// anthropic has no base URL). Provider-agnostic keys (model, tuning) always show.
const PROVIDER_FIELDS: Record<string, Set<string>> = {
  "openai-compatible": new Set(["OPENAI_BASE_URL", "OPENAI_API_KEY"]),
  anthropic: new Set(["ANTHROPIC_API_KEY"]),
  openrouter: new Set(["OPENROUTER_API_KEY"]),
  ollama: new Set(["OLLAMA_BASE_URL"]),
};
// Union of every provider's keys, derived so it can't drift from PROVIDER_FIELDS.
const PROVIDER_SPECIFIC_KEYS = new Set(
  Object.values(PROVIDER_FIELDS).flatMap((s) => [...s])
);

interface PromptBody {
  prompt: string;
  is_default?: boolean;
}

// Sends a sample payload to the saved WEBHOOK_URL and reports the outcome inline,
// using the shared action and feedback styles.
function WebhookTest() {
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [pending, setPending] = useState(false);

  const run = async () => {
    setPending(true);
    setMsg(null);
    try {
      const r = await api<{ delivered: boolean; status_code: number | null; error: string | null }>(
        "/api/v1/webhooks/test",
        { method: "POST" }
      );
      const code = r.status_code ? ` (${r.status_code})` : "";
      setMsg(
        r.delivered
          ? { ok: true, text: `Delivered${code}` }
          : { ok: false, text: `Failed${code}: ${r.error ?? "no response"}` }
      );
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      setMsg({
        ok: false,
        text: status === 409 ? "Save a webhook URL above first." : `Request failed (${status})`,
      });
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="mt-3 flex items-center gap-3 flex-wrap">
      <button type="button" className="btn-ghost" disabled={pending} onClick={run}>
        {pending ? "Testing..." : "Send test webhook"}
      </button>
      {msg && <span className={`text-sm ${msg.ok ? "text-accent" : "text-danger"}`} role={msg.ok ? "status" : "alert"}>{msg.text}</span>}
    </div>
  );
}

function Toggle({
  id,
  checked,
  onChange,
  label,
}: {
  id: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      id={id}
      role="switch"
      aria-checked={checked}
      aria-label={label ?? settingLabel(id)}
      className="toggle"
      onClick={() => onChange(!checked)}
    />
  );
}

export default function SettingsRoute() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const activeCategory = CATEGORIES.find((category) => category.name.toLowerCase() === params.get("category")) ?? CATEGORIES[0];
  // Shared app-wide auth status so a password change here also updates the
  // header (a separate query would leave the header's useAuth() stale).
  const { status: authStatus, refresh: refreshAuth } = useAuth();
  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<SettingsPayload>("/api/v1/settings"),
  });
  const promptQ = useQuery({
    queryKey: ["prompt"],
    queryFn: () => api<PromptBody>("/api/v1/prompt"),
  });
  const correctionsQ = useQuery({
    queryKey: ["corrections"],
    queryFn: () => api<Record<string, CorrectionEntry>>("/api/v1/corrections"),
  });
  const fallbacksQ = useQuery({
    queryKey: ["source-fallbacks"],
    queryFn: () => api<SourceFallbacksConfig>("/api/v1/source-fallbacks"),
  });
  const ocrLangsQ = useQuery({
    queryKey: ["ocr-languages"],
    queryFn: () =>
      api<{ languages: string[]; default: string }>(
        "/api/v1/settings/ocr/languages"
      ),
  });
  const ttsModelsQ = useQuery({
    queryKey: ["tts-models"],
    queryFn: () =>
      api<{ active: string | null; models: { name: string; languages: string[] }[] }>(
        "/api/v1/tts/models"
      ),
  });
  const activeJobsQ = useQuery({
    queryKey: ["jobs-processing"],
    queryFn: () => api<unknown[]>("/api/v1/jobs?status=processing&per_page=1"),
    refetchInterval: 15000,
  });
  const jobRunning = (activeJobsQ.data?.length ?? 0) > 0;
  const healthQ = useHealthLive();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [resetKeys, setResetKeys] = useState<Set<string>>(new Set());
  const [savedMsg, setSavedMsg] = useState<string | null>(null);
  // Settings search. qInput is what the box shows; q is the trimmed value the
  // matcher uses. Matching runs over the group titles and their setting keys,
  // which the page already holds, rather than over rendered text.
  const [qInput, setQInput] = useState("");
  const q = qInput.trim().toLowerCase();
  const searchRegionRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const chooseCategory = (name: string) => {
    setQInput("");
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("category", name.toLowerCase());
      return next;
    });
  };
  const clearSearch = () => {
    setQInput("");
    searchInputRef.current?.focus();
  };

  // Paint the matched text using the CSS Custom Highlight API: ranges are
  // registered with the browser, so nothing in the DOM is mutated and React
  // keeps sole ownership of the tree. Runs after the filter commits, so the
  // ranges point at sections the search has already expanded. Where the API is
  // missing (older Safari) filtering still works and only the paint is lost.
  useEffect(() => {
    if (typeof CSS === "undefined" || !("highlights" in CSS)) return;
    const region = searchRegionRef.current;
    if (!q || !region) {
      CSS.highlights.delete("settings-search");
      return;
    }
    const ranges: Range[] = [];
    const walker = document.createTreeWalker(region, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) =>
        node.nodeValue && node.parentElement?.offsetParent
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_REJECT,
    });
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const hay = node.nodeValue!.toLowerCase();
      for (let i = hay.indexOf(q); i !== -1; i = hay.indexOf(q, i + q.length)) {
        const range = document.createRange();
        range.setStart(node, i);
        range.setEnd(node, i + q.length);
        ranges.push(range);
      }
    }
    CSS.highlights.set("settings-search", new Highlight(...ranges));
    return () => {
      CSS.highlights.delete("settings-search");
    };
  });
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const seeded = useRef(false);
  // The seeded values (override-or-default) so save() can send only the keys
  // the operator actually changed -- otherwise every default would be written
  // as an explicit override.
  const baseline = useRef<Record<string, string>>({});
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const updateDraft = (key: string, value: string) => {
    setSaveErr(null);
    setDraft((prev) => ({ ...prev, [key]: value }));
    setResetKeys((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
  };

  // Seed the draft once on first arrival; subsequent refetches must not
  // clobber unsaved field edits.
  useEffect(() => {
    if (!seeded.current && settingsQ.data) {
      const next: Record<string, string> = {};
      for (const key of settingsQ.data.allowlist) {
        if (ENDPOINT_MANAGED_KEYS.has(key)) continue; // owned by their own widget
        // Stored override if present, else the effective default, so fields
        // show editable values instead of blank.
        const v = settingsQ.data.values[key] ?? settingsQ.data.defaults[key];
        next[key] = v === undefined || v === null ? "" : String(v);
      }
      setDraft(next);
      baseline.current = { ...next };
      seeded.current = true;
    }
  }, [settingsQ.data]);

  const putM = useMutation({
    mutationFn: (request: {
      payload: Record<string, unknown>;
      draftSnapshot: Record<string, string>;
      resetSnapshot: Set<string>;
    }) =>
      api("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify(request.payload),
      }),
    onSuccess: (_data, request) => {
      // The saved values are the new baseline; without this the form stays
      // "dirty" forever and the save bar never goes away.
      for (const key of Object.keys(request.payload)) {
        baseline.current[key] = request.draftSnapshot[key] ?? "";
      }
      setResetKeys((current) => {
        const next = new Set(current);
        for (const key of request.resetSnapshot) {
          if (request.payload[key] !== null) continue;
          if (draftRef.current[key] === request.draftSnapshot[key] && current.has(key)) next.delete(key);
        }
        return next;
      });
      setSavedMsg("Saved");
      setTimeout(() => setSavedMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["settings"] });
      // A saved provider/base-URL change means the model list may differ.
      qc.invalidateQueries({ queryKey: ["llm-models"] });
      setSaveErr(null);
    },
    onError: (e) => {
      // Show the server's reason (a rejected value names the key and the shape
      // it wants); a silent no-op reads as a successful save.
      const body = e instanceof ApiError ? (e.detail as { error?: string; detail?: string }) : null;
      setSavedMsg(null);
      setSaveErr(body?.error || body?.detail || "Save failed");
    },
  });

  const save = () => {
    const payload: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(draft)) {
      if (resetKeys.has(key)) {
        payload[key] = null;
        continue;
      }
      // Only persist keys the operator changed from the seeded value, so
      // leaving a field at its default doesn't pin it as an override.
      if (value === (baseline.current[key] ?? "")) continue;
      if (MASKED_KEYS.has(key)) {
        // Secrets are sent verbatim (never number-coerced). The mask sentinel
        // round-trips and the backend ignores it.
        payload[key] = value;
        continue;
      }
      if (value === "") {
        // Numbers and bools have no empty form, so an emptied one is a stray
        // edit. Blank text is explicit; reset uses JSON null.
        const def = settingsQ.data?.defaults[key];
        if (typeof def === "number") {
          setSaveErr(`${settingLabel(key)} must contain a number or use Reset to default.`);
          return;
        }
        if (typeof def === "boolean") continue;
        payload[key] = "";
        continue;
      }
      if (value === "true") payload[key] = true;
      else if (value === "false") payload[key] = false;
      else if (!Number.isNaN(Number(value)) && value.trim() !== "")
        payload[key] = Number(value);
      else payload[key] = value;
    }
    if (Object.keys(payload).length === 0) return;
    setSaveErr(null);
    putM.mutate({ payload, draftSnapshot: { ...draft }, resetSnapshot: new Set(resetKeys) });
  };

  // Drives the save bar and the unsaved-draft guard.
  const dirtyKeys = Object.keys(draft).filter(
    (key) => resetKeys.has(key) || draft[key] !== (baseline.current[key] ?? "")
  );
  const hasPendingWork = dirtyKeys.length > 0 || putM.isPending;
  useUnsavedChanges(hasPendingWork);

  if (settingsQ.isLoading) return <p className="text-mute text-sm" role="status">Loading settings...</p>;
  if (settingsQ.isError) {
    return (
      <div className="text-danger text-sm" role="alert">
        <p>Settings could not be loaded.</p>
        <button className="btn-ghost mt-2" onClick={() => settingsQ.refetch()}>
          Retry
        </button>
      </div>
    );
  }

  // Keys whose effective default is a real boolean render as a switch, not a
  // text field. The defaults map carries the typed value from the backend.
  const boolKeys = new Set(
    Object.entries(settingsQ.data?.defaults ?? {})
      .filter(([, v]) => typeof v === "boolean")
      .map(([k]) => k)
  );

  // Which keys each group actually shows: allowlisted by the backend, and for a
  // provider-specific key, only under its selected provider.
  const provider = draft["LLM_PROVIDER"] ?? "";
  const visibleKeysByGroup: Record<string, string[]> = {};
  for (const [group, keys] of Object.entries(GROUPS)) {
    visibleKeysByGroup[group] = keys.filter((k) => {
      if (!settingsQ.data?.allowlist.includes(k)) return false;
      if (k === "LLM_REASONING_EFFORT" && provider === "anthropic") return false;
      // Remote endpoint/credential fields are noise unless that backend is on.
      if (TTS_REMOTE_KEYS.has(k) && draft["TTS_BACKEND"] !== "openai-api") return false;
      if (WHISPER_REMOTE_KEYS.has(k) && draft["WHISPER_BACKEND"] !== "openai-api") return false;
      return !PROVIDER_SPECIFIC_KEYS.has(k) || (PROVIDER_FIELDS[provider]?.has(k) ?? false);
    });
  }

  // The hand-written sections, keyed by title so CATEGORIES can place them
  // alongside the generic groups. null means "nothing to show yet", which keeps
  // an empty shell out of the page while its query is still loading.
  const widgets: Record<string, ReactNode | null> = {
    "Cleanup prompt":
      promptQ.data !== undefined ? <PromptEditor initial={promptQ.data.prompt} /> : null,
    "Pronunciation corrections":
      correctionsQ.data !== undefined ? <CorrectionsTable initial={correctionsQ.data} /> : null,
    "Site overrides":
      fallbacksQ.data !== undefined ? <SourceFallbacksTable initial={fallbacksQ.data} /> : null,
    Voices: <VoicesWidget />,
    "End chime": <ChimeWidget />,
    "Authenticated feeds": <AuthenticatedFeedsWidget />,
    "Admin access": authStatus ? (
      <SecuritySection passwordSet={authStatus.password_set} onChanged={refreshAuth} />
    ) : null,
    "Sidecar settings": <SidecarSettingsWidget />,
    "System info": (
      <>
        <ReadOnlyRow label="Version" value={healthQ.data?.version ?? "loading"} />
        <ReadOnlyRow label="Uptime" value={formatUptime(healthQ.data?.uptime_seconds)} />
        <ReadOnlyRow label="Password set" value={String(authStatus?.password_set ?? "loading")} />
        <ReadOnlyRow label="Authenticated" value={String(authStatus?.authenticated ?? "loading")} />
        <div className="flex flex-wrap justify-between gap-2 text-sm pt-1">
          <span className="text-dim">API docs</span>
          <a
            href="/api/v1/docs"
            target="_blank"
            rel="noreferrer"
            className="text-accent hover:underline"
          >
            /api/v1/docs
          </a>
        </div>
      </>
    ),
  };

  // The app default for a key, as the string the form holds. Used both to
  // decide whether a card is still at its defaults and to put it back.
  const defaultString = (key: string): string => {
    const d = settingsQ.data?.defaults[key];
    return d === undefined || d === null ? "" : String(d);
  };

  const groupDiffersFromDefaults = (keys: string[]) =>
    keys.some(
      (key) =>
        (draft[key] ?? "") !== defaultString(key) ||
        Object.prototype.hasOwnProperty.call(settingsQ.data?.values ?? {}, key)
    );

  const resetGroupToDefaults = (keys: string[]) => {
    setSaveErr(null);
    setDraft((prev) => {
      const next = { ...prev };
      for (const key of keys) next[key] = defaultString(key);
      return next;
    });
    setResetKeys((prev) => {
      const next = new Set(prev);
      for (const key of keys) {
        if (Object.prototype.hasOwnProperty.call(settingsQ.data?.values ?? {}, key)) next.add(key);
        else next.delete(key);
      }
      return next;
    });
  };

  const presentByCategory = CATEGORIES.map((category) => ({
    ...category,
    entries: category.entries.filter((name) =>
      name in GROUPS ? visibleKeysByGroup[name].length > 0 : widgets[name] != null
    ),
  }));
  // Every word of the query must appear somewhere in the section's own text:
  // name, summary, help note, keys, labels, widget terms. Category names stay
  // out so "voice" does not light up every Voice section.
  const terms = q.replace(/_/g, " ").split(/\s+/);
  const matchedGroups = new Set(
    q
      ? presentByCategory
          .flatMap((category) => category.entries)
          .filter((group) => {
            const keys = visibleKeysByGroup[group] ?? [];
            const hay = [
              group,
              SECTION_SUMMARIES[group],
              GROUP_NOTES[group] ?? "",
              ...keys,
              ...keys.map(settingLabel),
              ...(WIDGET_SEARCH_TERMS[group] ?? []),
            ].join(" ").toLowerCase().replace(/_/g, " ");
            return terms.every((term) => hay.includes(term));
          })
      : []
  );

  return (
    <SettingsSearchContext.Provider value={q ? matchedGroups : null}>
    <div className="settings-page space-y-6">
      <div>
        <h1 className="text-2xl font-black tracking-tight">Settings</h1>
        <p className="text-sm text-dim mt-1">Choose a category or search all settings.</p>
      </div>
      <div className="search-wrap">
        <span className="search-icon" aria-hidden="true" />
        <input
          ref={searchInputRef}
          type="search"
          className="field"
          placeholder="Search all settings"
          autoComplete="off"
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") clearSearch(); }}
          aria-label="Search settings"
        />
        {qInput && (
          <button
            type="button"
            className="search-clear"
            onClick={clearSearch}
            aria-label="Clear search"
          >
            &times;
          </button>
        )}
      </div>

      <div className="settings-layout">
        <aside className="settings-sidebar">
          <nav className="settings-nav" aria-label="Settings categories">
            {CATEGORIES.map((category) => (
              <button key={category.name} type="button" aria-current={!q && activeCategory.name === category.name ? "true" : undefined}
                aria-controls="settings-sections" onClick={() => chooseCategory(category.name)}>
                <span>{category.name}</span>
                <span className="settings-nav-description">{category.description}</span>
              </button>
            ))}
          </nav>
          <div className="settings-category-select">
            <label className="label" htmlFor="settings-category">Settings category</label>
            <select id="settings-category" className="field" value={q ? "search" : activeCategory.name}
              onChange={(e) => chooseCategory(e.target.value)}>
              {q && <option value="search" disabled>Search results in all categories</option>}
              {CATEGORIES.map((category) => <option key={category.name}>{category.name}</option>)}
            </select>
          </div>
        </aside>
      <div ref={searchRegionRef} id="settings-sections" className="settings-content space-y-4">
      {q && (
        <div className="settings-search-status" role="status">
          <p>{matchedGroups.size === 0 ? `No settings match "${qInput.trim()}".` : `${matchedGroups.size} matching section${matchedGroups.size === 1 ? "" : "s"} across all categories`}</p>
          {matchedGroups.size === 0 && <p className="text-dim mt-1">Try a name such as voice, password or chapters.</p>}
          <button className="btn-ghost mt-3" type="button" onClick={clearSearch}>Back to {activeCategory.name}</button>
        </div>
      )}
      {presentByCategory.map((category) => {
        const active = q ? category.entries.some((name) => matchedGroups.has(name)) : activeCategory.name === category.name;
        return (
          <section key={category.name} hidden={!active} className="space-y-3" aria-labelledby={`category-${category.name}`}>
            <div className="settings-category-header">
              <h2 id={`category-${category.name}`} className="category-heading">{category.name}</h2>
              <p className="text-sm text-dim">{category.description}</p>
            </div>
            {category.entries.map((name) => {
              if (!(name in GROUPS)) {
                return (
                  <CollapsibleSection
                    key={name}
                    title={name}
                    description={SECTION_SUMMARIES[name]}
                    active={active}
                    storageKey={name === "Admin access" ? "settings-section-security" : undefined}
                    searchKey={name}
                    defaultOpen={name === "System info"}
                  >
                    {widgets[name]}
                  </CollapsibleSection>
                );
              }
              const group = name;
              const visible = visibleKeysByGroup[group];
              return (
                <CollapsibleSection
                  key={group}
                  title={group}
                  description={SECTION_SUMMARIES[group]}
                  active={active}
                  searchKey={group}
                  defaultOpen={group === "LLM"}
                >
                  {GROUP_NOTES[group] && (
                    <p className="text-sm text-dim mb-4 leading-relaxed">{GROUP_NOTES[group]}</p>
                  )}
            {visible.map((key) => {
              const isBool = boolKeys.has(key);
              // Fixed-choice settings render as a dropdown; keys with dynamic
              // options or extra behavior (TTS model/language lock) keep their
              // own branch below.
              const selectOptions =
                key === "LLM_PROVIDER"
                  ? PROVIDER_OPTIONS
                  : key === "EXTRACTION_ENGINE"
                    ? EXTRACTION_ENGINE_OPTIONS
                    : key === "TTS_BACKEND"
                      ? TTS_BACKEND_OPTIONS
                      : key === "WHISPER_BACKEND"
                        ? WHISPER_BACKEND_OPTIONS
                        : key === "PRONUNCIATION_SCOPE"
                          ? PRONUNCIATION_SCOPE_OPTIONS
                          : key === "LLM_REASONING_EFFORT"
                            ? ["none", "low", "medium", "high"]
                          : key === "OCR_LANGUAGE"
                            ? (ocrLangsQ.data?.languages ?? ["en"])
                          : key === "LOG_LEVEL"
                            ? ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
                          : key === "LOG_FORMAT"
                            ? ["json", "text"]
                            : null;
              // Languages follow the model picked in the form, not just the
              // loaded one.
              const ttsLanguages =
                ttsModelsQ.data?.models.find(
                  (m) => m.name === (draft["TTS_MODEL"] || ttsModelsQ.data?.active)
                )?.languages ?? ["en"];
              return (
                <div
                  key={key}
                  className={isBool ? "flex items-center justify-between gap-3 py-1" : undefined}
                >
                  <label className={`label ${isBool ? "mb-0" : ""}`} htmlFor={key}>
                    {settingLabel(key)}
                  </label>
                  {key === "TTS_MODEL" || key === "TTS_LANGUAGE" ? (
                    <select
                      id={key}
                      className="field"
                      value={draft[key] ?? ""}
                      disabled={jobRunning}
                      title={
                        jobRunning
                          ? "locked while a job is running"
                          : undefined
                      }
                      onChange={(e) =>
                        updateDraft(key, e.target.value)
                      }
                    >
                      {key === "TTS_MODEL" ? (
                        <>
                          <option value="">wrapper default</option>
                          {(ttsModelsQ.data?.models ?? []).map((m) => (
                            <option key={m.name} value={m.name}>
                              {m.name}
                            </option>
                          ))}
                        </>
                      ) : (
                        ttsLanguages.map((opt) => (
                          <option key={opt} value={opt}>
                            {opt}
                          </option>
                        ))
                      )}
                    </select>
                  ) : selectOptions ? (
                    <select
                      id={key}
                      className="field"
                      value={draft[key] ?? ""}
                      onChange={(e) =>
                        updateDraft(key, e.target.value)
                      }
                    >
                      {selectOptions.map((opt) => (
                        <option key={opt} value={opt}>
                          {opt}
                        </option>
                      ))}
                    </select>
                  ) : key === "LLM_MODEL" ? (
                    <ModelField
                      value={draft[key] ?? ""}
                      provider={draft["LLM_PROVIDER"] ?? ""}
                      draft={draft}
                      onChange={(v) => updateDraft(key, v)}
                    />
                  ) : isBool ? (
                    <Toggle
                      id={key}
                      checked={draft[key] === "true"}
                      onChange={(v) => updateDraft(key, v ? "true" : "false")}
                    />
                  ) : (
                    <input
                      id={key}
                      className={MASKED_KEYS.has(key) ? "field field-secret" : "field"}
                      type={MASKED_KEYS.has(key) ? SECRET_INPUT_TYPE : "text"}
                      autoComplete={MASKED_KEYS.has(key) ? "off" : undefined}
                      {...(MASKED_KEYS.has(key) ? PASSWORD_MANAGER_IGNORE : {})}
                      value={draft[key] ?? ""}
                      onChange={(e) =>
                        updateDraft(key, e.target.value)
                      }
                    />
                  )}
                  {key === "FEED_ARTWORK_URL" && (
                    <ArtworkPreview value={draft[key] ?? ""} />
                  )}
                </div>
              );
            })}            {group === "Webhooks" && <WebhookTest />}
            {/* Puts this card's fields back to the values the app ships with.
                Edits the form rather than saving, so it is undoable until you
                press save, like every other change on the page. Disabled when
                the card already matches, with the reason on hover: a control
                that does nothing and does not say why is a dead end. */}
            <div className="card-reset">
              <button
                type="button"
                className="reset-btn"
                disabled={!groupDiffersFromDefaults(visible)}
                title={
                  groupDiffersFromDefaults(visible)
                    ? "Put these fields back to the app defaults"
                    : "Already at the app defaults"
                }
                onClick={() => resetGroupToDefaults(visible)}
              >
                Reset to defaults
              </button>
            </div>
                </CollapsibleSection>
              );
            })}
          </section>
        );
      })}
      </div>
      </div>

      {/* Appears only when there is something to save, or something to report
          about the last attempt. Saving clears the dirty set, so the bar has to
          outlive that for its own confirmation to be readable. */}
      {(hasPendingWork || savedMsg || saveErr) && (
        <div className="save-bar">
        {saveErr ? (
            <span className="text-sm text-danger mr-auto" role="alert">{saveErr}</span>
          ) : savedMsg && dirtyKeys.length === 0 ? (
            <span className="text-sm text-accent mr-auto" role="status">{savedMsg}</span>
          ) : (
            <span className="text-sm text-dim mr-auto" role="status">
              {putM.isPending ? "Saving settings..." : `${dirtyKeys.length} unsaved ${dirtyKeys.length === 1 ? "change" : "changes"}`}
            </span>
          )}
          {dirtyKeys.length > 0 && (
            <button className="btn-primary save-btn" disabled={putM.isPending} onClick={save}>
              {putM.isPending ? "Saving..." : "Save settings"}
            </button>
          )}
        </div>
      )}
    </div>
    </SettingsSearchContext.Provider>
  );
}

// Live preview of the feed cover. An empty value previews the locally-seeded
// /media/default.jpg branding art. The published feed falls back to the same
// branding cover served from DEFAULT_ARTWORK_URL (a stable external .jpg) when
// no FEED_ARTWORK_URL is set; the preview uses the local copy so it always
// renders in-app without depending on the external URL.
function ArtworkPreview({ value }: { value: string }) {
  const src = value.trim() || "/media/default.jpg";
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  return (
    <div className="mt-2 flex items-center gap-3">
      {failed ? (
        <div
          className="artwork-preview grid place-items-center text-xs text-mute bg-surface"
        >
          No image
        </div>
      ) : (
        <img
          src={src}
          alt="feed cover preview"
          className="artwork-preview"
          onError={() => setFailed(true)}
        />
      )}
      <span className="text-sm text-mute">
        {value.trim() ? "Custom cover" : "Default cover"}
      </span>
    </div>
  );
}

function formatUptime(seconds: number | undefined): string {
  if (seconds === undefined) return "loading";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const parts = [];
  if (d) parts.push(`${d}d`);
  if (h) parts.push(`${h}h`);
  parts.push(`${m}m`);
  return parts.join(" ");
}

function ModelField({
  value,
  provider,
  draft,
  onChange,
}: {
  value: string;
  provider: string;
  draft: Record<string, string>;
  onChange: (v: string) => void;
}) {
  const qc = useQueryClient();
  // The backend lists models for the SAVED provider/base URL (it resolves them
  // from runtime settings, not the request), so the key is provider-only.
  // Saving settings invalidates this query; "refresh" flushes the server cache.
  const modelsQ = useQuery({
    queryKey: ["llm-models", provider],
    queryFn: () =>
      api<LlmModelsResponse>(
        `/api/v1/llm/models?provider=${encodeURIComponent(provider)}`
      ),
    enabled: provider !== "",
    staleTime: 60_000,
  });
  const refreshM = useMutation({
    mutationFn: () =>
      api<LlmModelsResponse>(
        `/api/v1/llm/models/refresh?provider=${encodeURIComponent(provider)}`,
        { method: "POST" }
      ),
    onSuccess: (data) => qc.setQueryData(["llm-models", provider], data),
  });

  const models = modelsQ.data?.models ?? [];
  const ids = models.map((m) => m.id);
  // Stored value not in the live list: keep it selectable (orphan option) so a
  // saved model that the endpoint no longer reports isn't silently dropped.
  const isOrphan = value !== "" && !ids.includes(value);
  const [freeText, setFreeText] = useState(false);
  const testConfigKey = JSON.stringify({
    provider,
    openaiBaseUrl: draft.OPENAI_BASE_URL,
    ollamaBaseUrl: draft.OLLAMA_BASE_URL,
    openaiKey: draft.OPENAI_API_KEY,
    anthropicKey: draft.ANTHROPIC_API_KEY,
    openrouterKey: draft.OPENROUTER_API_KEY,
  });
  const testM = useMutation({
    mutationFn: (_configKey: string) => {
      const baseUrl =
        provider === "ollama" ? draft.OLLAMA_BASE_URL : draft.OPENAI_BASE_URL;
      const keyName =
        provider === "anthropic"
          ? "ANTHROPIC_API_KEY"
          : provider === "openrouter"
            ? "OPENROUTER_API_KEY"
            : "OPENAI_API_KEY";
      const draftKey = draft[keyName];
      return api<LlmConnectionTestResponse>("/api/v1/llm/test", {
        method: "POST",
        body: JSON.stringify({
          provider,
          base_url: baseUrl || null,
          api_key: draftKey ?? "",
        }),
      });
    },
  });

  const testControl = (
    <>
      <button
        type="button"
        className="btn-ghost"
        disabled={testM.isPending || provider === ""}
        onClick={() => testM.mutate(testConfigKey)}
      >
        {testM.isPending ? "Testing..." : "Test provider"}
      </button>
      {testM.data && testM.variables === testConfigKey && (
        <span className={`text-sm ${testM.data.ok ? "text-accent" : "text-danger"}`} role={testM.data.ok ? "status" : "alert"}>
          {testM.data.detail}
        </span>
      )}
      {testM.isError && testM.variables === testConfigKey && (
        <span className="text-sm text-danger" role="alert">Provider test failed.</span>
      )}
    </>
  );

  if (freeText) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <input
          id="LLM_MODEL"
          className="field flex-1 min-w-[12rem]"
          value={value}
          placeholder="Model ID"
          onChange={(e) => onChange(e.target.value)}
        />
        <button type="button" className="btn-ghost" onClick={() => setFreeText(false)}>
          Choose from list
        </button>
        {testControl}
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <select
        id="LLM_MODEL"
        className="field flex-1 min-w-[12rem]"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">(none)</option>
        {isOrphan && <option value={value}>{value} (saved)</option>}
        {models.map((m) => (
          <option key={m.id} value={m.id}>
            {m.name}
          </option>
        ))}
      </select>
      <button
        type="button"
        className="btn-ghost"
        disabled={refreshM.isPending || provider === ""}
        onClick={() => refreshM.mutate()}
        title="Refresh model list from provider"
      >
        {refreshM.isPending ? "..." : "refresh"}
      </button>
      <button type="button" className="btn-ghost" onClick={() => setFreeText(true)}>
        Custom model
      </button>
      {testControl}
    </div>
  );
}

function SecuritySection({
  passwordSet,
  onChanged,
}: {
  passwordSet: boolean;
  onChanged: () => void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const m = useMutation({
    mutationFn: (clear: boolean) => {
      const body: Record<string, string> = { new_password: clear ? "" : next };
      if (passwordSet) body.current_password = current;
      return api("/api/v1/auth/password", { method: "PUT", body: JSON.stringify(body) });
    },
    onSuccess: (_data, clear) => {
      setMsg(clear ? "Password removed" : "Password saved");
      setCurrent("");
      setNext("");
      onChanged();
      setTimeout(() => setMsg(null), 2500);
    },
    onError: (e) => {
      if (e instanceof ApiError) {
        if (e.status === 401) setMsg("Current password is incorrect");
        else if (e.status === 400)
          setMsg(
            typeof e.detail === "object" && e.detail && "detail" in e.detail
              ? String((e.detail as { detail: unknown }).detail)
              : "Invalid request"
          );
        else setMsg(`error ${e.status}`);
      } else {
        setMsg((e as Error).message);
      }
    },
  });

  return (
    <section className="space-y-3">
      {!passwordSet && (
        <p className="text-danger text-sm">
          No password is set. Set one below to restrict admin access.
        </p>
      )}
      {passwordSet && (
        <div>
          <label className="label" htmlFor="cur-pw">
            Current password
          </label>
          <input
            id="cur-pw"
            className="field"
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </div>
      )}
      <div>
        <label className="label" htmlFor="new-pw">
          {passwordSet ? "New password (minimum 8 characters)" : "Password (minimum 8 characters)"}
        </label>
        <input
          id="new-pw"
          className="field"
          type="password"
          autoComplete="new-password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
        />
      </div>
      <div className="flex items-center gap-3 flex-wrap">
        <button
          className="btn-primary"
          disabled={m.isPending || next.length < 8}
          onClick={() => m.mutate(false)}
        >
          {passwordSet ? "Change password" : "Set password"}
        </button>
        {passwordSet && (
          <button
            className="btn-ghost btn-danger"
            disabled={m.isPending}
            onClick={() => {
              if (confirm("Remove the password and reopen the admin UI to anyone?"))
                m.mutate(true);
            }}
          >
            Remove password
          </button>
        )}
      <FeedbackMsg msg={msg} />
      </div>
    </section>
  );
}

function ReadOnlyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-wrap justify-between gap-2 text-sm">
      <span className="text-dim">{label}</span>
      <span className="text-fg font-mono break-all">{value}</span>
    </div>
  );
}

function PromptEditor({ initial }: { initial: string }) {
  const qc = useQueryClient();
  // Seed from the initial prop via lazy initializer so the editor is
  // pre-populated on mount; subsequent refetches arrive as a new
  // PromptEditor instance only when the parent unmounts/remounts (we
  // gate at the parent on data !== undefined), so in-progress edits are
  // never clobbered.
  const [text, setText] = useState(initial);
  const [msg, setMsg] = useState<string | null>(null);

  const m = useMutation({
    mutationFn: () =>
      api("/api/v1/prompt", {
        method: "PUT",
        body: JSON.stringify({ prompt: text }),
      }),
    onSuccess: () => {
      setMsg("Saved");
      setTimeout(() => setMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["prompt"] });
    },
    onError: () => setMsg("Save failed. Try again."),
  });

  // Reset clears the DB override; the response carries the packaged default,
  // which we load back into the editor.
  const reset = useMutation({
    mutationFn: () => api<PromptBody>("/api/v1/prompt", { method: "DELETE" }),
    onSuccess: (data) => {
      setText(data.prompt);
      setMsg("Reset to default");
      setTimeout(() => setMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["prompt"] });
    },
    onError: () => setMsg("Reset failed. Try again."),
  });
  useUnsavedChanges(text !== initial || m.isPending);

  return (
    <section className="space-y-3">
      <textarea
        aria-label="Cleanup prompt"
        className="field min-h-[200px] font-mono text-xs"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="flex flex-wrap items-center gap-3">
        <button className="btn-primary" disabled={m.isPending} onClick={() => m.mutate()}>
          {m.isPending ? "Saving..." : "Save prompt"}
        </button>
        <button
          className="btn-ghost"
          disabled={reset.isPending}
          onClick={() => reset.mutate()}
        >
          {reset.isPending ? "Resetting..." : "Reset to default"}
        </button>
        <FeedbackMsg msg={msg} />
      </div>
    </section>
  );
}

type Mode = "spell" | "word" | "override";

interface CorrectionEntry {
  mode: Mode;
  spoken: string;
  case_sensitive: boolean;
}

interface Row {
  id: number;
  k: string;
  v: string; // spoken
  mode: Mode;
  caseSensitive: boolean;
}

const rowsContent = (rows: { id: number }[]) =>
  JSON.stringify(rows.map(({ id: _id, ...rest }) => rest));

let _rowCounter = 0;
const newRow = (k = "", entry?: Partial<CorrectionEntry>): Row => ({
  id: ++_rowCounter,
  k,
  v: entry?.spoken ?? "",
  mode: entry?.mode ?? "override",
  caseSensitive: entry?.case_sensitive ?? false,
});

function CorrectionsTable({ initial }: { initial: Record<string, CorrectionEntry> }) {
  const qc = useQueryClient();
  // Lazy initializer: seeded from the initial prop. Parent only mounts
  // this component once the query has data, so refetches arriving as
  // new identical props won't reset rows.
  const [rows, setRows] = useState<Row[]>(() =>
    Object.entries(initial).map(([k, entry]) => newRow(k, entry))
  );
  const [msg, setMsg] = useState<string | null>(null);

  const m = useMutation({
    mutationFn: () => {
      const obj: Record<string, Partial<CorrectionEntry>> = {};
      for (const row of rows) {
        if (!row.k.trim()) continue;
        obj[row.k.trim()] = {
          mode: row.mode,
          spoken: row.v,
          case_sensitive: row.caseSensitive,
        };
      }
      return api("/api/v1/corrections", {
        method: "PUT",
        body: JSON.stringify(obj),
      });
    },
    onSuccess: () => {
      setMsg("Saved");
      setTimeout(() => setMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["corrections"] });
    },
    onError: () => setMsg("Save failed. Try again."),
  });

  // Clear all user corrections (built-in fixes are unaffected).
  const reset = useMutation({
    mutationFn: () => api("/api/v1/corrections", { method: "DELETE" }),
    onSuccess: () => {
      setRows([]);
      setMsg("Cleared");
      setTimeout(() => setMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["corrections"] });
    },
    onError: () => setMsg("Clear failed. Try again."),
  });
  useUnsavedChanges(
    rowsContent(rows) !== rowsContent(Object.entries(initial).map(([k, entry]) => newRow(k, entry))) ||
      m.isPending
  );

  return (
    <section className="space-y-3">
      <div className="builtin-note">
        <span className="builtin-note-tag">built-in</span>
        <p className="builtin-note-body">
          Built-in pronunciation fixes apply to every episode; your rules below
          override them. Full set:{" "}
          <code className="builtin-note-path">GET /api/v1/corrections/seed</code>.
        </p>
      </div>
      <LexiconLookup />
      <div className="space-y-2">
        {rows.map((row) => {
          const patch = (p: Partial<Row>) =>
            setRows((rs) => rs.map((r) => (r.id === row.id ? { ...r, ...p } : r)));
          return (
            <div key={row.id} className="flex flex-wrap items-center gap-2">
              <input
                className="field flex-1 min-w-[8rem]"
                placeholder="word"
                aria-label="Word to correct"
                value={row.k}
                onChange={(e) => patch({ k: e.target.value })}
              />
              <input
                className="field flex-1 min-w-[8rem]"
                placeholder="spoken"
                aria-label="Replacement pronunciation"
                value={row.v}
                onChange={(e) => patch({ v: e.target.value })}
              />
              <select
                className="field w-28"
                aria-label="Correction type"
                value={row.mode}
                onChange={(e) => patch({ mode: e.target.value as Mode })}
              >
                <option value="override">override</option>
                <option value="word">word</option>
                <option value="spell">spell</option>
              </select>
              <label className="text-sm text-mute flex items-center gap-1" title="case-sensitive">
                <input
                  type="checkbox"
                  aria-label="Case sensitive"
                  checked={row.caseSensitive}
                  onChange={(e) => patch({ caseSensitive: e.target.checked })}
                />
                Aa
              </label>
              <button
                className="text-mute hover:text-danger flex items-center justify-center w-8"
                aria-label="Remove pronunciation correction"
                onClick={() => setRows((rs) => rs.filter((r) => r.id !== row.id))}
              >
                &times;
              </button>
            </div>
          );
        })}
        <button className="btn-ghost" onClick={() => setRows((r) => [...r, newRow()])}>
          Add row
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-mute">Export:</span>
        <a className="btn-ghost" href="/api/v1/corrections/export?scope=user">
          JSON
        </a>
        <a className="btn-ghost" href="/api/v1/corrections/export?scope=all">
          Full lexicon (JSON)
        </a>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <button className="btn-primary" disabled={m.isPending} onClick={() => m.mutate()}>
          {m.isPending ? "Saving..." : "Save corrections"}
        </button>
        <button
          className="btn-ghost"
          disabled={reset.isPending || rows.length === 0}
          onClick={() => reset.mutate()}
        >
          {reset.isPending ? "Clearing..." : "Clear all"}
        </button>
        <FeedbackMsg msg={msg} />
      </div>
    </section>
  );
}

interface ProxyOption {
  key: string;
  label: string;
}

interface FallbackRule {
  host: string;
  proxy: string;
  custom_template: string;
  cookies: string;
}

interface SourceFallbacksConfig {
  default_proxy: string;
  min_chars: number;
  rules: FallbackRule[];
  available_proxies: ProxyOption[];
  builtin: { host: string; proxy: string }[];
}

interface FallbackRow {
  id: number;
  host: string;
  proxy: string; // "" -> use the global default
  customTemplate: string;
  cookies: string; // sentinel ("********") when a jar is stored; "" clears it
}

let _fbRowCounter = 0;
const newFallbackRow = (rule?: FallbackRule): FallbackRow => ({
  id: ++_fbRowCounter,
  host: rule?.host ?? "",
  proxy: rule?.proxy ?? "",
  customTemplate: rule?.custom_template ?? "",
  cookies: rule?.cookies ?? "",
});

function SourceFallbacksTable({ initial }: { initial: SourceFallbacksConfig }) {
  const qc = useQueryClient();
  // Lazy init from the initial prop; parent mounts only once the query resolves.
  const [defaultProxy, setDefaultProxy] = useState(initial.default_proxy);
  const [minChars, setMinChars] = useState(String(initial.min_chars));
  const [rows, setRows] = useState<FallbackRow[]>(() =>
    initial.rules.map((r) => newFallbackRow(r))
  );
  const [msg, setMsg] = useState<string | null>(null);

  const proxies = initial.available_proxies;
  const proxyLabel = (key: string) =>
    proxies.find((p) => p.key === key)?.label ?? key;

  const m = useMutation({
    mutationFn: () =>
      api("/api/v1/source-fallbacks", {
        method: "PUT",
        body: JSON.stringify({
          default_proxy: defaultProxy,
          min_chars: Number(minChars) || 0,
          rules: rows
            .filter((r) => r.host.trim())
            .map((r) => ({
              host: r.host.trim(),
              proxy: r.proxy,
              custom_template: r.customTemplate.trim(),
              // Cookies only apply to the browser strategies; switching away clears the
              // jar so the session secret isn't silently retained on a rule that won't use it.
              cookies: acceptsCookies(r.proxy) ? r.cookies.trim() : "",
            })),
        }),
      }),
    onSuccess: () => {
      setMsg("Saved");
      setTimeout(() => setMsg(null), 2000);
      qc.invalidateQueries({ queryKey: ["source-fallbacks"] });
    },
    onError: (e) => {
      const detail =
        e instanceof ApiError ? (e.detail as { detail?: string })?.detail : null;
      setMsg(detail ? `Save failed: ${detail}` : "Save failed. Try again.");
    },
  });

  useUnsavedChanges(
    defaultProxy !== initial.default_proxy ||
      minChars !== String(initial.min_chars) ||
      rowsContent(rows) !== rowsContent(initial.rules.map((r) => newFallbackRow(r))) ||
      m.isPending
  );

  // Run the saved rules against one URL so the operator can confirm a rule (and its
  // cookie jar) actually fetches the article. Never returns the cookie value.
  const [testUrl, setTestUrl] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);
  const testM = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; chars: number; strategy: string | null; title?: string; detail?: string }>(
        "/api/v1/source-fallbacks/test",
        { method: "POST", body: JSON.stringify({ url: testUrl.trim() }) }
      ),
    onSuccess: (r) =>
      setTestResult(
        r.ok
          ? `ok: ${r.chars.toLocaleString()} chars via ${r.strategy ?? "direct scrape"}` +
              (r.title ? ` (${r.title})` : "")
          : `no full article: ${r.detail || "came back below the threshold"}`
      ),
    onError: (e) =>
      setTestResult(
        (e instanceof ApiError ? (e.detail as { detail?: string })?.detail : null) || "Test failed"
      ),
  });

  return (
    <section className="space-y-3">
      <div className="builtin-note">
        <span className="builtin-note-tag">built-in</span>
        <p className="builtin-note-body">
          Built-in:{" "}
          {initial.builtin.map((b) => `${b.host} -> ${b.proxy}`).join(", ")}. Your rules
          below override these.
        </p>
      </div>
      <p className="text-mute text-xs">
        Override how a site is extracted: list a host and the strategy to use (paywall
        bypass, FlareSolverr, archive, or render for click-to-expand articles). The
        default applies to any host that scrapes near-empty.
      </p>
      <p className="text-mute text-xs">
        Cookie jar (flaresolverr and render): paste a logged-in Cookie header to fetch
        as a subscriber. Stored masked. Render clears DataDome walls FlareSolverr cannot.
      </p>

      <div className="flex flex-wrap items-end gap-4">
        <div>
          <label className="label" htmlFor="fb-default-proxy">
            Default strategy
          </label>
          <select
            id="fb-default-proxy"
            className="field w-56"
            value={defaultProxy}
            onChange={(e) => setDefaultProxy(e.target.value)}
          >
            {proxies.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="fb-min-chars">
            Teaser threshold (characters)
          </label>
          <input
            id="fb-min-chars"
            className="field w-36"
            type="number"
            min={1}
            value={minChars}
            onChange={(e) => setMinChars(e.target.value)}
          />
        </div>
      </div>

      <div className="space-y-3">
        {rows.map((row) => {
          const patch = (p: Partial<FallbackRow>) =>
            setRows((rs) => rs.map((r) => (r.id === row.id ? { ...r, ...p } : r)));
          const host = row.host.trim() || "this site";
          return (
            <div key={row.id} className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <input
                  className="field flex-1 min-w-[10rem]"
                  placeholder="domain"
                  aria-label="Site domain"
                  value={row.host}
                  onChange={(e) => patch({ host: e.target.value })}
                />
                <select
                  className="field w-48"
                  aria-label={`Extraction strategy for ${host}`}
                  value={row.proxy}
                  onChange={(e) => patch({ proxy: e.target.value })}
                >
                  <option value="">use default ({proxyLabel(defaultProxy)})</option>
                  {proxies.map((p) => (
                    <option key={p.key} value={p.key}>
                      {p.label}
                    </option>
                  ))}
                </select>
                <button
                  className="text-mute hover:text-danger flex items-center justify-center w-8"
                  aria-label={`Remove override for ${host}`}
                  onClick={() => setRows((rs) => rs.filter((r) => r.id !== row.id))}
                >
                  &times;
                </button>
              </div>
              {/* Per-rule settings hang off their rule so they never read as a separate rule. */}
              {row.proxy === "custom" && (
                <label className={RULE_DETAIL}>
                  <span className={RULE_DETAIL_LABEL}>Proxy template for {host}</span>
                  <input
                    className="field font-mono text-sm"
                    placeholder="https://reader.example/{url}"
                    value={row.customTemplate}
                    onChange={(e) => patch({ customTemplate: e.target.value })}
                  />
                </label>
              )}
              {acceptsCookies(row.proxy) && (
                <label className={RULE_DETAIL}>
                  <span className={RULE_DETAIL_LABEL}>
                    Cookie jar for {host} (optional, for a subscriber login)
                  </span>
                  <input
                    className="field field-secret font-mono text-sm"
                    type={SECRET_INPUT_TYPE}
                    name={`cookie-jar-${row.id}`}
                    autoComplete="off"
                    spellCheck={false}
                    {...PASSWORD_MANAGER_IGNORE}
                    placeholder="name=value; name2=value2"
                    value={row.cookies}
                    onChange={(e) => patch({ cookies: e.target.value })}
                  />
                </label>
              )}
            </div>
          );
        })}
        <button
          className="btn-ghost"
          onClick={() => setRows((r) => [...r, newFallbackRow()])}
        >
          Add site
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button className="btn-primary" disabled={m.isPending} onClick={() => m.mutate()}>
          {m.isPending ? "Saving..." : "Save site overrides"}
        </button>
        <FeedbackMsg msg={msg} />
      </div>

      <div className="space-y-1 pt-2">
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="field flex-1 min-w-[12rem]"
            placeholder="Test a URL"
            aria-label="URL to test against site overrides"
            value={testUrl}
            onChange={(e) => setTestUrl(e.target.value)}
          />
          <button
            className="btn-ghost"
            disabled={testM.isPending || !testUrl.trim()}
            onClick={() => {
              setTestResult(null);
              testM.mutate();
            }}
          >
            {testM.isPending ? "Testing..." : "Test"}
          </button>
        </div>
        {testResult && <p className="text-mute text-sm">{testResult}</p>}
      </div>
    </section>
  );
}

function LexiconLookup() {
  const [q, setQ] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const search = async () => {
    const term = q.trim();
    if (!term) return;
    const body = await api<{ entry: (CorrectionEntry & { origin: string }) | null }>(
      `/api/v1/corrections/lookup?q=${encodeURIComponent(term)}`
    );
    setResult(
      body.entry
        ? `${body.entry.origin}: "${body.entry.spoken}" (${body.entry.mode})`
        : "No match in the built-in lexicon"
    );
  };
  return (
    <div className="flex flex-wrap items-center gap-2">
      <input
        className="field flex-1 min-w-[8rem]"
        placeholder="Look up a word"
        aria-label="Look up a word"
        value={q}
        onChange={(e) => {
          setResult(null);
          setQ(e.target.value);
        }}
        onKeyDown={(e) => e.key === "Enter" && search()}
      />
      <button className="btn-ghost" onClick={search}>
        Look up
      </button>
      {result && <span className="text-sm text-mute" role="status" aria-live="polite">{result}</span>}
    </div>
  );
}

const VOICE_ACCEPT = ".wav,.mp3,.m4a,.aac,.flac,.ogg,.oga,.opus,audio/*";

function VoiceUploadButton({
  replace,
  onPick,
}: {
  replace: boolean;
  onPick: (file: File) => void;
}) {
  return (
    <label className="btn-ghost cursor-pointer">
      {replace ? "Replace" : "Upload"}
      <input
        type="file"
        accept={VOICE_ACCEPT}
        className="sr-only"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onPick(f);
          e.target.value = "";
        }}
      />
    </label>
  );
}

// One card per voice slot: badge, name (a rename input), duration, an inline player
// for the stored clip, then Replace / Audition / Clear. Clear is omitted (onClear
// undefined) for the last filled slot, so at least one voice always stays loaded.
function VoiceRow({
  badge,
  name,
  filled,
  durationSecs,
  previewUrl,
  onUpload,
  onAudition,
  auditioning,
  onClear,
}: {
  badge: string;
  name: ReactNode;
  filled: boolean;
  durationSecs: number | null;
  previewUrl: string;
  onUpload: (file: File) => void;
  onAudition: () => void;
  auditioning: boolean;
  onClear?: () => void;
}) {
  return (
    <div className="card p-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className="format-badge">{badge}</span>
        {name}
        <span className="text-sm text-mute shrink-0">
          {filled ? `${durationSecs ?? "?"}s` : "empty"}
        </span>
      </div>
      {filled && (
        <audio controls src={previewUrl} className="w-full" aria-label={`Preview reference voice slot ${badge}`} />
      )}
      <div className="flex flex-wrap gap-2">
        <VoiceUploadButton replace={filled} onPick={onUpload} />
        {filled && (
          <button className="btn-ghost" disabled={auditioning} onClick={onAudition}>
            {auditioning ? "Auditioning..." : "Audition"}
          </button>
        )}
        {filled && onClear && (
          <button className="btn-ghost btn-danger" onClick={onClear}>
            Clear
          </button>
        )}
      </div>
    </div>
  );
}

// Voices section: five slots, each with the same controls -- inline preview of the
// stored clip, audition (a TTS sample), replace, and clear. Uploads accept any common
// audio format; the backend transcodes non-WAV to WAV with ffmpeg.
function VoicesWidget() {
  const qc = useQueryClient();
  const slotsQ = useQuery({
    queryKey: ["voice-slots"],
    queryFn: () => api<VoiceSlot[]>("/api/v1/reference/slots"),
    staleTime: 60_000,
  });
  const [msg, setMsg] = useState<string | null>(null);
  const [auditionUrl, setAuditionUrl] = useState<string | null>(null);
  const [auditioning, setAuditioning] = useState<string | null>(null);
  const [sample, setSample] = useState(
    "But I must explain to you how all this mistaken idea of denouncing of a pleasure and praising pain was born and I will give you a complete account of the system, and expound the actual teachings of the great explorer of the truth, the master-builder of human happiness.",
  );
  // Bumped after any upload/clear so the inline preview <audio> refetches the
  // changed clip instead of replaying the browser-cached one.
  const [bust, setBust] = useState(0);

  useEffect(
    () => () => {
      if (auditionUrl) URL.revokeObjectURL(auditionUrl);
    },
    [auditionUrl]
  );

  const sendForm = async (path: string, method: string, fd?: FormData): Promise<Response> => {
    const headers: Record<string, string> = {};
    const csrf = readCsrf();
    if (csrf) headers["X-CSRF-Token"] = csrf;
    return fetch(path, { method, body: fd, credentials: "include", headers });
  };

  const invalidate = () => {
    setBust((b) => b + 1);
    qc.invalidateQueries({ queryKey: ["voice-slots"] });
  };

  const upload = async (path: string, f: File, ok: string) => {
    setMsg("Uploading...");
    const fd = new FormData();
    fd.append("voice", f);
    const r = await sendForm(path, "POST", fd);
    setMsg(r.ok ? ok : `upload failed (${r.status})`);
    if (r.ok) invalidate();
  };

  const audition = async (key: string, path: string) => {
    if (sample.trim().length < 4) {
      setMsg("Audition sample text must be at least 4 characters");
      return;
    }
    setAuditioning(key);
    setMsg(null);
    const fd = new FormData();
    fd.append("sample_text", sample);
    const r = await sendForm(path, "POST", fd);
    setAuditioning(null);
    if (!r.ok) {
      setMsg(`audition failed (${r.status})`);
      return;
    }
    setAuditionUrl(URL.createObjectURL(await r.blob()));
  };

  const clearSlot = async (slot: number) => {
    if (!confirm(`Clear voice slot ${slot}?`)) return;
    try {
      await api(`/api/v1/reference/slots/${slot}`, { method: "DELETE" });
      invalidate();
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      setMsg(
        status === 409
          ? "cannot clear the only loaded voice; add another slot first"
          : `slot ${slot} clear failed`,
      );
    }
  };

  const renameSlot = async (slot: number, label: string) => {
    const fd = new FormData();
    fd.append("label", label);
    const r = await sendForm(`/api/v1/reference/slots/${slot}/label`, "PUT", fd);
    setMsg(r.ok ? `slot ${slot} renamed` : `rename failed (${r.status})`);
    if (r.ok) qc.invalidateQueries({ queryKey: ["voice-slots"] });
  };

  const slots = slotsQ.data ?? [];
  const filledCount = slots.filter((s) => s.filled).length;

  return (
    <div className="space-y-3">
      <p className="text-sm text-mute">
        A random filled slot narrates each episode unless you choose a voice on Home.
        Keep at least one slot loaded. Upload WAV, MP3, M4A, FLAC or OGG.
      </p>
      <div>
        <label className="label" htmlFor="voice-sample">
          Audition sample text
        </label>
        <textarea
          id="voice-sample"
          className="field min-h-[120px] resize-y"
          value={sample}
          onChange={(e) => setSample(e.target.value)}
        />
      </div>
      <FeedbackMsg msg={msg} as="p" />
      {auditionUrl && (
        <audio src={auditionUrl} controls autoPlay className="w-full" aria-label="Generated voice audition" />
      )}

      {slots.map((s) => (
        <VoiceRow
          key={s.slot}
          badge={String(s.slot)}
          name={
            <input
              className="field flex-1"
              defaultValue={s.label ?? ""}
              placeholder={`Slot ${s.slot} label`}
              aria-label={`Name for reference voice slot ${s.slot}`}
              onBlur={(e) => {
                if ((e.target.value ?? "") !== (s.label ?? "")) renameSlot(s.slot, e.target.value);
              }}
            />
          }
          filled={s.filled}
          durationSecs={s.duration_secs}
          previewUrl={`/api/v1/reference/slots/${s.slot}/preview?v=${bust}`}
          onUpload={(f) => upload(`/api/v1/reference/slots/${s.slot}`, f, `slot ${s.slot} saved`)}
          onAudition={() =>
            audition(`slot-${s.slot}`, `/api/v1/reference/slots/${s.slot}/audition`)
          }
          auditioning={auditioning === `slot-${s.slot}`}
          // The last filled slot has no Clear -- at least one voice must stay loaded.
          onClear={s.filled && filledCount > 1 ? () => clearSlot(s.slot) : undefined}
        />
      ))}
    </div>
  );
}

// End-of-episode chime: a single uploaded clip appended to every episode when the
// CHIME_ENABLED toggle below is on. Uploading a clip and enabling it are separate --
// the toggle and status line make the active state explicit. One slot, like voices.
function ChimeWidget() {
  const qc = useQueryClient();
  const chimeQ = useQuery({
    queryKey: ["chime"],
    queryFn: () => api<{ present: boolean; duration_secs: number | null }>("/api/v1/chime"),
    staleTime: 60_000,
  });
  // Reuses the shared ["settings"] query (deduped by React Query) so the CHIME_ENABLED
  // toggle reflects the same effective value as the generic settings form.
  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<SettingsPayload>("/api/v1/settings"),
  });
  const [msg, setMsg] = useState<string | null>(null);
  const [bust, setBust] = useState(0);

  const enabled = Boolean(
    settingsQ.data?.values.CHIME_ENABLED ?? settingsQ.data?.defaults.CHIME_ENABLED
  );
  const toggleM = useMutation({
    mutationFn: (v: boolean) =>
      api("/api/v1/settings", { method: "PUT", body: JSON.stringify({ CHIME_ENABLED: v }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });

  const invalidate = () => {
    setBust((b) => b + 1);
    qc.invalidateQueries({ queryKey: ["chime"] });
  };

  const upload = async (f: File) => {
    setMsg("Uploading...");
    const fd = new FormData();
    fd.append("file", f);
    try {
      await postForm("/api/v1/chime", fd);
      setMsg("Chime saved");
      invalidate();
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      setMsg(`upload failed${status ? ` (${status})` : ""}`);
    }
  };

  const clear = async () => {
    if (!confirm("Remove the end-of-episode chime?")) return;
    try {
      await api("/api/v1/chime", { method: "DELETE" });
      setMsg("Chime removed");
      invalidate();
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      setMsg(`remove failed${status ? ` (${status})` : ""}`);
    }
  };

  const data = chimeQ.data;
  return (
    <div className="space-y-3">
      <p className="text-sm text-mute">
        Plays at the end of every episode. Upload WAV, MP3, M4A, FLAC or OGG, trimmed to 15 seconds.
      </p>
      <div className="flex items-center justify-between gap-3 py-1">
        <label className="label mb-0" htmlFor="CHIME_ENABLED">
          Enabled
        </label>
        <Toggle id="CHIME_ENABLED" checked={enabled} onChange={(v) => toggleM.mutate(v)} />
      </div>
      {data?.present && !enabled && (
        <p className="text-sm text-danger">
          A clip is uploaded, but the chime is off. Enable it above to hear it.
        </p>
      )}
      {!data?.present && enabled && (
        <p className="text-sm text-mute">
          The chime is enabled. Upload a clip to hear it at the end of each episode.
        </p>
      )}
      <FeedbackMsg msg={msg} as="p" />
      {data?.present && (
        <audio
          src={`/api/v1/chime/preview?v=${bust}`}
          controls
          className="w-full"
          aria-label="End-of-episode chime preview"
        />
      )}
      <div className="flex flex-wrap items-center gap-3">
        <label className="btn-ghost cursor-pointer">
          {data?.present ? "Replace clip" : "Upload clip"}
          <input
            type="file"
            accept="audio/*,.wav,.mp3,.m4a,.flac,.ogg"
            className="sr-only"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) upload(f);
              e.target.value = "";
            }}
          />
        </label>
        {data?.present && (
          <>
            <span className="text-sm text-mute">Chime: {data.duration_secs} seconds</span>
            <button className="btn-ghost btn-danger" onClick={clear}>
              Clear
            </button>
          </>
        )}
      </div>
    </div>
  );
}

// A read-only value with a copy-to-clipboard button. Selects on focus so the
// keyboard path works when the clipboard API is blocked (non-HTTPS origin).
function CopyRow({
  label,
  value,
  copied,
  onCopy,
}: {
  label: string;
  value: string;
  copied: boolean;
  onCopy: () => void;
}) {
  return (
    <div className="space-y-1">
      <label className="label">{label}</label>
      <div className="flex items-center gap-2">
        <input
          className="field flex-1 font-mono text-xs"
          aria-label={label}
          readOnly
          value={value}
          onFocus={(e) => e.target.select()}
        />
        <button className="btn-ghost shrink-0" onClick={onCopy}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

// Authenticated feeds: an optional global key required on every feed/media URL.
// The key is server-generated on enable; the operator copies the keyed feed URL
// to re-subscribe apps. Enabling or regenerating breaks existing subscriptions.
function AuthenticatedFeedsWidget() {
  const qc = useQueryClient();
  const authQ = useQuery({
    queryKey: ["feed-auth"],
    queryFn: () => api<FeedAuthStatus>("/api/v1/feed-auth"),
  });
  const [copied, setCopied] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  // Both mutations also invalidate ["settings"] so the Feed page's copy button
  // (which reads settings.feed_url) picks up the newly keyed/keyless URL.
  const toggleM = useMutation({
    mutationFn: (enabled: boolean) =>
      api<FeedAuthStatus>("/api/v1/feed-auth", {
        method: "POST",
        body: JSON.stringify({ enabled }),
      }),
    onSuccess: (d) => {
      qc.setQueryData(["feed-auth"], d);
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e) => setMsg(`failed${e instanceof ApiError ? ` (${e.status})` : ""}`),
  });

  const regenM = useMutation({
    mutationFn: () =>
      api<FeedAuthStatus>("/api/v1/feed-auth/regenerate", { method: "POST" }),
    onSuccess: (d) => {
      qc.setQueryData(["feed-auth"], d);
      qc.invalidateQueries({ queryKey: ["settings"] });
      setMsg("Key rotated. Subscribe again in each app using the new URL.");
    },
    onError: (e) => setMsg(`rotate failed${e instanceof ApiError ? ` (${e.status})` : ""}`),
  });

  const copy = async (field: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(field);
      setTimeout(() => setCopied((c) => (c === field ? null : c)), 1500);
    } catch {
      setMsg("Copy failed. Select the field and copy manually.");
    }
  };

  const regenerate = () => {
    if (!confirm("Rotate the feed key? Every subscribed app must re-subscribe with the new URL.")) {
      return;
    }
    setMsg(null);
    regenM.mutate();
  };

  const data = authQ.data;
  const enabled = Boolean(data?.enabled);

  return (
    <div className="space-y-3">
      <p className="text-sm text-mute">
        Require a key on feed and media URLs. When disabled, anyone with the URL can subscribe.
      </p>
      <div className="flex items-center justify-between gap-3 py-1">
        <label className="label mb-0" htmlFor="FEED_AUTH_ENABLED">
          Enabled
        </label>
        <Toggle
          id="FEED_AUTH_ENABLED"
          checked={enabled}
          onChange={(v) => {
            setMsg(null);
            toggleM.mutate(v);
          }}
        />
      </div>
      {enabled && (
        <p className="text-sm text-danger">
          Subscribe again using the feed URL below. Apps with the old URL cannot access the feed.
        </p>
      )}
      {enabled && data?.keyed_feed_url && (
        <CopyRow
          label="Feed URL"
          value={data.keyed_feed_url}
          copied={copied === "url"}
          onCopy={() => copy("url", data.keyed_feed_url!)}
        />
      )}
      {enabled && data?.key && (
        <CopyRow
          label="key"
          value={data.key}
          copied={copied === "key"}
          onCopy={() => copy("key", data.key!)}
        />
      )}
      <FeedbackMsg msg={msg} as="p" />
      {enabled && (
        <button className="btn-ghost btn-danger" onClick={regenerate} disabled={regenM.isPending}>
          {regenM.isPending ? "Rotating..." : "Regenerate key"}
        </button>
      )}
    </div>
  );
}

function SidecarSettingsWidget() {
  const qc = useQueryClient();
  const sidecarsQ = useQuery({
    queryKey: ["sidecar-settings"],
    queryFn: () => api<SidecarSettingsPayload>("/api/v1/settings/sidecars"),
    refetchInterval: (query) => {
      const state = query.state.data as SidecarSettingsPayload | undefined;
      return !state || !state.render.available || state.render.pending || !state.tts_wrapper.available || state.tts_wrapper.pending
        ? 10000
        : false;
    },
  });
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [resetKeys, setResetKeys] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const baseline = useRef<Record<string, string>>({});
  const draftRef = useRef(draft);
  const resetKeysRef = useRef(resetKeys);
  draftRef.current = draft;
  resetKeysRef.current = resetKeys;

  useEffect(() => {
    const data = sidecarsQ.data;
    if (!data) return;
    const next: Record<string, string> = { ...draftRef.current };
    const nextBaseline = { ...baseline.current };
    let changed = false;
    for (const group of SIDECAR_GROUPS) {
      const state = data[group.key];
      for (const key of group.keys) {
        const storageKey = `${group.key}.${key}`;
        const untouched = !resetKeysRef.current.has(storageKey) &&
          (draftRef.current[storageKey] === undefined || draftRef.current[storageKey] === baseline.current[storageKey]);
        if (!untouched) continue;
        const value = state.values[key] ?? state.defaults[key];
        const textValue = value === undefined || value === null ? "" : String(value);
        next[storageKey] = textValue;
        nextBaseline[storageKey] = textValue;
        if (draftRef.current[storageKey] !== textValue) changed = true;
      }
    }
    if (changed) setDraft(next);
    baseline.current = nextBaseline;
  }, [sidecarsQ.data]);

  const saveM = useMutation({
    mutationFn: (request: {
      payload: Partial<SidecarSettingsPayload>;
      draftSnapshot: Record<string, string>;
      resetSnapshot: Set<string>;
    }) =>
      api<SidecarSettingsPayload>("/api/v1/settings/sidecars", {
        method: "PUT",
        body: JSON.stringify(request.payload),
      }),
    onSuccess: (data, request) => {
      qc.setQueryData(["sidecar-settings"], data);
      qc.invalidateQueries({ queryKey: ["sidecar-settings"] });
      for (const group of SIDECAR_GROUPS) {
        const submitted = request.payload[group.key] as Record<string, unknown> | undefined;
        for (const key of Object.keys(submitted ?? {})) {
          const storageKey = `${group.key}.${key}`;
          baseline.current[storageKey] = request.draftSnapshot[storageKey] ?? "";
        }
      }
      setResetKeys((current) => {
        const next = new Set(current);
        for (const group of SIDECAR_GROUPS) {
          const submitted = request.payload[group.key] as Record<string, unknown> | undefined;
          for (const key of Object.keys(submitted ?? {})) {
            const storageKey = `${group.key}.${key}`;
            if (
              submitted?.[key] === null &&
              request.resetSnapshot.has(storageKey) &&
              current.has(storageKey) &&
              draftRef.current[storageKey] === request.draftSnapshot[storageKey]
            ) next.delete(storageKey);
          }
        }
        return next;
      });
      setError(null);
      setMessage("Sidecar settings saved.");
    },
    onError: (e) => {
      const status = e instanceof ApiError ? ` (HTTP ${e.status})` : "";
      setMessage(null);
      setError(`Sidecar settings could not be saved${status}.`);
    },
  });

  useUnsavedChanges(
    saveM.isPending ||
      resetKeys.size > 0 ||
      Object.keys(draft).some((key) => draft[key] !== (baseline.current[key] ?? ""))
  );

  if (sidecarsQ.isLoading) return <p className="text-mute text-sm" role="status">Loading sidecar settings...</p>;
  if (sidecarsQ.isError && !sidecarsQ.data) {
    return (
      <div className="text-danger text-sm" role="alert">
        <p>Sidecar settings could not be loaded.</p>
        <button className="btn-ghost mt-2" onClick={() => sidecarsQ.refetch()}>
          Retry
        </button>
      </div>
    );
  }

  const data = sidecarsQ.data!;
  const isOverridden = (group: (typeof SIDECAR_GROUPS)[number]["key"], key: string) =>
    Object.prototype.hasOwnProperty.call(data[group].values, key);
  const resetField = (group: (typeof SIDECAR_GROUPS)[number]["key"], key: string) => {
    const storageKey = `${group}.${key}`;
    const defaultValue = data[group].defaults[key];
    setDraft((prev) => ({ ...prev, [storageKey]: defaultValue == null ? "" : String(defaultValue) }));
    setResetKeys((prev) => {
      const next = new Set(prev);
      if (isOverridden(group, key)) next.add(storageKey);
      else next.delete(storageKey);
      return next;
    });
  };
  const updateField = (
    group: (typeof SIDECAR_GROUPS)[number]["key"],
    key: string,
    value: string
  ) => {
    const storageKey = `${group}.${key}`;
    setDraft((prev) => ({ ...prev, [storageKey]: value }));
    setResetKeys((prev) => {
      if (!prev.has(storageKey)) return prev;
      const next = new Set(prev);
      next.delete(storageKey);
      return next;
    });
  };
  const dirty = (group: (typeof SIDECAR_GROUPS)[number]["key"], key: string) => {
    const storageKey = `${group}.${key}`;
    return resetKeys.has(storageKey) || draft[storageKey] !== (baseline.current[storageKey] ?? "");
  };
  const save = () => {
    const payload: Record<string, Record<string, unknown>> = {};
    for (const group of SIDECAR_GROUPS) {
      const values: Record<string, unknown> = {};
      for (const key of group.keys) {
        if (!dirty(group.key, key)) continue;
        const storageKey = `${group.key}.${key}`;
        if (resetKeys.has(storageKey)) {
          values[key] = null;
        } else if (SIDECAR_BOOLEAN_KEYS.has(key)) {
          values[key] = draft[storageKey] === "true";
        } else if (SIDECAR_NUMERIC_KEYS.has(key)) {
          if (!draft[storageKey]?.trim()) {
            setMessage(null);
            setError(`${SIDECAR_LABELS[key]} must contain a number, or use Reset to default.`);
            return;
          }
          values[key] = Number(draft[storageKey]);
        } else {
          values[key] = draft[storageKey] ?? "";
        }
      }
      if (Object.keys(values).length) payload[group.key] = values;
    }
    if (Object.keys(payload).length) {
      setMessage(null);
      setError(null);
      saveM.mutate({
        payload: payload as Partial<SidecarSettingsPayload>,
        draftSnapshot: { ...draft },
        resetSnapshot: new Set(resetKeys),
      });
    }
  };
  const hasChanges = SIDECAR_GROUPS.some((group) => group.keys.some((key) => dirty(group.key, key)));

  return (
    <div className="space-y-4">
      {SIDECAR_GROUPS.map((group) => {
        const state = data[group.key];
        return (
          <section key={group.key} className="card p-4 space-y-3">
            <h3 className="section-title">{group.title}</h3>
            <p className="text-dim text-sm">{group.note}</p>
            {!state.available && (
              <p className="text-mute text-sm" role="status">
                Sidecar is offline. Saved overrides can still be edited for when it returns.
              </p>
            )}
            {state.pending && (
              <p className="text-danger text-sm" role="status">
                Saved settings have not reached the sidecar yet.
              </p>
            )}
            {group.keys.map((key) => {
              const storageKey = `${group.key}.${key}`;
              const id = `sidecar-${group.key}-${key.toLowerCase()}`;
              const label = SIDECAR_LABELS[key];
              const defaultValue = state.defaults[key];
              const overridden = isOverridden(group.key, key);
              const canReset = overridden || draft[storageKey] !== (defaultValue == null ? "" : String(defaultValue));
              return (
                <div key={key} className="sidecar-field">
                  {SIDECAR_BOOLEAN_KEYS.has(key) ? (
                    <div className="flex items-center justify-between gap-3 py-1">
                      <label className="label mb-0" htmlFor={id}>{label}</label>
                      <Toggle
                        id={id}
                        label={label}
                        checked={draft[storageKey] === "true"}
                        onChange={(value) => updateField(group.key, key, value ? "true" : "false")}
                      />
                    </div>
                  ) : (
                    <>
                      <label className="label" htmlFor={id}>{label}</label>
                      {key === "LOG_LEVEL" || key === "LOG_FORMAT" ? (
                        <select
                          id={id}
                          className="field"
                          value={draft[storageKey] ?? ""}
                          onChange={(e) => updateField(group.key, key, e.target.value)}
                        >
                          <option value="" disabled>Select a value</option>
                          {key === "LOG_LEVEL"
                            ? ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((value) => (
                                <option key={value} value={value}>{value}</option>
                              ))
                            : ["json", "text"].map((value) => (
                                <option key={value} value={value}>{value}</option>
                              ))}
                        </select>
                      ) : (
                        <input
                          id={id}
                          className="field"
                          type={SIDECAR_NUMERIC_KEYS.has(key) ? "number" : "text"}
                          min={SIDECAR_NUMERIC_KEYS.has(key) ? 0 : undefined}
                          step={SIDECAR_NUMERIC_KEYS.has(key) ? "any" : undefined}
                          value={draft[storageKey] ?? ""}
                          onChange={(e) => updateField(group.key, key, e.target.value)}
                        />
                      )}
                    </>
                  )}
                  {canReset && (
                    <button
                      type="button"
                      className="reset-btn"
                      disabled={saveM.isPending}
                      onClick={() => resetField(group.key, key)}
                      aria-label={`Reset ${label} to default`}
                    >
                      Reset to default
                    </button>
                  )}
                </div>
              );
            })}
          </section>
        );
      })}
      {error && <p className="text-danger text-sm" role="alert">{error}</p>}
      {message && <p className="text-accent text-sm" role="status">{message}</p>}
      <button className="btn-primary" disabled={!hasChanges || saveM.isPending} onClick={save}>
        {saveM.isPending ? "Saving sidecar settings..." : "Save sidecar settings"}
      </button>
    </div>
  );
}
