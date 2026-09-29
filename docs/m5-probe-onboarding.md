# M5 — підключення та життєвий цикл probe

**Статус: частково реалізовано.** API та Dashboard wizard видають короткоживучий одноразовий enrollment code і повертають готовий agent config після погашення. Windows і Ubuntu installers приймають код прихованим prompt-ом; wizard показує команду інсталяції для вибраної платформи. Додано Ubuntu `.deb` та Windows ZIP із приватним Python runtime, обидва мають SHA-256; tag-triggered GitHub Release workflow публікує обидва формати разом і тепер перевіряє offline-встановлення Ubuntu wheelhouse. Self-contained Windows пакет зібрався локально й пройшов імпортну перевірку. Існуючі Windows Actions source-Python service lifecycle jobs пройшли на run 36525504991 (Python 3.12–3.14); окремий bundled-runtime install/start/uninstall job уже доданий у робочу копію, але GitHub ще не виконав саме цю зміну. GitHub Release ще не опублікований, тому wizard поки використовує source fallback.

29.09.2026 пакет Windows зібраний повторно з поточної робочої копії. Вбудований Python 3.13.15 x64 успішно імпортував RTMP Monitor, pywin32 і службові модулі; ZIP містить installer, uninstaller і `pythonservice.exe`, а SHA-256 архіву збіглася з sidecar-файлом. Hash цього локального артефакту: `1ad7a0d8b67e0c47f38aa5da8cde5540f40a7578c3e76ac16bac7317e2b7f0d0`. Це перевіряє складання та вміст ZIP; Windows service install/start/uninstall smoke для нього ще не виконаний.

### Ubuntu `.deb` build та offline install — 2026-09-29

Перший повторний Jammy build виявив дві проблеми: системний pip 22.0.2 створював `UNKNOWN-0.0.0` замість app wheel, а wheelhouse не мав runtime-залежностей застосунку. Builder тепер створює ізольоване build venv із pip 23+, завантажує app wheel та повний набір бінарних runtime wheels і завершується помилкою, якщо app wheel відсутній або їх кілька.

Повторна збірка в Ubuntu 22.04 із системним Python 3.10.12 та pip 22.0.2 успішна. SHA-256 sidecar збіглася (`3512241d1a7275866e586b4ccd09f23dba3beb2c9af56aa9f184c9687e550b5c`); `dpkg-deb` показав `rtmp-monitor-agent 0.1.0 amd64`. Вміст пакета встановився у свіжий Python 3.10 venv із `pip --no-index`; імпорт RTMP Monitor та його залежностей і `rtmp-monitor --help` пройшли без PyPI. `dpkg` install, enrollment-wrapper help і purge hooks теж пройшли в Jammy-контейнері. Для hook smoke було примусово обійдено залежність FFmpeg, а systemd у контейнері не запускався; це не приймається як перевірка apt dependency resolution чи реального service start.

## Зміни

- Windows ZIP builder pinned-downloads Python 3.13 embedded distribution, verifies Python.org SHA-256, vendors app/pywin32/dependency wheels, and emits an archive checksum. The package installer copies that private runtime into `%ProgramFiles%\RTMPMonitor\runtime`, then registers the service without relying on a system Python or pip. FFmpeg/ffprobe remain separate machine-wide programs; WinGet can install `Gyan.FFmpeg` when unavailable. Source checkouts still support the machine-Python installation path for development.
- Dashboard wizard creates the probe and shows a one-time code plus install command. Windows downloads the bundled ZIP and checks its checksum, falling back to the source archive until a release asset exists. Ubuntu downloads and checks the `.deb` before installation and also has a source fallback. `release-agent.yml` builds both packages and publishes the versioned and stable `.deb` names plus the stable Windows ZIP when a `vX.Y.Z` tag matches `pyproject.toml`. On Windows, manual YAML installation remains available with `-ConfigPath`.
- `POST /api/v2/probe-enrollments` створює код із TTL 15 хв, прив'язаний до однієї probe й stream. У базі лежить лише SHA-256 хеш; повторне погашення, прострочений код і вимкнена probe відхиляються. Публічний central URL приймається лише як HTTPS origin; HTTP дозволений для loopback тесту. Видача enrollment поки підтримує `CLIENT`, `SERVER_EGRESS` і `SOURCE`; `SERVER_INGRESS` потребує SRS API налаштувань і лишився у ручному flow.
- Enrollment endpoint також можна викликати авторизованим `curl` запитом за прикладом у README. На Windows запускається `.\install.ps1 -ServerUrl https://monitor.example.net`, Ubuntu — `sudo ./install-agent.sh --server https://monitor.example.net`; обидва installer-и просять код приховано та зберігають отриманий JSON як YAML-сумісний конфіг.
- Новий `uninstall.ps1` спершу зупиняє службу, звільняє її Windows handle, потім видаляє `RtmpMonitorAgent` і програмні файли, а за замовчуванням також локальні config/outbox/logs. Параметр `-KeepLocalData` залишає `%ProgramData%\RTMPMonitor` для перевстановлення. На центральному сервері запис probe й надіслана історія зберігаються, доки адміністратор не видалить probe у Dashboard.
- `scripts/build-agent-deb.sh` створює Ubuntu 22.04 `amd64 .deb` з app wheel, усіма runtime-залежностями та packaging tools у wheelhouse для Python 3.10+, щоб pip не звертався до PyPI під час встановлення. Пакет спирається на `apt` для Python, venv, FFmpeg та CA certificates; окремий `rtmp-monitor-agent-install --server URL` запускає наявний прихований enrollment flow. Оновлення пакета зберігає конфіг; `apt remove` прибирає службу й програмні файли, `apt purge` додатково прибирає `/etc/rtmp-monitor-agent`, а локальну чергу/логи потрібно видаляти окремо за потреби.
- Центральний API вже має перегляд стану агентів, ротацію токена й `DELETE /api/v1/agents/{id}`, що відкликає токен та зберігає історію.

## Відомі межі

- Windows bundled agent no longer requires installing Python, but it still requires administrative PowerShell and machine-accessible FFmpeg/ffprobe. WinGet installs FFmpeg when present; if WinGet is missing, install FFmpeg machine-wide manually. The bundle is a ZIP, not a signed MSI.
- Ubuntu `install-agent.sh` тепер приймає Python 3.10+, тому Ubuntu 22.04 може використати системний Python без PPA чи заміни `/usr/bin/python3`. Збірка `.deb` включає Python wheelhouse, але системні Python/venv і FFmpeg досі встановлюються як apt-залежності. GitHub Release з package asset поки не створений, цифрового підпису немає, clean install/upgrade/remove на цільовому сервері не перевірені.
- Enrollment code endpoint, Dashboard wizard і installer prompt готові. Картка probe у панелі показує стан, платформу, роль та останній сигнал; revoke token доступний зі збереженням історії. Потрібно окремо пройти install/revoke на чистих Windows та Ubuntu системах.
- Одноразовий enrollment перевірений API тестами; Bash/PowerShell parser проходять. Повний Python 3.10 suite пройшов в Ubuntu 22.04 контейнері (`113 passed, 1 skipped`), Python 3.12 suite — на host. У тому самому Jammy runtime із системним FFmpeg 4.4.2 пройшли контрольований SRS fault smoke та двопробний live RTMP smoke. Це Docker Desktop, не фізична цільова машина; Ubuntu auto-dependency flow і цільова інсталяція лишаються відкритими.
- Новий `.deb` зібрався Python 3.10 build environment-ом; `dpkg-deb --info`, SHA-256 verification та `dpkg` install/remove/wrapper smoke пройшли в Ubuntu 22.04 контейнері. Окремо Python 3.10 встановив app wheel і всі залежності з wheelhouse у venv із `--no-index`; жодного звернення до PyPI під час цього install не було. `apt install` із фактичним задоволенням OS-залежностей і запуск служби під systemd не завершувався в цьому середовищі, тому ці критерії залишаються відкритими.
- Інсталяцію WinGet dependencies на чистому Windows-хості та фактичне повне видалення ще треба перевірити на Windows runner/цільовій машині; у цьому середовищі перевірятиметься PowerShell синтаксис і наявний production smoke шлях із уже доступними залежностями.

## CI перевірка service install

На GitHub Actions для `main` commit `a052931249bc253cfebc151ee1052bb8d5569d01` усі job-и `Windows production installer` для Python 3.12, 3.13 і 3.14 завершилися успішно; також пройшли Windows test/service lifecycle job-и на цих версіях та Ubuntu/Python 3.12. Це підтверджує реєстрацію, запуск і ініціалізацію агента як Windows Service у чистому runner-середовищі. Smoke підставляє тестові `ffmpeg.exe`/`ffprobe.exe` і не запускає WinGet для dependency install, тож чисте встановлення залежностей, інсталяція WinGet та removal на цільовому ПК лишаються окремими перевірками. Workflow: https://github.com/Sadoharu/rtmp-stream-monitor/actions/runs/36525504991.

The existing Windows system-Python service lifecycle check remains in `tests/smoke_windows_installer.ps1`. The same smoke now accepts `-BundlePath` and is wired to a separate Windows Actions job that builds the package, installs/starts the service using its private runtime, and removes it without configuring Python on that runner. This job has not run from this local checkout yet.

## Ubuntu `.deb` build

Build from an Ubuntu 22.04 `amd64` host with Python 3.10 and network access to PyPI:

```bash
bash scripts/build-agent-deb.sh ./dist
sha256sum --check ./dist/rtmp-monitor-agent_0.1.0_amd64.deb.sha256
```

Install the package and enroll/start the agent as two explicit steps:

```bash
sudo apt install ./dist/rtmp-monitor-agent_0.1.0_amd64.deb
sudo rtmp-monitor-agent-install --server https://monitor.example.net
```

The second command asks for the one-time code without echoing it. The `.deb` bundles Python wheels, but apt still installs the operating-system runtime and FFmpeg. A tag-matching release will make versioned and stable package assets available to the Dashboard's checksum-verified installer path; until the first release exists, the wizard falls back to its source-tarball installer. Clean target installation and upgrade smoke remain open.
