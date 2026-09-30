import { useMemo, useState, type FormEvent } from "react";
import { api, jsonBody, type Dashboard, type Enrollment, type Probe, type Stream } from "../api";
import { formatAge, formatDateTime, roleLabel, statusLabel } from "../format";

type Props = { dashboard: Dashboard; onRefresh: () => void };

const ARCHIVE_URL = "https://github.com/Sadoharu/rtmp-stream-monitor/archive/refs/heads/main";
const LATEST_RELEASE_URL = "https://github.com/Sadoharu/rtmp-stream-monitor/releases/latest/download";
function probeStreamUrl(stream: Stream | undefined, role: string): string {
  if (!stream) return "";
  if (role === "CLIENT") return stream.public_url;
  if (role === "SERVER_EGRESS") return stream.local_url;
  if (role === "SOURCE") return stream.source_url;
  return "";
}

function initialPlatform(): string {
  return /Windows/i.test(navigator.userAgent) ? "Windows" : "Ubuntu";
}

function probeGuidance(probe: Probe): { observation: string; nextStep: string; command?: string } | null {
  const windows = /windows/i.test(probe.platform);
  const serviceCheck = windows
    ? 'Get-Service RtmpMonitorAgent; Get-ChildItem "$env:ProgramData\\RtmpMonitor\\logs" -Filter "*.jsonl" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | ForEach-Object { Get-Content -Tail 40 $_.FullName }'
    : "sudo systemctl status rtmp-monitor-agent --no-pager; sudo journalctl -u rtmp-monitor-agent -n 40 --no-pager";

  switch (probe.status) {
    case "NEVER_SEEN":
      return {
        observation: "Після створення probe сервер ще не отримав від неї телеметрію. За цим станом не видно, чи інсталятор не завершився, чи комп’ютер не дістався сервера.",
        nextStep: "Перевірте службу й журнал на цьому комп’ютері. Якщо одноразовий код прострочений або вже використаний, створіть нову probe з новим кодом.",
        command: serviceCheck,
      };
    case "AGENT_OFFLINE":
      return {
        observation: `Probe ${probe.last_seen_age_seconds === null ? "ще не надсилала сигнал" : `не надсилала сигнал ${formatAge(probe.last_seen_age_seconds)}`}. Сервер не може визначити лише з цього, чи зупинилася служба, чи зник зв’язок із сервером моніторингу.`,
        nextStep: "Перевірте службу на комп’ютері probe та доступність HTTPS-адреси Dashboard з цієї мережі.",
        command: serviceCheck,
      };
    case "TELEMETRY_STALE":
      return {
        observation: `Зв’язок із probe є, але час останнього виміру — ${formatAge(probe.telemetry_age_seconds)}. Можлива затримка локальної черги або розбіжність годинника; цей стан сам по собі не визначає причину.`,
        nextStep: "Перевірте системний час, стан служби та журнал агента; порівняйте час нових вимірів після перевірки.",
        command: serviceCheck,
      };
    case "STREAM_OFFLINE":
      return {
        observation: "Probe надсилає дані, але її FFmpeg-процес або вхідний потік позначений як недоступний. Це ще не локалізує проблему до джерела, адреси чи мережі.",
        nextStep: "Перевірте URL і роль probe та чи відкривається цей самий потік із комп’ютера probe.",
        command: serviceCheck,
      };
    case "STREAM_STALLED":
      return {
        observation: "Probe надсилає телеметрію, але на цій точці довго не просуваються кадри.",
        nextStep: "Перевірте потік і адресу з цього комп’ютера, потім порівняйте часову шкалу з іншими probe, щоб побачити, де зупиняється медіа.",
        command: serviceCheck,
      };
    case "WARNING":
    case "CRITICAL":
      return {
        observation: "Probe надсилає телеметрію зі статусом проблеми.",
        nextStep: "Відкрийте графік і останню подію цієї probe: там будуть вимірювання, часові позначки та межі висновку.",
      };
    case "UNKNOWN":
      return {
        observation: "Зв’язок із probe є, але надійно класифікувати стан потоку поки не вдалося.",
        nextStep: "Перевірте останній sample та події на графіку; за потреби звірте журнал агента на цій машині.",
        command: serviceCheck,
      };
    default:
      return null;
  }
}

export function ProbeManager({ dashboard, onRefresh }: Props) {
  const [name, setName] = useState("");
  const [location, setLocation] = useState("");
  const [streamId, setStreamId] = useState(dashboard.streams[0]?.id ?? "");
  const [role, setRole] = useState("CLIENT");
  const [platform, setPlatform] = useState(initialPlatform);
  const [profile, setProfile] = useState("DEEP");
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [removingId, setRemovingId] = useState("");

  const stream = dashboard.streams.find((item) => item.id === streamId);
  const streamUrl = probeStreamUrl(stream, role);
  const centralUrl = window.location.origin;
  const probes = dashboard.agents;
  const command = useMemo(() => {
    if (!enrollment) return "";
    if (platform === "Windows") {
      return `Set-ExecutionPolicy -Scope Process Bypass -Force
$ErrorActionPreference = 'Stop'
$tmp = Join-Path $env:TEMP ('rtmp-monitor-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  $package = Join-Path $tmp 'rtmp-monitor-agent-windows.zip'
  $packageUri = '${LATEST_RELEASE_URL}/rtmp-monitor-agent-windows.zip'
  $hasPackage = $true
  try {
    Invoke-WebRequest -UseBasicParsing -Uri $packageUri -OutFile $package
  } catch {
    $hasPackage = $false
    Write-Host 'Windows installer package is not published yet; using the source installer.'
  }
  if ($hasPackage) {
    Invoke-WebRequest -UseBasicParsing -Uri ($packageUri + '.sha256') -OutFile ($package + '.sha256')
    $expected = ((Get-Content -Raw -LiteralPath ($package + '.sha256')).Trim() -split '\\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $package).Hash.ToLowerInvariant()
    if ($expected -notmatch '^[0-9a-f]{64}$' -or $actual -ne $expected) { throw 'Windows agent package SHA-256 verification failed.' }
    Expand-Archive -LiteralPath $package -DestinationPath $tmp
    Set-Location $tmp
  } else {
    $source = Join-Path $tmp 'source.zip'
    Invoke-WebRequest -UseBasicParsing -Uri '${ARCHIVE_URL}.zip' -OutFile $source
    Expand-Archive -LiteralPath $source -DestinationPath $tmp
    Set-Location (Join-Path $tmp 'rtmp-stream-monitor-main')
  }
  .\\install.ps1 -ServerUrl '${centralUrl}'
} finally {
  Set-Location $env:TEMP
  Remove-Item -LiteralPath $tmp -Recurse -Force
}`;
    }
    return `set -euo pipefail
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
package="$tmp/rtmp-monitor-agent_amd64.deb"
if curl -fsSL '${LATEST_RELEASE_URL}/rtmp-monitor-agent_amd64.deb' -o "$package"; then
  curl -fsSL '${LATEST_RELEASE_URL}/rtmp-monitor-agent_amd64.deb.sha256' -o "$package.sha256"
  (cd "$tmp" && sha256sum --check rtmp-monitor-agent_amd64.deb.sha256)
  sudo apt-get update
  sudo apt-get install -y "$package"
  sudo rtmp-monitor-agent-install --server '${centralUrl}'
else
  echo 'No packaged release is available yet; using the source installer.' >&2
  curl -fsSL '${ARCHIVE_URL}.tar.gz' | tar -xz -C "$tmp"
  sudo "$tmp/rtmp-stream-monitor-main/install.sh" agent --server '${centralUrl}'
fi`;
  }, [centralUrl, enrollment, platform]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setEnrollment(null);
    setBusy(true);
    try {
      const result = await api<Enrollment>("/api/v2/probe-enrollments", jsonBody({
        name: name.trim(),
        location: location.trim() || "unknown",
        platform,
        role,
        stream_id: streamId,
        central_url: centralUrl,
        profile,
      }));
      setEnrollment(result);
      setName("");
      onRefresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не вдалося створити код підключення.");
    } finally {
      setBusy(false);
    }
  };

  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setError("Браузер заборонив доступ до буфера обміну. Виділіть текст і скопіюйте його вручну.");
    }
  };

  const removeProbe = async (probe: Probe) => {
    if (!window.confirm(`Відкликати доступ probe «${probe.name}»? Історія вимірювань та інцидентів залишиться на сервері.`)) return;
    setRemovingId(probe.id);
    setError("");
    try {
      await api(`/api/v1/agents/${encodeURIComponent(probe.id)}`, { method: "DELETE" });
      onRefresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не вдалося відкликати probe.");
    } finally {
      setRemovingId("");
    }
  };

  return (
    <div className="probe-layout">
      <section className="panel setup-panel">
        <div className="panel-heading">
          <div><p className="eyebrow">ПІДКЛЮЧЕННЯ</p><h2>Додати пункт спостереження</h2></div>
          <span className="step-number">01</span>
        </div>
        <p className="panel-copy">Створіть одноразовий код. На цільовому комп’ютері команда встановить службу, збереже налаштування й запустить probe.</p>
        <form className="form-grid" onSubmit={submit}>
          <label className="field field-wide"><span>Потік</span>
            <select value={streamId} onChange={(event) => setStreamId(event.target.value)} required>
              <option value="" disabled>Виберіть потік</option>
              {dashboard.streams.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}
            </select>
          </label>
          <label className="field"><span>Назва probe</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="Наприклад, studio-win-01" maxLength={128} required /></label>
          <label className="field"><span>Локація</span><input value={location} onChange={(event) => setLocation(event.target.value)} placeholder="Студія, місто або майданчик" maxLength={256} /></label>
          <label className="field"><span>Що перевіряємо</span>
            <select value={role} onChange={(event) => setRole(event.target.value)}>
              <option value="CLIENT">Віддалений клієнт</option>
              <option value="SERVER_EGRESS">Локальну віддачу сервера</option>
              <option value="SOURCE">Вихід encoder/source</option>
            </select>
          </label>
          <label className="field"><span>Операційна система</span>
            <select value={platform} onChange={(event) => setPlatform(event.target.value)}>
              <option value="Windows">Windows</option><option value="Ubuntu">Ubuntu</option>
            </select>
          </label>
          <label className="field"><span>Режим аналізу</span>
            <select value={profile} onChange={(event) => setProfile(event.target.value)}>
              <option value="DEEP">Глибокий · декодування кадрів</option><option value="LIGHT">Полегшений · без декодування</option>
            </select>
          </label>
          <div className="field field-wide stream-target"><span>Адреса потоку для цієї точки</span><code>{streamUrl || "Для вибраної ролі не задано URL у параметрах потоку."}</code></div>
          <div className="field field-wide"><span>Central server</span><code>{centralUrl}</code></div>
          {centralUrl.startsWith("http://") && !["localhost", "127.0.0.1", "[::1]"].includes(new URL(centralUrl).hostname) && <p className="form-notice field-wide">Для віддаленого probe потрібен HTTPS. Відкрийте Dashboard через TLS reverse proxy.</p>}
          {error && <div className="form-error field-wide" role="alert">{error}</div>}
          <button className="button button-primary field-wide" disabled={busy || !dashboard.streams.length || !streamUrl}>
            {busy ? "Створюємо код…" : "Створити код підключення"}
          </button>
        </form>

        {enrollment && <div className="enrollment-result" role="status">
          <div className="enrollment-head"><span className="enrollment-icon">✓</span><div><strong>Код готовий</strong><small>Діє до {formatDateTime(enrollment.expires_at)} · використовується один раз</small></div></div>
          <div className="code-row"><code>{enrollment.code}</code><button className="button button-secondary" onClick={() => copy(enrollment.code)}>{copied ? "Скопійовано" : "Копіювати код"}</button></div>
          <div className="command-heading"><span>1</span><strong>На {platform}-комп’ютері виконайте команду</strong></div>
          <div className="command-box"><pre>{command}</pre><button aria-label="Копіювати команду" onClick={() => copy(command)}>⧉</button></div>
          <p className="hint">Під час інсталяції введіть код у прихованому запиті. Не надсилайте його в загальний чат і не зберігайте у файлі.</p>
        </div>}
      </section>

      <section className="panel probes-panel">
        <div className="panel-heading">
          <div><p className="eyebrow">ПІДКЛЮЧЕНІ ПРИСТРОЇ</p><h2>Пункти спостереження <span className="count-badge">{probes.length}</span></h2></div>
          <button className="button button-quiet" onClick={onRefresh} title="Оновити список">Оновити ↻</button>
        </div>
        {!probes.length ? <div className="empty-state"><div className="empty-orbit">◎</div><strong>Поки що немає підключених probe</strong><span>Створіть код ліворуч і встановіть агент на сервері або клієнті.</span></div> : <div className="probe-list">
          {probes.map((probe) => <ProbeCard key={probe.id} probe={probe} stream={dashboard.streams.find((item) => item.id === probe.stream_id)} removing={removingId === probe.id} onRemove={() => void removeProbe(probe)} />)}
        </div>}
      </section>
    </div>
  );
}

function ProbeCard({ probe, stream, removing, onRemove }: { probe: Probe; stream?: Stream; removing: boolean; onRemove: () => void }) {
  const guidance = probeGuidance(probe);
  return <article className="probe-card">
    <div className="probe-card-top">
      <span className={`status-light status-${probe.status.toLowerCase()}`} />
      <div className="probe-name"><strong>{probe.name}</strong><span>{roleLabel(probe.role)} · {stream?.name ?? probe.stream_id}</span></div>
      <span className={`status-pill status-${probe.status.toLowerCase()}`}>{statusLabel(probe.status)}</span>
    </div>
    <div className="probe-card-meta"><span>{probe.platform}</span><span>{probe.location || "Локацію не задано"}</span><span>Останній сигнал: {formatAge(probe.last_seen_age_seconds)}</span></div>
    {guidance && <div className="probe-guidance" role="note">
      <strong>Що відомо</strong><p>{guidance.observation}</p>
      <strong>Що перевірити</strong><p>{guidance.nextStep}</p>
      {guidance.command && <code>{guidance.command}</code>}
    </div>}
    <div className="probe-card-actions"><button className="button button-danger-quiet" disabled={removing} onClick={onRemove}>{removing ? "Відкликаємо…" : "Відкликати доступ"}</button></div>
  </article>;
}
