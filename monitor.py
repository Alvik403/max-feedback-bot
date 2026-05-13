"""Веб-дашборд заявок: темы, обновление без перезагрузки, заметки, закрытие, ответ пользователю."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional

from aiohttp import web
from maxapi.enums.attachment import AttachmentType
from maxapi.types.attachments.attachment import Attachment, ButtonsPayload
from maxapi.types.attachments.buttons.callback_button import CallbackButton

if TYPE_CHECKING:
    from maxapi import Bot

    from storage import Storage

log = logging.getLogger("monitor")

_DEBUG_CAP = 2500
_MSK_TZ = ZoneInfo("Europe/Moscow")


def ticket_followup_keyboard(ticket_id: int) -> Attachment:
    return Attachment(
        type=AttachmentType.INLINE_KEYBOARD,
        payload=ButtonsPayload(
            buttons=[
                [CallbackButton(text="Ответить", payload=f"ticket:reply:{ticket_id}")],
            ]
        ),
    )


def _now_iso() -> str:
    return datetime.now(_MSK_TZ).strftime("%Y-%m-%d %H:%M:%S МСК")


class Monitor:
    """Короткий лог для отладки (в дашборд не выводится)."""

    def __init__(self) -> None:
        self._debug: Deque[str] = deque(maxlen=_DEBUG_CAP)
        self._lock = threading.Lock()

    def add_debug(self, message: str) -> None:
        line = f"[{_now_iso()}] {message}"
        with self._lock:
            self._debug.append(line)

    def add_event(self, *args: Any, **kwargs: Any) -> None:
        """Совместимость с bot.py; события в память больше не копим."""
        return

    def add_submission(self, *args: Any, **kwargs: Any) -> None:
        """Совместимость с bot.py."""
        return

    def _snapshot_debug(self) -> list[str]:
        with self._lock:
            return list(self._debug)

    def build_app(
        self,
        *,
        bot: Optional[Bot] = None,
        storage: Optional[Storage] = None,
    ) -> web.Application:
        # Полный ключ: MONITOR_SECRET_FULL или устаревший MONITOR_SECRET.
        secret_full_eff = (
            os.environ.get("MONITOR_SECRET_FULL") or os.environ.get("MONITOR_SECRET") or ""
        ).strip()
        # Режим «без автора» для анонимных (ФИО / отдел / модуль скрыты в выдаче API).
        secret_redact_eff = (os.environ.get("MONITOR_SECRET_REDACT") or "").strip()
        has_monitor_auth = bool(secret_full_eff or secret_redact_eff)

        async def index(_request: web.Request) -> web.StreamResponse:
            return web.Response(
                text=_DASHBOARD_HTML, content_type="text/html", charset="utf-8"
            )

        def _auth_mode(request: web.Request) -> Optional[str]:
            if not has_monitor_auth:
                return None
            supplied = request.headers.get("X-Monitor-Key", "").strip()
            if not supplied:
                return None
            if secret_full_eff and hmac.compare_digest(supplied, secret_full_eff):
                return "full"
            if secret_redact_eff and hmac.compare_digest(supplied, secret_redact_eff):
                return "redact"
            return None

        def _auth_ok(request: web.Request) -> bool:
            return _auth_mode(request) is not None

        def _auth_error() -> web.Response:
            if not has_monitor_auth:
                return web.json_response(
                    {"error": "monitor password is not configured"}, status=503
                )
            return web.json_response({"error": "unauthorized"}, status=401)

        def _ticket_json(r: dict[str, Any], mode: str) -> dict[str, Any]:
            anon = bool(r["anonymous"])
            show_profile = mode == "full" or not anon
            fn = (r.get("full_name") or "") if show_profile else ""
            dept = (r.get("department") or "") if show_profile else ""
            mod = (r.get("module") or "") if show_profile else ""
            return {
                "id": r["id"],
                "user_id": r["user_id"],
                "chat_id": r["chat_id"],
                "kind": r["kind"],
                "text": r["text"],
                "anonymous": anon,
                "created_at": r["created_at"],
                "admin_note": r.get("admin_note") or "",
                "status": r.get("status") or "open",
                "full_name": fn,
                "department": dept,
                "module": mod,
                "thread_last_activity": (r.get("thread_last_activity") or r.get("created_at") or "")
                or "",
            }

        async def api_tickets(request: web.Request) -> web.Response:
            mode = _auth_mode(request)
            if mode is None:
                return _auth_error()
            if storage is None:
                return web.json_response({"error": "storage unavailable"}, status=503)
            rows = await asyncio.to_thread(storage.list_submissions_dashboard, 300)
            out = []
            for r in rows:
                out.append(_ticket_json(r, mode))
            return web.json_response({"tickets": out})

        async def api_ticket_thread(request: web.Request) -> web.Response:
            if not _auth_ok(request):
                return _auth_error()
            if storage is None:
                return web.json_response({"error": "storage unavailable"}, status=503)
            try:
                tid = int(request.match_info["id"])
            except (KeyError, ValueError):
                return web.json_response({"error": "bad id"}, status=400)
            msgs = await asyncio.to_thread(storage.get_submission_thread, tid)
            if not msgs:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response({"messages": msgs})

        async def api_ticket_patch(request: web.Request) -> web.Response:
            if not _auth_ok(request):
                return _auth_error()
            if storage is None:
                return web.json_response({"error": "storage unavailable"}, status=503)
            try:
                tid = int(request.match_info["id"])
            except (KeyError, ValueError):
                return web.json_response({"error": "bad id"}, status=400)
            try:
                body = await request.json()
            except json.JSONDecodeError:
                return web.json_response({"error": "json"}, status=400)
            note = body.get("admin_note")
            status_v = body.get("status")
            if note is None and status_v is None:
                return web.json_response({"error": "empty"}, status=400)
            if isinstance(note, str) and len(note) > 4000:
                return web.json_response({"error": "note too long"}, status=400)
            try:
                ok = await asyncio.to_thread(
                    storage.update_submission_ticket,
                    tid,
                    admin_note=note if isinstance(note, str) else None,
                    status=status_v if isinstance(status_v, str) else None,
                )
            except ValueError:
                return web.json_response({"error": "invalid status"}, status=400)
            if not ok:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response({"ok": True})

        async def api_ticket_reply(request: web.Request) -> web.Response:
            if not _auth_ok(request):
                return _auth_error()
            if bot is None or storage is None:
                return web.json_response({"error": "bot or storage unavailable"}, status=503)
            try:
                tid = int(request.match_info["id"])
            except (KeyError, ValueError):
                return web.json_response({"error": "bad id"}, status=400)
            try:
                body = await request.json()
            except json.JSONDecodeError:
                return web.json_response({"error": "json"}, status=400)
            text = (body.get("text") or "").strip()
            if not text:
                return web.json_response({"error": "empty text"}, status=400)
            if len(text) > 4000:
                return web.json_response({"error": "text too long"}, status=400)
            row = await asyncio.to_thread(storage.get_submission, tid)
            if not row:
                return web.json_response({"error": "not found"}, status=404)
            cid = row.get("chat_id")
            uid = row.get("user_id")
            if cid is None and uid is not None:
                urow = await asyncio.to_thread(storage.get_user, uid)
                if urow and urow.get("chat_id") is not None:
                    cid = urow["chat_id"]
            if cid is not None:
                kw_send: dict[str, Any] = {"chat_id": cid}
            elif uid is not None:
                kw_send = {"user_id": uid}
            else:
                return web.json_response({"error": "no delivery target"}, status=400)
            msg = f"Ответ по вашей заявке №{tid}:\n\n{text}"
            try:
                sent = await bot.send_message(
                    **kw_send,
                    text=msg,
                    attachments=[ticket_followup_keyboard(tid)],
                )
            except Exception as exc:
                log.warning("reply send failed: %s", exc)
                return web.json_response({"error": str(exc)}, status=502)
            if sent is not None and type(sent).__name__ == "Error":
                return web.json_response({"error": "api error"}, status=502)
            await asyncio.to_thread(storage.add_submission_reply, tid, text)
            return web.json_response({"ok": True})

        async def health(_request: web.Request) -> web.Response:
            return web.json_response({"status": "ok"})

        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_get("/healthz", health)
        app.router.add_get("/", index)
        app.router.add_get("/api/tickets", api_tickets)
        app.router.add_get("/api/tickets/{id}/thread", api_ticket_thread)
        app.router.add_patch("/api/tickets/{id}", api_ticket_patch)
        app.router.add_post("/api/tickets/{id}/reply", api_ticket_reply)
        return app


class MonitorLogHandler(logging.Handler):
    def __init__(self, monitor: Monitor) -> None:
        super().__init__()
        self.monitor = monitor

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.monitor.add_debug(msg)
        except Exception:
            pass


async def start_monitor_http(
    monitor: Monitor,
    host: str,
    port: int,
    *,
    bot: Optional[Bot] = None,
    storage: Optional[Storage] = None,
) -> web.AppRunner:
    app = monitor.build_app(bot=bot, storage=storage)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("веб-дашборд: http://%s:%s/", host, port)
    full_k = (
        os.environ.get("MONITOR_SECRET_FULL") or os.environ.get("MONITOR_SECRET") or ""
    ).strip()
    red_k = (os.environ.get("MONITOR_SECRET_REDACT") or "").strip()
    auth_state = (
        "пароли монитора настроены"
        if (full_k or red_k)
        else "пароли монитора не настроены"
    )
    monitor.add_debug(f"дашборд http://{host}:{port}/ ({auth_state})")
    return runner


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Заявки</title>
<style>
:root[data-theme="dark"] {
  --bg: #0b0d11;
  --surface: #131820;
  --border: #283041;
  --text: #e8eaef;
  --muted: #8b93a5;
  --accent: #5b8def;
  --accent-hi: #7aa3f5;
  --ok: #3ecf8e;
  --warn: #e7a23d;
  --danger: #f87171;
  --complaint: #e7a23d;
  --proposal: #5b8def;
  --scrollbar-track: #0f1218;
  --scrollbar-thumb: #3d475c;
  --scrollbar-thumb-hover: #5a657d;
}
:root[data-theme="light"] {
  --bg: #eef0f4;
  --surface: #ffffff;
  --border: #dde1e8;
  --text: #1a1d23;
  --muted: #5c6370;
  --accent: #2563eb;
  --accent-hi: #1d4ed8;
  --ok: #059669;
  --warn: #d97706;
  --danger: #dc2626;
  --complaint: #d97706;
  --proposal: #2563eb;
  --scrollbar-track: var(--bg);
  --scrollbar-thumb: #98a3b8;
  --scrollbar-thumb-hover: #7e8a9f;
}
* { box-sizing: border-box; }
body {
  margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; color: var(--text);
  font-size: 15px; line-height: 1.45; min-height: 100vh;
  background:
    radial-gradient(circle at 10% -10%, color-mix(in srgb, var(--accent) 24%, transparent), transparent 34rem),
    radial-gradient(circle at 92% 0%, color-mix(in srgb, var(--complaint) 16%, transparent), transparent 30rem),
    var(--bg);
}
.scroll-y,
textarea.field-thread {
  scrollbar-gutter: stable;
  scrollbar-width: auto;
  scrollbar-color: var(--scrollbar-thumb) var(--scrollbar-track);
}
.scroll-y::-webkit-scrollbar,
textarea.field-thread::-webkit-scrollbar { width: 12px; height: 12px; }
.scroll-y::-webkit-scrollbar-track,
textarea.field-thread::-webkit-scrollbar-track {
  background: var(--scrollbar-track);
  border-radius: 8px;
  margin: 4px 0;
}
.scroll-y::-webkit-scrollbar-thumb,
textarea.field-thread::-webkit-scrollbar-thumb {
  background: var(--scrollbar-thumb);
  border-radius: 8px;
  border: 3px solid var(--scrollbar-track);
}
.scroll-y::-webkit-scrollbar-thumb:hover,
textarea.field-thread::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-thumb-hover); }
.scroll-y::-webkit-scrollbar-corner,
textarea.field-thread::-webkit-scrollbar-corner { background: var(--scrollbar-track); }
.top {
  width: 100%; max-width: min(1560px, calc(100vw - 24px)); margin: 0 auto;
  padding: clamp(14px, 2vw, 24px) clamp(12px, 2vw, 20px);
  display: flex; align-items: center; justify-content: space-between; gap: 1rem;
}
.brand { min-width: 0; display: flex; align-items: center; }
.top h1 { margin: 0; font-size: clamp(1.2rem, 2vw, 1.65rem); font-weight: 700; letter-spacing: -0.03em; line-height: 1.15; }
.top-actions { display: flex; align-items: center; gap: 0.55rem; flex-shrink: 0; }
.btn-theme {
  width: 46px; height: 46px; border-radius: 50%;
  border: 1px solid color-mix(in srgb, var(--accent) 68%, var(--border));
  background: linear-gradient(
    160deg,
    color-mix(in srgb, var(--accent-hi) 26%, var(--surface)),
    color-mix(in srgb, var(--accent) 14%, var(--surface))
  );
  cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 1.4rem; line-height: 1;
  flex-shrink: 0;
  box-shadow:
    inset 0 1px 0 color-mix(in srgb, var(--text) 10%, transparent),
    0 0 0 1px color-mix(in srgb, var(--accent) 28%, transparent),
    0 4px 22px color-mix(in srgb, var(--accent) 38%, transparent);
  transition: border-color 0.15s, transform 0.12s, box-shadow 0.15s, background 0.15s;
}
.btn-theme:hover {
  border-color: var(--accent-hi);
  background: linear-gradient(
    160deg,
    color-mix(in srgb, var(--accent-hi) 38%, var(--surface)),
    color-mix(in srgb, var(--accent) 22%, var(--surface))
  );
  box-shadow:
    inset 0 1px 0 color-mix(in srgb, var(--text) 12%, transparent),
    0 0 0 1px color-mix(in srgb, var(--accent-hi) 42%, transparent),
    0 6px 28px color-mix(in srgb, var(--accent) 52%, transparent);
  transform: scale(1.05);
}
.auth-screen {
  position: fixed; inset: 0; z-index: 20; display: grid; place-items: center; padding: 1rem;
  background:
    radial-gradient(circle at 20% 20%, color-mix(in srgb, var(--accent) 28%, transparent), transparent 28rem),
    color-mix(in srgb, var(--bg) 92%, #000);
  backdrop-filter: blur(14px);
}
.auth-screen[hidden] { display: none !important; }
.auth-card {
  width: min(100%, 420px); padding: clamp(1.1rem, 4vw, 1.55rem); border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
  border-radius: 22px; background: color-mix(in srgb, var(--surface) 94%, transparent);
  box-shadow: 0 24px 70px rgba(0,0,0,0.28), 0 0 0 1px color-mix(in srgb, var(--text) 5%, transparent);
}
.auth-card h2 { margin: 0 0 0.4rem; font-size: 1.25rem; letter-spacing: -0.03em; }
.auth-card p { margin: 0 0 1rem; color: var(--muted); }
.auth-card input {
  width: 100%; padding: 0.76rem 0.85rem; border-radius: 12px; border: 1px solid var(--border);
  background: var(--bg); color: var(--text); font: inherit; outline: none;
}
.auth-card input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
.auth-card .btn { width: 100%; justify-content: center; margin-top: 0.8rem; }
.auth-hint { min-height: 1.25em; margin-top: 0.65rem; color: var(--danger); font-size: 0.84rem; }
.app-shell[aria-hidden="true"] { filter: blur(3px); pointer-events: none; user-select: none; }
.layout {
  width: 100%;
  max-width: min(1560px, calc(100vw - 24px));
  margin: 0 auto;
  padding: 0 clamp(12px, 2vw, 20px) clamp(1.4rem, 4vw, 2.4rem);
  display: grid;
  grid-template-columns: minmax(0, 1.38fr) minmax(0, 1fr);
  gap: clamp(14px, 1.8vw, 22px);
  align-items: stretch;
}
@media (max-width: 1080px) { .layout { grid-template-columns: 1fr; } }
.layout > .panel { min-width: 0; }
.layout > .panel:last-child {
  display: flex;
  flex-direction: column;
  min-height: 400px;
  max-height: min(88vh, 820px);
}
.panel {
  background: color-mix(in srgb, var(--surface) 94%, transparent); border: 1px solid color-mix(in srgb, var(--border) 84%, var(--accent));
  border-radius: 18px; overflow: hidden; min-height: 360px;
  box-shadow: 0 10px 32px rgba(0,0,0,0.08), inset 0 1px 0 color-mix(in srgb, var(--text) 5%, transparent);
}
[data-theme="dark"] .panel { box-shadow: 0 14px 42px rgba(0,0,0,0.38), inset 0 1px 0 color-mix(in srgb, var(--text) 5%, transparent); }
.split { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); min-height: 400px; min-width: 0; }
@media (max-width: 720px) { .split { grid-template-columns: 1fr; } }
.lane { display: flex; flex-direction: column; min-height: 0; min-width: 0; border-right: 1px solid var(--border); }
.lane:last-child { border-right: none; }
@media (max-width: 720px) {
  .lane { border-right: none !important; border-bottom: none !important; }
}
.lane-h {
  padding: 0.78rem 0.95rem; font-weight: 650; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em;
  display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;
}
.lane-h-label { display: inline-flex; align-items: center; gap: 0.38rem; min-width: 0; flex-wrap: wrap; }
.unread-dot {
  flex-shrink: 0;
  width: 0.5rem;
  height: 0.5rem;
  border-radius: 50%;
  background: color-mix(in srgb, var(--accent-hi) 94%, transparent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 42%, transparent);
}
.unread-dot[hidden] { display: none !important; }
.lane-complaint .lane-h {
  color: var(--complaint);
  background: linear-gradient(135deg, color-mix(in srgb, var(--complaint) 18%, transparent), transparent 60%);
  border-bottom: 3px solid color-mix(in srgb, var(--complaint) 65%, var(--border));
}
.lane-proposal .lane-h {
  color: var(--proposal);
  background: linear-gradient(135deg, color-mix(in srgb, var(--proposal) 16%, transparent), transparent 60%);
  border-bottom: 3px solid color-mix(in srgb, var(--proposal) 55%, var(--border));
}
.cnt { font-variant-numeric: tabular-nums; opacity: 0.9; font-weight: 600; background: color-mix(in srgb, var(--text) 8%, transparent); padding: 0.12rem 0.45rem; border-radius: 6px; }
.lane-items { flex: 1; overflow-y: auto; overflow-x: hidden; max-height: min(72vh, 620px); padding: 0 4px 0.35rem 0; min-height: 200px; }
.card {
  margin: 0.5rem 0.65rem; padding: 0.78rem 0.86rem; border-radius: 14px; border: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg) 90%, var(--surface)); cursor: pointer;
  transition: transform 0.12s, box-shadow 0.12s, border-color 0.12s, background 0.12s;
}
.card:hover { transform: translateY(-2px); border-color: color-mix(in srgb, var(--accent) 42%, var(--border)); box-shadow: 0 8px 22px rgba(0,0,0,0.1); }
[data-theme="dark"] .card:hover { box-shadow: 0 8px 28px rgba(0,0,0,0.45); }
.card.sel { border-color: var(--accent); box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 35%, transparent); }
.card-unread { position: relative; }
.card-unread:not(.sel)::after {
  content: '';
  position: absolute;
  top: 10px;
  right: 10px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--accent-hi);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 40%, transparent);
  pointer-events: none;
}
.card.closed { opacity: 0.68; }
.card-top { display: flex; align-items: center; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.35rem; }
.chip { font-size: 0.7rem; font-weight: 650; padding: 0.14rem 0.42rem; border-radius: 6px; }
.chip-id { background: color-mix(in srgb, var(--accent) 22%, transparent); color: var(--accent-hi); }
.chip-off { background: color-mix(in srgb, var(--muted) 22%, transparent); color: var(--muted); }
.sub { font-size: 0.75rem; color: var(--muted); margin-top: 0.35rem; }
.preview { font-size: 0.86rem; color: var(--text); opacity: 0.92; margin-top: 0.25rem; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.35; }
.panel-h-detail {
  flex-shrink: 0;
  padding: 0.7rem 1rem;
  border-bottom: 1px solid var(--border);
  font-size: 0.8rem;
  color: var(--muted);
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 0.55rem;
  flex-wrap: wrap;
}
.panel-h-detail-title { flex: 1 1 auto; min-width: 6rem; font-size: inherit; color: inherit; font-weight: inherit; }
.btn-mobile-back {
  display: none !important;
  flex-shrink: 0;
  padding: 0.38rem 0.72rem !important;
  font-size: 0.82rem !important;
  margin-right: auto;
}
.detail {
  flex: 1;
  min-height: 0;
  padding: 1rem;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.detail-head {
  flex-shrink: 0;
  margin-bottom: 0.35rem;
  padding-bottom: 1rem;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
}
.detail-head-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.65rem;
  margin-bottom: 0.55rem;
}
.detail-head-top h2 {
  margin: 0;
  flex: 1;
  min-width: 0;
  font-size: 1.12rem;
  font-weight: 650;
  letter-spacing: -0.025em;
  line-height: 1.28;
  color: var(--text);
}
.detail-close-btn {
  flex-shrink: 0;
  margin-top: 0.04rem;
  padding: 0.36rem 0.78rem !important;
  font-size: 0.82rem !important;
  white-space: nowrap;
}
.detail-visibility-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.45rem;
}
.badge-visibility {
  display: inline-flex;
  align-items: center;
  font-size: 0.72rem;
  font-weight: 650;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  padding: 0.26rem 0.65rem;
  border-radius: 999px;
  border: 1px solid transparent;
  line-height: 1.2;
}
.badge-visibility.is-anon {
  color: color-mix(in srgb, var(--text) 72%, var(--muted));
  background: color-mix(in srgb, var(--muted) 13%, transparent);
  border-color: color-mix(in srgb, var(--border) 78%, transparent);
}
.badge-visibility.is-public {
  color: color-mix(in srgb, var(--ok) 18%, var(--text));
  background: color-mix(in srgb, var(--ok) 12%, transparent);
  border-color: color-mix(in srgb, var(--ok) 32%, var(--border));
}
.badge-status {
  display: inline-flex;
  align-items: center;
  font-size: 0.68rem;
  font-weight: 650;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding: 0.22rem 0.5rem;
  border-radius: 8px;
  color: var(--muted);
  background: color-mix(in srgb, var(--muted) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--border) 85%, transparent);
}
.badge-status[hidden] { display: none !important; }
.profile-strip {
  margin-top: 0.55rem;
  font-size: 0.875rem;
  font-weight: 500;
  line-height: 1.55;
  letter-spacing: 0.01em;
  color: color-mix(in srgb, var(--text) 92%, var(--muted));
  padding: 0.52rem 0.72rem;
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface) 55%, var(--bg));
  border: 1px solid color-mix(in srgb, var(--border) 82%, transparent);
  font-variant-numeric: tabular-nums;
}
.profile-strip[hidden] { display: none !important; }
.section-label {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: clamp(0.76rem, 1.95vw, 0.855rem);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.075em;
  color: color-mix(in srgb, var(--text) 72%, var(--muted));
  margin-bottom: 0.48rem;
  line-height: 1.3;
}
.section-label::before {
  content: '';
  flex-shrink: 0;
  width: 4px;
  height: 1.05em;
  border-radius: 3px;
  background: var(--accent);
  opacity: 0.95;
}
.section-label-history::before { background: color-mix(in srgb, var(--complaint) 78%, var(--accent)); }
.section-label-note::before { background: var(--accent); }
.meta { font-size: 0.8rem; color: var(--muted); margin-bottom: 0.65rem; }
.thread-wrap .section-label { flex-shrink: 0; margin-bottom: 0.52rem; }
.detail-body {
  flex: 1 1 auto;
  min-height: 0;
  width: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.detail-body > .detail-head { flex-shrink: 0; }
.detail-body > .thread-wrap {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  margin-bottom: 0.65rem;
  overflow: hidden;
}
.detail-body > .block,
.detail-body > .actions { flex-shrink: 0; }
.thread {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  margin-bottom: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 0.45rem 0.35rem;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg) 88%, var(--surface));
}
.bubble { max-width: 94%; padding: 0.55rem 0.72rem; border-radius: 14px; font-size: 0.88rem; white-space: pre-wrap; word-break: break-word; }
.bubble.user { align-self: flex-start; background: color-mix(in srgb, var(--muted) 16%, transparent); margin-right: auto; }
.bubble.admin { align-self: flex-end; background: color-mix(in srgb, var(--accent) 18%, transparent); border: 1px solid color-mix(in srgb, var(--accent) 38%, var(--border)); margin-left: auto; }
.bubble .who { font-size: 0.68rem; font-weight: 650; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 0.25rem; opacity: 0.85; }
.bubble .when { font-size: 0.68rem; color: var(--muted); margin-top: 0.4rem; }
.block { margin: 0.4rem 0; }
.thread-wrap + .block { margin-top: 0.08rem; }
.block .section-label { margin-bottom: 0.42rem; }
textarea.field-thread {
  width: 100%; min-height: 62px; padding: 0.45rem 0.72rem; border-radius: 12px; border: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg) 88%, var(--surface));
  color: var(--text); font-family: inherit; font-size: 0.88rem; line-height: 1.42;
  resize: vertical; outline: none;
}
#note.field-thread { min-height: 62px; }
#reply.field-thread { min-height: 54px; }
textarea.field-thread:focus { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent); }
.actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.45rem; align-items: center; width: 100%; box-sizing: border-box; }
.actions-reply-with-msg { gap: 0.45rem; }
.actions-reply-with-msg > .btn { flex-shrink: 0; }
.actions-reply-with-msg > .msg.ok,
.actions-reply-with-msg > .msg.err {
  margin-left: auto;
  align-self: center;
  flex: 0 1 auto;
  max-width: min(58%, 18rem);
  justify-content: flex-end;
  text-align: right;
}
.btn { border: 1px solid var(--border); background: var(--surface); color: var(--text); padding: 0.5rem 0.9rem; border-radius: 10px; cursor: pointer; font-size: 0.88rem; display: inline-flex; align-items: center; justify-content: center; gap: 0.35rem; }
.btn:hover { border-color: var(--accent); color: var(--accent); }
.btn-primary { background: var(--accent); color: #fff; border-color: transparent; }
.btn-primary:hover { background: var(--accent-hi); color: #fff; }
.btn-ghost { background: color-mix(in srgb, var(--surface) 74%, transparent); }
.msg {
  display: none;
  margin: 0;
  min-height: 0;
  max-width: 100%;
  padding: 0.32rem 0.62rem;
  border-radius: 9px;
  font-size: 0.78rem;
  line-height: 1.25;
  border: 1px solid transparent;
  background: color-mix(in srgb, var(--surface) 90%, var(--bg));
  box-shadow: 0 2px 10px rgba(0,0,0,0.12);
  word-break: break-word;
}
.msg.err {
  display: inline-flex;
  align-items: center;
  color: color-mix(in srgb, var(--danger) 80%, var(--text));
  border-color: color-mix(in srgb, var(--danger) 45%, var(--border));
}
.msg.ok {
  display: inline-flex;
  align-items: center;
  color: color-mix(in srgb, var(--ok) 78%, var(--text));
  border-color: color-mix(in srgb, var(--ok) 40%, var(--border));
}
.empty { padding: 2rem 1rem; text-align: center; color: var(--muted); font-size: 0.92rem; }
.detail > .empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 12rem;
}
@media (max-width: 720px) {
  .top {
    flex-direction: row;
    flex-wrap: nowrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }
  .brand { flex: 1 1 auto; min-width: 0; }
  .top h1 {
    margin: 0;
    padding: 0;
    font-size: clamp(1.05rem, 4.2vw, 1.4rem);
    line-height: 1.05;
    display: inline-block;
    vertical-align: middle;
  }
  .top-actions {
    flex-shrink: 0;
    width: auto;
    justify-content: flex-end;
    align-items: center;
    gap: 0.5rem;
    margin-left: auto;
    padding-top: 0;
  }
  .layout { max-width: 100%; padding-inline: 10px; }
  .panel { border-radius: 16px; min-height: 0; }
  .split {
    grid-template-columns: 1fr;
    gap: clamp(10px, 3vw, 14px);
    min-height: 0;
  }
  .lane {
    border-radius: 14px !important;
    overflow: hidden;
    border: 1px solid var(--border) !important;
  }
  .lane-h {
    cursor: pointer;
    user-select: none;
    -webkit-tap-highlight-color: transparent;
    margin: 0;
    touch-action: manipulation;
  }
  .lane:not(.lane-expanded) .lane-items {
    display: none !important;
  }
  .lane.lane-expanded .lane-items {
    display: flex !important;
    flex-direction: column;
    flex: 1;
    max-height: min(56vh, 520px);
    min-height: 120px;
  }
  .layout:not(.mobile-detail) > .panel:last-child {
    display: none !important;
  }
  .layout.mobile-detail > .panel:first-child {
    display: none !important;
  }
  .layout.mobile-detail > .panel:last-child {
    display: flex !important;
    flex-direction: column;
    max-height: none !important;
    min-height: min(92vh, 900px);
  }
  .layout.mobile-detail > .panel:last-child .detail {
    flex: 1;
    min-height: 0;
    max-height: min(88vh, 900px);
  }
  .layout.mobile-detail .btn-mobile-back {
    display: inline-flex !important;
  }
  .detail-body > .thread-wrap {
    flex: 1 1 auto;
    min-height: 0;
    max-height: none;
    margin-bottom: 0.5rem;
  }
  .lane-items { max-height: none; }
  .detail { min-height: 0; }
  .actions .btn { flex: 1 1 auto; min-width: 0; }
  .actions-reply-with-msg .btn { flex: 0 1 auto; }
}
@media (max-width: 480px) {
  body { font-size: 14px; }
  .top { padding-block: clamp(12px, 3vw, 18px); }
  .top-actions { justify-content: flex-end; gap: 0.45rem; }
  .btn-theme { width: 42px; height: 42px; }
  .btn-ghost { padding-inline: 0.72rem; }
  .card { margin-inline: 0.45rem; }
  .detail { padding: 0.82rem; }
}
</style>
</head>
<body>
<div class="auth-screen" id="authScreen" hidden>
  <form class="auth-card" id="authForm">
    <h2>Вход в монитор</h2>
    <p>Укажите один из паролей: режим без данных автора у анонимных заявок или полный просмотр (ФИО, отдел, модуль).</p>
    <input type="password" id="authPassword" autocomplete="current-password" placeholder="Пароль" aria-label="Пароль"/>
    <button type="submit" class="btn btn-primary">Войти</button>
    <div class="auth-hint" id="authHint"></div>
  </form>
</div>
<div class="app-shell" id="appShell">
<div class="top">
  <div class="brand">
    <h1>Заявки</h1>
  </div>
  <div class="top-actions">
    <button type="button" class="btn btn-ghost" id="btnAuth" title="Выйти из монитора">Выход</button>
    <button type="button" class="btn-theme" id="btnTheme" title="Тема" aria-label="Сменить тему"><span id="themeIcon"></span></button>
  </div>
</div>
<div class="layout" id="mainLayout">
  <div class="panel">
    <div class="split" id="splitBoard">
      <section class="lane lane-complaint" aria-expanded="false">
        <div class="lane-h"><span class="lane-h-label">Обращения<span class="unread-dot" id="laneUnreadComplaint" hidden title="Есть непрочитанные сообщения"></span></span><span class="cnt" id="cntComplaint">0</span></div>
        <div class="lane-items scroll-y" id="listComplaint"></div>
      </section>
      <section class="lane lane-proposal" aria-expanded="false">
        <div class="lane-h"><span class="lane-h-label">Предложения<span class="unread-dot" id="laneUnreadProposal" hidden title="Есть непрочитанные сообщения"></span></span><span class="cnt" id="cntProposal">0</span></div>
        <div class="lane-items scroll-y" id="listProposal"></div>
      </section>
    </div>
  </div>
  <div class="panel">
    <div class="panel-h-detail">
      <button type="button" class="btn btn-ghost btn-mobile-back" id="btnMobileBack" aria-label="К списку заявок">← Списки</button>
      <span class="panel-h-detail-title">Карточка</span>
    </div>
    <div class="detail" id="detail">
      <div class="empty">Выберите заявку слева</div>
    </div>
  </div>
</div>
</div>
<script>
(function(){
  var POLL_MS = 12000;
  var tickets = [];
  var selectedId = null;
  var lastThreadJson = '';
  var lastThreadTicketId = null;
  var detailDelegationBound = false;
  var MOBILE_MQ = window.matchMedia('(max-width: 720px)');

  function isMobileUi(){
    return !!(MOBILE_MQ && MOBILE_MQ.matches);
  }

  function mainLayoutEl(){
    return document.getElementById('mainLayout');
  }

  function expandLaneForSelected(){
    if (!isMobileUi()) return;
    var c = document.querySelector('.lane-complaint');
    var p = document.querySelector('.lane-proposal');
    if (!c || !p) return;
    c.classList.remove('lane-expanded');
    p.classList.remove('lane-expanded');
    var t = findTicket(selectedId);
    if (!t) {
      c.removeAttribute('aria-expanded');
      p.removeAttribute('aria-expanded');
      return;
    }
    if (t.kind === 'proposal') {
      p.classList.add('lane-expanded');
      c.setAttribute('aria-expanded', 'false');
      p.setAttribute('aria-expanded', 'true');
    } else {
      c.classList.add('lane-expanded');
      c.setAttribute('aria-expanded', 'true');
      p.setAttribute('aria-expanded', 'false');
    }
  }

  function enterMobileDetail(){
    if (!isMobileUi()) return;
    var ly = mainLayoutEl();
    if (ly) ly.classList.add('mobile-detail');
    window.scrollTo(0, 0);
  }

  function exitMobileDetail(){
    var ly = mainLayoutEl();
    if (ly) ly.classList.remove('mobile-detail');
    expandLaneForSelected();
    window.scrollTo(0, 0);
  }

  function onMobileViewportChange(){
    if (!isMobileUi()) {
      var ly = mainLayoutEl();
      if (ly) ly.classList.remove('mobile-detail');
      document.querySelectorAll('#splitBoard .lane').forEach(function(el){
        el.classList.remove('lane-expanded');
        el.removeAttribute('aria-expanded');
      });
    }
  }

  try {
    if (MOBILE_MQ.addEventListener)
      MOBILE_MQ.addEventListener('change', onMobileViewportChange);
    else if (MOBILE_MQ.addListener)
      MOBILE_MQ.addListener(onMobileViewportChange);
  } catch (e) {}

  var root = document.documentElement;
  var detailMsgTimer = null;

  function setDetailMsg(type, text){
    var m = document.getElementById('msg');
    if (!m) return;
    if (detailMsgTimer) {
      clearTimeout(detailMsgTimer);
      detailMsgTimer = null;
    }
    var t = String(text || '').trim();
    if (!t) {
      m.className = 'msg';
      m.textContent = '';
      return;
    }
    m.className = 'msg ' + (type === 'err' ? 'err' : 'ok');
    m.textContent = t;
    detailMsgTimer = setTimeout(function(){
      var current = document.getElementById('msg');
      if (!current) return;
      current.className = 'msg';
      current.textContent = '';
      detailMsgTimer = null;
    }, type === 'err' ? 4500 : 2500);
  }

  function theme(){
    var t = localStorage.getItem('cpz_theme') || 'dark';
    root.setAttribute('data-theme', t === 'light' ? 'light' : 'dark');
    syncThemeIcon();
  }
  function syncThemeIcon(){
    var el = document.getElementById('themeIcon');
    if (!el) return;
    var dark = root.getAttribute('data-theme') !== 'light';
    el.textContent = dark ? '\u2600' : '\u263d';
  }
  function toggleTheme(){
    var cur = root.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    var next = cur === 'light' ? 'dark' : 'light';
    localStorage.setItem('cpz_theme', next);
    theme();
  }
  theme();
  document.getElementById('btnTheme').onclick = toggleTheme;

  document.getElementById('splitBoard').addEventListener('click', function(ev){
    if (!isMobileUi()) return;
    var h = ev.target.closest('.lane-h');
    if (!h || !document.getElementById('splitBoard').contains(h)) return;
    var lane = h.closest('.lane');
    if (!lane) return;
    var open = lane.classList.contains('lane-expanded');
    document.querySelectorAll('#splitBoard > .lane').forEach(function(el){
      el.classList.remove('lane-expanded');
    });
    if (!open) lane.classList.add('lane-expanded');
    document.querySelectorAll('#splitBoard > .lane').forEach(function(el){
      var on = el.classList.contains('lane-expanded');
      el.setAttribute('aria-expanded', on ? 'true' : 'false');
    });
  });

  document.getElementById('btnMobileBack').onclick = function(){
    exitMobileDetail();
  };

  function authEls(){
    return {
      screen: document.getElementById('authScreen'),
      shell: document.getElementById('appShell'),
      input: document.getElementById('authPassword'),
      hint: document.getElementById('authHint')
    };
  }
  function showAuth(message){
    var el = authEls();
    el.screen.hidden = false;
    el.shell.setAttribute('aria-hidden', 'true');
    el.hint.textContent = message || '';
    setTimeout(function(){ el.input.focus(); }, 0);
  }
  function hideAuth(){
    var el = authEls();
    el.screen.hidden = true;
    el.shell.removeAttribute('aria-hidden');
    el.hint.textContent = '';
  }
  function authMessage(status, fallback){
    if (status === 401) return 'Неверный пароль.';
    if (status === 503) return 'Не задан ни MONITOR_SECRET_FULL / MONITOR_SECRET, ни MONITOR_SECRET_REDACT на сервере.';
    return fallback || 'Не удалось войти.';
  }
  document.getElementById('authForm').onsubmit = function(ev){
    ev.preventDefault();
    var v = document.getElementById('authPassword').value.trim();
    if (!v) { showAuth('Введите пароль.'); return; }
    localStorage.setItem('cpz_monitor_key', v);
    hideAuth();
    fetchTickets();
  };
  document.getElementById('btnAuth').onclick = function(){
    localStorage.removeItem('cpz_monitor_key');
    localStorage.removeItem('cpz_seen_thread_activity');
    showAuth('Введите пароль заново.');
  };
  function esc(s){
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  /** Строки БД в UTC (... UTC); для показа — Europe/Moscow. Сравнения в коде по исходным UTC не трогаем. */
  function utcStampToMskDisplay(s){
    if (!s) return '';
    var t = String(s).trim().replace(/\s*UTC\s*$/i,'').trim();
    var parts = t.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$/);
    if (!parts) return String(s).trim();
    var ms = Date.UTC(+parts[1], +parts[2]-1, +parts[3], +parts[4], +parts[5], +parts[6]);
    try {
      var fmt = new Intl.DateTimeFormat('ru-RU', {
        timeZone: 'Europe/Moscow',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
      });
      return fmt.format(new Date(ms)).replace(',', '') + ' МСК';
    } catch (e) {
      return String(s).trim();
    }
  }

  function keyHeaders(){
    var h = { 'Content-Type': 'application/json' };
    var k = localStorage.getItem('cpz_monitor_key') || '';
    if (k) h['X-Monitor-Key'] = k;
    return h;
  }

  function handleAuthError(status){
    if (status !== 401 && status !== 503) return false;
    if (status === 401) localStorage.removeItem('cpz_monitor_key');
    showAuth(authMessage(status));
    return true;
  }

  var SEEN_KEY = 'cpz_seen_thread_activity';

  function getSeenMap(){
    try {
      var j = JSON.parse(localStorage.getItem(SEEN_KEY) || '{}');
      return j && typeof j === 'object' ? j : {};
    } catch (e) {
      return {};
    }
  }

  function saveSeenMap(m){
    try {
      localStorage.setItem(SEEN_KEY, JSON.stringify(m));
    } catch (e) {}
  }

  function threadActivityFromTicket(t){
    return String((t && (t.thread_last_activity || t.created_at)) || '').trim();
  }

  function ticketHasUnread(t){
    var act = threadActivityFromTicket(t);
    if (!act) return false;
    var seen = String((getSeenMap()[String(t.id)]) || '').trim();
    return act > seen;
  }

  function markDetailThreadSeen(tid, messages){
    var root = document.querySelector('.detail-body');
    if (!root || parseInt(root.getAttribute('data-ticket-id'), 10) !== tid) return;
    if (selectedId !== tid) return;
    var mx = '';
    if (messages && messages.length) {
      for (var i = 0; i < messages.length; i++) {
        var c = String((messages[i].created_at || '')).trim();
        if (c > mx) mx = c;
      }
    }
    var t = findTicket(tid);
    var lst = threadActivityFromTicket(t);
    var best = (mx >= lst) ? mx : lst;
    if (!best) return;
    var m = getSeenMap();
    var key = String(tid);
    var prev = String(m[key] || '').trim();
    if (prev && best <= prev) return;
    m[key] = best;
    saveSeenMap(m);
  }

  function updateUnreadIndicators(){
    var uComp = false, uProp = false, i, t;
    for (i = 0; i < tickets.length; i++) {
      t = tickets[i];
      if (!ticketHasUnread(t)) continue;
      if (t.kind === 'proposal') uProp = true;
      else uComp = true;
    }
    var elC = document.getElementById('laneUnreadComplaint');
    var elP = document.getElementById('laneUnreadProposal');
    if (elC) elC.hidden = !uComp;
    if (elP) elP.hidden = !uProp;
    document.querySelectorAll('.card[data-id]').forEach(function(node){
      var id = parseInt(node.getAttribute('data-id'), 10);
      var tk = findTicket(id);
      node.classList.toggle('card-unread', !!(tk && ticketHasUnread(tk)));
    });
  }

  function ticketSnapshot(t){
    return JSON.stringify([
      t.id,
      t.kind,
      t.status || 'open',
      t.anonymous ? 1 : 0,
      (t.text || '').slice(0, 200),
      t.admin_note || '',
      (t.full_name || ''),
      (t.department || ''),
      (t.module || ''),
      threadActivityFromTicket(t),
    ]);
  }

  function threadSnapshot(messages){
    if (!messages || !messages.length) return '';
    var parts = [];
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i];
      parts.push([m.role, m.created_at || '', m.text || ''].join('|'));
    }
    return parts.join('\n');
  }

  function subline(t){
    return (t.anonymous ? '\u0410\u043d\u043e\u043d\u0438\u043c\u043d\u043e' : '\u041f\u0443\u0431\u043b\u0438\u0447\u043d\u043e');
  }

  function cardHtml(t){
    var cls = 'card' + (selectedId === t.id ? ' sel' : '') + (t.status === 'closed' ? ' closed' : '');
    if (ticketHasUnread(t)) cls += ' card-unread';
    var pv = (t.text || '').replace(/\s+/g, ' ');
    if (pv.length > 140) pv = pv.slice(0, 140) + '\u2026';
    var h = '';
    h += '<div class="' + cls + '" data-id="' + t.id + '">';
    h += '<div class="card-top">';
    h += '<span class="chip chip-id">\u2116' + t.id + '</span>';
    if (t.status === 'closed') h += '<span class="chip chip-off">\u0437\u0430\u043a\u0440\u044b\u0442\u0430</span>';
    h += '</div>';
    h += '<div class="preview">' + esc(pv) + '</div>';
    h += '<div class="sub">' + esc(subline(t)) + '</div>';
    h += '</div>';
    return h;
  }

  function bindCards(container){
    container.querySelectorAll('.card').forEach(function(node){
      node.onclick = function(){ select(parseInt(node.getAttribute('data-id'), 10)); };
    });
  }

  var lastListSig = '';

  function listSignature(comp, prop){
    var a = [];
    for (var i = 0; i < comp.length; i++) a.push(ticketSnapshot(comp[i]));
    var b = [];
    for (var j = 0; j < prop.length; j++) b.push(ticketSnapshot(prop[j]));
    return a.join('\f') + '\n---\n' + b.join('\f');
  }

  function updateListSelection(){
    document.querySelectorAll('.card').forEach(function(node){
      var id = parseInt(node.getAttribute('data-id'), 10);
      if (id === selectedId) node.classList.add('sel');
      else node.classList.remove('sel');
    });
  }

  function renderList(){
    var cEl = document.getElementById('listComplaint');
    var pEl = document.getElementById('listProposal');
    var stC = cEl.scrollTop;
    var stP = pEl.scrollTop;
    var comp = [];
    var prop = [];
    for (var i = 0; i < tickets.length; i++) {
      if (tickets[i].kind === 'proposal') prop.push(tickets[i]);
      else comp.push(tickets[i]);
    }
    document.getElementById('cntComplaint').textContent = String(comp.length);
    document.getElementById('cntProposal').textContent = String(prop.length);
    var sig = listSignature(comp, prop);
    if (sig === lastListSig) {
      updateListSelection();
      cEl.scrollTop = stC;
      pEl.scrollTop = stP;
      updateUnreadIndicators();
      return;
    }
    lastListSig = sig;
    if (!comp.length) cEl.innerHTML = '<div class="empty">\u041d\u0435\u0442 \u043e\u0431\u0440\u0430\u0449\u0435\u043d\u0438\u0439</div>';
    else {
      var hc = '';
      for (var a = 0; a < comp.length; a++) hc += cardHtml(comp[a]);
      cEl.innerHTML = hc;
      bindCards(cEl);
    }
    if (!prop.length) pEl.innerHTML = '<div class="empty">\u041d\u0435\u0442 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0439</div>';
    else {
      var hp = '';
      for (var b = 0; b < prop.length; b++) hp += cardHtml(prop[b]);
      pEl.innerHTML = hp;
      bindCards(pEl);
    }
    cEl.scrollTop = stC;
    pEl.scrollTop = stP;
    updateUnreadIndicators();
  }

  function findTicket(id){
    for (var i = 0; i < tickets.length; i++) if (tickets[i].id === id) return tickets[i];
    return null;
  }

  function renderThread(messages, tid, incremental){
    var el = document.getElementById('thread');
    if (!el) return;
    var snap = String(tid) + '\v' + threadSnapshot(messages || []);
    if (incremental && lastThreadTicketId === tid && snap === lastThreadJson) return;
    lastThreadTicketId = tid;
    lastThreadJson = snap;
    var nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 56;
    if (!messages || !messages.length) {
      el.innerHTML = '<div class="empty" style="padding:1rem">\u041d\u0435\u0442 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439</div>';
      return;
    }
    var h = '';
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i];
      var adm = m.role === 'admin';
      h += '<div class="bubble ' + (adm ? 'admin' : 'user') + '">';
      h += '<div class="who">' + (adm ? '\u041f\u043e\u0434\u0434\u0435\u0440\u0436\u043a\u0430' : '\u0410\u0432\u0442\u043e\u0440') + '</div>';
      h += esc(m.text || '');
      h += '<div class="when">' + esc(utcStampToMskDisplay(m.created_at || '')) + '</div>';
      h += '</div>';
    }
    el.innerHTML = h;
    if (nearBottom || !incremental) el.scrollTop = el.scrollHeight;
    markDetailThreadSeen(tid, messages);
    updateUnreadIndicators();
  }

  function loadThread(tid, incremental){
    fetch('/api/tickets/' + tid + '/thread', { headers: keyHeaders() })
      .then(function(r){ return r.json().then(function(j){ return { r: r, j: j }; }); })
      .then(function(x){
        if (handleAuthError(x.r.status)) return;
        if (x.r.ok) renderThread(x.j.messages || [], tid, incremental);
        else renderThread([], tid, false);
      })
      .catch(function(){ renderThread([], tid, false); });
  }

  function bindDetailDelegationOnce(){
    if (detailDelegationBound) return;
    detailDelegationBound = true;
    document.getElementById('detail').addEventListener('click', function(e){
      var root = e.target.closest('.detail-body');
      if (!root) return;
      var tid = parseInt(root.getAttribute('data-ticket-id'), 10);
      if (!tid) return;
      var btn = e.target.closest('button');
      if (!btn) return;
      if (btn.id === 'saveNote') {
        var v = document.getElementById('note').value;
        fetch('/api/tickets/' + tid, { method: 'PATCH', headers: keyHeaders(), body: JSON.stringify({ admin_note: v }) })
          .then(function(r){ return r.json().then(function(j){ return { r: r, j: j }; }); })
          .then(function(x){
            if (handleAuthError(x.r.status)) return;
            if (x.r.ok) { setDetailMsg('ok', '\u0421\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u043e'); fetchTickets(); }
            else { setDetailMsg('err', x.j.error || String(x.r.status)); }
          }).catch(function(){ setDetailMsg('err', '\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c'); });
        return;
      }
      if (btn.id === 'closeT') {
        var st = root.getAttribute('data-status') || 'open';
        var next = st === 'closed' ? 'open' : 'closed';
        fetch('/api/tickets/' + tid, { method: 'PATCH', headers: keyHeaders(), body: JSON.stringify({ status: next }) })
          .then(function(r){ return r.json().then(function(j){ return { r: r, j: j }; }); })
          .then(function(x){
            if (handleAuthError(x.r.status)) return;
            if (x.r.ok) {
              setDetailMsg('ok', next === 'closed' ? '\u0417\u0430\u043a\u0440\u044b\u0442\u0430' : '\u041e\u0442\u043a\u0440\u044b\u0442\u0430');
              root.setAttribute('data-status', next);
              btn.textContent = next === 'closed' ? '\u041e\u0442\u043a\u0440\u044b\u0442\u044c' : '\u0417\u0430\u043a\u0440\u044b\u0442\u044c';
              fetchTickets();
            } else {
              setDetailMsg('err', x.j.error || String(x.r.status));
            }
          });
        return;
      }
      if (btn.id === 'sendReply') {
        var replyEl = document.getElementById('reply');
        if (!replyEl) return;
        var v = replyEl.value.trim();
        if (!v) return;
        fetch('/api/tickets/' + tid + '/reply', { method: 'POST', headers: keyHeaders(), body: JSON.stringify({ text: v }) })
          .then(function(r){ return r.json().then(function(j){ return { r: r, j: j }; }); })
          .then(function(x){
            if (handleAuthError(x.r.status)) return;
            if (x.r.ok) {
              setDetailMsg('ok', '\u041e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e');
              replyEl.value = '';
              loadThread(tid, false);
              fetchTickets();
            } else { setDetailMsg('err', x.j.error || String(x.r.status)); }
          });
      }
    });
  }

  function syncDetailPanel(t){
    var root = document.querySelector('.detail-body');
    if (!root || parseInt(root.getAttribute('data-ticket-id'), 10) !== t.id) return false;
    var kindLabel = t.kind === 'proposal' ? '\u041f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0435' : '\u041e\u0431\u0440\u0430\u0449\u0435\u043d\u0438\u0435';
    var h2 = document.getElementById('detailTitle');
    var mv = document.getElementById('detailMetaVis');
    var statusEl = document.getElementById('detailStatus');
    var profEl = document.getElementById('detailProf');
    if (h2) h2.textContent = '\u2116' + t.id + ' \u00b7 ' + kindLabel;
    if (mv) {
      mv.textContent = subline(t);
      mv.className = 'badge-visibility ' + (t.anonymous ? 'is-anon' : 'is-public');
    }
    if (statusEl) statusEl.hidden = t.status !== 'closed';
    root.setAttribute('data-status', t.status === 'closed' ? 'closed' : 'open');
    var btn = document.getElementById('closeT');
    if (btn) btn.textContent = t.status === 'closed' ? '\u041e\u0442\u043a\u0440\u044b\u0442\u044c' : '\u0417\u0430\u043a\u0440\u044b\u0442\u044c';
    var prof = [t.full_name, t.department, t.module].filter(Boolean).join(' \u00b7 ');
    if (profEl) {
      if (!prof) { profEl.hidden = true; profEl.textContent = ''; }
      else { profEl.hidden = false; profEl.textContent = prof; }
    }
    var noteEl = document.getElementById('note');
    if (noteEl && document.activeElement !== noteEl) noteEl.value = t.admin_note || '';
    updateListSelection();
    loadThread(t.id, true);
    return true;
  }

  function syncDetailAfterPoll(){
    if (!selectedId) return;
    var t = findTicket(selectedId);
    var d = document.getElementById('detail');
    if (!t) {
      selectedId = null;
      lastListSig = '';
      lastThreadTicketId = null;
      lastThreadJson = '';
      d.innerHTML = '<div class="empty">\u041d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445</div>';
      exitMobileDetail();
      renderList();
      return;
    }
    var root = document.querySelector('.detail-body');
    if (root && parseInt(root.getAttribute('data-ticket-id'), 10) === selectedId)
      syncDetailPanel(t);
    else
      select(selectedId);
  }

  function select(id){
    selectedId = id;
    renderList();
    var t = findTicket(id);
    var d = document.getElementById('detail');
    if (!t) {
      d.innerHTML = '<div class="empty">\u041d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445</div>';
      exitMobileDetail();
      return;
    }
    var prof = [t.full_name, t.department, t.module].filter(Boolean).join(' \u00b7 ');
    var note = t.admin_note || '';
    var kindLabel = t.kind === 'proposal' ? '\u041f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0435' : '\u041e\u0431\u0440\u0430\u0449\u0435\u043d\u0438\u0435';
    var st = (t.status === 'closed') ? 'closed' : 'open';
    lastThreadTicketId = null;
    lastThreadJson = '';
    var html = '';
    html += '<div class="detail-body" data-ticket-id="' + esc(String(t.id)) + '" data-status="' + esc(st) + '">';
    html += '<div class="detail-head">';
    html += '<div class="detail-head-top">';
    html += '<h2 id="detailTitle">\u2116' + t.id + ' \u00b7 ' + esc(kindLabel) + '</h2>';
    html += '<button type="button" class="btn btn-ghost detail-close-btn" id="closeT">' + (t.status === 'closed' ? '\u041e\u0442\u043a\u0440\u044b\u0442\u044c' : '\u0417\u0430\u043a\u0440\u044b\u0442\u044c') + '</button>';
    html += '</div>';
    html += '<div class="detail-visibility-row">';
    html += '<span id="detailMetaVis" class="badge-visibility ' + (t.anonymous ? 'is-anon' : 'is-public') + '">' + esc(subline(t)) + '</span>';
    html += '<span id="detailStatus" class="badge-status"' + (t.status === 'closed' ? '' : ' hidden') + '>\u0437\u0430\u043a\u0440\u044b\u0442\u0430</span>';
    html += '</div>';
    if (prof)
      html += '<div id="detailProf" class="profile-strip">' + esc(prof) + '</div>';
    else
      html += '<div id="detailProf" class="profile-strip" hidden></div>';
    html += '</div>';
    html += '<div class="thread-wrap"><label class="section-label section-label-history">\u0418\u0441\u0442\u043e\u0440\u0438\u044f</label><div class="thread scroll-y" id="thread"></div></div>';
    html += '<div class="block"><label class="section-label section-label-note" for="note">\u0417\u0430\u043c\u0435\u0442\u043a\u0430</label><textarea id="note" class="scroll-y field-thread">' + esc(note) + '</textarea></div>';
    html += '<div class="actions"><button type="button" class="btn btn-primary" id="saveNote">\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c</button></div>';
    html += '<div class="block"><label class="section-label section-label-note" for="reply">\u041e\u0442\u0432\u0435\u0442 \u0432 \u0447\u0430\u0442 MAX</label><textarea id="reply" class="scroll-y field-thread" placeholder="\u0422\u0435\u043a\u0441\u0442 \u0443\u0439\u0434\u0451\u0442 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044e\u2026"></textarea></div>';
    html += '<div class="actions actions-reply-with-msg"><button type="button" class="btn btn-primary" id="sendReply">\u041e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c</button><div class="msg" id="msg"></div></div>';
    html += '</div>';
    d.innerHTML = html;
    bindDetailDelegationOnce();
    enterMobileDetail();
    loadThread(t.id, false);
  }

  function fetchTickets(){
    var key = localStorage.getItem('cpz_monitor_key') || '';
    if (!key) {
      if (document.getElementById('authScreen').hidden) showAuth('Введите пароль администратора.');
      return;
    }
    fetch('/api/tickets', { headers: keyHeaders() })
      .then(function(r){ return r.json().then(function(j){ return { r: r, j: j }; }); })
      .then(function(x){
        if (handleAuthError(x.r.status)) return;
        if (!x.r.ok) { showAuth(x.j.error || String(x.r.status)); return; }
        hideAuth();
        tickets = x.j.tickets || [];
        if (selectedId && !findTicket(selectedId)) {
          selectedId = null;
          lastListSig = '';
          document.getElementById('detail').innerHTML =
            '<div class="empty">\u0417\u0430\u044f\u0432\u043a\u0430 \u0443\u0436\u0435 \u043d\u0435 \u0432 \u0441\u043f\u0438\u0441\u043a\u0435</div>';
          exitMobileDetail();
          lastThreadTicketId = null;
          lastThreadJson = '';
        }
        renderList();
        syncDetailAfterPoll();
      })
      .catch(function(){ showAuth('Не удалось подключиться к монитору.'); });
  }

  fetchTickets();
  setInterval(fetchTickets, POLL_MS);
})();
</script>
</body>
</html>"""
