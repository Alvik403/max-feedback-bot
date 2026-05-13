"""
Бот жалоб и предложений в MAX: регистрация по полям → меню.
Жалобы и предложения: выбор анонимно/публично на каждую заявку. SQLite-хранилище.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from maxapi import Bot, Dispatcher
from maxapi.enums.parse_mode import ParseMode
from maxapi.types import (
    BotCommand,
    BotStarted,
    CommandStart,
    Message,
    MessageCallback,
    MessageCreated,
)
from maxapi.types.attachments.attachment import ButtonsPayload

from maxapi.types.attachments.buttons.callback_button import CallbackButton

from maxapi.context import MemoryContext, State, StatesGroup

from monitor import Monitor, MonitorLogHandler, start_monitor_http
from storage import Storage, format_profile_line

LOG_LEVEL_NAME = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.INFO)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("aiohttp.client").setLevel(logging.WARNING)

log = logging.getLogger("cpz_bot")

APP_TITLE = "Бот жалоб и предложений"
MONITOR_HOST = os.environ.get("MONITOR_HOST", "0.0.0.0").strip()
MONITOR_PORT = int(os.environ.get("MONITOR_PORT", "15000").strip())
DATABASE_PATH = os.environ.get("DATABASE_PATH", "./data/bot.sqlite").strip()


class Flow(StatesGroup):
    registration_fio = State()
    registration_dept = State()
    registration_module = State()
    main = State()
    pick_suggestion_privacy = State()
    awaiting_suggestion = State()
    pick_complaint_privacy = State()
    awaiting_complaint = State()
    awaiting_ticket_reply = State()
    settings = State()
    edit_fio = State()
    edit_dept = State()
    edit_module = State()


def _token() -> str:
    token = (os.environ.get("MAX_BOT_TOKEN") or "").strip()
    if not token:
        log.error(
            "MAX_BOT_TOKEN не задана. Передайте токен в окружении или в .env для docker-compose."
        )
        sys.exit(1)
    return token


monitor = Monitor()
storage = Storage(DATABASE_PATH)
mh = MonitorLogHandler(monitor)
mh.setLevel(LOG_LEVEL)
mh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

for _name in ("cpz_bot", "monitor", "maxapi.dispatcher"):
    lg = logging.getLogger(_name)
    lg.addHandler(mh)
    lg.setLevel(LOG_LEVEL)
    lg.propagate = True


async def db_run(fn, /, *args, **kwargs):
    def _call():
        return fn(*args, **kwargs)

    return await asyncio.to_thread(_call)


_MONTHS_GEN = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

_MSK_TZ = ZoneInfo("Europe/Moscow")


def _short(val: str, max_len: int = 72) -> str:
    s = (val or "").strip() or "—"
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _format_dt_ru(created_at: str | None) -> str:
    if not created_at:
        return "—"
    s = created_at.strip().replace(" UTC", "").strip()
    try:
        dt_utc = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        dt_msk = dt_utc.astimezone(_MSK_TZ)
        return (
            f"{dt_msk.day} {_MONTHS_GEN[dt_msk.month - 1]} {dt_msk.year}, "
            f"{dt_msk.strftime('%H:%M')} МСК"
        )
    except ValueError:
        return created_at.strip()


def _format_dt_ticket(created_at: str | None) -> str:
    """Дата заявки/сообщений в карточке без суффикса «МСК»."""
    if not created_at:
        return "—"
    s = created_at.strip().replace(" UTC", "").strip()
    try:
        dt_utc = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        dt_msk = dt_utc.astimezone(_MSK_TZ)
        return (
            f"{dt_msk.day} {_MONTHS_GEN[dt_msk.month - 1]} {dt_msk.year}, "
            f"{dt_msk.strftime('%H:%M')}"
        )
    except ValueError:
        return created_at.strip()


def _user_profile_section(row: dict | None, *, title: str = "Ваш профиль") -> str:
    if not row:
        return f"{title}\n · данные недоступны"
    fn = (row.get("full_name") or "").strip() or "—"
    dep = (row.get("department") or "").strip() or "—"
    mod = (row.get("module") or "").strip() or "—"
    return f"{title}\n · ФИО: {fn}\n · Подразделение: {dep}\n · Модуль: {mod}"


def _user_profile_section_html(row: dict | None, *, title: str = "Ваш профиль") -> str:
    if not row:
        return f"{html.escape(title)}\n · данные недоступны"
    fn = html.escape((row.get("full_name") or "").strip() or "—")
    dep = html.escape((row.get("department") or "").strip() or "—")
    mod = html.escape((row.get("module") or "").strip() or "—")
    return (
        f"{html.escape(title)}\n · ФИО: {fn}\n · Подразделение: {dep}\n · Модуль: {mod}"
    )


def _main_menu_text(row: dict | None) -> str:
    """HTML-текст главного меню (нужен parse_mode=ParseMode.HTML)."""
    return (
        "<b>Главное меню</b>\n\n"
        + _user_profile_section_html(row)
        + "\n\nВыберите действие:"
    )


def _plain_then_main_menu_html(plain_lead: str, row: dict | None) -> str:
    return html.escape(plain_lead) + _main_menu_text(row)


async def _main_menu_text_for_user(user_id: int) -> str:
    row = await db_run(storage.get_user, user_id)
    return _main_menu_text(row)


def _settings_screen_text(
    row: dict,
    *,
    moment_note: str | None = None,
) -> str:
    parts = [
        "Настройки профиля",
        "",
        _user_profile_section(row, title="Текущие данные"),
    ]
    note = (moment_note or "").strip()
    if note:
        parts.extend(["", "─" * 28, "", note])
    return "\n".join(parts)


def _my_submissions_list_intro(rows: list) -> str:
    """Короткий текст к списку кнопок «Мои заявки» (сами номера только на кнопках)."""
    if not rows:
        return (
            "Мои заявки\n\n"
            "Здесь появятся ваши обращения и предложения из бота. Пока список пуст."
        )
    return (
        "Мои заявки\n\n"
        "Нажмите кнопку с номером заявки, чтобы открыть историю переписки с поддержкой "
        "и при необходимости отправить ответ."
    )


USER_TICKET_CHUNK = 3400


def _split_long_message(text: str, max_len: int = USER_TICKET_CHUNK) -> list[str]:
    text = (text or "").strip()
    if len(text) <= max_len:
        return [text] if text else [""]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return chunks


def _html_br_plain(s: str) -> str:
    return html.escape(s or "").replace("\n", "<br>")


def _ticket_block_sep_html() -> str:
    """Горизонтальный разделитель как в образце сообщения."""
    return html.escape("━━━━━━━━━━━━")


def _ticket_status_line(open_ok: bool, anonymous: bool) -> str:
    """○/✓ + Открыта/Закрыта · Публичная|Анонимная."""
    vis = "Анонимная" if anonymous else "Публичная"
    if open_ok:
        return f"○ Открыта · {vis}"
    return f"✓ Закрыта · {vis}"


def _split_ticket_opener_chunks(hdr: str, plain_ticket_text: str) -> list[str]:
    """Разбивает длинную заявку на части, не режет HTML-теги шапки."""
    fb = (plain_ticket_text or "").strip()
    first_blob = hdr + _html_br_plain(fb)
    slack = USER_TICKET_CHUNK - 80
    if len(first_blob) <= slack:
        return [first_blob.strip()]
    segments_plain = _split_long_message(fb, max(560, slack - len(hdr)))
    out: list[str] = []
    for i, seg in enumerate(segments_plain):
        body = _html_br_plain(seg.strip())
        if i == 0:
            out.append((hdr + body).strip())
        else:
            cont = "<i>Продолжение текста заявки</i>\n\n" + body
            out.append(cont.strip())
    return out


def _user_ticket_detail_html_chunks(sub: dict, thread: list) -> list[str]:
    """Карточка заявки для чата (HTML): шапка, текст, сообщения порциями по лимиту."""
    kind_title = (
        "Предложение" if sub.get("kind") == "proposal" else "Обращение / жалоба"
    )
    anonymous = bool(sub.get("anonymous"))
    status_raw = str(sub.get("status") or "open").strip()
    is_open = status_raw != "closed"
    created_show = html.escape(_format_dt_ticket(sub.get("created_at")))
    sep_h = _ticket_block_sep_html()
    status_plain = _ticket_status_line(is_open, anonymous)
    nid = html.escape(str(sub["id"]))
    hdr = (
        f"<b>Заявка №{nid} — {html.escape(kind_title)}</b>\n\n"
        f"{html.escape(status_plain)}\n"
        f"{created_show}\n"
        f"{sep_h}\n"
        f"<b>Текст заявки</b>\n\n"
    )
    if not thread:
        return [
            hdr
            + "<i>Сообщений пока нет.</i>"
        ]

    first_plain = str(thread[0].get("text") or "").strip()
    opener_segs = _split_ticket_opener_chunks(hdr, first_plain)

    rest_msgs = thread[1:]
    hist_intro = f"{sep_h}\n<b>Переписка</b>\n\n"

    chunks: list[str] = []

    if not rest_msgs:
        tail_segs = list(opener_segs)
        tail_segs[-1] = (
            tail_segs[-1].rstrip()
            + "\n\n"
            + sep_h
            + "\n<b>Переписка</b>\n\n<i>Сообщений пока нет.</i>"
        )
        return tail_segs

    for seg in opener_segs[:-1]:
        chunks.append(seg)
    buf = opener_segs[-1] + "\n\n" + hist_intro
    frag_limit = USER_TICKET_CHUNK - 280

    n_msg = len(rest_msgs)
    for idx, m in enumerate(rest_msgs):
        is_last = idx == n_msg - 1
        role = str(m.get("role") or "")
        when_esc = html.escape(_format_dt_ticket(m.get("created_at")))
        body = _html_br_plain(str(m.get("text") or "").strip())
        if role == "user":
            card = (
                f"🤖 <i>Вы · {when_esc}</i>\n"
                + (body if body else "<i>—</i>")
                + "\n"
            )
        else:
            card = (
                f"🧑‍💻 <b>Поддержка</b> · <i>{when_esc}</i>\n"
                + (body if body else "<i>—</i>")
                + "\n"
            )
        if not is_last:
            card += "\n"
        if len(buf) + len(card) > frag_limit and buf.strip():
            chunks.append(buf.strip())
            buf = f"{sep_h}\n<b>Переписка</b> <i>(продолжение)</i>\n\n" + card
        else:
            buf += card

    if buf.strip():
        chunks.append(buf.strip())
    if not chunks:
        return opener_segs
    return chunks


def my_submissions_keyboard(rows: list, *, opened_from_settings: bool) -> str:
    btn_rows = []
    for r in rows:
        rid = int(r["id"])
        kind_s = str(r.get("kind") or "")
        status = str(r.get("status") or "open").strip()
        tag = "Предложение" if kind_s == "proposal" else "Обращение"
        suff = "" if status == "open" else " ✓"
        label = f"№{rid} · {tag}{suff}"
        if len(label) > 64:
            label = f"№{rid}{suff}"
        btn_rows.append(
            [CallbackButton(text=label, payload=f"myticket:view:{rid}")]
        )
    if opened_from_settings:
        btn_rows.append(
            [CallbackButton(text="« Настройки", payload="settings:resume")]
        )
    else:
        btn_rows.append(
            [CallbackButton(text="« Главное меню", payload="menu:back")]
        )
    return ButtonsPayload(buttons=btn_rows).pack()


def user_ticket_detail_keyboard(ticket_id: int) -> str:
    return ButtonsPayload(
        buttons=[
            [CallbackButton(text="Ответить", payload=f"uticket:reply:{ticket_id}")],
            [
                CallbackButton(
                    text="« К списку заявок",
                    payload="menu:my_submissions",
                )
            ],
        ]
    ).pack()


async def _send_user_ticket_detail(
    b: Bot, kw: dict, user_id: int, ticket_id: int
) -> None:
    sub = await db_run(storage.get_submission, ticket_id)
    if not sub or int(sub["user_id"]) != user_id:
        await b.send_message(
            **kw,
            text="Заявка не найдена или вам недоступна.",
            attachments=[main_menu_keyboard()],
        )
        return
    thread = await db_run(storage.get_submission_thread, ticket_id)
    chunks = _user_ticket_detail_html_chunks(sub, thread)
    attach_kb = user_ticket_detail_keyboard(int(sub["id"]))
    n = len(chunks)
    for i, chunk in enumerate(chunks):
        if i == n - 1:
            await b.send_message(
                **kw,
                text=chunk,
                parse_mode=ParseMode.HTML,
                attachments=[attach_kb],
            )
        else:
            await b.send_message(
                **kw, text=chunk, parse_mode=ParseMode.HTML
            )


def start_keyboard():
    return ButtonsPayload(
        buttons=[
            [CallbackButton(text="Открыть главное меню", payload="start:menu")],
            [CallbackButton(text="Заполнить профиль заново", payload="start:reregister")],
        ]
    ).pack()


def main_menu_keyboard():
    return ButtonsPayload(
        buttons=[
            [CallbackButton(text="Внести предложение", payload="menu:suggest")],
            [CallbackButton(text="Подать жалобу или обращение", payload="menu:complaint")],
            [CallbackButton(text="Мои заявки", payload="menu:my_submissions")],
            [CallbackButton(text="Настройки профиля", payload="menu:settings")],
        ]
    ).pack()


def cancel_only_keyboard():
    return ButtonsPayload(
        buttons=[[CallbackButton(text="Отмена", payload="input:cancel")]]
    ).pack()


def submission_privacy_keyboard(kind: str) -> str:
    """kind: suggest | complaint"""
    prefix = "sub_suggest:" if kind == "suggest" else "sub_complaint:"
    return ButtonsPayload(
        buttons=[
            [
                CallbackButton(text="Анонимно", payload=f"{prefix}anon"),
                CallbackButton(text="Публично", payload=f"{prefix}public"),
            ],
            [CallbackButton(text="Отмена", payload="input:cancel")],
        ]
    ).pack()


def settings_keyboard() -> str:
    return ButtonsPayload(
        buttons=[
            [CallbackButton(text="Изменить ФИО", payload="edit:fio")],
            [CallbackButton(text="Изменить подразделение", payload="edit:dept")],
            [CallbackButton(text="Изменить модуль", payload="edit:mod")],
            [CallbackButton(text="Мои заявки", payload="menu:my_submissions")],
            [CallbackButton(text="В главное меню", payload="menu:back")],
        ]
    ).pack()


bot = Bot(_token())
dp = Dispatcher()


def _chat_kw(event_msg) -> dict:
    cid = event_msg.recipient.chat_id
    uid = event_msg.recipient.user_id
    kw: dict = {"chat_id": cid}
    if uid is not None:
        kw["user_id"] = uid
    return kw


async def _try_delete_prompt_message(msg: Message, api: Bot | None = None) -> None:
    """Убирает сообщение с нажатой кнопкой, чтобы старый текст не оставался в чате."""
    mid = getattr(getattr(msg, "body", None), "mid", None)
    if not mid:
        return
    client = api or getattr(msg, "bot", None) or bot
    try:
        await client.delete_message(message_id=mid)
    except Exception as exc:
        log.debug("удаление сообщения с кнопками: %s", exc)


def _mid_from_send_result(sent) -> str | None:
    """ID сообщения из ответа API send_message (SendedMessage), иначе None."""
    if sent is None:
        return None
    msg = getattr(sent, "message", None)
    if msg is None:
        return None
    body = getattr(msg, "body", None)
    if body is None:
        return None
    mid = getattr(body, "mid", None)
    return str(mid) if mid else None


async def _discard_draft_prompt(b, context: MemoryContext) -> None:
    """Удаляет сохранённое сообщение-подсказку к вводу текста заявки (если есть)."""
    data = await context.get_data()
    mid = data.get("draft_prompt_mid")
    if mid:
        try:
            await b.delete_message(message_id=mid)
        except Exception as exc:
            log.debug("удаление подсказки ввода: %s", exc)
    await context.update_data(draft_prompt_mid=None)


async def _touch_chat(user_id: int, chat_id: int | None) -> None:
    row = await db_run(storage.get_user, user_id)
    if row:
        await db_run(storage.update_user_chat, user_id, chat_id)


async def _send_registered_start(b, kw: dict, *, title: str | None = None) -> None:
    head = title or f"Вы уже зарегистрированы в «{APP_TITLE}»."
    await b.send_message(
        **kw,
        text=(
            f"{head}\n\n"
            "Нажмите кнопку ниже, чтобы открыть меню действий, "
            "или заполните профиль заново (данные будут заменены)."
        ),
        attachments=[start_keyboard()],
    )


async def _begin_registration(chat_id: int, user_id: int, context: MemoryContext) -> None:
    await context.clear()
    await context.set_state(Flow.registration_fio)
    await bot.send_message(
        chat_id=chat_id,
        user_id=user_id,
        text=(
            f"Регистрация в «{APP_TITLE}».\n\n"
            "Шаг 1 из 3: введите ваше ФИО одним сообщением "
            "(например: Иванов Иван Иванович)."
        ),
    )


async def _finalize_registration_to_main(
    b,
    kw: dict,
    context: MemoryContext,
    user_id: int,
    chat_id: int | None,
    *,
    default_anonymous: bool = True,
) -> None:
    data = await context.get_data()
    fn = (data.get("reg_fio") or "").strip()
    dep = (data.get("reg_dept") or "").strip()
    mod = (data.get("reg_module") or "").strip()
    await db_run(
        storage.upsert_user,
        user_id,
        chat_id=chat_id,
        full_name=fn,
        department=dep,
        module=mod,
        default_anonymous=default_anonymous,
    )
    row = await db_run(storage.get_user, user_id)
    await context.clear()
    await context.set_state(Flow.main)
    monitor.add_event(
        "registered",
        user_id=user_id,
        chat_id=chat_id,
        detail=f"{fn[:40]}… {dep[:40]}…",
    )
    intro = (
        "Регистрация завершена.\n\n"
        "При каждой отправке предложения, жалобы или обращения вы выберете видимость заявки.\n\n"
    )
    await b.send_message(
        **kw,
        text=html.escape(intro) + _main_menu_text(row),
        parse_mode=ParseMode.HTML,
        attachments=[main_menu_keyboard()],
    )


@dp.message_created(CommandStart())
async def cmd_start(event: MessageCreated, context: MemoryContext) -> None:
    cid = event.message.recipient.chat_id
    uid = event.message.sender.user_id
    log.info("/start chat_id=%s user=%s", cid, uid)
    monitor.add_event("cmd_start", user_id=uid, chat_id=cid, state=str(await context.get_state()))
    await _touch_chat(uid, cid)

    row = await db_run(storage.get_user, uid)
    await context.clear()
    await context.set_state(Flow.main)
    if row:
        await _send_registered_start(event.message.bot, _chat_kw(event.message))
        return
    await _begin_registration(cid, uid, context)


@dp.bot_started()
async def on_bot_started(event: BotStarted, context: MemoryContext) -> None:
    cid = event.chat_id
    uid = event.user.user_id
    log.info("bot_started chat_id=%s", cid)
    monitor.add_event("bot_started", chat_id=cid, user_id=uid)
    await _touch_chat(uid, cid)

    row = await db_run(storage.get_user, uid)
    await context.clear()
    await context.set_state(Flow.main)
    if row:
        await _send_registered_start(bot, {"chat_id": cid, "user_id": uid})
        return
    await _begin_registration(cid, uid, context)


@dp.message_callback()
async def on_callback(event: MessageCallback, context: MemoryContext) -> None:
    if event.callback.user.is_bot:
        return

    pl = (event.callback.payload or "").strip()
    st = await context.get_state()
    uid = event.callback.user.user_id
    cid = event.message.recipient.chat_id

    monitor.add_debug(f"callback payload={pl!r} state={st!s} user={uid}")
    monitor.add_event("callback", user_id=uid, chat_id=cid, state=str(st), detail=pl)

    b = event.bot or event.message.bot or bot
    kw = _chat_kw(event.message)

    async def ack(note: str = "") -> None:
        await event.answer(notification=note or " ")

    # --- Ответ по заявке (сообщение от поддержки) ---
    if pl.startswith("ticket:reply:"):
        rest = pl.split(":", 2)[-1].strip()
        try:
            tid = int(rest)
        except ValueError:
            await ack()
            return
        sub = await db_run(storage.get_submission, tid)
        if not sub or int(sub["user_id"]) != uid:
            await ack("Эта заявка недоступна.")
            return
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await context.set_state(Flow.awaiting_ticket_reply)
        await context.update_data(
            reply_ticket_id=tid,
            draft_prompt_mid=None,
            ticket_reply_nav=None,
            ticket_reply_review_id=None,
        )
        sent = await b.send_message(
            **kw,
            text=(
                f"Заявка №{tid}.\n\n"
                "Напишите ваш ответ одним сообщением или нажмите «Отмена»."
            ),
            attachments=[cancel_only_keyboard()],
        )
        await context.update_data(draft_prompt_mid=_mid_from_send_result(sent))
        return

    if pl.startswith("uticket:reply:"):
        suf = pl.removeprefix("uticket:reply:").strip()
        try:
            tid_u = int(suf)
        except ValueError:
            await ack()
            return
        sub_u = await db_run(storage.get_submission, tid_u)
        if not sub_u or int(sub_u["user_id"]) != uid:
            await ack("Эта заявка недоступна.")
            return
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await context.set_state(Flow.awaiting_ticket_reply)
        await context.update_data(
            reply_ticket_id=tid_u,
            draft_prompt_mid=None,
            ticket_reply_nav="detail",
            ticket_reply_review_id=tid_u,
        )
        sent_u = await b.send_message(
            **kw,
            text=(
                f"Заявка №{tid_u}.\n\n"
                "Напишите ваш ответ одним сообщением или нажмите «Отмена». "
                "Вы вернётесь к истории этой заявки."
            ),
            attachments=[cancel_only_keyboard()],
        )
        await context.update_data(draft_prompt_mid=_mid_from_send_result(sent_u))
        return

    # --- Отмена ввода ---
    if pl == "input:cancel":
        if st == Flow.awaiting_ticket_reply:
            data_tr = await context.get_data()
            nav = data_tr.get("ticket_reply_nav")
            tid_rev = data_tr.get("ticket_reply_review_id")
            await ack("Отменено")
            await _try_delete_prompt_message(event.message, b)
            await context.update_data(
                reply_ticket_id=None,
                draft_prompt_mid=None,
                ticket_reply_nav=None,
                ticket_reply_review_id=None,
            )
            await context.set_state(Flow.main)
            if nav == "detail" and tid_rev is not None:
                try:
                    await _send_user_ticket_detail(
                        b, kw, uid, int(tid_rev)
                    )
                except (TypeError, ValueError):
                    await b.send_message(
                        **kw,
                        text=await _main_menu_text_for_user(uid),
                        parse_mode=ParseMode.HTML,
                        attachments=[main_menu_keyboard()],
                    )
                return
            await b.send_message(
                **kw,
                text=await _main_menu_text_for_user(uid),
                parse_mode=ParseMode.HTML,
                attachments=[main_menu_keyboard()],
            )
            return
        if st in (Flow.awaiting_suggestion, Flow.pick_suggestion_privacy):
            await ack("Отменено")
            await _try_delete_prompt_message(event.message, b)
            await context.set_state(Flow.main)
            await context.update_data(submission_anonymous=None, draft_prompt_mid=None)
            await b.send_message(
                **kw,
                text=await _main_menu_text_for_user(uid),
                parse_mode=ParseMode.HTML,
                attachments=[main_menu_keyboard()],
            )
            return
        if st in (Flow.awaiting_complaint, Flow.pick_complaint_privacy):
            await ack("Отменено")
            await _try_delete_prompt_message(event.message, b)
            await context.set_state(Flow.main)
            await context.update_data(submission_anonymous=None, draft_prompt_mid=None)
            await b.send_message(
                **kw,
                text=await _main_menu_text_for_user(uid),
                parse_mode=ParseMode.HTML,
                attachments=[main_menu_keyboard()],
            )
            return
        if st in (Flow.edit_fio, Flow.edit_dept, Flow.edit_module):
            await ack("Отменено")
            await _try_delete_prompt_message(event.message, b)
            await context.set_state(Flow.settings)
            await context.update_data(draft_prompt_mid=None)
            row_u = await db_run(storage.get_user, uid)
            await b.send_message(
                **kw,
                text=_settings_screen_text(row_u) if row_u else "Настройки профиля",
                attachments=[settings_keyboard()],
            )
            return
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await context.update_data(draft_prompt_mid=None)
        return

    if pl == "start:menu":
        await ack()
        await _try_delete_prompt_message(event.message, b)
        row = await db_run(storage.get_user, uid)
        if not row:
            await _begin_registration(cid, uid, context)
            return
        await context.set_state(Flow.main)
        await b.send_message(
            **kw,
            text=await _main_menu_text_for_user(uid),
            parse_mode=ParseMode.HTML,
            attachments=[main_menu_keyboard()],
        )
        return

    if pl == "start:reregister":
        await ack()
        await _try_delete_prompt_message(event.message, b)
        row = await db_run(storage.get_user, uid)
        if not row:
            await _begin_registration(cid, uid, context)
            return
        await _begin_registration(cid, uid, context)
        await b.send_message(
            **kw,
            text="Заполним профиль заново: предыдущие ФИО, подразделение и модуль будут заменены после завершения.",
        )
        return

    if pl == "menu:suggest" and st == Flow.main:
        row = await db_run(storage.get_user, uid)
        if not row:
            await ack()
            await _try_delete_prompt_message(event.message, b)
            await _begin_registration(cid, uid, context)
            return
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await context.set_state(Flow.pick_suggestion_privacy)
        await b.send_message(
            **kw,
            text="Как отправить это предложение?\nВыберите вариант для текущей заявки.",
            attachments=[submission_privacy_keyboard("suggest")],
        )
        return

    if pl == "menu:complaint" and st == Flow.main:
        row = await db_run(storage.get_user, uid)
        if not row:
            await ack()
            await _try_delete_prompt_message(event.message, b)
            await _begin_registration(cid, uid, context)
            return
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await context.set_state(Flow.pick_complaint_privacy)
        await b.send_message(
            **kw,
            text="Как отправить это обращение?\nВыберите вариант для текущей заявки.",
            attachments=[submission_privacy_keyboard("complaint")],
        )
        return

    if pl in ("sub_suggest:anon", "sub_suggest:public") and st == Flow.pick_suggestion_privacy:
        anon = pl.endswith("anon")
        await context.update_data(submission_anonymous=anon)
        await context.set_state(Flow.awaiting_suggestion)
        await ack("Ок")
        await _try_delete_prompt_message(event.message, b)
        sent = await b.send_message(
            **kw,
            text="Опишите ваше предложение одним сообщением.",
            attachments=[cancel_only_keyboard()],
        )
        await context.update_data(draft_prompt_mid=_mid_from_send_result(sent))
        return

    if pl in ("sub_complaint:anon", "sub_complaint:public") and st == Flow.pick_complaint_privacy:
        anon = pl.endswith("anon")
        await context.update_data(submission_anonymous=anon)
        await context.set_state(Flow.awaiting_complaint)
        await ack("Ок")
        await _try_delete_prompt_message(event.message, b)
        sent = await b.send_message(
            **kw,
            text="Опишите вашу проблему или жалобу одним сообщением.",
            attachments=[cancel_only_keyboard()],
        )
        await context.update_data(draft_prompt_mid=_mid_from_send_result(sent))
        return

    if pl == "menu:settings" and st == Flow.main:
        row = await db_run(storage.get_user, uid)
        if not row:
            await ack()
            await _try_delete_prompt_message(event.message, b)
            await _begin_registration(cid, uid, context)
            return
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await context.set_state(Flow.settings)
        await b.send_message(
            **kw,
            text=_settings_screen_text(row),
            attachments=[settings_keyboard()],
        )
        return

    if pl == "edit:fio" and st == Flow.settings:
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await context.set_state(Flow.edit_fio)
        await b.send_message(
            **kw,
            text="Введите новое ФИО одним сообщением (старое будет удалено и заменено).",
            attachments=[cancel_only_keyboard()],
        )
        return

    if pl == "edit:dept" and st == Flow.settings:
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await context.set_state(Flow.edit_dept)
        await b.send_message(
            **kw,
            text="Введите новое подразделение (старое будет заменено).",
            attachments=[cancel_only_keyboard()],
        )
        return

    if pl == "edit:mod" and st == Flow.settings:
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await context.set_state(Flow.edit_module)
        await b.send_message(
            **kw,
            text="Введите новый модуль (старое будет заменено).",
            attachments=[cancel_only_keyboard()],
        )
        return

    if pl == "settings:resume":
        row_sr = await db_run(storage.get_user, uid)
        if not row_sr:
            await ack()
            return
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await context.set_state(Flow.settings)
        await b.send_message(
            **kw,
            text=_settings_screen_text(row_sr),
            attachments=[settings_keyboard()],
        )
        return

    if pl.startswith("myticket:view:"):
        row_mv = await db_run(storage.get_user, uid)
        if not row_mv:
            await ack()
            await b.send_message(**kw, text="Сначала пройдите регистрацию: /start")
            return
        suf_mv = pl.removeprefix("myticket:view:").strip()
        try:
            view_tid = int(suf_mv)
        except ValueError:
            await ack()
            return
        sub_mv = await db_run(storage.get_submission, view_tid)
        if not sub_mv or int(sub_mv["user_id"]) != uid:
            await ack("Нет доступа к этой заявке.")
            return
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await _send_user_ticket_detail(b, kw, uid, view_tid)
        return

    if pl == "menu:my_submissions" and st in (Flow.main, Flow.settings):
        row_ms = await db_run(storage.get_user, uid)
        if not row_ms:
            await ack()
            await _try_delete_prompt_message(event.message, b)
            await _begin_registration(cid, uid, context)
            return
        await ack()
        await _try_delete_prompt_message(event.message, b)
        opened_from_settings = st == Flow.settings
        rows_ms = await db_run(storage.list_user_submissions, uid, 25)
        intro = _my_submissions_list_intro(rows_ms)
        kb_ms = (
            settings_keyboard()
            if not rows_ms and opened_from_settings
            else main_menu_keyboard()
            if not rows_ms
            else my_submissions_keyboard(
                rows_ms, opened_from_settings=opened_from_settings
            )
        )
        await b.send_message(**kw, text=intro, attachments=[kb_ms])
        return

    if pl == "menu:back":
        await ack()
        await _try_delete_prompt_message(event.message, b)
        await context.set_state(Flow.main)
        await b.send_message(
            **kw,
            text=await _main_menu_text_for_user(uid),
            parse_mode=ParseMode.HTML,
            attachments=[main_menu_keyboard()],
        )
        return

    await ack()


@dp.message_created(Flow.awaiting_ticket_reply)
async def on_ticket_user_reply(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    uid = event.message.sender.user_id
    cid = event.message.recipient.chat_id
    await _touch_chat(uid, cid)

    raw = (event.message.body.text or "").strip()
    if not raw:
        await event.message.answer(
            "Введите текст ответа или нажмите «Отмена».",
            attachments=[cancel_only_keyboard()],
        )
        return

    data = await context.get_data()
    tid = data.get("reply_ticket_id")
    if tid is None:
        await context.set_state(Flow.main)
        await context.update_data(
            reply_ticket_id=None,
            draft_prompt_mid=None,
            ticket_reply_nav=None,
            ticket_reply_review_id=None,
        )
        await event.message.answer(
            "Сессия ответа устарела. Откройте меню.",
            attachments=[main_menu_keyboard()],
        )
        return
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        tid = 0
    sub = await db_run(storage.get_submission, tid)
    if not sub or int(sub["user_id"]) != uid:
        await _discard_draft_prompt(event.message.bot, context)
        await context.set_state(Flow.main)
        await context.update_data(
            reply_ticket_id=None,
            draft_prompt_mid=None,
            ticket_reply_nav=None,
            ticket_reply_review_id=None,
        )
        row_lost = await db_run(storage.get_user, uid)
        await event.message.answer(
            _plain_then_main_menu_html("Заявка не найдена или недоступна.\n\n", row_lost),
            parse_mode=ParseMode.HTML,
            attachments=[main_menu_keyboard()],
        )
        return

    nav = data.get("ticket_reply_nav")
    tid_rev_raw = data.get("ticket_reply_review_id")

    was_closed = str(sub.get("status") or "open").strip() == "closed"

    await db_run(storage.add_submission_reply, tid, raw, from_user=True)
    if was_closed:
        await db_run(storage.update_submission_ticket, tid, status="open")

    await _discard_draft_prompt(event.message.bot, context)
    await context.set_state(Flow.main)
    await context.update_data(
        reply_ticket_id=None,
        draft_prompt_mid=None,
        ticket_reply_nav=None,
        ticket_reply_review_id=None,
    )

    b_u = event.message.bot or bot
    kw_u = _chat_kw(event.message)
    detail_id: int | None = None
    if nav == "detail" and tid_rev_raw is not None:
        try:
            detail_id = int(tid_rev_raw)
        except (TypeError, ValueError):
            detail_id = tid

    reopen_note = "\nЗаявка снова открыта для обработки." if was_closed else ""

    if detail_id is not None:
        await event.message.answer("Ответ отправлен." + reopen_note)
        await _send_user_ticket_detail(b_u, kw_u, uid, detail_id)
        return

    row_ok = await db_run(storage.get_user, uid)
    await event.message.answer(
        _plain_then_main_menu_html(
            "Ответ отправлен." + reopen_note + "\n\n", row_ok
        ),
        parse_mode=ParseMode.HTML,
        attachments=[main_menu_keyboard()],
    )


@dp.message_created(Flow.awaiting_suggestion)
async def on_suggestion(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    uid = event.message.sender.user_id
    cid = event.message.recipient.chat_id
    await _touch_chat(uid, cid)

    raw = (event.message.body.text or "").strip()
    if not raw:
        await event.message.answer("Пришлите текст предложения.", attachments=[cancel_only_keyboard()])
        return
    data = await context.get_data()
    anon = data.get("submission_anonymous")
    if anon is None:
        anon = True
    user = await db_run(storage.get_user, uid)
    if not user:
        await _discard_draft_prompt(event.message.bot, context)
        await context.set_state(Flow.main)
        await context.update_data(submission_anonymous=None)
        await event.message.answer("Сначала пройдите регистрацию: /start")
        return
    pline = format_profile_line(
        user["full_name"], user["department"], user["module"]
    )
    await db_run(
        storage.add_submission,
        uid,
        cid,
        "proposal",
        raw,
        bool(anon),
    )
    monitor.add_submission(
        "proposal",
        user_id=uid,
        chat_id=cid,
        anonymous=bool(anon),
        profile_line=pline,
        text=raw,
    )
    await _discard_draft_prompt(event.message.bot, context)
    await context.set_state(Flow.main)
    await context.update_data(submission_anonymous=None)
    row_prop = await db_run(storage.get_user, uid)
    await event.message.answer(
        _plain_then_main_menu_html("Ваше предложение зарегистрировано.\n\n", row_prop),
        parse_mode=ParseMode.HTML,
        attachments=[main_menu_keyboard()],
    )


@dp.message_created(Flow.awaiting_complaint)
async def on_complaint(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    uid = event.message.sender.user_id
    cid = event.message.recipient.chat_id
    await _touch_chat(uid, cid)

    raw = (event.message.body.text or "").strip()
    if not raw:
        await event.message.answer("Пришлите текст обращения.", attachments=[cancel_only_keyboard()])
        return
    data = await context.get_data()
    anon = data.get("submission_anonymous")
    if anon is None:
        anon = True
    user = await db_run(storage.get_user, uid)
    if not user:
        await _discard_draft_prompt(event.message.bot, context)
        await context.set_state(Flow.main)
        await context.update_data(submission_anonymous=None)
        await event.message.answer("Сначала пройдите регистрацию: /start")
        return
    pline = format_profile_line(
        user["full_name"], user["department"], user["module"]
    )
    await db_run(
        storage.add_submission,
        uid,
        cid,
        "complaint",
        raw,
        bool(anon),
    )
    monitor.add_submission(
        "complaint",
        user_id=uid,
        chat_id=cid,
        anonymous=bool(anon),
        profile_line=pline,
        text=raw,
    )
    await _discard_draft_prompt(event.message.bot, context)
    await context.set_state(Flow.main)
    await context.update_data(submission_anonymous=None)
    row_comp = await db_run(storage.get_user, uid)
    await event.message.answer(
        _plain_then_main_menu_html("Ваше обращение зарегистрировано.\n\n", row_comp),
        parse_mode=ParseMode.HTML,
        attachments=[main_menu_keyboard()],
    )


@dp.message_created(Flow.registration_fio)
async def on_registration_fio(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    raw = (event.message.body.text or "").strip()
    if not raw:
        await event.message.answer("Нужно ввести ФИО текстом.")
        return
    await context.update_data(reg_fio=raw)
    await context.set_state(Flow.registration_dept)
    await event.message.answer(
        "Шаг 2 из 3: введите подразделение одним сообщением "
        "(например: Отдел продаж)."
    )


@dp.message_created(Flow.registration_dept)
async def on_registration_dept(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    raw = (event.message.body.text or "").strip()
    if not raw:
        await event.message.answer("Нужно ввести подразделение текстом.")
        return
    await context.update_data(reg_dept=raw)
    await context.set_state(Flow.registration_module)
    await event.message.answer(
        "Шаг 3 из 3: введите модуль одним сообщением "
        "(например: Модуль 3)."
    )


@dp.message_created(Flow.registration_module)
async def on_registration_module(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    uid = event.message.sender.user_id
    cid = event.message.recipient.chat_id
    raw = (event.message.body.text or "").strip()
    if not raw:
        await event.message.answer("Нужно ввести модуль текстом.")
        return
    await context.update_data(reg_module=raw)
    monitor.add_event(
        "registration_fields_done",
        user_id=uid,
        chat_id=cid,
        detail="→ main (default anon)",
    )
    await _finalize_registration_to_main(
        event.message.bot,
        _chat_kw(event.message),
        context,
        uid,
        cid,
    )


@dp.message_created(Flow.pick_suggestion_privacy)
async def on_pick_suggestion_noise(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    await event.message.answer(
        "Выберите для этой заявки кнопками ниже.",
        attachments=[submission_privacy_keyboard("suggest")],
    )


@dp.message_created(Flow.pick_complaint_privacy)
async def on_pick_complaint_noise(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    await event.message.answer(
        "Выберите для этого обращения кнопками ниже.",
        attachments=[submission_privacy_keyboard("complaint")],
    )


@dp.message_created(Flow.edit_fio)
async def on_edit_fio(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    uid = event.message.sender.user_id
    raw = (event.message.body.text or "").strip()
    if not raw:
        await event.message.answer("Введите новое ФИО.", attachments=[cancel_only_keyboard()])
        return
    row_before = await db_run(storage.get_user, uid)
    old = _short((row_before.get("full_name") if row_before else "") or "")
    await db_run(storage.update_full_name, uid, raw)
    row_after = await db_run(storage.get_user, uid)
    await context.set_state(Flow.settings)
    new_s = _short(raw)
    note = (
        f"Изменено ФИО: «{old}» → «{new_s}»" if old != new_s else None
    )
    await event.message.answer(
        _settings_screen_text(row_after, moment_note=note),
        attachments=[settings_keyboard()],
    )


@dp.message_created(Flow.edit_dept)
async def on_edit_dept(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    uid = event.message.sender.user_id
    raw = (event.message.body.text or "").strip()
    if not raw:
        await event.message.answer(
            "Введите подразделение.", attachments=[cancel_only_keyboard()]
        )
        return
    row_before = await db_run(storage.get_user, uid)
    old = _short((row_before.get("department") if row_before else "") or "")
    await db_run(storage.update_department, uid, raw)
    row_after = await db_run(storage.get_user, uid)
    await context.set_state(Flow.settings)
    new_s = _short(raw)
    note = (
        f"Изменено подразделение: «{old}» → «{new_s}»"
        if old != new_s
        else None
    )
    await event.message.answer(
        _settings_screen_text(row_after, moment_note=note),
        attachments=[settings_keyboard()],
    )


@dp.message_created(Flow.edit_module)
async def on_edit_module(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    uid = event.message.sender.user_id
    raw = (event.message.body.text or "").strip()
    if not raw:
        await event.message.answer("Введите модуль.", attachments=[cancel_only_keyboard()])
        return
    row_before = await db_run(storage.get_user, uid)
    old = _short((row_before.get("module") if row_before else "") or "")
    await db_run(storage.update_module, uid, raw)
    row_after = await db_run(storage.get_user, uid)
    await context.set_state(Flow.settings)
    new_s = _short(raw)
    note = (
        f"Изменён модуль: «{old}» → «{new_s}»" if old != new_s else None
    )
    await event.message.answer(
        _settings_screen_text(row_after, moment_note=note),
        attachments=[settings_keyboard()],
    )


@dp.message_created(Flow.settings)
async def on_settings_chatter(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    uid = event.message.sender.user_id
    row = await db_run(storage.get_user, uid)
    text = (
        _settings_screen_text(row)
        if row
        else "Настройки профиля.\n\nСначала пройдите регистрацию: /start"
    )
    await event.message.answer(
        text,
        attachments=[settings_keyboard()],
    )


@dp.message_created(Flow.main)
async def on_main_chatter(event: MessageCreated, context: MemoryContext) -> None:
    if event.message.sender.is_bot:
        return
    uid = event.message.sender.user_id
    cid = event.message.recipient.chat_id
    await _touch_chat(uid, cid)
    monitor.add_event(
        "main_extra_text",
        user_id=uid,
        chat_id=cid,
        detail=(event.message.body.text or "")[:80],
    )
    row = await db_run(storage.get_user, uid)
    if row:
        await event.message.answer(
            _main_menu_text(row),
            parse_mode=ParseMode.HTML,
            attachments=[main_menu_keyboard()],
        )
    else:
        await event.message.answer(
            "Вы ещё не зарегистрированы. Нажмите /start или пройдите регистрацию.",
            attachments=[start_keyboard()],
        )


async def _log_bot_address() -> None:
    try:
        me = await bot.get_me()
    except Exception as exc:
        log.warning("get_me: %s", exc)
        return
    slug = (me.username or "").lstrip("@")
    log.info("бот: %s id=%s @%s", me.full_name, me.user_id, slug or "—")
    if slug:
        log.info("https://max.ru/%s | max://max.ru/%s", slug, slug)
    monitor.add_debug(f"бот подключён: {me.full_name} id={me.user_id} @{slug or 'без username'}")


async def _register_menu_commands() -> None:
    try:
        await bot.set_my_commands(
            BotCommand(
                name="start",
                description="Открыть бот жалоб и предложений",
            ),
        )
    except Exception as exc:
        log.warning("set_my_commands: %s", exc)


async def main() -> None:
    full_k = (
        os.environ.get("MONITOR_SECRET_FULL") or os.environ.get("MONITOR_SECRET") or ""
    ).strip()
    red_k = (os.environ.get("MONITOR_SECRET_REDACT") or "").strip()
    if not full_k and not red_k:
        log.warning(
            "Монитор без паролей: задайте MONITOR_SECRET_FULL / MONITOR_SECRET и/или "
            "MONITOR_SECRET_REDACT — иначе API дашборда вернёт 503."
        )
    log.info("старт: polling + монитор :%s DB=%s", MONITOR_PORT, DATABASE_PATH)
    await start_monitor_http(
        monitor, MONITOR_HOST, MONITOR_PORT, bot=bot, storage=storage
    )
    await _log_bot_address()
    await _register_menu_commands()
    await bot.delete_webhook()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
