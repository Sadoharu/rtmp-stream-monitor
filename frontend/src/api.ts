export type Stream = {
  id: string;
  name: string;
  local_url: string;
  public_url: string;
  source_url: string;
};

export type Probe = {
  id: string;
  name: string;
  location: string;
  platform: string;
  role: string;
  stream_id: string;
  last_seen_at: string | null;
  last_seen_age_seconds: number | null;
  telemetry_observed_at: string | null;
  telemetry_age_seconds: number | null;
  status: string;
  metrics: Record<string, unknown>;
  events: ProbeEvent[];
};

export type ProbeEvent = {
  code: string;
  severity?: string;
  timestamp?: string;
  details?: Record<string, unknown>;
};

export type Incident = {
  id: string;
  stream_id: string;
  opened_at: string;
  updated_at: string;
  resolved_at: string | null;
  severity: string;
  diagnosis: string;
  probable_location: string;
  affected_agents: string[];
  symptoms: Array<Record<string, unknown>>;
  context: Record<string, unknown>;
  active: boolean;
};

export type Dashboard = {
  generated_at: string;
  streams: Stream[];
  agents: Probe[];
  incidents: Incident[];
  openai_explanations_available: boolean;
  thresholds: { agent_offline_seconds: number; stream_offline_seconds: number };
  clock_warning: { warning: boolean; message?: string; [key: string]: unknown } | null;
};

export type SeriesPoint = {
  timestamp: string;
  bucket_seconds: number;
  min_bps: number;
  avg_bps: number;
  max_bps: number;
  sample_count: number;
  expected_count: number;
  quality: "MEASURED" | "PARTIAL" | string;
  measurement_window_seconds_avg: number;
  last_observed_at: string;
};

export type SeriesGap = { from: string; to: string; reason: string };

export type ProbeSeries = {
  probe: { id: string; name: string; role: string; profile: string; platform: string };
  metric: string;
  unit: string;
  points: SeriesPoint[];
  gaps: SeriesGap[];
};

export type SeriesResponse = {
  stream_id: string;
  from: string;
  to: string;
  actual_resolution_seconds: number;
  generated_at: string;
  series: ProbeSeries[];
};

export type Evidence = {
  id?: string;
  probe_id: string | null;
  metric: string;
  observed_at: string | null;
  value: unknown;
  unit: string | null;
  comparison: unknown;
  fact?: string;
};

export type TimelineEvent = {
  id: string;
  kind: "incident" | "probe_event" | string;
  stream_id: string;
  probe_id: string | null;
  probe_name: string | null;
  role: string | null;
  code: string;
  severity: "INFO" | "WARNING" | "CRITICAL" | string;
  started_at: string;
  ended_at: string | null;
  state: "ACTIVE" | "RESOLVED" | string;
  summary: string;
  explanation: string;
  cause_key?: string;
  confidence: "UNCONFIRMED" | "LIKELY" | "CONFIRMED" | string;
  probable_location: string | null;
  evidence: Evidence[];
  evidence_ids?: string[];
  other_possible_causes?: string[];
  next_checks?: string[];
  explanation_source?: string;
  ai_explanation_available?: boolean;
};

export type EventsResponse = {
  stream_id: string;
  from: string;
  to: string;
  generated_at: string;
  events: TimelineEvent[];
};

export type Enrollment = {
  agent_id: string;
  name: string;
  code: string;
  expires_at: string;
  expires_in_seconds: number;
};

export type Explanation = {
  likely_cause?: string;
  cause_key?: string;
  confidence?: string;
  evidence?: Array<{ id?: string; fact?: string; [key: string]: unknown }>;
  evidence_ids?: string[];
  other_possible_causes?: string[];
  next_checks?: string[];
  ai_status?: string;
  [key: string]: unknown;
};

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = localStorage.getItem("rtmp-monitor-token");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
  if (response.status === 204) return undefined as T;
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail : response.statusText || `HTTP ${response.status}`);
  }
  return body as T;
}

export const jsonBody = (value: unknown): Pick<RequestInit, "method" | "body"> => ({
  method: "POST",
  body: JSON.stringify(value),
});
