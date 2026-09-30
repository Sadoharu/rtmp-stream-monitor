# RTMP Stream Diagnostic Monitor

Центральний сервіс збирає телеметрію від окремих probe-агентів і об'єднує симптоми в incidents із полем **Probable location**. Реалізація розрахована на безперервний запуск: systemd або Windows Service керує агентом, який аналізує потік через FFmpeg/ffprobe або читає SRS ingress counters, тримає обмежену локальну SQLite-чергу, а центральний сервіс зберігає telemetry та incidents у SQLite через SQLAlchemy.

## Спостережні точки та межі діагностики

- `SOURCE` — агент на encoder/source читає вихідний RTMP до RTMP-сервера. Це найближче практичне спостереження до того, що encoder передає в мережу.
- `SERVER_EGRESS` — агент на Ubuntu RTMP-сервері читає потік через `rtmp://127.0.0.1:1935/...`. Це перевіряє локальну віддачу сервера.
- `CLIENT` — віддалений агент читає публічний RTMP URL. Підтримуються Ubuntu та Windows і кілька клієнтських агентів.
- `SERVER_INGRESS` для SRS читає read-only `/api/v1/streams` endpoint на самому сервері й показує publisher activity, ingress bytes/bitrate, frame counters та codec metadata. RTMP handshake на тестовому потоці визначив SRS 6.0.184. HTTP API підтверджує publisher і лічильники, але не декодує кадри та не перевіряє GOP; якщо egress зламаний за активного SRS publisher, діагноз лишається непідтвердженим. Для frame-level перевірки додайте окремий `SOURCE` або майбутній decoded ingress adapter. Loopback FFmpeg probe завжди має роль `SERVER_EGRESS`, а не ingress.

Для клієнтського probe вкажіть публічний URL потоку, наприклад `rtmp://stream.example.net:1935/live/demo`. Для локального виходу сервера можна використати `rtmp://127.0.0.1:1935/live/demo`. Перевірте, що порт central API `8090` доступний агентам.

### Enrollment probe

Спершу додайте потік і задайте адресу для потрібної точки: `public_url` для віддаленого клієнта, `local_url` для виходу RTMP-сервера або `source_url` для encoder. У розділі **Пункти спостереження** виберіть потік, роль, Windows/Ubuntu і профіль; панель видасть одноразовий код та готові команди. Код діє 15 хвилин і створює лише вибраний probe. Для віддалених агентів відкривайте панель через HTTPS.

На Windows команда майстра спершу шукає останній Windows ZIP і перевіряє SHA-256. Опублікований пакет містить приватний Python runtime, тож окремо встановлювати Python не потрібно; FFmpeg/ffprobe інсталятор ставить machine-wide через WinGet. Якщо GitHub Release ще не опублікований або Windows asset недоступний, команда переходить на source installer, для якого потрібен машинний Python 3.12+. На Ubuntu майстер так само спершу завантажує останній `.deb` та перевіряє SHA-256, після чого `apt` встановлює системні залежності, а інсталятор приховано просить enrollment code. Для Ubuntu 22.04 достатньо Python 3.10; замінювати `/usr/bin/python3` і вручну редагувати YAML не потрібно. До першої публікації GitHub Release майстер використовує source fallback. Ручна реєстрація доступна через `POST /api/v2/probe-enrollments`, а готовий YAML можна встановити командою `sudo ./install.sh agent --config /path/to/agent.yaml`. `SERVER_INGRESS` поки під'єднується ручним способом, бо йому потрібна окрема SRS API конфігурація.

Correlation використовує останні спостереження кожної ролі у 20-секундному wall-clock вікні та перевіряє розкид media PTS (default tolerance 5 секунд). PTS lag від локального egress до клієнта підтримує діагноз network path, коли обидва probes працюють у `LIGHT` mode; у `DEEP` mode lag може бути наслідком повільного decode. SRS SERVER_INGRESS counters підтверджують publisher і рух байтів, але не доводять, що вхідні кадри декодуються чи мають коректний GOP. Для frame-level source/media діагнозу використовуйте SOURCE probe або decoded ingress adapter; без такої перевірки місце збою лишається непідтвердженим. Це ймовірне місце, не математичний доказ: RTMP/TCP не переносить наскрізний ідентифікатор кадру, тому точна прив'язка до одного media packet між різними probes обмежена.

Докладніше про обраний аналіз та обмеження — [docs/architecture.md](docs/architecture.md).

## Встановлення центрального сервера Ubuntu

### Docker Compose (рекомендовано для нової інсталяції)

Потрібні Docker Engine і Docker Compose plugin v2. У каталозі репозиторію запустіть від звичайного користувача, який має доступ до Docker (не через `sudo`):

```bash
./docker-setup.sh
```

Майстер запитає порт панелі й запустить central service. Він завантажує налаштований версійний образ `ghcr.io/sadoharu/rtmp-stream-monitor`; якщо образ ще не опублікований або недоступний, збирає його з поточного checkout. SQLite та admin token зберігаються в іменованому Docker volume `central-data`, логи — у `central-logs`, а backup bundles — у локальному `backups/`. Команда виведе адресу панелі й admin token. Старі каталоги `data/` і `logs/` імпортуються у volumes один раз, якщо volumes ще порожні. Не додавайте `.env`, `secrets/openai_api_key`, `data/`, `logs/` або `backups/` до Git.

Перевірити стан і логи:

```bash
docker compose ps
docker compose logs -f central
curl -fsS http://127.0.0.1:8090/healthz
```

Для підключення віддалених probes налаштуйте HTTPS reverse proxy та firewall; майстер enrollment вимагає HTTPS для віддаленого central URL. Порт за замовчуванням слухає всі адреси. Локальну адресу прив'язки та порт можна змінити у `.env` (`RTMP_MONITOR_BIND_HOST`, `RTMP_MONITOR_PORT`). OpenAI-пояснення вимкнені, доки ви не запишете ключ у Docker secret:

```bash
chmod 700 secrets
umask 077
printf '%s' 'YOUR_OPENAI_API_KEY' > secrets/openai_api_key
docker compose up -d --force-recreate central
```

Не кладіть ключ у `.env`, YAML чи probe-конфіг.

Резервна копія з узгодженого SQLite snapshot і перевіркою цілісності:

```bash
./scripts/docker-backup.sh
```

Відновлення з backup зупиняє central service, зберігає поточну базу перед заміною і запускає service з відновленою базою:

```bash
./scripts/docker-restore.sh ./backups/central-YYYYMMDDTHHMMSSZ.tar.gz
docker compose ps
```

Backup bundle містить узгоджену SQLite-копію та admin token; файл має права лише для власника. Зберігайте його поза сервером у захищеному сховищі. Bundle не містить `.env` і `secrets/openai_api_key`; зберігайте секрет окремо. Для оновлення зробіть backup, задайте потрібний тег образу у `.env` (наприклад, `RTMP_MONITOR_IMAGE=ghcr.io/sadoharu/rtmp-stream-monitor:0.1.0`) і виконайте `./docker-setup.sh`. Якщо образ цього тегу ще не опублікований, setup збере поточний checkout. Якщо нова версія несумісна з даними, поверніть попередній тег образу й виконайте restore.

#### Перенесення з наявного systemd сервера

Перед переходом можна без зупинки сервісу зібрати read-only стан системи. Команда не читає вміст admin token і не друкує URL-и чи назви потоків:

```bash
sudo bash scripts/docker-migration-preflight.sh
```

За нестандартних шляхів передайте `RTMP_MONITOR_PREFLIGHT_DB=/absolute/path/to/central.db` та/або `RTMP_MONITOR_PREFLIGHT_TOKEN=/absolute/path/to/admin.token`, наприклад через `sudo env ... bash scripts/docker-migration-preflight.sh`. Перевірка рахує рядки таблиць, тому на великій базі може тривати; вона не змінює файли та не зупиняє central.

На тому самому хості використовуйте той самий каталог checkout для Compose. Спершу створіть `.env` і каталоги без запуску контейнера, звичайним користувачем:

```bash
./docker-setup.sh --prepare-only
sudo systemctl stop rtmp-monitor-central
sudo env PYTHONPATH="$PWD/src" python3 -m rtmp_monitor.docker_migration \
  --snapshot-source /var/lib/rtmp-monitor/central.db \
  --snapshot-target "$PWD/data/central.db"
sudo install -o "$(id -u)" -g "$(id -g)" -m 600 /var/lib/rtmp-monitor/admin.token data/admin.token
./docker-setup.sh
docker compose ps
```

Команда створює SQLite snapshot через `Connection.backup()`, перевіряє integrity та кількість рядків таблиць і відмовляється перезаписувати наявну ціль. Вона безпечніша за копіювання одного `central.db`, якщо поряд є WAL-файл. Оригінали в `/var/lib/rtmp-monitor` лишаються на місці. `docker-setup.sh` повторно перевіряє та імпортує пару DB/token у порожній named volume. Після перевірки панелі вимкніть автозапуск старого сервісу, щоб він не зайняв той самий порт після reboot:

```bash
sudo systemctl disable rtmp-monitor-central
```

Rollback: зупиніть Compose, вивантажте DB/token із named volume командами `docker compose run --rm --no-deps -T --entrypoint cat central /data/central.db > data/central.db` та аналогічною для `/data/admin.token`, встановіть ці файли назад у `/var/lib/rtmp-monitor/`, поверніть власника `rtmp-monitor:rtmp-monitor`, потім `sudo systemctl enable --now rtmp-monitor-central`. Старий systemd binary/config не видаляйте, доки Compose версія не перевірена.

SQLite, Compose healthcheck, setup і backup/restore описані докладніше у [звіті M4](docs/m4-docker-compose.md).

### Systemd / Python (альтернативний спосіб)

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

1. У Dashboard відкрийте **Пункти спостереження**, виберіть потік, роль `CLIENT`, платформу Windows і профіль. Створіть одноразовий код та скопіюйте згенеровану команду PowerShell.
2. Запустіть PowerShell **від Administrator** і вставте команду з майстра. Вона завантажить ZIP у `%TEMP%`, перевірить SHA-256, розпакує пакет і запустить інсталятор; Git і системний Python не потрібні. Якщо ZIP ще не опублікований, команда перейде на source fallback, для якого потрібен machine-wide Python 3.12+. Введіть одноразовий код у прихованому запиті. Інсталятор перевірить FFmpeg/ffprobe і за потреби встановить їх machine-wide через WinGet. Перегляньте та прийміть умови пакетів, показані WinGet. Документація Microsoft описує [WinGet](https://learn.microsoft.com/en-us/windows/package-manager/winget/install); FFmpeg пакет: [Gyan.FFmpeg](https://github.com/microsoft/winget-pkgs/tree/master/manifests/g/Gyan/FFmpeg). Якщо WinGet відсутній, встановіть FFmpeg machine-wide вручну. Служба запускається від `LocalSystem`, тому не використовує програми з `C:\Users\...`.

Пакетний інсталятор розміщує приватний Python runtime у `%ProgramFiles%\RTMPMonitor\runtime`. Оновлення замінює runtime та перезапускає службу, зберігаючи налаштування й локальну чергу. Для source fallback `install.ps1` використовує machine-wide Python 3.12+; Python Launcher `py` може показувати per-user версію, тому fallback шукає Python у машинній інсталяції, а не покладається на перший результат `py`.

Installer створює Windows Service `RtmpMonitorAgent` з automatic startup. Перевірити стан можна через `Get-Service RtmpMonitorAgent`; структуровані логи зберігаються в `%ProgramData%\RtmpMonitor\logs`.

Для повного видалення зупиніть і видаліть службу, програмні файли, локальний токен, конфігурацію, чергу та логи з PowerShell Administrator:

```powershell
.\uninstall.ps1
```

Скрипт просить підтвердження. Параметр `-KeepLocalData` лишає `%ProgramData%\RTMPMonitor` для перевстановлення або ручного збереження черги. Після видалення агента його запис і вже надіслана історія лишаються на central server; у Dashboard можна видалити probe, щоб відкликати його токен, не втрачаючи історію.

При оновленні інсталятор зберігає наявний `%ProgramData%\RtmpMonitor\agent.yaml`. Щоб замінити його новим Dashboard YAML, запустіть `.\install.ps1 -ConfigPath .\config\agent.yaml -ReplaceConfig`.

## Додавання потоку та клієнтів

У розділі **Потоки** додайте RTMP-потік, який уже передає ваша encoder/RTMP-система. Monitor не публікує потік і не перезапускає його: він відкриває його як читач. Збережіть три адреси окремо: `source_url` — encoder, `local_url` — loopback-адреса на RTMP-сервері, `public_url` — адреса, доступна клієнтам. Наприклад, якщо потік називається `poland`, локальна адреса може бути `rtmp://127.0.0.1:1935/live/poland`, а клієнтська — `rtmp://stream.example.net:1935/live/poland`.

Потім відкрийте **Пункти спостереження** й створіть probe для кожної машини та ролі. Майстер видає одноразовий enrollment code і команду інсталяції, тож не потрібно вручну збирати YAML чи копіювати довготривалий токен. Додайте `SERVER_EGRESS` на RTMP-сервер і один або кілька `CLIENT` на мережевих клієнтах. `SOURCE` додавайте на encoder, якщо треба звузити пошук несправності до або після RTMP-сервера.

## Dashboard та incidents

Сторінка потоку показує виміряний бітрейт окремою лінією для кожного probe; значення можна приховати, щоб порівняти решту. Прогалини телеметрії лишаються прогалинами, короткі спади зберігаються як мінімум бакета, а перемикачі періоду й zoom допомагають знайти точний час. Під графіком на спільній часовій шкалі відображаються інциденти, помилки агента й медіаподії; натисніть маркер, щоб побачити пояснення, виміри-докази, рівень упевненості та наступні перевірки. Головна сторінка також показує стан probe, FPS, codec, останній keyframe/GOP, decode errors, RTT і retransmits.

Incident створюється із симптомів, видимих у відповідних probes. Система використовує `Probable location` там, де точну причину неможливо довести. Агент offline показується окремо від stream offline/stalled.

У картці інциденту є дія **«Пояснити причину й наступні кроки»**. Без OpenAI вона формує локальне пояснення з телеметрії та вказує відсутні докази. Щоб увімкнути додаткове формулювання OpenAI, на центральному сервері відредагуйте `sudoedit /etc/rtmp-monitor/openai.env` і додайте `OPENAI_API_KEY=...`; за потреби задайте `OPENAI_MODEL=gpt-6-luna`. Потім виконайте `sudo systemctl restart rtmp-monitor-central`. Installer створює файл із правами `root:rtmp-monitor` та `0640`; ключ не кладеться в YAML або probe. Кнопка робить запит лише після натискання. До OpenAI йдуть псевдонімізовані події, відносний час і дозволений набір метрик; stream URL, назви probe, токени та FFmpeg stderr виключені. Запит використовує `store:false`; це вимикає збереження стану відповіді в API, але не є обіцянкою нульового зберігання всіх даних провайдером. Див. [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) та [Data controls](https://developers.openai.com/api/docs/guides/your-data).

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

Щоб прибрати client probe із сервера, натисніть **Remove probe** на його картці після оновлення версії з цією функцією або викличте `DELETE /api/v1/agents/{agent_id}` із Dashboard bearer token. Це відкликає його token і прибирає картку з активних probes, але зберігає історичні telemetry та incidents. Повторно створивши probe з тим самим ім'ям, можна зареєструвати його знову. На Windows не видаляйте службу командою `sc.exe delete` перед зупинкою: відкрийте PowerShell від Administrator у каталозі репозиторію та виконайте `.\uninstall.ps1`. Скрипт спочатку зупиняє службу, потім видаляє її та `C:\Program Files\RTMPMonitor`; за замовчуванням також прибирає локальні config/queue/logs із `C:\ProgramData\RTMPMonitor`. Для Ubuntu без Debian package зупиніть службу й приберіть unit та програму: `sudo systemctl disable --now rtmp-monitor-agent.service && sudo rm -f /etc/systemd/system/rtmp-monitor-agent.service && sudo systemctl daemon-reload && sudo rm -rf /opt/rtmp-monitor-agent /etc/rtmp-monitor-agent`. Видалення `/var/lib/rtmp-monitor` або `/var/lib/rtmp-monitor-agent` знищує базу/чергу та потребує окремого підтвердження адміністратора.

Після Windows uninstall перевірте стан команди `Get-Service RtmpMonitorAgent -ErrorAction SilentlyContinue` і теки `Test-Path "$env:ProgramFiles\RTMPMonitor"`, `Test-Path "$env:ProgramData\RTMPMonitor"`. Якщо раніше вже виконали `sc.exe delete RtmpMonitorAgent`, наступний `Stop-Service` може відповісти, що службу не знайдено: запит видалення вже поданий. Якщо `sc.exe query RtmpMonitorAgent` повертає помилку 1072 (службу позначено для видалення), перезавантажте Windows, щоб звільнити її системний handle.

Для probe, встановленого Debian package, виконайте `sudo apt remove rtmp-monitor-agent`: це зупиняє службу та прибирає програму, залишаючи конфігурацію в `/etc/rtmp-monitor-agent` і локальну чергу/логи. `sudo apt purge rtmp-monitor-agent` додатково видаляє конфігурацію; `/var/lib/rtmp-monitor-agent` і `/var/log/rtmp-monitor-agent` залишаються, доки адміністратор не прибере їх окремо.

Щоб прибрати центральний код і конфігурацію після зупинки служби, виконайте `sudo rm -rf /opt/rtmp-monitor /etc/rtmp-monitor`; для probe використайте `/opt/rtmp-monitor-agent /etc/rtmp-monitor-agent`. Щоб також видалити дані, окремо перевірте та видаліть відповідний `/var/lib/rtmp-monitor*` каталог і логи. На Windows у elevated PowerShell зупиніть та видаліть службу командами `Stop-Service RtmpMonitorAgent` і `sc.exe delete RtmpMonitorAgent`. За потреби видаліть пакет `rtmp-stream-monitor` із машинного Python, шлях до якого надрукував інсталятор (`<python.exe> -m pip uninstall rtmp-stream-monitor`), потім видаліть `C:\Program Files\RTMPMonitor` і, якщо не потрібні, логи та конфігурацію в `C:\ProgramData\RtmpMonitor`.

## Розробка та перевірки

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m pytest
rtmp-monitor server --config config/central.dev.yaml
```

Зміни панелі робіть у `frontend/`. Для локальної роботи фронтенд проксить API до central service на `127.0.0.1:8090`; перед перевіркою в браузері запустіть backend, потім:

```powershell
cd frontend
npm ci
npm run typecheck
npm run build
```

Для dev server відкрийте окремий термінал у `frontend/` і запустіть `npm run dev`.

Production-збірка потрапляє до `src/rtmp_monitor/static/` і віддається FastAPI. `npm run build` не запускається на production-хості.

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
