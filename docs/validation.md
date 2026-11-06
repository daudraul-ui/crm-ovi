# Проверка этапа A — 6 ноября 2026

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
- `pg_dump -Fc` → `pg_restore --single-transaction` в отдельную PostgreSQL БД: успешно, восстановлено 19 записей django_migrations. Media volume и полный shell-сценарий восстановления не проверялись.

## Ограничение окружения

Полный одновременный запуск Compose (web/worker/beat/Caddy) не подтверждён: Docker с драйвером VFS исчерпал диск окружения 8 ГБ при создании копий файловых систем контейнеров (`no space left on device`). Временные ресурсы удалены, дисковое пространство освобождено. Проверки приложения, PostgreSQL, Redis и Celery выполнены отдельно. End-to-end HTTPS через Caddy, полный сценарий shell-скриптов backup/restore с media volume и нагрузка не проверены. Полный Compose необходимо прогнать на целевом сервере с достаточным свободным диском перед эксплуатацией.

Внешние бизнес-процессы и интеграции не запускались и не реализованы.

## Дополнительная проверка Compose (в работе)

В текущей managed-среде подтверждены Docker VFS и около 715 МБ свободного диска; полный стек повторно локально не запускался, данные и Docker-ресурсы не очищались. Добавлен отдельный workflow `Compose integration` на GitHub-hosted Ubuntu runner и сценарий двух изолированных Compose-проектов. Статус до фактического прогона: **не подтверждено**.

План проверки: шесть сервисов одновременно, healthchecks, миграции, HTTP Caddy→Gunicorn, WhiteNoise/static SHA-256 при DEBUG=0, закрытые URL и download, Redis cache, worker/result backend, реальная периодическая доставка Beat, backup PostgreSQL + полный media volume и restore в новые тома с проверкой исходных данных. TLS с публичным сертификатом и DNS на VPS остаётся отдельной невыполненной проверкой. Очистка Docker и удаление существующих томов не применяются.
