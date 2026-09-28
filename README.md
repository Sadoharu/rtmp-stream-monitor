# RTMP Stream Diagnostic Monitor

Центральний сервіс збирає телеметрію від окремих probe-агентів і об'єднує симптоми в incidents із полем **Probable location**. Реалізація розрахована на безперервний запуск: systemd або Windows Service керує агентом, агент контролює FFmpeg/ffprobe, тримає обмежену локальну SQLite-чергу, а центральний сервіс зберігає telemetry та incidents у SQLite через SQLAlchemy.

## Спостережні точки та межі діагностики

- `SOURCE` — агент на encoder/source читає вихідний RTMP до RTMP-сервера. Це найближче практичне спостереження до того, що encoder передає в мережу.
- `SERVER_EGRESS` — агент на Ubuntu RTMP-сервері читає потік через `rtmp://127.0.0.1:1935/...`. Це перевіряє локальну віддачу сервера.
- `CLIENT` — віддалений агент читає публічний RTMP URL. Підтримуються Ubuntu та Windows і кілька клієнтських агентів.
- `SERVER_INGRESS` потребує прямої інтеграції з конкретним сервером або ingest hook. Цей репозиторій не знає, який RTMP server реалізований на Ubuntu, тому відхиляє створення такого generic probe і не заявляє loopback reader як справжнє ingress-спостереження. Поки що використовуйте source-side `SOURCE` probe або показуйте стан `SERVER_EGRESS_LOCAL`.

Для клієнтського probe вкажіть публічний URL потоку, наприклад `rtmp://stream.example.net:1935/live/demo`. Для локального виходу сервера можна використати `rtmp://127.0.0.1:1935/live/demo`. Перевірте, що порт central API `8090` доступний агентам.

Correlation використовує останні спостереження кожної ролі у 20-секундному wall-clock вікні та перевіряє розкид media PTS (default tolerance 5 секунд). PTS lag від локального egress до клієнта підтримує діагноз network path, коли обидва probes працюють у `LIGHT` mode; у `DEEP` mode lag може бути наслідком повільного decode. Encoder-side probe сам по собі не доводить, що ingest на сервері був чистим: для цього потрібен реальний `SERVER_INGRESS` hook. За його відсутності діагноз явно лишається непідтвердженим. Це ймовірне місце, не математичний доказ: RTMP/TCP не переносить наскрізний ідентифікатор кадру, тому точна прив'язка до одного media packet між різними probes обмежена.

Докладніше про обраний аналіз та обмеження — [docs/architecture.md](docs/architecture.md).

## Встановлення центрального сервера Ubuntu

```bash
sudo ./install.sh
```

Скрипт створює системного користувача, Python virtual environment, SQLite storage, конфігурацію й systemd service `rtmp-monitor-central`. Інсталятор не відкриває firewall автоматично.

```bash
sudo systemctl status rtmp-monitor-central
sudo journalctl -u rtmp-monitor-central -f
```

Відкрийте `http://SERVER:8090`. Dashboard token створюється під час першого запуску і зберігається локально з обмеженими правами:

```bash
sudo -u rtmp-monitor /opt/rtmp-monitor/.venv/bin/rtmp-monitor show-admin-token --config /etc/rtmp-monitor/central.yaml
```

У Dashboard виберіть **Add stream**. Для `demo` уже підставлені local та remote URL; відредагуйте їх за потреби. Доступ до API й Dashboard вимагає bearer token. Для probes створюються окремі одноразово показані agent tokens; збережіть їх у конфігурації агента. Не публікуйте файли з секретами.

Конфігурація central server: `/etc/rtmp-monitor/central.yaml`. За замовчуванням SQLite database зберігається в `/var/lib/rtmp-monitor/central.db`, логи — у `/var/log/rtmp-monitor`. SQLAlchemy дозволяє задати PostgreSQL через `database_url` без змін у business logic.

## Встановлення Ubuntu probe

Спершу створіть probe в Dashboard. Скопіюйте згенеровану конфігурацію в локальний файл із токеном, наприклад `/root/demo-agent.yaml`, а потім:

```bash
sudo ./install.sh agent /root/demo-agent.yaml
```

Для server-local probe виберіть роль `SERVER_EGRESS`; для віддаленого клієнта — `CLIENT`. Для source-side probe додайте в stream поле source/encoder URL і виберіть роль `SOURCE`.

```bash
sudo systemctl status rtmp-monitor-agent
sudo journalctl -u rtmp-monitor-agent -f
```

Служба запускається після reboot і перезапускається після помилки. Дані без центрального сервера залишаються в обмеженій локальній SQLite outbox. Якщо черга досягне заданого ліміту, найстаріші рядки будуть вилучені; кількість вилучених рядків показується в telemetry.

Після тривалої недоступності central server агент передає збережену telemetry з її початковим `observed_at`. Центральна система показує окремий стан `TELEMETRY_STALE`, доки не надійде свіже спостереження; сам факт отримання старого sample не означає, що потік зараз працює.

## Встановлення Windows probe

1. Створіть probe у Dashboard і скопіюйте його YAML у `config/agent.yaml` (або передайте інший шлях у параметрі `-ConfigPath`).
2. Встановіть Python 3.12+ x64 **для всіх користувачів** (у звичайному інсталяторі Python виберіть `Install for all users`). Windows-служба запускається від `LocalSystem`, тому Python із профілю `C:\Users\...` їй недоступний. Інсталятор автоматично шукає машинну інсталяцію в реєстрі Windows та `C:\Program Files`, незалежно від того, який Python обирає `py -3`. Для Python у власній папці передайте повний шлях параметром `-PythonPath`. Служба використовує вибраний машинний Python без virtualenv, як рекомендує [pywin32 для Windows Services](https://github.com/mhammond/pywin32#running-as-a-windows-service). Також переконайтеся, що `ffmpeg.exe` і `ffprobe.exe` доступні через PATH. FFmpeg можна встановити через `winget install Gyan.FFmpeg`.
3. Запустіть PowerShell від Administrator:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

Installer створює Windows Service `RtmpMonitorAgent` з automatic startup. Перевірити стан можна через `Get-Service RtmpMonitorAgent`; структуровані логи зберігаються в `%ProgramData%\RtmpMonitor\logs`.

При оновленні інсталятор зберігає наявний `%ProgramData%\RtmpMonitor\agent.yaml`. Щоб замінити його новим Dashboard YAML, запустіть `.\install.ps1 -ConfigPath .\config\agent.yaml -ReplaceConfig`.

## Додавання stream та probe

У stream зберігаються три окремі URL: `source_url` (за наявності), `local_url` для сервера та `public_url` для клієнтів. Не підміняйте `source_url` loopback адресою сервера. Для кожної комбінації `agent + stream` створюйте окремий probe й окремий токен. Клієнтів можна додавати скільки потрібно.

Dashboard після створення probe один раз показує готовий YAML із token, stream URL і network destination. Збережіть його на відповідному host перед інсталяцією.

## Dashboard та incidents

Головний екран показує стан кожного агента окремо від стану потоку, FPS, codec, bitrate, frame age, keyframe/GOP, decode errors, CPU/RAM, RTT і transport counters. Timeline синхронізується за wall-clock часом і містить telemetry за останні шість годин. Клік на incident відкриває symptoms, timeline-контекст і останні суттєві рядки FFmpeg stderr.

Incident створюється із симптомів, видимих у відповідних probes. Система використовує `Probable location` там, де точну причину неможливо довести. Агент offline показується окремо від stream offline/stalled.

## Аналіз медіа та profiles

- `DEEP` (default): FFmpeg decode до null output, `showinfo` для кадрів/PTS/keyframe, `freezedetect`, `silencedetect`, `ashowinfo` і контроль decode errors із stderr. Transcoding не виконується. Повний decode може вимагати помітного CPU; перевірте завантаження на вашому потоці.
- `LIGHT`: ffprobe читає стиснені пакети й аналізує packet timestamps, key packet flags, розміри пакетів і GOP без video decode. Детальні decode/corrupt frame та freeze/silence checks у цьому профілі недоступні.
- GOP threshold можна задати `monitoring.keyframe_gap_threshold`; інакше він визначається з медіанного GOP, із початковим порогом 5 секунд.

FFmpeg керується агентом як subprocess: progress щосекунди, warning після відсутності progress, stall за відсутності media і restart після тривалішої відсутності. Reconnect застосовує exponential backoff. stderr не накопичується у нескінченному файлі: важливі повідомлення пишуться окремо в обертовий `ffmpeg-stderr.jsonl`, а для event зберігається останній контекст.

## Network і clock telemetry

Linux використовує `ss -ti` для TCP_INFO відповідного напрямку, якщо утиліта й права ОС дозволяють; RTT/ICMP probe виконується окремо. Windows читає системний counter `SegmentsRetransmitted` і стан TCP-з'єднання; retransmit counter на Windows є host-wide різницею між samples, а не лічильником одного RTMP socket. Окремий Windows retransmit без інших мережевих ознак дає `NETWORK_PATH_UNCONFIRMED`, щоб не приписувати сторонній TCP-трафік RTMP. ICMP може бути заблокований. Відсутній provider не ламає медіамоніторинг.

NTP status береться з `timedatectl` (Linux) або `w32tm` (Windows); система не змінює NTP налаштування. Offset береться з NTP provider, а за його відсутності — приблизно з центрального HTTP `Date` (точність до секунди). За spread більше `clock_offset_warning_ms` Dashboard показує `CLOCK NOT SYNCHRONIZED`. Для коректної кореляції налаштуйте NTP на всіх hosts.

Pcap ring buffer не реалізований: він опційний і не використовується для постійного моніторингу.

## Retention та логи

- 1-second raw telemetry: 7 днів;
- агреговані 1-minute metrics: 90 днів;
- incidents: 180 днів;
- application logs: 30 днів, щоденна ротація та gzip;
- FFmpeg diagnostic log: окрема ротація за розміром, останні 10 файлів.

Agent queue за замовчуванням обмежена 50 MiB і 50 000 рядків. Параметри можна задати в YAML.

## Логи й troubleshooting

Central service:

```bash
sudo journalctl -u rtmp-monitor-central -f
sudo tail -f /var/log/rtmp-monitor/rtmp-monitor.jsonl
```

Ubuntu agent:

```bash
sudo journalctl -u rtmp-monitor-agent -f
sudo tail -f /var/log/rtmp-monitor-agent/rtmp-monitor.jsonl
sudo tail -f /var/log/rtmp-monitor-agent/ffmpeg-stderr.jsonl
```

Якщо probe offline — перевірте службу, доступність `http://SERVER:8090/healthz`, stream ID, токен і firewall. Якщо stream offline — перевірте URL із того самого host командою `ffprobe -hide_banner -v error -show_streams 'rtmp://...'`. Якщо status є, але network counters відсутні, перевірте `ss` на Linux або ICMP/PowerShell policy на Windows.

## Оновлення

Збережіть конфігурації, оновіть checkout, потім знову запустіть потрібний installer. Central installer зберігає наявний `/etc/rtmp-monitor/central.yaml`. Windows installer зберігає встановлений agent YAML, якщо не задано `-ReplaceConfig`; Linux agent installer копіює YAML із шляху, переданого в команді. Переконайтеся, що відома резервна копія SQLite перед великим оновленням.

## Видалення

Зупиніть та вимкніть потрібну службу, потім видаліть unit і каталоги за вашим retention policy:

```bash
sudo systemctl disable --now rtmp-monitor-central
sudo rm /etc/systemd/system/rtmp-monitor-central.service
sudo systemctl daemon-reload
```

Для агента використайте `rtmp-monitor-agent.service`. Видалення `/var/lib/rtmp-monitor` або `/var/lib/rtmp-monitor-agent` знищує базу/чергу та потребує окремого підтвердження адміністратора.

Щоб прибрати центральний код і конфігурацію після зупинки служби, виконайте `sudo rm -rf /opt/rtmp-monitor /etc/rtmp-monitor`; для probe використайте `/opt/rtmp-monitor-agent /etc/rtmp-monitor-agent`. Щоб також видалити дані, окремо перевірте та видаліть відповідний `/var/lib/rtmp-monitor*` каталог і логи. На Windows у elevated PowerShell зупиніть та видаліть службу командами `Stop-Service RtmpMonitorAgent` і `sc.exe delete RtmpMonitorAgent`. За потреби видаліть пакет `rtmp-stream-monitor` із машинного Python, шлях до якого надрукував інсталятор (`<python.exe> -m pip uninstall rtmp-stream-monitor`), потім видаліть `C:\Program Files\RTMPMonitor` і, якщо не потрібні, логи та конфігурацію в `C:\ProgramData\RtmpMonitor`.

## Розробка та перевірки

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m pytest
rtmp-monitor server --config config/central.dev.yaml
```

Перевірки classifier симулюють source, restream, network-path та client-only failures. Live acceptance test треба виконати на тих Ubuntu/Windows hosts і через той самий RTMP шлях, де система працюватиме цілодобово.

Щоб перевірити активний RTMP URL через локальний тимчасовий central collector і один deep probe:

```powershell
python scripts/live-agent-smoke.py "rtmp://HOST:1935/live/STREAM" --seconds 30 --network
```

Команда друкує codec, роздільність, frame/keyframe counters, timestamp anomalies та мережеву телеметрію. `--network` додає RTT/ICMP, втрати пакетів і доступні платформні TCP counters. Цей smoke test запускає probe в поточному процесі й не перевіряє встановлену Windows-службу чи повний acceptance сценарій із перериванням потоку.
