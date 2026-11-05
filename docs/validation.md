# Проверка этапа A — 6 ноября 2026

- Python 3.12.14, Django 5.2.19, DRF 3.16.1.
- Pytest / SQLite: **48 passed**.
- Pytest / PostgreSQL 17.11: **48 passed**; отдельная чистая тестовая БД.
- `migrate --noinput`: все миграции применены к PostgreSQL и SQLite.
- `makemigrations --check --dry-run`: No changes detected.
- `manage.py check`: No issues.
- Ruff (F/I): passed; Python-файлы отформатированы.
- `npm ci` / Tailwind production CSS: сборка прошла; JS локальные.
- Chromium desktop 1440×1000 и mobile 390×844: вход паролем, подключение TOTP, главная с данными БД, HTMX-поиск, Alpine-меню — passed, без JS-ошибок.
- Dockerfile: образ собран с Python 3.12, зависимости установлены, collectstatic выполнен под непривилегированным UID 10001.
- `docker compose config --quiet`: passed; PostgreSQL и Redis запущены, healthcheck healthy, миграции и bootstrap через Compose выполнены.
- Redis cache + реальный Celery Worker + result backend: постановка и выполнение `notify_overdue` подтверждены.

## Ограничение окружения

Полный одновременный запуск Compose (web/worker/beat/Caddy) не подтверждён: Docker с драйвером VFS исчерпал диск окружения 8 ГБ при создании копий файловых систем контейнеров (`no space left on device`). Временные ресурсы удалены, дисковое пространство освобождено. Проверки приложения, PostgreSQL, Redis и Celery выполнены отдельно. End-to-end HTTPS через Caddy, полный сценарий shell-скриптов backup/restore с media volume и нагрузка не проверены. Полный Compose необходимо прогнать на целевом сервере с достаточным свободным диском перед эксплуатацией.

Внешние бизнес-процессы и интеграции не запускались и не реализованы.
