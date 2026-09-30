import { useEffect, useState } from "react";
import { api, type Explanation, type TimelineEvent } from "../api";
import { eventLabel, formatDateTime, formatDuration, formatDurationSeconds, roleLabel, severityLabel } from "../format";

type Props = { event: TimelineEvent | null; onClose: () => void };

const EVIDENCE_LABELS: Record<string, string> = {
  received_media_bitrate_bps: "Прийнятий медіабітрейт",
  last_frame_age: "Вік останнього відеокадру",
  last_audio_frame_age: "Вік останнього аудіокадру",
  last_frame_age_seconds: "Вік відеокадру під час події",
  last_audio_age_seconds: "Вік аудіо під час події",
  frame_age_seconds: "Вік кадру під час події",
  age_seconds: "Вік виміру під час події",
  gap_seconds: "Інтервал між вимірами",
  duration_seconds: "Тривалість симптому",
  value: "Медіачас події",
  seconds_without_keyframe: "Час без ключового кадру",
  threshold_seconds: "Поріг виявлення",
  expected_gop_seconds: "Звичний інтервал між ключовими кадрами",
  expected_gop_frames: "Очікувана кількість кадрів у GOP",
  progress_age_seconds: "Час без звіту FFmpeg",
  media_age_seconds: "Час без нових медіаданих",
  seconds_without_ingress_progress: "Час без руху на вході сервера",
  delta_seconds: "Зміна часу",
  decode_errors: "Помилки декодування",
  reconnect_count: "Перепідключення",
  previous_pts: "Попередній PTS",
  pts: "Поточний PTS",
  previous_dts: "Попередній DTS",
  dts: "Поточний DTS",
  rtt_ms: "Мережева затримка RTT",
  packet_loss_percent: "Втрата ICMP-відповідей",
  tcp_retransmissions: "Повторні передачі TCP",
  tcp_duplicate_ack_episodes: "Епізоди TCP duplicate ACK",
  tcp_duplicate_acks: "Повторні TCP-підтвердження",
  stream_index: "Індекс медіапотоку",
  return_code: "Код завершення FFmpeg",
  last_ingress_progress_age: "Вік останнього руху на вході",
  ingress_recv_kbps_30s: "Приймання SRS за 30 секунд",
  ingress_recv_bytes: "Прийняті байти SRS",
  ingress_frames: "Кадри, зафіксовані SRS",
  srs_api_available: "API SRS доступне",
  ingress_active: "Публікація активна",
  "network.tcp_retransmissions": "Повторні передачі TCP на хості",
  "network.tcp_duplicate_ack_episodes": "Епізоди duplicate ACK у TCP-з’єднанні probe",
  "network.tcp_duplicate_acks": "Повторні TCP-підтвердження у з’єднанні probe",
  "network.tcp_receiver_stats_status": "Доступність окремого TCP виміру",
  "network.rtt_ms": "Мережева затримка RTT",
  "network.packet_loss_percent": "Втрата ICMP-відповідей",
  "network.tcp_state": "Стан TCP-з'єднання",
  "network.provider": "Джерело мережевого виміру",
};

function evidenceLabel(metric: string): string {
  const key = metric.startsWith("event.details.") ? metric.slice("event.details.".length) : metric;
  return EVIDENCE_LABELS[metric] ?? EVIDENCE_LABELS[key] ?? key.replaceAll("_", " ").replace(/^./, (first) => first.toUpperCase());
}

function contextualEvidenceLabel(metric: string, code: string): string {
  const key = metric.startsWith("event.details.") ? metric.slice("event.details.".length) : metric;
  if (key === "duration_seconds") {
    if (code.startsWith("SILENCE_")) return "Тривалість тиші";
    if (code.startsWith("FREEZE_")) return "Тривалість завмирання";
  }
  return evidenceLabel(metric);
}

function evidenceValue(value: unknown, unit: string | null): string {
  if (typeof value === "number" && Number.isFinite(value)) {
    const formatted = new Intl.NumberFormat("uk-UA", { maximumFractionDigits: unit === "count" ? 0 : 3 }).format(value);
    if (unit === "bps") return `${new Intl.NumberFormat("uk-UA", { maximumFractionDigits: 2 }).format(value / 1_000_000)} Mbps`;
    if (unit === "s") return `${formatted} с`;
    if (unit === "ms") return `${formatted} мс`;
    if (unit === "%") return `${formatted}%`;
    if (unit === "count") return formatted;
    if (unit === "code") return formatted;
    if (unit === "kbps") return `${formatted} кбіт/с`;
    if (unit === "bytes") return `${formatted} Б`;
    if (unit === "frames") return `${formatted} кадрів`;
    return unit ? `${formatted} ${unit}` : formatted;
  }
  if (typeof value === "boolean") return value ? "Так" : "Ні";
  if (typeof value === "string") return `${value}${unit ? ` ${unit}` : ""}`;
  return "—";
}

function eventDuration(event: TimelineEvent): string {
  if (event.code === "FREEZE_DURATION" || event.code === "SILENCE_DURATION") {
    const measured = event.evidence.find((item) => item.metric === "event.details.duration_seconds")?.value;
    return typeof measured === "number" ? formatDurationSeconds(measured) : "—";
  }
  if (event.ended_at) return formatDuration(event.started_at, event.ended_at);
  return event.state === "ACTIVE" ? formatDuration(event.started_at) : "—";
}

function evidenceComparison(value: unknown): string | null {
  if (typeof value !== "string" || !value) return null;
  const windowMatch = /^window=(\d+(?:\.\d+)?)s$/.exec(value);
  if (windowMatch) return `Вікно вимірювання: ${windowMatch[1]} с`;
  return value;
}

export function EventDetails({ event, onClose }: Props) {
  const [explanation, setExplanation] = useState<Explanation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setExplanation(null);
    setError("");
  }, [event?.id]);

  if (!event) return null;

  const requestAi = async () => {
    if (event.kind !== "incident") return;
    setBusy(true);
    setError("");
    try {
      const response = await api<Explanation>(`/api/v1/incidents/${encodeURIComponent(event.id)}/explanation`, { method: "POST" });
      setExplanation(response);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не вдалося отримати пояснення.");
    } finally {
      setBusy(false);
    }
  };

  return <div className="drawer-scrim" onMouseDown={(mouseEvent) => { if (mouseEvent.target === mouseEvent.currentTarget) onClose(); }}>
    <aside className="event-drawer" aria-labelledby="event-title" role="dialog" aria-modal="true">
      <div className="drawer-topline"><span className={`severity-mark severity-${event.severity.toLowerCase()}`} />
        <span>{severityLabel(event.severity)}</span><span className="drawer-spacer" />
        <button className="icon-button" onClick={onClose} aria-label="Закрити">×</button>
      </div>
      <h2 id="event-title">{eventLabel(event)}</h2>
      <p className="drawer-summary">{event.explanation}</p>
      <div className="event-metadata">
        <div><span>Початок</span><strong>{formatDateTime(event.started_at)}</strong></div>
        <div><span>Тривалість</span><strong>{eventDuration(event)}</strong></div>
        <div><span>Пункт</span><strong>{event.probe_name || "Кілька probe"}{event.role ? ` · ${roleLabel(event.role)}` : ""}</strong></div>
        <div><span>Стан / упевненість</span><strong>{event.state === "ACTIVE" ? "Триває" : "Завершено"} · {event.confidence === "CONFIRMED" ? "підтверджено" : event.confidence === "LIKELY" ? "імовірно" : "не підтверджено"}</strong></div>
      </div>

      {event.probable_location && <section className="drawer-section probable-box">
        <p className="eyebrow">ЙМОВІРНЕ МІСЦЕ ПРОБЛЕМИ</p>
        <strong>{event.probable_location}</strong>
        {event.confidence !== "CONFIRMED" && <small>Це висновок за наявними спостереженнями, а не остаточний доказ.</small>}
      </section>}

      <section className="drawer-section">
        <div className="drawer-section-heading"><h3>Що підтверджують дані</h3><span>{event.evidence.length}</span></div>
        {event.evidence.length ? <ul className="evidence-list">{event.evidence.map((item, index) => {
          const fact = typeof item.fact === "string" && item.fact.length > 0;
          const comparison = evidenceComparison(item.comparison);
          return <li key={item.id ?? `${item.metric}-${item.observed_at ?? "unknown"}-${index}`}><span className="evidence-pin" /><div>
            <strong>{fact ? item.fact : contextualEvidenceLabel(item.metric, event.code)}</strong>
            {!fact && <span className="evidence-value">{evidenceValue(item.value, item.unit)}{comparison ? ` · ${comparison}` : ""}</span>}
            {item.observed_at && <small>{formatDateTime(item.observed_at)}</small>}
          </div></li>;
        })}</ul> : <p className="muted">Для цієї події не збережено окремих вимірів як доказів.</p>}
      </section>

      {!!event.other_possible_causes?.length && <section className="drawer-section">
        <h3>Що ще могло вплинути</h3><ul className="plain-list">{event.other_possible_causes.map((cause) => <li key={cause}>{cause}</li>)}</ul>
      </section>}

      {!!event.next_checks?.length && <section className="drawer-section">
        <h3>Що перевірити</h3><ol className="plain-list">{event.next_checks.map((step) => <li key={step}>{step}</li>)}</ol>
      </section>}

      {event.kind === "incident" && event.ai_explanation_available && <section className="drawer-section ai-section">
        <div className="ai-heading"><div><h3>Пояснення OpenAI</h3><p>Запит виконується лише після натискання. Модель отримує відібрані телеметричні факти.</p></div><span className="ai-spark">✳</span></div>
        <button className="button button-secondary" disabled={busy} onClick={() => void requestAi()}>{busy ? "Готуємо пояснення…" : explanation?.ai_status === "cached" ? "Оновити пояснення" : "Пояснити інцидент"}</button>
        {error && <p className="form-error" role="alert">{error}</p>}
        {explanation && <div className="ai-result"><strong>{String(explanation.likely_cause ?? "")}</strong>{explanation.next_checks?.length ? <ul>{explanation.next_checks.map((step) => <li key={step}>{step}</li>)}</ul> : null}<small>Джерело: {explanation.ai_status ?? "rules"}</small></div>}
      </section>}
    </aside>
  </div>;
}
