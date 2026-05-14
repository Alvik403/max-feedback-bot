# MAX: бот жалоб и предложений

Бот в мессенджере MAX, SQLite, веб-дашборд заявок (история, заметки, закрытие, ответ пользователю).

## Быстрый старт

1. Скопируйте `cp .env.example .env`, укажите `MAX_BOT_TOKEN` и переменные доступа к монитору (см. комментарии в `.env.example`).
2. Запуск: `docker compose up -d --build` или локально `pip install -r requirements.txt`, затем `python bot.py` с тем же `.env`.
3. В Docker монитор доступен по **HTTPS :443** (nginx) и параллельно по **HTTP :15000** напрямую к боту. Проверка: `GET https://<IP>/health` или `GET http://<IP>:15000/health`.

## HTTPS (Docker, доступ по IP:443)

Снаружи публикуется **nginx** с TLS на **443**; тот же монитор можно открыть без nginx по **`http://<IP>:15000/`** (удобно для отладки; в продакшене по возможности используйте только HTTPS).

**На сервере (Linux), в каталоге репозитория** — подставьте свой публичный IP вместо `ВАШ_IP`:

```bash
chmod +x scripts/gen-selfsigned-ip-cert.sh
./scripts/gen-selfsigned-ip-cert.sh ВАШ_IP
# или так же: ./scripts/gen-selfsigned-ip-cert.sh https://ВАШ_IP/
docker compose up -d --build
```

Если видите **`permission denied`** к `unix:///var/run/docker.sock` — добавьте пользователя в группу `docker` и перелогиньтесь (или один раз запустите **`sudo docker compose up -d --build`**):

```bash
sudo usermod -aG docker "$USER"
# выйти из SSH и зайти снова, затем снова без sudo:
docker compose up -d --build
```

Откройте в браузере `https://ВАШ_IP/` (единожды примите предупреждение о сертификате).

Порт **80** в compose тоже проброшен и редиректит на HTTPS. Если 80 занят, в `docker-compose.yml` у сервиса `nginx-https` уберите строку `"80:80"`.

## Продакшен

- Секреты только через окружение или secret manager; `.env` не коммитится.
- Для домена с доверенным сертификатом замените содержимое `certs/` на выпуск Let's Encrypt или корпоративный CA и при необходимости поправьте `server_name` в `nginx/https.conf`.
- Делайте резервные копии SQLite (том `cpz_bot_data`, путь задаётся `DATABASE_PATH`).

## Стек

- Python 3.12, `maxapi`, `aiohttp`
- Точка входа: `python -u bot.py` (см. `Dockerfile`)
