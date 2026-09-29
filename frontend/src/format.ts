import type { Probe, TimelineEvent } from "./api";

export type RangeKey = "15m" | "1h" | "6h" | "24h" | "7d";

export const RANGES: Array<{ key: RangeKey; label: string; ms: number; resolution: string }> = [
  { key: "15m", label: "15 хв", ms: 15 * 60_000, resolution: "1s" },
  { key: "1h", label: "1 год", ms: 60 * 60_000, resolution: "5s" },
  { key: "6h", label: "6 год", ms: 6 * 60 * 60_000, resolution: "10s" },
  { key: "24h", label: "24 год", ms: 24 * 60 * 60_000, resolution: "1m" },
  { key: "7d", label: "7 днів", ms: 7 * 24 * 60 * 60_000, resolution: "5m" },
];

export const ROLE_LABEL: Record<string, string> = {
  SOURCE: "Джерело",
  SERVER_INGRESS: "Вхід сервера",
  SERVER_EGRESS: "Вихід сервера",
  CLIENT: "Клієнт",
};

export const STATUS_LABEL: Record<string, string> = {
  OK: "Працює",
  WARNING: "Увага",
  CRITICAL: "Критично",
  AGENT_OFFLINE: "Probe офлайн",
  STREAM_OFFLINE: "Потік офлайн",
  STREAM_STALLED: "Потік завис",
  TELEMETRY_STALE: "Дані застаріли",
  NEVER_SEEN: "Очікує підключення",
  UNKNOWN: "Невідомо",
};

const EVENT_LABEL: Record<string, string> = {
  CLIENT_PROBLEM: "Помилка на клієнті",
  CLIENT_PATH_UNCONFIRMED: "На клієнті є симптом, шлях доставки не локалізовано",
  NETWORK_PATH_PROBLEM: "Проблема мережевого шляху",
  NETWORK_PATH_UNCONFIRMED: "Є мережевий сигнал, його вплив не підтверджено",
  SOURCE_OR_INGEST_PROBLEM: "Проблема джерела або входу сервера",
  SOURCE_OR_INGEST_UNCONFIRMED: "На вході є симптом, його поширення не підтверджено",
  RTMP_SERVER_RESTREAM_PROBLEM: "Проблема локальної віддачі сервера",
  RTMP_SERVER_RESTREAM_UNCONFIRMED: "На виході є симптом, стан входу не підтверджено",
  SOURCE_TO_SERVER_UNCONFIRMED: "Проблему між джерелом і сервером не локалізовано",
  UPSTREAM_OR_SERVER_UNCONFIRMED: "Є симптоми, джерело ще не підтверджене",
  AGENT_OFFLINE: "Probe перестав передавати дані",
  FREEZE_START: "Почалося завмирання відео",
  FREEZE_DURATION: "Тривалість завмирання відео",
  FREEZE_END: "Відео відновилося",
  KEYFRAME_GAP: "Завеликий інтервал між ключовими кадрами",
  KEYFRAME_GAP_END: "Інтервал ключових кадрів відновився",
  DECODE_ERROR: "Помилка декодування кадру",
  PTS_REGRESSION: "Час кадру повернувся назад",
  DTS_REGRESSION: "Порушився порядок декодування кадрів",
  PTS_JUMP: "Стрибок медіачасу",
  STREAM_STALL: "Припинили надходити кадри",
  PROGRESS_STALE: "FFmpeg не звітує про прогрес",
  AUDIO_MISSING: "Зникло аудіо",
  SILENCE_START: "Почалася тиша в аудіо",
  SILENCE_DURATION: "Виміряно тривалість тиші в аудіо",
  SILENCE_END: "Аудіо відновилося",
  AV_TIMESTAMP_DRIFT: "Розійшлися часові позначки аудіо й відео",
  FFMPEG_DEAD: "Процес FFmpeg перестав відповідати",
  FFMPEG_EXIT: "Процес FFmpeg завершився",
  FFMPEG_RESTART: "Probe перезапустив FFmpeg",
  PROBE_ERROR: "Помилка в агенті спостереження",
  SRS_PUBLISH_STATE_UNAVAILABLE: "SRS не надав стан публікації",
  SRS_COUNTERS_UNAVAILABLE: "SRS не надав лічильники медіаданих",
  SRS_API_UNAVAILABLE: "Немає відповіді від API SRS",
  INGRESS_RECOVERED: "Спостереження за входом SRS відновилося",
  TCP_RETRANSMISSION: "Повторна передача TCP-пакетів",
  TCP_RETRANSMITS: "Повторні передачі TCP-пакетів",
  TCP_RESET: "TCP-з'єднання скинулось",
  PACKET_LOSS: "Втрата мережевих пакетів",
  RTT_SPIKE: "Зросла затримка мережі",
};

export function eventLabel(event: Pick<TimelineEvent, "code" | "summary">): string {
  if (EVENT_LABEL[event.code]) return EVENT_LABEL[event.code];
  if (event.summary && event.summary !== event.code) return event.summary;
  return "Подію потрібно перевірити";
}

export function severityLabel(value: string): string {
  return ({ INFO: "Інформація", WARNING: "Попередження", CRITICAL: "Критична" } as Record<string, string>)[value] ?? value;
}

export function roleLabel(value: string): string {
  return ROLE_LABEL[value] ?? value.replaceAll("_", " ");
}

export function statusLabel(value: string): string {
  return STATUS_LABEL[value] ?? value.replaceAll("_", " ");
}

export function formatMbps(bps: number | null | undefined, decimals = 2): string {
  if (bps === null || bps === undefined || !Number.isFinite(bps)) return "—";
  if (bps === 0) return "0 Mbps";
  return `${(bps / 1_000_000).toFixed(decimals)} Mbps`;
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "Немає даних";
  if (seconds < 1) return "щойно";
  if (seconds < 60) return `${Math.round(seconds)} с тому`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} хв тому`;
  return `${Math.floor(seconds / 3600)} год тому`;
}

export function formatDuration(from: string, to?: string | null): string {
  const start = Date.parse(from);
  const end = to ? Date.parse(to) : Date.now();
  const seconds = (end - start) / 1000;
  return formatDurationSeconds(seconds);
}

export function formatDurationSeconds(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)} с`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  if (minutes < 60) return `${minutes} хв ${rest} с`;
  return `${Math.floor(minutes / 60)} год ${Math.floor(minutes % 60)} хв`;
}

export function formatTime(value: string | null | undefined, options?: Intl.DateTimeFormatOptions): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("uk-UA", options ?? { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}

export function formatDateTime(value: string | null | undefined): string {
  return formatTime(value, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function latestBitrate(probes: Probe[]): number | null {
  const values = probes
    .map((probe) => probe.metrics.received_media_bitrate_bps)
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  if (!values.length) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

export function overallStatus(probes: Probe[]): string {
  if (!probes.length) return "UNKNOWN";
  if (probes.some((probe) => ["CRITICAL", "STREAM_OFFLINE", "STREAM_STALLED"].includes(probe.status))) return "CRITICAL";
  if (probes.some((probe) => ["WARNING", "AGENT_OFFLINE", "TELEMETRY_STALE"].includes(probe.status))) return "WARNING";
  if (probes.every((probe) => probe.status === "NEVER_SEEN")) return "UNKNOWN";
  return "OK";
}

export function mediaSummary(probe: Probe) {
  const m = probe.metrics;
  const nested = (m.network && typeof m.network === "object" ? m.network : {}) as Record<string, unknown>;
  return {
    bitrate: typeof m.received_media_bitrate_bps === "number" ? m.received_media_bitrate_bps : null,
    fps: typeof m.source_fps === "number" ? m.source_fps : typeof m.frame_rate === "number" ? m.frame_rate : null,
    resolution: typeof m.resolution === "string" ? m.resolution : "—",
    codec: typeof m.video_codec === "string" ? m.video_codec.toUpperCase() : "—",
    keyframeAge: typeof m.last_keyframe_age === "number" ? m.last_keyframe_age : null,
    gop: typeof m.current_gop_duration === "number" ? m.current_gop_duration : null,
    decodeErrors: typeof m.decode_errors === "number" ? m.decode_errors : 0,
    retransmits: typeof nested.tcp_retransmissions === "number" ? nested.tcp_retransmissions : typeof m.tcp_retransmissions === "number" ? m.tcp_retransmissions : null,
    rtt: typeof nested.rtt_ms === "number" ? nested.rtt_ms : typeof m.rtt_ms === "number" ? m.rtt_ms : null,
  };
}
