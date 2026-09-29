# RTMP Monitor 2.0 — контракт series/events (M0)

Цей контракт задає дані для головного графіка. Усі часові поля — ISO 8601 UTC із суфіксом `Z` або явним offset. Не перетворювати невідомі виміри на нулі та не домальовувати лінію через прогалину.

## Бітрейт

`received_media_bitrate_bps` — швидкість надходження розміру encoded audio/video пакетів, виміряна за wall-clock вікном у probe. Це медіабайтність, а не швидкість Ethernet/IP/RTMP з протокольними накладними витратами. Показувати її як Mbps. В одному sample зберігати `measurement_window_seconds`; профіль `DEEP`/`LIGHT` впливає на інші показники, але семантика бітрейту має бути однакова.

На сервер надходить приблизно односекундний sample. Для запиту з coarse resolution API агрегує samples в UTC-бакети та повертає min/avg/max. Для raw resolution поля min/avg/max дорівнюють значенню sample, якщо бакет містить один sample. Бакети без samples не мають точки; API окремо повертає прогалини. Нуль є дійсним виміром і не означає відсутності даних.

## GET `/api/v2/streams/{stream_id}/series`

Query parameters:

- `from`, `to`: обов'язкові RFC 3339 моменти; `from < to`.
- `resolution`: запитана тривалість бакета (`1s`, `5s`, `10s`, `1m`, `5m`, `1h`). Сервер повертає `actual_resolution_seconds`, який може бути збільшений для обмеження числа точок.
- `probe_ids`: необов'язковий список ID, повторюваний query parameter; без нього — усі probe потоку.

TypeScript DTO:

```ts
type ProbeRole = "SOURCE" | "SERVER_INGRESS" | "SERVER_EGRESS" | "CLIENT";
type DataQuality = "MEASURED" | "PARTIAL" | "MISSING" | "INVALID";
type GapReason = "NO_SAMPLE" | "PROBE_OFFLINE" | "MEASUREMENT_UNAVAILABLE" | "MEASUREMENT_WARMUP";

interface SeriesResponse {
  stream_id: string;
  from: string;                    // UTC
  to: string;                      // UTC
  actual_resolution_seconds: number;
  generated_at: string;            // UTC server time
  series: ProbeSeries[];
}

interface ProbeSeries {
  probe: {
    id: string;
    name: string;
    role: ProbeRole;
    profile: "LIGHT" | "DEEP" | "unknown"; // unknown for legacy/fully rolled-up history without recent profile data
    platform: string;
  };
  metric: "received_media_bitrate_bps";
  unit: "bps";
  points: Array<{
    timestamp: string;             // UTC bucket start
    bucket_seconds: number;
    min_bps: number;
    avg_bps: number;
    max_bps: number;
    sample_count: number;
    expected_count: number;
    quality: "MEASURED" | "PARTIAL";
    measurement_window_seconds_avg: number;
    last_observed_at: string;      // UTC time of newest raw sample in bucket
  }>;
  gaps: Array<{
    from: string;                  // UTC, inclusive
    to: string;                    // UTC, exclusive
    reason: GapReason;
  }>;
}
```

Example (shortened):

```json
{
  "stream_id": "demo-h264",
  "from": "2026-09-29T04:15:00Z",
  "to": "2026-09-29T04:16:00Z",
  "actual_resolution_seconds": 1,
  "generated_at": "2026-09-29T04:16:00.200Z",
  "series": [{
    "probe": {"id":"p-1","name":"client-win-01","role":"CLIENT","profile":"DEEP","platform":"Windows"},
    "metric": "received_media_bitrate_bps",
    "unit": "bps",
    "points": [
      {"timestamp":"2026-09-29T04:15:00Z","bucket_seconds":1,"min_bps":6900000,"avg_bps":6900000,"max_bps":6900000,"sample_count":1,"expected_count":1,"quality":"MEASURED","measurement_window_seconds_avg":1,"last_observed_at":"2026-09-29T04:15:00.910Z"},
      {"timestamp":"2026-09-29T04:15:01Z","bucket_seconds":1,"min_bps":1100000,"avg_bps":1100000,"max_bps":1100000,"sample_count":1,"expected_count":1,"quality":"MEASURED","measurement_window_seconds_avg":1,"last_observed_at":"2026-09-29T04:15:01.920Z"}
    ],
    "gaps": [{"from":"2026-09-29T04:15:10Z","to":"2026-09-29T04:15:14Z","reason":"PROBE_OFFLINE"}]
  }]
}
```

Bucket assignment is UTC epoch aligned: `[bucket_start, bucket_start + bucket_seconds)`. `expected_count` is derived from the configured agent publish interval for that probe and bucket width; `sample_count < expected_count` yields `PARTIAL`. An empty range is represented as a gap, never as zero. For raw samples with no measured bitrate, use `MEASUREMENT_UNAVAILABLE`; after a reconnect, do not emit a measured point until the rolling window is full, and expose `MEASUREMENT_WARMUP`. Legacy history is not backfilled from stream metadata or FFmpeg's output bitrate. Keep both `observed_at` (agent clock) and `received_at` (server clock) internally so clock skew and delivery delay remain visible.

## GET `/api/v2/streams/{stream_id}/events`

Query parameters `from`, `to`, optional repeated `probe_ids`, and optional `severity`. Return both raw probe events and correlated incidents in one normalized list. `started_at` and `ended_at` are event time, not database ingestion time; instantaneous events use `ended_at: null`.

```ts
type EventKind = "probe_event" | "incident";
type Severity = "INFO" | "WARNING" | "CRITICAL";

interface StreamEvent {
  id: string;
  kind: EventKind;
  stream_id: string;
  probe_id: string | null;
  probe_name: string | null;
  role: ProbeRole | null;
  code: string;                     // stable machine code, e.g. STREAM_STALL
  severity: Severity;
  started_at: string;               // UTC
  ended_at: string | null;          // UTC
  state: "ACTIVE" | "RESOLVED";
  summary: string;                  // deterministic human-readable text
  explanation: string;              // evidence-grounded; may state uncertainty
  confidence: "CONFIRMED" | "LIKELY" | "UNCONFIRMED";
  cause_key?: string;               // deterministic incident assessment, when this is an incident
  probable_location: string | null;
  evidence_ids?: string[];
  other_possible_causes?: string[];
  next_checks?: string[];
  explanation_source?: "deterministic_rules" | "openai";
  ai_explanation_available?: boolean;
  evidence: Array<{
    id?: string;                    // present for deterministic incident facts
    probe_id: string | null;
    metric: string;
    observed_at: string | null;     // null for a fact synthesized from multiple observations
    value: number | string | boolean | null;
    unit: string | null;
    comparison: string | null;
    fact?: string;                  // human-readable deterministic fact, with no fabricated timestamp
  }>;
}

interface EventsResponse {
  stream_id: string;
  from: string;
  to: string;
  generated_at: string;
  events: StreamEvent[];            // sorted by started_at ascending
}
```

The explanation layer may rephrase the deterministic summary, but it may only receive these evidence fields and must preserve `confidence` and uncertainty. Do not infer a network root cause from `CLIENT_PROBLEM` alone.

M1 pairs `FREEZE_START`/`FREEZE_END`, `SILENCE_START`/`SILENCE_END` and `KEYFRAME_GAP`/`KEYFRAME_GAP_END` when both occur in the selected interval. A start without its matching end remains an open event in that interval; instantaneous markers have a null `ended_at`. M3 will refine active state and evidence from current probe state.

## Compatibility and response limits

- Existing `/api/v1/telemetry` ingest and v1 agents remain supported during migration.
- Reject invalid ranges with `422`; unknown streams return `404`.
- Cap returned points per probe (initial target 10,000) by increasing `actual_resolution_seconds`, while retaining min/avg/max. Paginate events only if required by measured load.
- Deterministic tests must cover UTC bucket boundaries, partial buckets, real zero vs missing, 5-second dip preservation in a 1-minute aggregate, and event ordering/filters before UI integration.
