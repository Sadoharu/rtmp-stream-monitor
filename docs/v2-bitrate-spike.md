# RTMP Monitor 2.0 — вимірювальний spike бітрейту (M0)

## Навіщо

`LIGHT` зараз отримує `packet.size` з `ffprobe`. У `DEEP` `ffmpeg -progress` повертає `bitrate=N/A` для null output; поле `bitrate` у заголовку відео є довідковими метаданими, а не виміряною швидкістю. Графік не повинен подавати це поле як отриманий бітрейт.

## Попередня перевірка на реальному потоці

29.09.2026 з Windows-хоста через переданий користувачем тестовий RTMP URL перевірено `ffprobe` і поточну deep-команду FFmpeg. Джерело оголосило H.264 1920×1080 50 fps та AAC 48 kHz. `ffprobe -show_packets` повертав розмір кожного аудіо/відеопакета. Поточна deep-команда показала `bitrate=N/A`, `total_size=N/A`; вона не має фактичного байтового лічильника після читання RTMP.

Перевірений варіант для одного читача: до наявного FFmpeg decode/null pipeline додати другий output того самого input, який мапить оригінальні audio/video пакети з `-c copy` у `-f framecrc -hash crc32` на stdout; progress перенести на `pipe:2`. FFmpeg multiplexes обидва outputs з одного input/demux, отже це не створює другого RTMP-з'єднання. Рядок framecrc містить output stream index і packet size; агент може timestamp-ити рядок на його фактичному надходженні через `time.monotonic()`. `framehash -hash none` на тестовій збірці не спрацював, а CRC32 дає потрібний лічильник без MD5/SHA.

Перевірена форма команди (URL не записується в репозиторій):

```powershell
ffmpeg -hide_banner -nostats -loglevel warning -progress pipe:2 -stats_period 1 `
  -rw_timeout 15000000 -i $StreamUrl `
  -t 8 -map '0:v?' -map '0:a?' -vf showinfo -af ashowinfo -f null - `
  -t 8 -map '0:v?' -map '0:a?' -c copy -f framecrc -hash crc32 pipe:1
```

У 8-секундному capture framecrc повернув 776 аудіо/відео packet rows та 8.1–8.7 MB payload, а FFmpeg progress для null output залишився `bitrate=N/A`. Індекси framecrc — output indices з заголовків `#media_type`; не копіювати індекси ffprobe input. У тесті output video мав індекс 0, audio — 1, а input індекси були навпаки.

## Повторні виміри та накладні витрати

На цьому Windows-хості (Intel i9-12900KF, 24 логічні CPU) тричі послідовно запускалися 8-секундні LIGHT, поточний DEEP та DEEP із framecrc. FFmpeg build `N-113802-g22845fbb8-2024-02-24-nonfree`; виміри треба повторити на FFmpeg builds для підтримуваних Windows/Ubuntu пакетів.

| Режим | Media Mbps, діапазон | CPU одного логічного ядра, середнє | Peak RSS, середнє |
|---|---:|---:|---:|
| LIGHT / ffprobe | 8.17–9.27 | 1.9% | 27.0 MB |
| DEEP / поточний decode | не вимірюється; `bitrate=N/A` | 28.9% | 180.4 MB |
| DEEP / спільний demux + framecrc | 8.11–8.59 | 31.4% | 180.2 MB |

У цьому короткому послідовному замірі framecrc додав у середньому 2.4 відсоткового пункту CPU одного ядра (близько 8% від базового DEEP); зміна RSS у межах шуму заміру. Це напрямний результат трьох послідовних capture, не гарантія для інших машин. LIGHT і DEEP читали потік окремо в часі, тому різниця їхніх Mbps не є похибкою вимірювача.

Окремий arrival-time capture дав 776 пакетів на прохід. Перший секундний бакет після connect був близько 19.7–19.9 Mbps, наступні — 4.5–10.1 Mbps; подібний стартовий сплеск був і з `-flush_packets 1`, і без нього. Це одна послідовна пара на змінному потоці, тож не доводить перевагу одного flush-режиму. Не показувати перші секунди як усталений тренд: давати `MEASUREMENT_WARMUP` до накопичення повного вікна. Arrival time у stdout pipe — час видачі packet tap процесом, а не timestamp фізичного приходу мережевого кадру; у M1 перевірити FFmpeg scheduling і decoder backpressure.

## Додаткові перевірки, що лишилися до повного M6

- Rolling measurement перенесений у production agent; три live прогони одночасних Windows `LIGHT`/`DEEP` підтвердили виміряні серії одного потоку в V2 API. Деталі: [M6 live validation](m6-live-validation.md).
- Контрольований шість секунд чорний кадр пройшов наскрізь через FFmpeg encoder → локальний SRS RTMP → одночасні `LIGHT`/`DEEP` Windows probes → SQLite collector → V2 series/events API. В обох профілях мінімум був 0.159 Mbps проти піку 4.635/4.743 Mbps (3.4%/3.3%); у `DEEP` записалися `FREEZE_START` та `FREEZE_END`, а після чорного інтервалу бітрейт відновився приблизно до 4.4 Mbps. Цей контрольований fixture був 720p50, тому це не замінює замір окремого production FFmpeg build на 1080p50.

Відтворюваний локальний прогін на Windows (потрібні Docker із `ossrs/srs:6`, FFmpeg у PATH із `libx264`, Python 3.12 та встановлені залежності проєкту):

```powershell
docker run -d --name rtmp-monitor-m0-srs -p 127.0.0.1:19350:1935 ossrs/srs:6
```

В одному PowerShell запустіть fixture publisher; в іншому відразу запустіть 55-секундний capture. Довший publisher залишає запас для ручного відкриття dashboard і не обриває джерело до кінця вимірювання:

```powershell
.\scripts\publish-rtmp-fault-fixture.ps1 -Seconds 120 -DipStartSeconds 50 -DipDurationSeconds 6
py -3.12 scripts/live-multiprobe-smoke.py rtmp://127.0.0.1:19350/live/m0-fault-fixture `
  --seconds 55 --profiles LIGHT DEEP `
  --require-event FREEZE_START --require-event FREEZE_END `
  --max-bitrate-floor-ratio 0.5 --serve-after 240
```

Після smoke dashboard і тимчасова база лишаються доступні на localhost на вказаний час; URL та одноразовий admin token друкуються в консолі. Увійдіть у браузері, виберіть 15 хв, перевірте лінії/маркери й відкрийте `FREEZE_START`; не публікуйте token. Після завершення приберіть лише тестовий сервер командою `docker rm -f rtmp-monitor-m0-srs`. Publisher за замовчуванням слухає тільки localhost, передає 720p50 test pattern + аудіотон і затемнює кадр між 20-ю та 26-ю секундами. Аргументи сценарію описані через `-Seconds`, `-DipStartSeconds` і `-DipDurationSeconds` у [fixture script](../scripts/publish-rtmp-fault-fixture.ps1).
- 29.09.2026 цей самий 720p50 контрольований сценарій пройшов у тимчасовому Ubuntu 22.04 контейнері: Python 3.10.12, системний FFmpeg 4.4.2. `LIGHT` повернув 21, `DEEP` — 20 виміряних секундних buckets; мінімум/пік становили 0.068/7.479 Mbps (`LIGHT`) та 0/7.327 Mbps (`DEEP`). У `DEEP` API отримав `FREEZE_START` при виміряних 0 bps і віці останнього кадру/аудіо 1.094 s, а `FREEZE_END` — після відновлення до 7.327 Mbps. SRS продовжував приймати publish протягом fixture; це контрольований чорний кадр/freeze сценарій, а не мережева втрата.
- Окремий 40-секундний read-only capture того ж дня з двома Ubuntu probes на наданому live RTMP повернув 28 `LIGHT` і 26 `DEEP` виміряних buckets. Середнє/мінімум/максимум: 8.443/2.760/15.139 Mbps (`LIGHT`) і 8.236/0.449/18.443 Mbps (`DEEP`). Пропущені секунди були явно `NO_SAMPLE`; цей результат не визначає, чи пропуск спричинив планувальник, FFmpeg pipe чи інше.
- У цьому 40-секундному capture FFmpeg показав 1.1% CPU / 50.2 MB RSS (`LIGHT`) та 26.2% CPU / 177.0 MB RSS (`DEEP`). В окремому 20-секундному двопроцесному прогоні спільний Docker `eth0` лічильник змінився на 64.80 MiB RX і 3.69 MiB TX (середнє 21.23/1.21 Mbit/s). Це сумарна дельта одного контейнера для двох probes з транспортними накладними витратами, не per-agent мережевий бенчмарк чи оцінка production capacity.
- Ці перевірки працювали в Docker Desktop на Windows, а не на фізичному цільовому Ubuntu-сервері. Повторення на фізичному хості, production FFmpeg bundles, відсутність audio/video track, reconnect/кінець pipe та stderr parsing ще потребують перевірки.
- Початковий CPU/RSS benchmark був на одному Windows FFmpeg build; додано Jammy apt FFmpeg runtime у Docker Desktop. Додаткові CPU/RSS/мережеві витрати слід виміряти з FFmpeg, який реально постачається для Windows/Ubuntu, на цільовому фізичному хості й із повним набором агентів.

## Попереднє рішення

Для M1 реалізувати `received_media_bitrate_bps` за rolling window з часом надходження пакетів. У LIGHT — на вже наявних ffprobe packet rows; у DEEP — packet-copy/framecrc output того самого FFmpeg demux input. Це сума encoded audio/video packet bytes без RTMP/TCP/IP overhead; так і називати метрику в API та інтерфейсі. Не використовувати `stream_metadata["bitrate"]` або nominal codec bitrate.

Зберігати короткий кільцевий буфер packet sizes з monotonic timestamp і щосекунди публікувати вимір за останнє rolling-вікно. На reconnect очищувати baseline; до накопичення мінімального вікна значення — `null` з причиною `MEASUREMENT_WARMUP`. Коли пакетів немає, показувати gap/agent offline, не 0 bps.

**Статус M0 spike:** framecrc packet-size path перевірений на реальному 1080p/50 fps RTMP потоці; є три короткі CPU/RSS повтори та окремий arrival/flush capture. Rolling window реалізований у production agent і перевірений live в обох профілях, включно з одночасним API-прогоном. Контрольований 720p50 RTMP test у локальному SRS зберіг 6-секундний bitrate dip і DEEP freeze markers наскрізно до V2 API; той самий інтервал перевірено в production dashboard: графік показав обидва профілі й подієві маркери, а картка — виміряну тривалість `6.02 s`, бітрейт і межу висновку. Під час перевірки виправлено сирий підпис `FREEZE_DURATION` і хибну тривалість точкової події. Відтворення доступне через fixture scripts. Фізичний цільовий Ubuntu host і production FFmpeg bundles залишаються відкритими.
