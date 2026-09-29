import { lazy, Suspense, useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { api, jsonBody, type Dashboard, type EventsResponse, type Incident, type Probe, type SeriesResponse, type Stream, type TimelineEvent } from "./api";
import { EventDetails } from "./components/EventDetails";
import { ProbeManager } from "./components/ProbeManager";
import { RANGES, eventLabel, formatAge, formatDateTime, formatDuration, formatMbps, mediaSummary, overallStatus, roleLabel, severityLabel, statusLabel, type RangeKey } from "./format";

type Page = "streams" | "stream" | "probes" | "incidents";
const StreamChart = lazy(() => import("./components/StreamChart").then((module) => ({ default: module.StreamChart })));

const PAGE_LABEL: Record<Page, string> = { streams: "Потоки", stream: "Огляд потоку", probes: "Пункти спостереження", incidents: "Історія інцидентів" };

export function App() {
  const [token, setToken] = useState(() => localStorage.getItem("rtmp-monitor-token") ?? "");
  const [authState, setAuthState] = useState<"checking" | "signed-out" | "ready">("checking");
  const [loginToken, setLoginToken] = useState("");
  const [loginError, setLoginError] = useState("");
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [incidentList, setIncidentList] = useState<Incident[]>([]);
  const [streamId, setStreamId] = useState(() => localStorage.getItem("rtmp-monitor-stream") ?? "");
  const [page, setPage] = useState<Page>("streams");
  const [range, setRange] = useState<RangeKey>("1h");
  const [live, setLive] = useState(true);
  const [series, setSeries] = useState<SeriesResponse | null>(null);
  const [events, setEvents] = useState<EventsResponse | null>(null);
  const [selectedEvent, setSelectedEvent] = useState<TimelineEvent | null>(null);
  const [visibleProbeIds, setVisibleProbeIds] = useState<string[] | null>(null);
  const [showStreamDialog, setShowStreamDialog] = useState(false);
  const [streamSaving, setStreamSaving] = useState(false);
  const [streamError, setStreamError] = useState("");
  const [globalError, setGlobalError] = useState("");
  const [streamDraft, setStreamDraft] = useState({ id: "", name: "", local_url: "", public_url: "", source_url: "" });

  useEffect(() => {
    if (!token) {
      setAuthState("signed-out");
      return;
    }
    let cancelled = false;
    void api<{ authenticated: boolean }>("/api/v1/auth/check")
      .then(() => { if (!cancelled) setAuthState("ready"); })
      .catch(() => {
        if (cancelled) return;
        localStorage.removeItem("rtmp-monitor-token");
        setToken("");
        setAuthState("signed-out");
      });
    return () => { cancelled = true; };
  }, [token]);

  const refreshDashboard = useCallback(async () => {
    const next = await api<Dashboard>("/api/v1/dashboard");
    setDashboard(next);
    setGlobalError("");
    return next;
  }, []);

  const refreshIncidents = useCallback(async () => {
    const next = await api<Incident[]>("/api/v1/incidents?limit=500");
    setIncidentList(next);
  }, []);

  useEffect(() => {
    if (authState !== "ready") return;
    let cancelled = false;
    const refresh = async () => {
      try {
        await refreshDashboard();
        if (!cancelled && page === "incidents") await refreshIncidents();
      } catch (caught) {
        if (!cancelled) setGlobalError(caught instanceof Error ? caught.message : "Не вдалося оновити дані.");
      }
    };
    void refresh();
    if (!live) return () => { cancelled = true; };
    const interval = window.setInterval(() => void refresh(), 12_000);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [authState, live, page, refreshDashboard, refreshIncidents]);

  const streams = dashboard?.streams ?? [];
  const activeStream = streams.find((item) => item.id === streamId) ?? streams[0] ?? null;
  const streamProbes = useMemo(() => dashboard?.agents.filter((probe) => probe.stream_id === activeStream?.id) ?? [], [dashboard?.agents, activeStream?.id]);
  const streamIncidents = useMemo(() => (page === "incidents" ? incidentList : dashboard?.incidents ?? []).filter((item) => !activeStream || item.stream_id === activeStream.id), [activeStream, dashboard?.incidents, incidentList, page]);

  useEffect(() => {
    if (!activeStream) return;
    if (streamId !== activeStream.id) setStreamId(activeStream.id);
    localStorage.setItem("rtmp-monitor-stream", activeStream.id);
  }, [activeStream, streamId]);

  useEffect(() => {
    if (page !== "stream" || !activeStream || authState !== "ready") {
      setSeries(null);
      setEvents(null);
      return;
    }
    let cancelled = false;
    const load = async () => {
      const selectedRange = RANGES.find((item) => item.key === range) ?? RANGES[1];
      const to = Date.now();
      const from = to - selectedRange.ms;
      const query = new URLSearchParams({ from: new Date(from).toISOString(), to: new Date(to).toISOString(), resolution: selectedRange.resolution });
      try {
        const [nextSeries, nextEvents] = await Promise.all([
          api<SeriesResponse>(`/api/v2/streams/${encodeURIComponent(activeStream.id)}/series?${query}`),
          api<EventsResponse>(`/api/v2/streams/${encodeURIComponent(activeStream.id)}/events?${query}`),
        ]);
        if (cancelled) return;
        setSeries(nextSeries);
        setEvents(nextEvents);
        setVisibleProbeIds((current) => {
          if (current === null) return nextSeries.series.map((row) => row.probe.id);
          const valid = new Set(nextSeries.series.map((row) => row.probe.id));
          return current.filter((id) => valid.has(id));
        });
      } catch (caught) {
        if (!cancelled) setGlobalError(caught instanceof Error ? caught.message : "Не вдалося завантажити часові ряди.");
      }
    };
    void load();
    if (!live) return () => { cancelled = true; };
    const interval = window.setInterval(() => void load(), 10_000);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [activeStream?.id, authState, live, page, range]);

  const signIn = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoginError("");
    localStorage.setItem("rtmp-monitor-token", loginToken.trim());
    try {
      await api<{ authenticated: boolean }>("/api/v1/auth/check");
      setToken(loginToken.trim());
      setAuthState("ready");
      setLoginToken("");
    } catch (caught) {
      localStorage.removeItem("rtmp-monitor-token");
      setLoginError(caught instanceof Error ? caught.message : "Невірний токен доступу.");
    }
  };

  const signOut = () => {
    localStorage.removeItem("rtmp-monitor-token");
    setToken("");
    setDashboard(null);
    setAuthState("signed-out");
  };

  const openIncident = async (incident: Incident) => {
    setGlobalError("");
    const start = Date.parse(incident.opened_at);
    const end = incident.resolved_at ? Date.parse(incident.resolved_at) : Date.now();
    const query = new URLSearchParams({ from: new Date(start - 5 * 60_000).toISOString(), to: new Date(Math.max(end + 5 * 60_000, start + 60_000)).toISOString() });
    try {
      const response = await api<EventsResponse>(`/api/v2/streams/${encodeURIComponent(incident.stream_id)}/events?${query}`);
      const event = response.events.find((row) => row.id === incident.id);
      setSelectedEvent(event ?? {
        id: incident.id, kind: "incident", stream_id: incident.stream_id, probe_id: null,
        probe_name: incident.affected_agents.join(", ") || null, role: null, code: incident.diagnosis,
        severity: incident.severity, started_at: incident.opened_at, ended_at: incident.resolved_at,
        state: incident.active ? "ACTIVE" : "RESOLVED", summary: incident.diagnosis,
        explanation: incident.probable_location, confidence: "UNCONFIRMED",
        probable_location: incident.probable_location, evidence: [],
      });
    } catch (caught) {
      setGlobalError(caught instanceof Error ? caught.message : "Не вдалося відкрити інцидент.");
    }
  };

  const saveStream = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setStreamError("");
    setStreamSaving(true);
    try {
      const created = await api<Stream>("/api/v1/streams", jsonBody({ ...streamDraft, id: streamDraft.id.trim(), name: streamDraft.name.trim() }));
      setStreamId(created.id);
      setPage("stream");
      setShowStreamDialog(false);
      setStreamDraft({ id: "", name: "", local_url: "", public_url: "", source_url: "" });
      await refreshDashboard();
    } catch (caught) {
      setStreamError(caught instanceof Error ? caught.message : "Не вдалося створити потік.");
    } finally {
      setStreamSaving(false);
    }
  };

  const toggleProbe = (probeId: string) => {
    setVisibleProbeIds((current) => {
      const visible = current ?? (series?.series.map((row) => row.probe.id) ?? []);
      return visible.includes(probeId) ? visible.filter((id) => id !== probeId) : [...visible, probeId];
    });
  };

  if (authState === "checking") return <div className="app-loading"><div className="brand-mark">R</div><span>Підключаємося до монітора…</span></div>;
  if (authState === "signed-out") return <main className="login-shell">
    <section className="login-card">
      <div className="brand-lockup"><span className="brand-symbol">R</span><span>RTMP <b>MONITOR</b></span></div>
      <p className="eyebrow">ЦЕНТР ДІАГНОСТИКИ ПОТОКУ</p>
      <h1>Увійдіть до моніторингу</h1>
      <p className="muted">Вставте admin token, який зберіг установник центрального сервера.</p>
      <form onSubmit={signIn}>
        <label className="field"><span>Admin token</span><input type="password" value={loginToken} onChange={(event) => setLoginToken(event.target.value)} autoComplete="current-password" autoFocus required /></label>
        {loginError && <p className="form-error" role="alert">{loginError}</p>}
        <button className="button button-primary login-submit">Підключитися <span>→</span></button>
      </form>
      <small className="login-footnote">Токен зберігається лише в цьому браузері.</small>
    </section>
  </main>;

  const view = page === "stream" && !activeStream ? "streams" : page;
  const goToStream = (id: string) => { setStreamId(id); setPage("stream"); };
  const activeCount = (dashboard?.agents ?? []).filter((probe) => probe.status === "OK" || probe.status === "WARNING").length;
  const pageSubtitles: Record<Page, string> = {
    streams: "Огляд джерел та стану передачі",
    stream: activeStream ? `Спільна часова шкала · ${activeStream.id}` : "Оберіть потік",
    probes: "Реєстрація та здоров’я агентів",
    incidents: "Події, що змінили стан потоку",
  };

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand-lockup"><span className="brand-symbol">R</span><span>RTMP <b>MONITOR</b></span></div>
      <div className="sidebar-workspace"><span className="workspace-indicator" /><span>Центральний сервер</span></div>
      <nav className="side-nav" aria-label="Головна навігація">
        <p className="nav-caption">МОНІТОРИНГ</p>
        <NavButton active={view === "streams" || view === "stream"} onClick={() => setPage(activeStream ? "stream" : "streams")} icon="◉" label="Потоки" badge={streams.length} />
        <NavButton active={view === "probes"} onClick={() => setPage("probes")} icon="⌁" label="Пункти спостереження" badge={dashboard?.agents.length ?? 0} />
        <NavButton active={view === "incidents"} onClick={() => setPage("incidents")} icon="⌖" label="Інциденти" badge={(dashboard?.incidents ?? []).filter((item) => item.active).length} />
      </nav>
      <div className="sidebar-spacer" />
      <div className="sidebar-health"><span className="health-pulse" /><div><strong>Монітор працює</strong><small>{activeCount} probe передають дані</small></div></div>
      <button className="nav-button signout-button" onClick={signOut}><span className="nav-icon">↪</span><span>Вийти</span></button>
      <div className="sidebar-version">RTMP Monitor · v2</div>
    </aside>

    <div className="main-column">
      <header className="topbar">
        <div className="breadcrumbs"><span>Моніторинг</span><span className="crumb-sep">/</span><strong>{PAGE_LABEL[view]}</strong></div>
        <div className="topbar-actions">
          {activeStream && <label className="stream-picker"><span>Потік</span><select value={activeStream.id} onChange={(event) => goToStream(event.target.value)} aria-label="Вибрати потік">{streams.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
          <span className={`live-status ${live ? "is-live" : "is-paused"}`}><i />{live ? "ОНОВЛЕННЯ LIVE" : "ПАУЗА"}</span>
          <button className="icon-button refresh-button" onClick={() => { void refreshDashboard().catch((caught) => setGlobalError(String(caught))); if (view === "incidents") void refreshIncidents(); }} aria-label="Оновити дані" title="Оновити дані">↻</button>
          {view !== "probes" && <button className="button button-secondary add-probe-top" onClick={() => setPage("probes")}>+ Додати probe</button>}
        </div>
      </header>

      <main className="content-area">
        <div className="page-heading">
          <div><p className="eyebrow">{PAGE_LABEL[view].toUpperCase()}</p><h1>{view === "stream" && activeStream ? activeStream.name : PAGE_LABEL[view]}</h1><p className="page-subtitle">{pageSubtitles[view]}</p></div>
          <div className="heading-actions">
            {view === "stream" && <button className={`button ${live ? "button-quiet" : "button-secondary"}`} onClick={() => setLive((current) => !current)}>{live ? "Ⅱ Пауза" : "▶ Продовжити"}</button>}
            {view === "streams" && <button className="button button-primary" onClick={() => { setStreamError(""); setShowStreamDialog(true); }}>+ Додати потік</button>}
          </div>
        </div>
        {globalError && <div className="global-alert" role="alert"><span>!</span>{globalError}<button onClick={() => setGlobalError("")}>Закрити</button></div>}
        {dashboard?.clock_warning?.warning && <div className="clock-alert"><span>◷</span><div><strong>Годинники probe не синхронізовані</strong><small>{dashboard.clock_warning.message ?? "Порівняння часу між точками може бути неточним."}</small></div></div>}

        {view === "streams" && <StreamsPage dashboard={dashboard} onOpen={goToStream} onAdd={() => setShowStreamDialog(true)} />}
        {view === "stream" && activeStream && <StreamPage
          stream={activeStream}
          probes={streamProbes}
          incidents={dashboard?.incidents.filter((item) => item.stream_id === activeStream.id) ?? []}
          series={series}
          events={events}
          range={range}
          setRange={setRange}
          live={live}
          onToggleProbe={toggleProbe}
          visibleProbeIds={visibleProbeIds ?? series?.series.map((row) => row.probe.id) ?? []}
          onSelectEvent={setSelectedEvent}
          onOpenProbes={() => setPage("probes")}
        />}
        {view === "probes" && dashboard && <ProbeManager dashboard={dashboard} onRefresh={() => void refreshDashboard()} />}
        {view === "incidents" && <IncidentPage incidents={streamIncidents} streams={streams} onOpen={(incident) => void openIncident(incident)} />}
      </main>
    </div>

    {showStreamDialog && <AddStreamDialog
      draft={streamDraft}
      error={streamError}
      saving={streamSaving}
      onChange={setStreamDraft}
      onClose={() => setShowStreamDialog(false)}
      onSave={(event) => void saveStream(event)}
    />}
    <EventDetails event={selectedEvent} onClose={() => setSelectedEvent(null)} />
  </div>;
}

function NavButton({ active, onClick, icon, label, badge }: { active: boolean; onClick: () => void; icon: string; label: string; badge: number }) {
  return <button className={`nav-button ${active ? "active" : ""}`} onClick={onClick}><span className="nav-icon">{icon}</span><span>{label}</span><span className="nav-badge">{badge}</span></button>;
}

function StreamsPage({ dashboard, onOpen, onAdd }: { dashboard: Dashboard | null; onOpen: (id: string) => void; onAdd: () => void }) {
  const streams = dashboard?.streams ?? [];
  if (!streams.length) return <section className="first-run panel">
    <div className="first-run-art"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><span>↗</span><i /><b /></div>
    <p className="eyebrow">ПОЧАТОК РОБОТИ</p><h2>Додайте перший потік</h2>
    <p>Вкажіть RTMP-адреси локального виходу сервера та клієнтів. Monitor лише читає потік і не впливає на його трансляцію.</p>
    <button className="button button-primary" onClick={onAdd}>Створити потік <span>→</span></button>
  </section>;

  return <div className="streams-page">
    <div className="overview-counters">
      <SummaryCard label="Потоки" value={String(streams.length).padStart(2, "0")} detail="у моніторингу" icon="◉" />
      <SummaryCard label="Probe онлайн" value={String(dashboard?.agents.filter((probe) => ["OK", "WARNING"].includes(probe.status)).length ?? 0).padStart(2, "0")} detail={`з ${dashboard?.agents.length ?? 0} зареєстрованих`} icon="⌁" />
      <SummaryCard label="Активні інциденти" value={String(dashboard?.incidents.filter((incident) => incident.active).length ?? 0).padStart(2, "0")} detail="потребують уваги" icon="⌖" alert={(dashboard?.incidents.filter((incident) => incident.active).length ?? 0) > 0} />
    </div>
    <div className="section-heading"><div><h2>Ваші потоки</h2><span>Виберіть потік, щоб переглянути бітрейт та часову шкалу</span></div><button className="button button-secondary" onClick={onAdd}>+ Додати потік</button></div>
    <div className="stream-grid">
      {streams.map((stream, index) => {
        const probes = dashboard?.agents.filter((item) => item.stream_id === stream.id) ?? [];
        const status = overallStatus(probes);
        const incidents = dashboard?.incidents.filter((item) => item.stream_id === stream.id) ?? [];
        const latest = incidents[0];
        const primaryProbe = probes.find((probe) => probe.role === "SERVER_EGRESS") ?? probes[0];
        const currentBps = typeof primaryProbe?.metrics.received_media_bitrate_bps === "number" ? primaryProbe.metrics.received_media_bitrate_bps : null;
        return <button className="stream-card" key={stream.id} onClick={() => onOpen(stream.id)}>
          <div className="stream-card-top"><span className={`stream-glyph glyph-${index % 4}`}>{stream.name.slice(0, 1).toUpperCase()}</span><span className={`status-pill status-${status.toLowerCase()}`}><i />{statusLabel(status)}</span></div>
          <div className="stream-card-name"><h3>{stream.name}</h3><code>{stream.id}</code></div>
          <div className="stream-card-stats"><div><span>{primaryProbe?.role === "SERVER_EGRESS" ? "Server egress" : "Останній probe"}</span><strong>{currentBps === null ? "—" : formatMbps(currentBps)}</strong></div><div><span>Пункти</span><strong>{probes.length}</strong></div></div>
          <div className="stream-card-footer"><span>{latest?.active ? <><i className="incident-dot" />{eventLabel({ code: latest.diagnosis, summary: latest.diagnosis })}</> : latest ? `Останній інцидент ${formatAge((Date.now() - Date.parse(latest.opened_at)) / 1000)}` : "Інцидентів поки не було"}</span><b>→</b></div>
        </button>;
      })}
    </div>
  </div>;
}

function SummaryCard({ label, value, detail, icon, alert = false }: { label: string; value: string; detail: string; icon: string; alert?: boolean }) {
  return <article className={`summary-card ${alert ? "summary-alert" : ""}`}><span className="summary-icon">{icon}</span><div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div></article>;
}

function StreamPage({ stream, probes, incidents, series, events, range, setRange, live, visibleProbeIds, onToggleProbe, onSelectEvent, onOpenProbes }: {
  stream: Stream; probes: Probe[]; incidents: Incident[]; series: SeriesResponse | null; events: EventsResponse | null;
  range: RangeKey; setRange: (range: RangeKey) => void; live: boolean; visibleProbeIds: string[];
  onToggleProbe: (id: string) => void; onSelectEvent: (event: TimelineEvent) => void; onOpenProbes: () => void;
}) {
  const healthy = probes.filter((probe) => ["OK", "WARNING"].includes(probe.status)).length;
  const status = overallStatus(probes);
  const incidentCount = incidents.filter((incident) => incident.active).length;
  const preferredProbe = probes.find((probe) => probe.role === "SERVER_EGRESS") ?? probes[0];
  const lastBitrate = preferredProbe ? mediaSummary(preferredProbe).bitrate : null;
  const latestAt = probes.map((probe) => probe.telemetry_observed_at).filter((value): value is string => Boolean(value)).sort().at(-1) ?? null;
  const listedEvents = [...(events?.events ?? [])].sort((left, right) => Date.parse(right.started_at) - Date.parse(left.started_at)).slice(0, 6);
  const hasClientProbe = probes.some((probe) => probe.role === "CLIENT");
  const hasServerEgressProbe = probes.some((probe) => probe.role === "SERVER_EGRESS");
  const pipelineNote = hasClientProbe && !hasServerEgressProbe
    ? "Клієнтський probe показує отримання після мережі. Додайте SERVER_EGRESS, щоб порівняти його з локальною віддачею сервера."
    : hasServerEgressProbe && !hasClientProbe
      ? "SERVER_EGRESS показує локальну віддачу. Додайте CLIENT, щоб перевірити доставку через мережу до отримувачів."
      : hasServerEgressProbe && hasClientProbe && !probes.some((probe) => probe.role === "SOURCE" || probe.role === "SERVER_INGRESS")
        ? "Порівняння сервера й клієнта допомагає оцінити downstream шлях. Для перевірки проблеми до RTMP-сервера додайте SOURCE або SERVER_INGRESS."
        : "Кожен probe показує стан лише у своїй точці. Порівнюйте сусідні спостереження, щоб локалізувати, де змінюється потік.";

  return <div className="stream-view">
    <section className="stream-status-row">
      <div className="stream-live-summary"><span className={`large-status-dot status-${status.toLowerCase()}`} /><div><strong>{statusLabel(status)}</strong><small>{healthy} із {probes.length} точок надсилають свіжі дані</small></div></div>
      <div className="status-divider" />
      <div className="stream-status-item"><span>Остання телеметрія</span><strong>{formatAge(latestAt ? (Date.now() - Date.parse(latestAt)) / 1000 : null)}</strong></div>
      <div className="stream-status-item"><span>Відкрита RTMP-адреса</span><strong className="url-value">{stream.public_url || stream.local_url || "URL не задано"}</strong></div>
      <div className={`incident-count ${incidentCount ? "has-incidents" : ""}`}><span>Активні інциденти</span><strong>{incidentCount}</strong></div>
    </section>

    <section className="metrics-strip">
      <article className="metric-card metric-primary"><div className="metric-topline"><span>ВИМІРЯНИЙ BITRATE</span><span className="metric-live-dot" /></div><strong>{formatMbps(lastBitrate)}</strong><small>{preferredProbe ? `з ${preferredProbe.name}` : "очікуємо перший sample"}</small></article>
      <article className="metric-card"><div className="metric-topline"><span>FPS</span><span className="metric-symbol">◫</span></div><strong>{preferredProbe && typeof preferredProbe.metrics.source_fps === "number" ? preferredProbe.metrics.source_fps.toFixed(1) : "—"}</strong><small>{preferredProbe ? mediaSummary(preferredProbe).resolution : "даних ще немає"}</small></article>
      <article className="metric-card"><div className="metric-topline"><span>KEYFRAME / GOP</span><span className="metric-symbol">✦</span></div><strong>{preferredProbe && mediaSummary(preferredProbe).keyframeAge !== null ? `${mediaSummary(preferredProbe).keyframeAge?.toFixed(1)} с` : "—"}</strong><small>{preferredProbe ? `поточний GOP ${mediaSummary(preferredProbe).gop?.toFixed(1) ?? "—"} с` : "даних ще немає"}</small></article>
      <article className="metric-card"><div className="metric-topline"><span>ПОМИЛКИ ДЕКОДУВАННЯ</span><span className="metric-symbol">⊗</span></div><strong className={preferredProbe && mediaSummary(preferredProbe).decodeErrors > 0 ? "value-warning" : ""}>{preferredProbe ? mediaSummary(preferredProbe).decodeErrors : "—"}</strong><small>{preferredProbe ? mediaSummary(preferredProbe).codec : "очікуємо probe"}</small></article>
      <article className="metric-card"><div className="metric-topline"><span>NETWORK / RTT</span><span className="metric-symbol">⌁</span></div><strong>{preferredProbe && mediaSummary(preferredProbe).rtt !== null ? `${mediaSummary(preferredProbe).rtt} ms` : "—"}</strong><small>{preferredProbe && mediaSummary(preferredProbe).retransmits !== null ? `${mediaSummary(preferredProbe).retransmits} повторів TCP` : "лічильник не доступний"}</small></article>
    </section>

    <section className="panel graph-panel">
      <div className="graph-header">
        <div><div className="graph-title-row"><h2>Бітрейт і події</h2><span className="live-window"><i />{live ? "LIVE" : "ІСТОРІЯ"}</span></div><p>Порівняйте точки спостереження на спільній часовій шкалі</p></div>
        <div className="range-switch" role="group" aria-label="Період графіка">{RANGES.map((item) => <button className={range === item.key ? "selected" : ""} key={item.key} onClick={() => setRange(item.key)}>{item.label}</button>)}</div>
      </div>
      {series?.series.length ? <Suspense fallback={<div className="chart-loading"><span className="loader-ring" /><strong>Завантажуємо модуль графіка…</strong></div>}><StreamChart
        series={series}
        events={events}
        from={Date.parse(series.from)}
        to={Date.parse(series.to)}
        visibleProbeIds={visibleProbeIds}
        onToggleProbe={onToggleProbe}
        onSelectEvent={onSelectEvent}
      /></Suspense> : <div className="chart-loading"><span className="loader-ring" /><strong>{probes.length ? "Завантажуємо часові ряди…" : "Додайте probe, щоб побачити телеметрію"}</strong><small>{probes.length ? "Дані з’являться після першого вимірювання." : "Порівняйте локальну віддачу сервера та віддалені клієнти."}</small>{!probes.length && <button className="button button-secondary" onClick={onOpenProbes}>Додати пункт спостереження</button>}</div>}
    </section>

    <div className="below-graph-grid">
      <section className="panel pipeline-panel">
        <div className="section-heading compact"><div><h2>Шлях потоку</h2><span>Що бачить кожен пункт спостереження</span></div></div>
        {probes.length ? <div className="pipeline-list">{probes.map((probe, index) => <div className="pipeline-step" key={probe.id}>
          {index > 0 && <div className={`pipeline-link ${probe.status === "OK" ? "link-ok" : "link-warning"}`}><i /></div>}
          <span className={`pipeline-node-icon status-${probe.status.toLowerCase()}`}>{probe.role === "CLIENT" ? "▣" : probe.role === "SOURCE" ? "◉" : "▤"}</span>
          <div className="pipeline-node-copy"><strong>{probe.name}</strong><span>{roleLabel(probe.role)} · {probe.location}</span></div>
          <span className={`status-pill status-${probe.status.toLowerCase()}`}>{statusLabel(probe.status)}</span>
        </div>)}</div> : <p className="muted empty-compact">Поки немає зареєстрованих пунктів.</p>}
        <p className="pipeline-note">{pipelineNote}</p>
      </section>

      <section className="panel recent-events-panel">
        <div className="section-heading compact"><div><h2>Останні події</h2><span>{events?.events.length ?? 0} за вибраний період</span></div><button className="text-button" onClick={() => setRange("24h")}>24 год →</button></div>
        {!listedEvents.length ? <p className="muted empty-compact">За цей період подій немає.</p> : <div className="recent-event-list">{listedEvents.map((event) => <button className="recent-event" key={event.id} onClick={() => onSelectEvent(event)}>
          <span className={`event-severity-dot severity-${event.severity.toLowerCase()}`} />
          <span className="recent-event-copy"><strong>{eventLabel(event)}</strong><small>{event.probe_name || "Інцидент"} · {formatDateTime(event.started_at)}</small></span>
          <span className="event-arrow">→</span>
        </button>)}</div>}
      </section>
    </div>

    <section className="panel probe-strip-panel">
      <div className="section-heading compact"><div><h2>Пункти спостереження</h2><span>Свіжість даних і сигнал кожної точки</span></div><button className="text-button" onClick={onOpenProbes}>Керувати probe →</button></div>
      {!probes.length ? <p className="muted empty-compact">Немає probe для цього потоку.</p> : <div className="stream-probe-grid">{probes.map((probe) => <ProbeMiniCard key={probe.id} probe={probe} />)}</div>}
    </section>
  </div>;
}

function ProbeMiniCard({ probe }: { probe: Probe }) {
  const m = mediaSummary(probe);
  return <article className="probe-mini-card"><div className="probe-mini-top"><span className={`status-light status-${probe.status.toLowerCase()}`} /><div><strong>{probe.name}</strong><small>{roleLabel(probe.role)} · {probe.location}</small></div><span className={`status-pill status-${probe.status.toLowerCase()}`}>{statusLabel(probe.status)}</span></div><div className="probe-mini-metrics"><div><span>Бітрейт</span><strong>{formatMbps(m.bitrate)}</strong></div><div><span>Останній кадр</span><strong>{typeof probe.metrics.last_frame_age === "number" ? `${probe.metrics.last_frame_age.toFixed(2)} с` : "—"}</strong></div><div><span>Дані</span><strong>{formatAge(probe.telemetry_age_seconds)}</strong></div></div></article>;
}

function IncidentPage({ incidents, streams, onOpen }: { incidents: Incident[]; streams: Stream[]; onOpen: (incident: Incident) => void }) {
  const [severity, setSeverity] = useState("ALL");
  const [state, setState] = useState("ALL");
  const [search, setSearch] = useState("");
  const filtered = incidents.filter((incident) => (severity === "ALL" || incident.severity === severity) && (state === "ALL" || (state === "ACTIVE") === incident.active) && `${incident.diagnosis} ${incident.probable_location} ${incident.affected_agents.join(" ")}`.toLowerCase().includes(search.toLowerCase()));
  return <section className="panel incident-page-panel">
    <div className="incident-filters"><label className="search-field"><span>⌕</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Пошук за симптомом або probe" /></label><select value={severity} onChange={(event) => setSeverity(event.target.value)} aria-label="Фільтр серйозності"><option value="ALL">Усі рівні</option><option value="CRITICAL">Критичні</option><option value="WARNING">Попередження</option></select><select value={state} onChange={(event) => setState(event.target.value)} aria-label="Фільтр стану"><option value="ALL">Усі стани</option><option value="ACTIVE">Активні</option><option value="RESOLVED">Завершені</option></select></div>
    {!filtered.length ? <div className="empty-state"><div className="empty-orbit">⌖</div><strong>{incidents.length ? "Немає інцидентів за цими фільтрами" : "Інцидентів поки не було"}</strong><span>Тут з’являться згруповані події, коли probe зафіксують проблему.</span></div> : <div className="table-scroll"><table className="incident-table"><thead><tr><th>ЧАС</th><th>ПОТІК</th><th>ПРОБЛЕМА</th><th>ПУНКТИ</th><th>ЙМОВІРНЕ МІСЦЕ</th><th>ТРИВАЛІСТЬ</th><th>СТАН</th></tr></thead><tbody>{filtered.map((incident) => <tr key={incident.id} onClick={() => onOpen(incident)} tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") onOpen(incident); }}>
      <td className="incident-time">{formatDateTime(incident.opened_at)}</td><td>{streams.find((stream) => stream.id === incident.stream_id)?.name ?? incident.stream_id}</td><td><span className={`status-pill status-${incident.severity.toLowerCase()}`}>{severityLabel(incident.severity)}</span><strong className="incident-code">{eventLabel({ code: incident.diagnosis, summary: incident.diagnosis })}</strong></td><td>{incident.affected_agents.join(", ") || "—"}</td><td>{incident.probable_location}</td><td>{formatDuration(incident.opened_at, incident.resolved_at ?? incident.updated_at)}</td><td><span className={`state-chip ${incident.active ? "state-active" : ""}`}><i />{incident.active ? "Активний" : "Завершений"}</span></td>
    </tr>)}</tbody></table></div>}
  </section>;
}

type StreamDraft = { id: string; name: string; local_url: string; public_url: string; source_url: string };

function AddStreamDialog({ draft, error, saving, onChange, onClose, onSave }: { draft: StreamDraft; error: string; saving: boolean; onChange: (draft: StreamDraft) => void; onClose: () => void; onSave: (event: FormEvent<HTMLFormElement>) => void }) {
  const field = (key: keyof typeof draft, value: string) => onChange({ ...draft, [key]: value });
  return <div className="modal-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className="modal-card" role="dialog" aria-modal="true" aria-labelledby="add-stream-title">
    <div className="modal-header"><div><p className="eyebrow">КОНФІГУРАЦІЯ ПОТОКУ</p><h2 id="add-stream-title">Додати потік</h2></div><button className="icon-button" onClick={onClose} aria-label="Закрити">×</button></div>
    <p className="panel-copy">Вкажіть RTMP-адреси для точок спостереження. Monitor підключається як читач і не змінює потік.</p>
    <form className="form-grid" onSubmit={onSave}>
      <label className="field"><span>ID потоку</span><input value={draft.id} onChange={(event) => field("id", event.target.value)} pattern="[A-Za-z0-9_.-]+" maxLength={128} placeholder="poland" required /></label>
      <label className="field"><span>Назва</span><input value={draft.name} onChange={(event) => field("name", event.target.value)} maxLength={128} placeholder="Poland live" required /></label>
      <label className="field field-wide"><span>Локальний URL на RTMP-сервері</span><input type="url" value={draft.local_url} onChange={(event) => field("local_url", event.target.value)} placeholder="rtmp://127.0.0.1:1935/live/poland" /></label>
      <label className="field field-wide"><span>Публічний URL для клієнтів</span><input type="url" value={draft.public_url} onChange={(event) => field("public_url", event.target.value)} placeholder="rtmp://stream.example.net:1935/live/poland" /></label>
      <label className="field field-wide"><span>URL виходу encoder <small>необов’язково</small></span><input type="url" value={draft.source_url} onChange={(event) => field("source_url", event.target.value)} placeholder="rtmp://encoder.example.net:1935/live/poland" /></label>
      {error && <div className="form-error field-wide" role="alert">{error}</div>}
      <div className="modal-actions field-wide"><button type="button" className="button button-quiet" onClick={onClose}>Скасувати</button><button className="button button-primary" disabled={saving}>{saving ? "Зберігаємо…" : "Створити потік"}</button></div>
    </form>
  </section></div>;
}
