# MAX: бот жалоб и предложений

Бот в мессенджере MAX, SQLite, веб-дашборд заявок (история, заметки, закрытие, ответ пользователю).

## Быстрый старт

1. Скопируйте `cp .env.example .env`, укажите `MAX_BOT_TOKEN` и переменные доступа к монитору (см. комментарии в `.env.example`).
2. Запуск: `docker compose up -d --build` или локально `pip install -r requirements.txt`, затем `python bot.py` с тем же `.env`.
3. В Docker монитор наружу открыт по **HTTPS на порту 443** (см. ниже). Проверка: `GET https://<IP>/health` (без авторизации; браузер спросит доверие к самоподписанному сертификату).

## HTTPS (Docker, доступ по IP:443)

Снаружи публикуется **nginx** с TLS; контейнер бота слушает HTTP только во внутренней сети на `15000`.

**На сервере (Linux), в каталоге репозитория** — подставьте свой публичный IP вместо `ВАШ_IP`:

```bash
chmod +x scripts/gen-selfsigned-ip-cert.sh
./scripts/gen-selfsigned-ip-cert.sh ВАШ_IP
docker compose up -d --build
```

Откройте в браузере `https://ВАШ_IP/` (единожды примите предупреждение о сертификате).

Порт **80** в compose тоже проброшен и редиректит на HTTPS. Если 80 занят, в `docker-compose.yml` у сервиса `nginx-https` уберите строку `"80:80"`.

Отладка без TLS: временно добавьте в `docker-compose.override.yml` проброс `"15000:15000"` у сервиса `complaint-suggestion-bot` и перезапустите compose.

## Продакшен

- Секреты только через окружение или secret manager; `.env` не коммитится.
- Для домена с доверенным сертификатом замените содержимое `certs/` на выпуск Let's Encrypt или корпоративный CA и при необходимости поправьте `server_name` в `nginx/https.conf`.
- Делайте резервные копии SQLite (том `cpz_bot_data`, путь задаётся `DATABASE_PATH`).

## Стек

- Python 3.12, `maxapi`, `aiohttp`
- Точка входа: `python -u bot.py` (см. `Dockerfile`)
