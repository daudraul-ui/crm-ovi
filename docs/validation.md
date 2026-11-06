# Проверка этапа A — 6 ноября 2026

## Первичная проверка приложения

- Python 3.12.14, Django 5.2.19, DRF 3.16.1.
- Pytest / SQLite: **51 passed**.
- Pytest / PostgreSQL 17.11: **51 passed**; отдельная чистая тестовая БД.
- `migrate --noinput`: все миграции применены к PostgreSQL и SQLite.
- `makemigrations --check --dry-run`: No changes detected.
- `manage.py check`: No issues.
- Ruff (F/I): passed; Python-файлы отформатированы.
- `npm ci` / Tailwind production CSS: сборка прошла; JS локальные.
- Chromium desktop 1440×1000 и mobile 390×844: вход паролем, подключение TOTP, главная с данными БД, HTMX-поиск, Alpine-меню — passed, без JS-ошибок.
- Dockerfile: образ собран с Python 3.12, зависимости установлены, collectstatic выполнен под непривилегированным UID 10001.
- `docker compose config --quiet`: passed; PostgreSQL и Redis запущены, healthcheck healthy, миграции и bootstrap через Compose выполнены.
- Redis cache + реальный Celery Worker + result backend: постановка и выполнение `notify_overdue` подтверждены.
- Celery Beat: процесс запущен с PersistentScheduler и Redis broker.
- `pg_dump -Fc` → `pg_restore --single-transaction` в отдельную PostgreSQL БД: успешно, восстановлено 19 записей django_migrations. На момент первичной проверки media volume и полный shell-сценарий восстановления не проверялись; ниже — результат дополнительного CI.

## Дополнительная проверка полного Docker Compose — PASS

Проверенный код: [`1b2e454`](https://github.com/daudraul-ui/crm-ovi/commit/1b2e4547e395101d311f7a60d3391eacecfa279a).

- [Compose integration #4 — success](https://github.com/daudraul-ui/crm-ovi/actions/runs/42185974362).
- [Tests #10 — success](https://github.com/daudraul-ui/crm-ovi/actions/runs/42185974377): PostgreSQL, миграции и 51 тест.
- [Диагностические логи Compose](https://github.com/daudraul-ui/crm-ovi/actions/runs/42185974362/artifacts/13487097810), retention 7 дней; после истечения срока остаётся журнал workflow.

Managed-среда по-прежнему использует Docker VFS; при начале дополнительной проверки свободно около 715 МБ. Повторный полный запуск в ней не предпринимался. GitHub-hosted Ubuntu runner имеет driver `overlayfs`, около 88 ГБ свободного диска в проверенном прогоне, Docker Compose v5.1.3. Это позволило выполнить полную проверку без очистки Docker. Первый прогон нового workflow выявил ошибку пути импорта в CI probe; она исправлена до указанного успешного прогона.

| Проверка | Фактический результат |
|---|---|
| Сборка основного Dockerfile | PASS; один образ для web/worker/beat, collectstatic при сборке |
| Одновременная работа шести сервисов | PASS; web/Gunicorn, PostgreSQL, Redis, Worker, Beat, Caddy; также после восстановления |
| Healthchecks | PASS; web, PostgreSQL и Redis имеют статус healthy. Worker/Beat проверены выполнением задач, Caddy — HTTP-запросами; отдельных healthchecks у них нет |
| Миграции | PASS; migrate на новом source volume, makemigrations --check, отсутствие неприменённых миграций в восстановленной БД |
| HTTP-прокси Caddy → Gunicorn | PASS; /health/ возвращает 200 и `ok`, /login/ — 200; главная доступна с подтверждённой 2FA-сессией |
| Статические файлы | PASS; DEBUG=0, app.css/htmx.min.js/alpine.min.js через Caddy/WhiteNoise побайтно совпадают с исходниками (SHA-256) |
| Защищённые файлы | PASS; /media/, /private_media/, /.env, /.git/config возвращают 404; анонимному запросу не отдаётся документ, с разрешённой сессией — attachment с ожидаемым SHA-256 |
| Redis cache | PASS; запись/чтение через Django cache |
| Worker и result backend | PASS; notify_overdue.delay() выполнена реальным Worker, результат получен из Redis, уведомление записано в PostgreSQL |
| Beat → Redis → Worker → PostgreSQL | PASS; после создания второй просроченной задачи без ручной постановки Beat вызвал обработку и появилось второе уведомление |
| Backup PostgreSQL + media | PASS; запись остановлена (web/worker/beat), выполнен scripts/backup.sh; оба архива непустые |
| Restore PostgreSQL + media | PASS; scripts/restore.sh в другом новом Compose-проекте с отдельными volumes; исходная БД и исходный media volume не перезаписывались |
| Проверка восстановленных данных | PASS; пользователь, роль, TOTP-устройство, клиент, проект, связи, две задачи и уведомления присутствуют; миграции применены |
| Проверка восстановленного media volume | PASS; полный список двух файлов (документ и вложенный бинарный файл) и SHA-256 каждого совпадают с source; документ скачан через Caddy после restore |
| Сохранность исходного тестового проекта | PASS; source повторно запущен, проверены данные, файлы, healthchecks и прокси |
| Защита существующей резервной копии | PASS локально; backup.sh отказывается писать в уже существующий каталог, контрольный файл не изменён |

### Изоляция и отличия CI от production

Используется основной compose.yaml, без подмены Caddy/Gunicorn/PostgreSQL/Redis фейками. Дополнительный compose.ci.yaml ограничивает concurrency Worker до 1 и сокращает интервал Beat с 300 до 5 секунд. Приложение работает при DEBUG=0; Caddy слушает локальный HTTP на loopback-портах 18080/18081. Новые source/restore проекты и volumes имеют уникальные имена по workflow run ID и attempt. Для HTTP-проверки защищённых страниц сессия с подтверждённым TOTP создаётся проверочным скриптом; полный интерактивный вход паролем/TOTP через HTTP-прокси здесь не повторялся (проверен ранее в Chromium).

В managed-среде существующие данные не изменялись. В CI не запускались prune, очистка Docker или удаление томов. pg_restore --clean применяется только к новой изолированной целевой БД; source не изменяется. Удаляются автоматически только временные one-off контейнеры команд через --rm; данные volumes сохраняются до штатного завершения временного runner. Artifacts содержат только логи, без runtime.env, backup-архивов и session cookies.

### Невыполненные проверки

- Публичный HTTPS/TLS, DNS, ACME-выпуск и продление сертификата, доверие цепочке и HTTP→HTTPS redirect на VPS: **не проверены**. VPS/домен для этой задачи не предоставлены. CI доказывает работу HTTP-прокси, а не публичного TLS.
- Вход с Secure cookies и CSRF через публичный HTTPS-домен: **не проверен**. Для приёмки VPS нужно проверить HTTPS /health/, редирект с HTTP, пароль/TOTP, CSRF и защищённое скачивание без отключения проверки сертификата.
- Полный Compose в исходной managed VFS-среде: **не проверен повторно**, ограничение диска сохраняется; подтверждён запуск на GitHub runner.
- Нагрузочные тесты, отказоустойчивость, восстановление после повреждения архивов, backup при продолжающейся записи, restore поверх непустого production media volume, offsite-шифрование и расписание backup: **не проверены**. Restore в этом сценарии выполнялся только в новые volumes.

Бизнес-процессы и внешние интеграции не добавлялись. PR #1 не слит в main.


## TASK 001.1 — исправления первого тестирования

На локальном коде выполнены:

- Pytest / SQLite: **72 passed**, включая 21 дополнительный сценарий.
- Миграция 0001→0004 с существующими регионами двух сетей, дублями названий, магазинами, форматом, клиентом и проектом: PASS. Все исходные строки регионов сохранены, магазины переназначены на общий регион с аудитом исходного ID; связи проектов и формат магазина сохранены.
- Unicode-регистр, пробелы и ограничение уникальности региона в БД: PASS.
- Создание сотрудника, несколько ролей, менеджер клиента, запрет повышения финансовых прав администратором, TOTP и серверные запреты: PASS.
- Связи сеть↔юрлица, необязательное юрлицо/формат магазина, запрет чужих юрлиц/форматов, защита используемой связи: PASS.
- Chromium / desktop 1440×1000 и mobile 390×844: PASS — сотрудники/роли, менеджер, карточка сети, обновление зависимых полей, глобальный регион, создание формата без потери ввода и четыре ссылки на главной. Ошибок JavaScript и горизонтального переполнения viewport не обнаружено.
- Django check / makemigrations --check: PASS; Tailwind CSS собран, JS syntax и Ruff F/I проверены.

Новые проверки включены в pytest и Compose integration, результат GitHub Actions будет записан после завершения прогона. Публичный TLS на VPS и остальные ранее отмеченные невыполненные инфраструктурные проверки не повторялись. Новые бизнес-процессы не добавлены.
