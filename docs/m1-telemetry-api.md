# M1 — вимірювач і версований API

**Статус: реалізовано й перевірено на Windows і в Ubuntu 22.04/Jammy-контейнері зі SQLite.** Повний runtime на фізичному цільовому Linux-хості та PostgreSQL лишаються неперевіреними.

## Що вимірюється

`received_media_bitrate_bps` — сума розмірів отриманих encoded audio/video packet payloads за останнє повне односекундне wall-clock вікно. Це не швидкість Ethernet/IP/RTMP із протокольними накладними витратами. Час видачі packet row фіксується `time.monotonic()` у процесі агента; він відображає доставку до pipe FFmpeg/ffprobe, а не timestamp мережевого кадру.

- `LIGHT` рахує розміри audio/video-пакетів на вже відкритому `ffprobe` з'єднанні; нерозпізнані й data-пакети не додаються.
- `DEEP` декодує основний FFmpeg output і паралельно копіює оригінальні audio/video-пакети у `framecrc` output того самого input/demux. Це одне RTMP-з'єднання. FFmpeg `-progress` переїхав на stderr, `framecrc` — у stdout.
- До першого медіапакета та першої повної секунди якість — `MEASUREMENT_WARMUP`, значення — `null`. Якщо потік уже доставляв медіапакети, а повне останнє вікно порожнє, результат — виміряний `0 bps`. Якщо агент не зміг визначити типи потоків, якість — `MEASUREMENT_UNAVAILABLE`.
- На reconnect rolling baseline очищується. Старий `bitrate_estimate_bps` лишений як alias виміряного значення тільки для сумісності. FFmpeg output rate більше не записується у неоднозначний `bitrate`; старий екран окремо називає `ffmpeg_output_bitrate`.

Кожний секундний snapshot зберігає UTC `observed_at`, серверний `received_at`, quality, вікно, кількість медіапакетів і sample interval. Старі агенти можуть і далі надсилати записи без нового виміру.

## API

- `GET /api/v2/streams/{stream_id}/series?from=...&to=...&resolution=1s|5s|10s|1m|5m|1h&probe_ids=...` повертає probe-окремі min/avg/max, sample/expected counts, measurement window, останній час виміру та стислий список прогалин. Сервер збільшує resolution за потреби, щоб обмежити точки приблизно 10 тисячами на probe; інтервал до 90 днів.
- `GET /api/v2/streams/{stream_id}/events?from=...&to=...&probe_ids=...&severity=...` повертає raw probe markers та incidents на спільній часовій шкалі. `*_START`/`*_END` утворюють інтервал, якщо обидва маркери є у вибраному вікні. Пояснення raw marker прямо каже, що одна подія не доводить кореневу причину; confidence для цієї M1 відповіді — `UNCONFIRMED`.
- Обидва запити потребують чинної admin bearer авторизації та повертають `404` для невідомого потоку/probe й `422` для невалідного діапазону.
- Для недавніх даних SQLite та PostgreSQL групують raw telemetry запитом у базі. Після raw retention читаються лише нові rolled-up записи з metric-specific `count` та `last_observed_at`; старі агрегати без виміряного поля лишаються прогалиною, без backfill із codec metadata або FFmpeg bitrate. Збережені агрегати мають хвилинний min/max, тож короткий dip лишається видимим на довшому інтервалі.
- Існуючий `/api/v1/ingest` і `/api/v1/timeline` не змінені. У retention aggregate тепер зберігає кількість вимірів і час останнього sample для кожної числової метрики.

Прогалина без телеметрії позначається `NO_SAMPLE`; явний warmup/unavailable — відповідним reason. Для хвоста після останнього heartbeat API використовує `PROBE_OFFLINE`, коли вік даних перевищив налаштований timeout. Missing row ніколи не перетворюється на нуль. Конкретну причину втрати ще треба класифікувати за зібраними probe, це належить до M3.

## Перевірка

- Увесь наявний `pytest` набір: пройшов; один тест пропущений штатно.
- Нові перевірки покривають DEEP framecrc parse, фільтр data-stream, прогрів, валідний zero, спільний demux command, годинний bucket із п'ятисекундним падінням до zero, події/їх межі, відсутні samples, агрегат після retention і auth/range validation.
- На Windows з FFmpeg `N-113802-g22845fbb8-2024-02-24-nonfree` по 8 секунд перевірено реальний тестовий RTMP у `LIGHT` і `DEEP`: перший виміряв 9.11 Mbps, другий 7.69 Mbps; обидва мали quality `MEASURED`, 1s window і понад 800 пакетів. Це послідовні capture змінного потоку, тому різниця між числами не є порівнянням точності профілів. Обидва процеси були зупинені тестом; return code 1 очікуваний після примусового завершення. У `DEEP` progress і decode FPS продовжили надходити.
- Оновлений 20-секундний Windows smoke на user-provided live RTMP у `LIGHT` та `DEEP` пройшов наскрізно: агент надіслав `MEASURED` бітрейт із 1-секундним вікном, а `/api/v2/streams/{id}/series` повернув виміряні 1-секундні buckets. `/events` повернув коректний порожній результат для інтервалу без інцидентів. Це два послідовні короткі capture змінного потоку, не порівняння точності профілів і не перевірка network-failure сценарію.
- У тимчасовому Ubuntu 22.04 контейнері з Python 3.10.12 та системним FFmpeg 4.4.2 весь набір пройшов: `113 passed, 1 skipped`. Контрольований SRS/FFmpeg fault fixture у `LIGHT` і `DEEP` записав виміряні series; у `DEEP` зафіксував `FREEZE_START` на 0 bps та `FREEZE_END` після відновлення до 7.327 Mbps. Деталі — у [M6 live validation](m6-live-validation.md). Це Docker Desktop runtime, не фізичний цільовий сервер.
- Два одночасні Ubuntu probes на наданому live RTMP протягом 40 секунд надіслали 28 `LIGHT` і 26 `DEEP` виміряних секундних buckets. Інтервали без рядка лишилися `NO_SAMPLE`, не були перетворені на нуль. Це підтверджує вимірювач на цій Ubuntu/FFmpeg збірці, але не пояснює причини пропусків.

## Обмеження, які лишились

- Packet arrival — час запису FFmpeg/ffprobe у pipe; буферизація планувальника та decode backpressure можуть змістити короткі сплески. Потрібні повторний A/B із CPU/RAM на цільовому сервері й перевірка scheduling/backpressure там.
- Windows прогін використовував наявний non-free build, а не production installer binaries. Ubuntu smoke використовував Jammy FFmpeg 4.4.2 у Docker Desktop; production binaries та фізичний Ubuntu-хост ще треба перевірити.
- PostgreSQL гілки SQL написані, але інфраструктури PostgreSQL у цьому середовищі немає для runtime-тесту.
- React/ECharts тепер підключені до цих endpoints у production Dashboard. У браузері перевірено user-provided live stream через один Windows CLIENT probe і контрольований dip/freeze через два одночасні Windows CLIENT probes: обидві лінії, маркери та картка події з числовими доказами. API-навантаження на тимчасових fixtures (345 600 samples для 24 год/4 probes і 40 320 хвилинних rollups для історичних 7 днів) у повторних прогонах відповідало за 1,9–4,1 с та 1,0–2,1 с відповідно. Браузерний fixture перевірив 4 серії у вікнах 24 год/7 днів; деталі — у [звіті M2](v2-development-plan-for-sol.md), відтворення — [benchmark-series-load.py](../scripts/benchmark-series-load.py). Залишаються multi-probe browser перевірка з незалежних вузлів, фізичний Linux host і PostgreSQL runtime. Успадкований M0 HTML-макет із синтетичними даними лишається окремою UX-довідкою й не є продуктом.
