# RTMP Stream Diagnostic Monitor

Центральний сервіс збирає телеметрію від окремих probe-агентів і об'єднує симптоми в incidents із полем **Probable location**. Реалізація розрахована на безперервний запуск: systemd або Windows Service керує агентом, який аналізує потік через FFmpeg/ffprobe або читає SRS ingress counters, тримає обмежену локальну SQLite-чергу, а центральний сервіс зберігає telemetry та incidents у SQLite через SQLAlchemy.

## Спостережні точки та межі діагностики

- `SOURCE` — агент на encoder/source читає вихідний RTMP до RTMP-сервера. Це найближче практичне спостереження до того, що encoder передає в мережу.
- `SERVER_EGRESS` — агент на Ubuntu RTMP-сервері читає потік через `rtmp://127.0.0.1:1935/...`. Це перевіряє локальну віддачу сервера.
- `CLIENT` — віддалений агент читає публічний RTMP URL. Підтримуються Ubuntu та Windows і кілька клієнтських агентів.
- `SERVER_INGRESS` для SRS читає read-only `/api/v1/streams` endpoint на самому сервері й показує publisher activity, ingress bytes/bitrate, frame counters та codec metadata. RTMP handshake на тестовому потоці визначив SRS 6.0.184. HTTP API підтверджує publisher і лічильники, але не декодує кадри та не перевіряє GOP; якщо egress зламаний за активного SRS publisher, діагноз лишається непідтвердженим. Для frame-level перевірки додайте окремий `SOURCE` або майбутній decoded ingress adapter. Loopback FFmpeg probe завжди має роль `SERVER_EGRESS`, а не ingress.

Для клієнтського probe вкажіть публічний URL потоку, наприклад `rtmp://stream.example.net:1935/live/demo`. Для локального виходу сервера можна використати `rtmp://127.0.0.1:1935/live/demo`. Перевірте, що порт central API `8090` доступний агентам.

Correlation використовує останні спостереження кожної ролі у 20-секундному wall-clock вікні та перевіряє розкид media PTS (default tolerance 5 секунд). PTS lag від локального egress до клієнта підтримує діагноз network path, коли обидва probes працюють у `LIGHT` mode; у `DEEP` mode lag може бути наслідком повільного decode. SRS SERVER_INGRESS counters підтверджують publisher і рух байтів, але не доводять, що вхідні кадри декодуються чи мають коректний GOP. Для frame-level source/media діагнозу використовуйте SOURCE probe або decoded ingress adapter; без такої перевірки місце збою лишається непідтвердженим. Це ймовірне місце, не математичний доказ: RTMP/TCP не переносить наскрізний ідентифікатор кадру, тому точна прив'язка до одного media packet між різними probes обмежена.

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

### Безпечний мережевий доступ

API перевіряє bearer token, але HTTP сам по собі не шифрує токени й telemetry. Для доступу через інтернет або недовірені мережі не виставляйте порт `8090` напряму: обмежте його firewall і поставте перед central server TLS reverse proxy. Якщо proxy працює на тому ж host, задайте в `/etc/rtmp-monitor/central.yaml`:

```yaml
bind_host: 127.0.0.1
bind_port: 8090
```

Налаштуйте proxy передавати `Authorization` до backend та обслуговувати Dashboard і `/api/v1/*` через HTTPS. У probe YAML використовуйте URL central server із `https://`, наприклад `https://monitor.example.net`; стандартна перевірка сертифіката залишається увімкненою. Після зміни central config перезапустіть `rtmp-monitor-central`. Якщо TLS proxy не налаштований, дозволяйте прямий HTTP доступ до `8090` лише у довіреній ізольованій мережі або через VPN. SRS HTTP API для ingress probe залишайте прив'язаним до `127.0.0.1:1985` і не публікуйте назовні.

Конфігурація central server: `/etc/rtmp-monitor/central.yaml`. За замовчуванням SQLite database зберігається в `/var/lib/rtmp-monitor/central.db`, логи — у `/var/log/rtmp-monitor`. SQLAlchemy дозволяє задати PostgreSQL через `database_url` без змін у business logic.

## Встановлення Ubuntu probe

Спершу створіть probe в Dashboard. Скопіюйте згенеровану конфігурацію в локальний файл із токеном, наприклад `/root/demo-agent.yaml`, а потім:

```bash
sudo ./install.sh agent /root/demo-agent.yaml
```

Для server-local FFmpeg probe виберіть роль `SERVER_EGRESS`; для віддаленого клієнта — `CLIENT`. Для source-side probe додайте в stream поле source/encoder URL і виберіть роль `SOURCE`. На SRS-хості виберіть `SERVER_INGRESS`, щоб читати SRS HTTP API counters.

Для SRS увімкніть локальний read-only HTTP API у його конфігурації, збережіть `raw_api` вимкненим і не відкривайте порт `1985` назовні:

```conf
http_api {
    enabled on;
    listen 127.0.0.1:1985;
    raw_api {
        enabled off;
    }
}
```

Застосуйте конфігурацію звичним для цього SRS deployment способом і перевірте API на сервері:

```bash
curl -fsS http://127.0.0.1:1985/api/v1/versions
curl -fsS 'http://127.0.0.1:1985/api/v1/streams/?count=500'
```

Dashboard створить `srs_api.base_url: http://127.0.0.1:1985`. Якщо на SRS ввімкнена HTTP API Basic Auth, додайте в YAML probe пару `srs_api.username` і `srs_api.password`; не додавайте credentials до URL. Конфігурація зберігається з обмеженими правами.

```bash
sudo systemctl status rtmp-monitor-agent
sudo journalctl -u rtmp-monitor-agent -f
```

Служба запускається після reboot і перезапускається після помилки. Дані без центрального сервера залишаються в обмеженій локальній SQLite outbox. Якщо черга досягне заданого ліміту, найстаріші рядки будуть вилучені; кількість вилучених рядків показується в telemetry.

При пошкодженні локальної outbox SQLite агент ізолює пошкоджені файли з суфіксом `.corrupt-*`, записує помилку в лог і створює нову порожню чергу; telemetry, що зберігалася лише у пошкодженій базі, може бути втрачена. Окремі некоректні JSON-записи видаляються без скидання решти черги. Тимчасові помилки SQLite, зокрема `database is locked`, не трактуються як пошкодження.

Після тривалої недоступності central server агент передає збережену telemetry з її початковим `observed_at`. Центральна система показує окремий стан `TELEMETRY_STALE`, доки не надійде свіже спостереження; сам факт отримання старого sample не означає, що потік зараз працює.

## Встановлення Windows probe

1. Створіть probe у Dashboard і скопіюйте його YAML у `config/agent.yaml` (або передайте інший шлях у параметрі `-ConfigPath`).
2. Встановіть Python 3.12+ x64 **для всіх користувачів** (у звичайному інсталяторі Python виберіть `Install for all users`). Windows-служба запускається від `LocalSystem`, тому Python із профілю `C:\Users\...` їй недоступний. Інсталятор автоматично шукає машинну інсталяцію в реєстрі Windows та `C:\Program Files`, незалежно від того, який Python обирає `py -3`. Якщо Python розташований в іншій машинно-доступній папці поза профілем користувача, передайте його повний шлях через `-PythonPath`; шлях під `C:\Users\...` буде відхилено. Служба використовує вибраний машинний Python без virtualenv, як рекомендує [pywin32 для Windows Services](https://github.com/mhammond/pywin32#running-as-a-windows-service). FFmpeg також має бути встановлений поза профілем користувача; інсталятор записує абсолютні шляхи `ffmpeg.exe` і `ffprobe.exe` у захищений конфіг, щоб служба знайшла їх під `LocalSystem`. Наприклад: `winget install --id Gyan.FFmpeg --scope machine`.

   `py -3.13` може й надалі показувати Python із профілю користувача навіть за наявності окремої машинної інсталяції. Перевірте список інтерпретаторів і машинні шляхи в PowerShell:

   ```powershell
   py -0p
   Get-ChildItem "$env:ProgramFiles\Python*" -Directory -ErrorAction SilentlyContinue |
     ForEach-Object { Join-Path $_.FullName 'python.exe' } |
     Where-Object { Test-Path $_ }
   Get-ChildItem 'HKLM:\SOFTWARE\Python\PythonCore' -ErrorAction SilentlyContinue |
     ForEach-Object {
       $key = Join-Path $_.PSPath 'InstallPath'
       if (Test-Path $key) {
         [pscustomobject]@{ Version = $_.PSChildName; Path = (Get-Item $key).GetValue('') }
       }
     }
   ```

   Машинна інсталяція зазвичай розташована в `C:\Program Files\Python313\python.exe`; шлях із `C:\Users\...\AppData\...` належить профілю користувача. Під час встановлення `install.ps1` друкує версію та повний шлях вибраного Python (`Using Python ... at ...`).

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

Головний екран показує стан кожного агента окремо від стану потоку, заявлений input FPS, окрему швидкість обробки FFmpeg, codec, bitrate, frame age, keyframe/GOP, decode errors, CPU/RAM, RTT і transport counters. Швидкість FFmpeg може бути вищою або нижчою за частоту джерела й не є FPS потоку. Timeline синхронізується за wall-clock часом і містить telemetry за останні шість годин. Статуси зменшуються окремо для кожного probe, а event та суттєві network samples зберігаються з точним часом; маркери клікабельні й відкривають пов'язаний incident або деталі sample. Клік на incident відкриває symptoms, timeline-контекст і останні суттєві рядки FFmpeg stderr.

Incident створюється із симптомів, видимих у відповідних probes. Система використовує `Probable location` там, де точну причину неможливо довести. Агент offline показується окремо від stream offline/stalled.

## Аналіз медіа та profiles

- `DEEP` (default): FFmpeg decode до null output, `showinfo` для кадрів/PTS/keyframe, `freezedetect`, `silencedetect`, `ashowinfo` і контроль decode errors із stderr. Transcoding не виконується. Повний decode може вимагати помітного CPU; перевірте завантаження на вашому потоці.
- `LIGHT`: ffprobe читає стиснені пакети й аналізує packet timestamps, key packet flags, розміри пакетів і GOP без video decode. Детальні decode/corrupt frame та freeze/silence checks у цьому профілі недоступні.
- GOP threshold можна задати `monitoring.keyframe_gap_threshold`; інакше він визначається з медіанного GOP, із початковим порогом 5 секунд.

FFmpeg керується агентом як subprocess: progress щосекунди, warning після відсутності progress, stall за відсутності media і restart після тривалішої відсутності. Reconnect застосовує exponential backoff. stderr не накопичується у нескінченному файлі: важливі повідомлення пишуться окремо в обертовий `ffmpeg-stderr.jsonl`, а для event зберігається останній контекст.

## Network і clock telemetry

Linux використовує `ss -ti` для TCP_INFO відповідного напрямку, якщо утиліта й права ОС дозволяють; `tcp_retransmissions` показує дельту між samples, а `tcp_retransmissions_total` — загальний лічильник відповідного socket. RTT/ICMP probe виконується окремо. Windows читає системний counter `SegmentsRetransmitted` і стан TCP-з'єднання; retransmit counter на Windows є host-wide різницею між samples, а не лічильником одного RTMP socket. Мережеві counters та RTT не впливають на діагноз, якщо snapshot старший за два інтервали probe (мінімальна межа 15 секунд). Окремий Windows retransmit без інших мережевих ознак дає `NETWORK_PATH_UNCONFIRMED`, щоб не приписувати сторонній TCP-трафік RTMP. TCP state супроводжується віком sample: якщо після старого `not-established` sample надійшли нові кадри, цей стан не використовується як доказ мережевої причини пізнішого media event. ICMP може бути заблокований: нуль відповідей показується як `NO_REPLY`, але не трактується як 100% втрат або доказ мережевої проблеми. Одна втрачена відповідь із трьох теж не достатня для мережевого діагнозу; суттєві часткові ICMP-втрати додають таку ознаку. Відсутній provider не ламає медіамоніторинг.

NTP status береться з `timedatectl` (Linux) або `w32tm` (Windows); система не змінює NTP налаштування. Offset береться з NTP provider, а за його відсутності — з timestamp приймання telemetry центральним сервером; невизначеність оцінюється як половина часу запиту. Для сумісності зі старим central API agent може використати HTTP `Date` із секундною точністю. Dashboard враховує похибку вимірювання і попереджає, якщо нижня межа абсолютного skew або міжprobe spread перевищує `clock_offset_warning_ms`; явний статус NTP unsynchronized також дає попередження. Для коректної кореляції налаштуйте NTP на всіх hosts.

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

Збережіть конфігурації, оновіть checkout, потім знову запустіть потрібний installer. Ubuntu installers перезапускають службу після встановлення, щоб застосувати новий код і до першого запуску, і під час оновлення активної служби. Central installer зберігає наявний `/etc/rtmp-monitor/central.yaml`. Windows installer зберігає встановлений agent YAML, якщо не задано `-ReplaceConfig`; Linux agent installer копіює YAML із шляху, переданого в команді. Windows-служба автоматично стартує після reboot і налаштована на повторний запуск після аварійного завершення з паузами 5, 15 і 60 секунд. Переконайтеся, що відома резервна копія SQLite перед великим оновленням.

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

Windows installer discovery regression test:

```powershell
.\tests\test_windows_python_discovery.ps1
```

Він перевіряє знаходження Python через реєстр і `Program Files`, а також блокує інтерпретатор із user profile для Windows-служби.

Повний smoke test production Windows installer потребує elevated PowerShell. Він встановлює та запускає Windows-службу, перевіряє створення логу, а потім видаляє тестову службу та створені каталоги. CI запускає його на чистому Windows runner:

```powershell
.\tests\smoke_windows_installer.ps1
```

Щоб перевірити активний RTMP URL через локальний тимчасовий central collector і один deep probe:

```powershell
python scripts/live-agent-smoke.py "rtmp://HOST:1935/live/STREAM" --seconds 30 --network
```

Команда друкує codec, роздільність, frame/keyframe counters, timestamp anomalies, мережеву телеметрію та clock-warning evidence з урахуванням похибки вимірювання. `--network` додає RTT/ICMP, втрати пакетів і доступні платформні TCP counters. Цей smoke test запускає probe в поточному процесі й не перевіряє встановлену Windows-службу чи повний acceptance сценарій із перериванням потоку.
