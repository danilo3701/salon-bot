"""
app/bot/handlers/master.py  — v13

Панель мастера. Все переходы через edit_or_reply (плавно, без добавления сообщений).
Календарь унифицирован с клиентом (build_calendar_buttons).
Расписание — аккуратная сетка 2 кнопки в ряд.
"""
from __future__ import annotations

import logging
import re
import types
from datetime import date as dt_date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

from app.bot.helpers import (
    set_step,
    K_BAN, K_BOOKING, K_CLIENT, K_DISPATCH, K_NOTE, K_PORTFOLIO, K_REVIEW, K_SERVICE,
    back_master, build_calendar_buttons, build_calendar_grid_master, edit_or_reply, fmt_booking, kb,
    plural, safe_reply, safe_send, stars, svc, uid_uname,
)
from app.core.settings import settings
from app.core.money import format_eur, parse_eur_input_to_cents
from app.core.time_utils import fmt_date, fmt_slot, local_today
from app.models.domain import BookingStatus

logger = logging.getLogger("salon.master")

_SIGN = f"\n\n— {settings.MASTER_USERNAME}"

_PRICE_HELP = (
    "ℹ️ <b>Как вводить цену в EUR</b>\n"
    "• Целое число: <code>25</code>\n"
    "• С центами через точку: <code>25.50</code>\n"
    "• С центами через запятую: <code>25,50</code>\n\n"
    "Можно добавить сразу несколько услуг одной строкой:\n"
    "<code>Маникюр 25; Гель-лак 35.50; Наращивание 40</code>"
)

REQ_FILTER_TODAY = "today"
REQ_FILTER_TOMORROW = "tomorrow"
REQ_FILTER_WEEK = "week"
REQ_FILTER_ALL = "all"
REQ_PAGE_SIZE = 8


def _parse_services_batch(raw: str) -> tuple[list[tuple[str, int]] | None, str]:
    parts = [p.strip() for p in (raw or "").split(";") if p.strip()]
    if not parts:
        return None, "Введите услугу в формате: Название 25 или несколько через `;`."
    out: list[tuple[str, int]] = []
    for i, part in enumerate(parts, start=1):
        m = re.match(r"^(.+?)\s+(\d+(?:[.,]\d{1,2})?)$", part)
        if not m:
            return None, f"Позиция {i}: используйте формат `Название 25.50`."
        name = m.group(1).strip()
        cents = parse_eur_input_to_cents(m.group(2))
        if not name or len(name) > 80 or cents is None:
            return None, f"Позиция {i}: проверьте название и цену."
        out.append((name, cents))
    return out, ""


def register_steps(dispatcher):
    dispatcher.register("master_note",    _step_note)
    dispatcher.register("import_clients", _step_import_clients)
    dispatcher.register("srv_add_name",   _step_srv_add_name)
    dispatcher.register("srv_add_price",  _step_srv_add_price)
    dispatcher.register("srv_edit_price", _step_srv_edit_price)
    dispatcher.register("srv_rename",     _step_srv_rename)
    dispatcher.register("profile_bio",    _step_profile_bio)
    dispatcher.register("profile_contact", _step_profile_contact)
    dispatcher.register("profile_photo",  _step_profile_photo)
    dispatcher.register("address_text",   _step_address_text)
    dispatcher.register("address_google", _step_address_google)
    dispatcher.register("address_apple",  _step_address_apple)
    dispatcher.register("address_extra",  _step_address_extra)
    dispatcher.register("address_photo",  _step_address_photo)
    dispatcher.register("slot_add_start", _step_slot_start)
    dispatcher.register("slot_add_end",   _step_slot_end)
    dispatcher.register("ads_add_time_input", _step_ads_add_time_input)
    dispatcher.register("ads_add_period_input", _step_ads_add_period_input)
    dispatcher.register("portfolio_add",  _step_portfolio_add)


K_TIMESLOT = "svc_timeslot"
K_WEEKLY_SCHEDULE = "svc_weekly_schedule"


def _tsvc(context):
    return svc(context, K_TIMESLOT)


def _wsvc(context):
    return svc(context, K_WEEKLY_SCHEDULE)


_WD_SHORT = {
    1: "Пн",
    2: "Вт",
    3: "Ср",
    4: "Чт",
    5: "Пт",
    6: "Сб",
    7: "Вс",
}
_WD_FULL = {
    1: "ПОНЕДЕЛЬНИК",
    2: "ВТОРНИК",
    3: "СРЕДА",
    4: "ЧЕТВЕРГ",
    5: "ПЯТНИЦА",
    6: "СУББОТА",
    7: "ВОСКРЕСЕНЬЕ",
}


def _cb_ads_root() -> str:
    return "ads:root"


def _cb_ads_wd(weekday: int) -> str:
    return f"ads:wd:{weekday}"


def _cb_ads_add(weekday: int) -> str:
    return f"ads:add:{weekday}"


def _cb_ads_rm(weekday: int, hhmm: str) -> str:
    return f"ads:rm:{weekday}:{hhmm.replace(':', '')}"


def _cb_ads_rmok(weekday: int, hhmm: str) -> str:
    return f"ads:rmok:{weekday}:{hhmm.replace(':', '')}"


def _cb_ads_day(weekday: int, is_open: bool) -> str:
    return f"ads:day:{weekday}:{1 if is_open else 0}"


def _cb_ads_dayok(weekday: int, is_open: bool, ok: bool) -> str:
    return f"ads:dayok:{weekday}:{1 if is_open else 0}:{1 if ok else 0}"


def _cb_ads_per(weekday: int, is_closed: bool) -> str:
    return f"ads:per:{weekday}:{1 if is_closed else 0}"


def _cb_ads_copy(weekday: int) -> str:
    return f"ads:copy:{weekday}"


def _cb_ads_copyto(source_weekday: int, target_weekday: int) -> str:
    return f"ads:copyto:{source_weekday}:{target_weekday}"


def _from_hhmm_token(token: str) -> str | None:
    if not token or not token.isdigit() or len(token) != 4:
        return None
    hh = int(token[:2])
    mm = int(token[2:])
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


def _normalize_hhmm(raw: str) -> str | None:
    text = (raw or "").strip().replace(".", ":")
    if text and text.count(":") == 1:
        a, b = text.split(":")
        if a.isdigit() and b.isdigit():
            text = f"{int(a):02d}:{int(b):02d}"
    if not re.match(r"^\d{2}:\d{2}$", text):
        return None
    hh = int(text[:2])
    mm = int(text[3:])
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return text


def _normalize_period(raw: str) -> tuple[str, str] | None:
    text = (raw or "").strip().replace("–", "-").replace("—", "-")
    if "-" not in text:
        return None
    left, right = text.split("-", 1)
    start = _normalize_hhmm(left)
    end = _normalize_hhmm(right)
    if not start or not end or start >= end:
        return None
    return start, end


def _is_filled(value: str) -> str:
    return "✅ Заполнен" if (value or "").strip() else "❌ Не задан"


def _is_photo_filled(value: str) -> str:
    return "✅ Загружено" if (value or "").strip() else "❌ Не загружено"


def _normalize_profile_text(update: Update) -> str:
    return ((update.message.text or "") if update.message else "").strip()


def _save_runtime_text(key: str, value: str):
    settings.set_runtime_value(key, "" if value.lower() == "удалить" else value)


def _is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in settings.ADMIN_IDS)


async def cmd_admin(update: Update, context: CallbackContext):
    if not _is_admin(update):
        if update.message:
            await safe_reply(update, "Команда недоступна.")
        return
    set_step(context.user_data, None)
    await show_master_menu(update, context)


def _request_filter_match(booking, filter_type: str, today_iso: str) -> bool:
    if booking.status != BookingStatus.ACTIVE:
        return False
    if booking.date < today_iso:
        return False
    d = dt_date.fromisoformat(booking.date)
    today = dt_date.fromisoformat(today_iso)
    if filter_type == REQ_FILTER_TODAY:
        return d == today
    if filter_type == REQ_FILTER_TOMORROW:
        return d == (today + timedelta(days=1))
    if filter_type == REQ_FILTER_WEEK:
        start = today
        end = today + timedelta(days=6)
        return start <= d <= end
    return True


def _request_status_label(booking) -> str:
    if booking.confirmed_by_master:
        return "✅ Подтверждена"
    return "🕐 Ожидает подтверждения"


def _event_label(event_type: str) -> str:
    return {
        "created": "Создана",
        "rescheduled": "Перенос",
        "cancelled": "Отмена",
        "confirmed_by_master": "Подтверждена мастером",
    }.get(event_type, event_type)


def _requests_counts(rows: list, today_iso: str) -> dict[str, int]:
    return {
        REQ_FILTER_TODAY: sum(1 for b in rows if _request_filter_match(b, REQ_FILTER_TODAY, today_iso)),
        REQ_FILTER_TOMORROW: sum(1 for b in rows if _request_filter_match(b, REQ_FILTER_TOMORROW, today_iso)),
        REQ_FILTER_WEEK: sum(1 for b in rows if _request_filter_match(b, REQ_FILTER_WEEK, today_iso)),
        REQ_FILTER_ALL: sum(1 for b in rows if _request_filter_match(b, REQ_FILTER_ALL, today_iso)),
    }


# ════════════════════════════════════════════════════════════════════════════════
# ГЛАВНОЕ МЕНЮ МАСТЕРА
# ════════════════════════════════════════════════════════════════════════════════

async def show_master_menu(update: Update, context: CallbackContext):
    if not _is_admin(update):
        if update.callback_query:
            await update.callback_query.answer("Недостаточно прав.", show_alert=True)
        elif update.message:
            await safe_reply(update, "Команда недоступна.")
        return
    if update.callback_query:
        await update.callback_query.answer()
    from app.core.time_utils import local_now
    now      = local_now()
    _months = ["января","февраля","марта","апреля","мая","июня",
                 "июля","августа","сентября","октября","ноября","декабря"]
    now_str  = f"{now.day} {_months[now.month-1]}, {now.strftime('%H:%M')}"
    bookings = svc(context, K_BOOKING).future_active()
    today_iso = local_today().isoformat()
    active_future = [b for b in bookings if b.status == BookingStatus.ACTIVE and b.date >= today_iso]
    active_future.sort(key=lambda x: (x.date, x.time))
    today_rows = [b for b in active_future if b.date == today_iso]
    nearest = today_rows[0] if today_rows else (active_future[0] if active_future else None)
    next_line = "⏭ <b>Ближайшая запись:</b> —"
    if nearest:
        client = svc(context, K_CLIENT).get(nearest.user_id)
        cname = client.display_name if client else "Клиент"
        cphone = client.phone if client else "—"
        day_label = "Сегодня" if nearest.date == today_iso else fmt_date(nearest.date)
        next_line = (
            f"⏭ <b>Ближайшая запись:</b>\n"
            f"{day_label}, {fmt_slot(nearest.time)} · {nearest.service}\n"
            f"👤 {cname} · 📞 {cphone}"
        )

    # Убираем ведущий ноль у числа месяца
    await edit_or_reply(
        update,
        f"👑 <b>Панель мастера</b>\n📅 {now_str}\n\n{next_line}",
        kb([
            (f"📋 Мои записи ({len(active_future)})", "adm_requests"),
            ("📋 Сегодня",       "adm_today"),
            ("📅 Календарь",     "adm_calendar"),
            ("👥 Клиенты",       "adm_crm"),
            ("⭐ Отзывы",        "adm_reviews"),
            ("📊 Статистика",    "adm_stats"),
            ("💅 Услуги",        "adm_services"),
            ("⏰ Расписание",    "adm_schedule"),
            ("📸 Портфолио",     "adm_portfolio"),
            ("👩‍🎨 О мастере",     "adm_about"),
            ("📨 Пригласить",    "adm_invite"),
            ("🚫 Чёрный список", "adm_blacklist"),
        ], cols=2),
    )


# ════════════════════════════════════════════════════════════════════════════════
# ЗАПИСИ СЕГОДНЯ
# ════════════════════════════════════════════════════════════════════════════════

async def adm_today(update: Update, context: CallbackContext):
    """Записи на сегодня: список текстом + кнопка на каждую запись. Только кнопка Назад."""
    await update.callback_query.answer()
    today_str = local_today().isoformat()
    rows      = svc(context, K_BOOKING).by_date(today_str)

    if not rows:
        await edit_or_reply(update,
            f"📋 <b>Сегодня, {fmt_date(today_str)}</b>\n\nЗаписей нет.",
            kb([("◀️ Назад", "master_menu")]))
        return

    lines = [f"📋 <b>Сегодня, {fmt_date(today_str)}</b>\n"]
    buttons = []
    for b in rows:
        client = svc(context, K_CLIENT).get(b.user_id)
        name   = client.display_name if client else "—"
        phone  = client.phone        if client else "—"
        lines.append(
            f"⏰ <b>{fmt_slot(b.time)}</b>  👤 {name}  📞 {phone}\n"
            f"   💅 {b.service}\n"
        )
        buttons.append((f"⏰ {fmt_slot(b.time)} — {name}", f"adm_cancel_{b.id}"))

    await edit_or_reply(update,
        "\n".join(lines),
        kb(buttons + [("◀️ Назад", "master_menu")]))


async def adm_requests(update: Update, context: CallbackContext):
    if not _is_admin(update):
        await update.callback_query.answer("Недостаточно прав.", show_alert=True)
        return
    await update.callback_query.answer()
    await _render_requests(update, context, REQ_FILTER_ALL, 1)


async def adm_requests_filter(update: Update, context: CallbackContext):
    if not _is_admin(update):
        await update.callback_query.answer("Недостаточно прав.", show_alert=True)
        return
    await update.callback_query.answer()
    filter_type = update.callback_query.data.replace("adm_req_filter_", "")
    await _render_requests(update, context, filter_type, 1)


async def adm_requests_page(update: Update, context: CallbackContext):
    if not _is_admin(update):
        await update.callback_query.answer("Недостаточно прав.", show_alert=True)
        return
    await update.callback_query.answer()
    payload = update.callback_query.data.replace("adm_req_page_", "")
    if payload == "noop":
        return
    filter_type, page_raw = payload.split("_", 1)
    await _render_requests(update, context, filter_type, int(page_raw))


async def _render_requests(update: Update, context: CallbackContext, filter_type: str, page: int):
    rows = svc(context, K_BOOKING).future_active()
    today_iso = local_today().isoformat()
    rows = [b for b in rows if _request_filter_match(b, REQ_FILTER_ALL, today_iso)]
    rows.sort(key=lambda x: (x.date, x.time))
    counts = _requests_counts(rows, today_iso)
    filtered = [b for b in rows if _request_filter_match(b, filter_type, today_iso)]

    total = len(filtered)
    total_pages = max(1, (total + REQ_PAGE_SIZE - 1) // REQ_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * REQ_PAGE_SIZE
    page_items = filtered[start:start + REQ_PAGE_SIZE]

    lines = ["💼 <b>Мои записи</b>", ""]
    if not page_items:
        lines.append("По выбранному фильтру записей нет.")
    else:
        for b in page_items:
            lines.append(
                f"#{b.id} · {_request_status_label(b)} · "
                f"{'Сегодня' if b.date == today_iso else fmt_date(b.date)} {fmt_slot(b.time)}"
            )
    text = "\n".join(lines)

    def _active(ft: str, label: str) -> str:
        return f"✅ {label}" if filter_type == ft else label

    grid = []
    for b in page_items:
        grid.append((f"#{b.id}", f"adm_req_open_{b.id}"))

    nav = []
    if page > 1:
        nav.append(("◀️", f"adm_req_page_{filter_type}_{page - 1}"))
    nav.append((f"·{page}/{total_pages}·", "adm_req_page_noop"))
    if page < total_pages:
        nav.append(("▶️", f"adm_req_page_{filter_type}_{page + 1}"))

    buttons = [
        (_active(REQ_FILTER_TODAY, f"📆 Сегодня ({counts[REQ_FILTER_TODAY]})"), "adm_req_filter_today"),
        (_active(REQ_FILTER_TOMORROW, f"📆 Завтра ({counts[REQ_FILTER_TOMORROW]})"), "adm_req_filter_tomorrow"),
        (_active(REQ_FILTER_WEEK, f"📆 Эта неделя ({counts[REQ_FILTER_WEEK]})"), "adm_req_filter_week"),
        (_active(REQ_FILTER_ALL, "📁 Все"), "adm_req_filter_all"),
    ]
    buttons.extend(grid)
    buttons.extend(nav)
    buttons.append(("← Назад", "master_menu"))

    await edit_or_reply(update, text, kb(buttons, cols=2))


async def adm_request_open(update: Update, context: CallbackContext):
    if not _is_admin(update):
        await update.callback_query.answer("Недостаточно прав.", show_alert=True)
        return
    await update.callback_query.answer()
    bid = int(update.callback_query.data.replace("adm_req_open_", ""))
    b = svc(context, K_BOOKING).get(bid)
    if not b:
        await edit_or_reply(update, "Запись не найдена.", kb([("← К списку", "adm_requests")]))
        return
    client = svc(context, K_CLIENT).get(b.user_id)
    name = client.display_name if client else "Клиент"
    phone = client.phone if client else "—"

    card = svc(context, K_CLIENT).card(b.user_id)
    last_visits = []
    if card:
        today = local_today().isoformat()
        past = [
            x for x in card.bookings
            if x.status in (BookingStatus.ACTIVE, BookingStatus.COMPLETED) and x.date < today
        ]
        past.sort(key=lambda x: (x.date, x.time), reverse=True)
        last_visits = past[:3]

    lines = [
        f"📥 <b>Запись #{b.id}</b>",
        f"🗓 {fmt_date(b.date)}, {fmt_slot(b.time)}",
        f"💅 {b.service}",
        f"👤 {name}",
        f"📞 {phone}",
        f"Статус: {_request_status_label(b)}",
    ]
    if last_visits:
        lines.append("")
        lines.append("<b>Последние визиты:</b>")
        for v in last_visits:
            lines.append(f"• {fmt_date(v.date)} — {v.service}")

    events = svc(context, K_BOOKING).events(b.id, limit=6)
    if events:
        lines.append("")
        lines.append("<b>Журнал:</b>")
        for ev in reversed(events):
            dt = ev.created_at.strftime("%d.%m %H:%M") if ev.created_at else "—"
            payload = f" · {ev.payload}" if ev.payload else ""
            lines.append(f"• {dt} — {_event_label(ev.event_type)}{payload}")

    await edit_or_reply(
        update,
        "\n".join(lines),
        kb([
            ("✅ Подтвердить", f"adm_req_confirm_{b.id}"),
            ("🔄 Перенести", f"reschedule_{b.id}"),
            ("👤 Карточка клиента", f"crm_{b.user_id}"),
            ("← К списку", "adm_requests"),
            ("🏠 Меню", "master_menu"),
        ], cols=2),
    )


async def adm_request_confirm(update: Update, context: CallbackContext):
    if not _is_admin(update):
        await update.callback_query.answer("Недостаточно прав.", show_alert=True)
        return
    await update.callback_query.answer()
    bid = int(update.callback_query.data.replace("adm_req_confirm_", ""))
    result = svc(context, K_BOOKING).confirm_by_master(bid)
    if not result.ok:
        await edit_or_reply(update, f"⚠️ {result.error}", kb([("← К списку", "adm_requests")]))
        return
    b = result.booking
    await safe_send(
        context.bot,
        b.user_id,
        f"✅ Мастер подтвердил вашу запись.\n\n{fmt_booking(b)}{_SIGN}",
        kb([("📋 Мои записи", "my_bookings")]),
    )
    update.callback_query.data = f"adm_req_open_{bid}"
    await adm_request_open(update, context)


async def _show_day(update, context, date_str: str,
                    back_label: str = "◀️ Календарь",
                    back_cb:    str = "adm_calendar"):
    day_info = svc(context, K_BOOKING).slots_for_day(date_str)
    rows = svc(context, K_BOOKING).by_date(date_str)
    booked_by_time = {b.time: b for b in rows}

    free_count = len([x for x in day_info["slots"] if x["status"] == "free"])
    blocked_count = len([x for x in day_info["slots"] if x["status"] == "blocked"])
    booked_count = len([x for x in day_info["slots"] if x["status"] == "booked"])

    lines = [
        f"📅 <b>{fmt_date(date_str)}</b>",
        "",
        f"🟢 Свободно: {free_count}  🔒 Закрыто: {blocked_count}  📌 Записей: {booked_count}",
        "",
    ]
    buttons: list[tuple[str, str]] = []

    for slot_info in day_info["slots"]:
        slot = slot_info["time"]
        status = slot_info["status"]
        booking = booked_by_time.get(slot)
        if status == "booked" and booking:
            client = svc(context, K_CLIENT).get(booking.user_id)
            name = client.display_name if client else "—"
            lines.append(f"📌 {slot} — {name}")
            buttons.append((f"❌ Отменить {slot}", f"adm_cancel_{booking.id}"))
        elif status == "blocked":
            lines.append(f"🔒 {slot} — закрыто")
            buttons.append((f"🔓 Открыть {slot}", f"adm_slot_on_{date_str}_{slot}"))
        else:
            lines.append(f"🟢 {slot} — свободно")
            buttons.append((f"🔒 Закрыть {slot}", f"adm_slot_off_{date_str}_{slot}"))

    if day_info["day_blocked"]:
        buttons.append(("✅ Открыть весь день", f"adm_day_open_{date_str}"))
    else:
        buttons.append(("🚫 Закрыть весь день", f"adm_day_close_{date_str}"))

    weekday = dt_date.fromisoformat(date_str).isoweekday()
    day_obj = dt_date.fromisoformat(date_str)
    prev_day = (day_obj - timedelta(days=1)).isoformat()
    next_day = (day_obj + timedelta(days=1)).isoformat()
    buttons.append(("🧩 Шаблон этого дня", _cb_ads_wd(weekday)))
    buttons.append(("◀️ Календарь", "adm_calendar"))
    buttons.append(("◀️ День-1", f"calnav_{prev_day}"))
    buttons.append(("День+1 ▶️", f"calnav_{next_day}"))

    await edit_or_reply(update, "\n".join(lines), kb(buttons, cols=2))


# ════════════════════════════════════════════════════════════════════════════════
# ОТМЕНА ЗАПИСИ МАСТЕРОМ
# ════════════════════════════════════════════════════════════════════════════════

async def adm_cancel_booking(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    bid    = int(update.callback_query.data.replace("adm_cancel_", ""))
    b      = svc(context, K_BOOKING).get(bid)
    if not b:
        await edit_or_reply(update, "Запись не найдена.", kb(back_master()))
        return
    client = svc(context, K_CLIENT).get(b.user_id)
    name   = client.display_name if client else "—"
    await edit_or_reply(update,
        f"Отменить запись <b>{name}</b>?\n\n{fmt_booking(b)}",
        kb([
            ("✅ Да, отменить", f"adm_cancel_yes_{bid}"),
            ("◀️ Назад",        f"calday_{b.date}"),
        ]))


async def adm_cancel_confirmed(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    bid    = int(update.callback_query.data.replace("adm_cancel_yes_", ""))
    uid, _ = uid_uname(update)
    result = svc(context, K_BOOKING).cancel(bid, uid)
    if not result.ok:
        await edit_or_reply(update, f"⚠️ {result.error}", kb(back_master()))
        return
    b      = result.booking
    client = svc(context, K_CLIENT).get(b.user_id)
    name   = client.display_name if client else "—"
    await edit_or_reply(update,
        f"✅ Запись {name} на {fmt_slot(b.time)} отменена.",
        kb([(f"◀️ {fmt_date(b.date)}", f"calday_{b.date}")] + back_master()))
    await safe_send(context.bot, b.user_id,
        f"❌ Ваша запись отменена мастером.\n\n"
        f"{fmt_booking(b)}\n\n"
        f"По вопросам: {settings.MASTER_USERNAME}",
        kb([("📅 Записаться заново", "book_service")]))


# ════════════════════════════════════════════════════════════════════════════════
# КАЛЕНДАРЬ МАСТЕРА — унифицирован с клиентом
# ════════════════════════════════════════════════════════════════════════════════

async def adm_calendar(update: Update, context: CallbackContext,
                       week_offset: int = 0):
    await update.callback_query.answer()

    calendar_all = svc(context, K_BOOKING).calendar(days=60)

    markup, week_header = build_calendar_grid_master(
        all_dates       = calendar_all,
        prefix          = "calday_",
        week_offset     = week_offset,
        week_nav_prefix = "cal_week_",
        back_buttons    = back_master(),
    )

    nl = chr(10)
    legend = (
        week_header + nl + nl
        + "🟢 свободен  🟡 частично  🔴 полный  🚫 закрыт" + nl + nl
        + "Нажмите на день для просмотра"
    )

    await edit_or_reply(update, legend, markup)


async def adm_calendar_week(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    offset = int(update.callback_query.data.replace("cal_week_", ""))
    await adm_calendar(update, context, week_offset=offset)


async def adm_calendar_day(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    date_str = update.callback_query.data.replace("calday_", "")
    await _show_day(update, context, date_str)


async def adm_calendar_day_nav(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    date_str = update.callback_query.data.replace("calnav_", "")
    await _show_day(update, context, date_str)


# ════════════════════════════════════════════════════════════════════════════════
# ПЕРЕНОС — мастер подтверждает / отклоняет
# ════════════════════════════════════════════════════════════════════════════════

async def adm_rconfirm(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    bid    = int(update.callback_query.data.replace("adm_rconfirm_", ""))
    result = svc(context, K_BOOKING).confirm_reschedule(bid)
    if not result.ok:
        await edit_or_reply(update, f"⚠️ {result.error}", kb(back_master()))
        return
    b = result.booking
    await edit_or_reply(update,
        f"✅ Перенос подтверждён: {fmt_date(b.date)} {fmt_slot(b.time)}",
        kb(back_master()))
    await safe_send(context.bot, b.user_id,
        f"✅ Мастер подтвердил перенос!\n\n{fmt_booking(b)}{_SIGN}")


async def adm_rdecline(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    bid = int(update.callback_query.data.replace("adm_rdecline_", ""))
    b   = svc(context, K_BOOKING).get(bid)
    svc(context, K_BOOKING).decline_reschedule(bid)
    await edit_or_reply(update, "❌ Перенос отклонён.", kb(back_master()))
    if b:
        await safe_send(context.bot, b.user_id,
            f"❌ Мастер не смог перенести запись.\n\n"
            f"Ваша запись остаётся: {fmt_date(b.date)} {fmt_slot(b.time)}\n"
            f"По вопросам: {settings.MASTER_USERNAME}")


# ════════════════════════════════════════════════════════════════════════════════
# БЛОКИРОВКА ДНЕЙ / СЛОТОВ
# ════════════════════════════════════════════════════════════════════════════════

async def adm_day_close(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    date_str = update.callback_query.data.replace("adm_day_close_", "")
    svc(context, K_BOOKING).block_day(date_str)
    await _show_day(update, context, date_str)


async def adm_day_open(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    date_str = update.callback_query.data.replace("adm_day_open_", "")
    svc(context, K_BOOKING).unblock_day(date_str)
    await _show_day(update, context, date_str)


async def adm_slot_off(update: Update, context: CallbackContext):
    """Закрыть конкретный слот на конкретный день."""
    await update.callback_query.answer()
    # callback: adm_slot_off_2099-10-01_09:00
    data     = update.callback_query.data.replace("adm_slot_off_", "")
    date_str = data[:10]
    time_str = data[11:]
    svc(context, K_BOOKING).block_slot(date_str, time_str)
    await _show_day(update, context, date_str)


async def adm_slot_on(update: Update, context: CallbackContext):
    """Открыть конкретный слот на конкретный день."""
    await update.callback_query.answer()
    data     = update.callback_query.data.replace("adm_slot_on_", "")
    date_str = data[:10]
    time_str = data[11:]
    svc(context, K_BOOKING).unblock_slot(date_str, time_str)
    await _show_day(update, context, date_str)


# Обратная совместимость (старые callback из кэша кнопок)
async def adm_block(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    await show_master_menu(update, context)

async def adm_unblock(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    await show_master_menu(update, context)

async def cb_block_day(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    date_str = update.callback_query.data.replace("block_", "")
    svc(context, K_BOOKING).block_day(date_str)
    await _show_day(update, context, date_str)

async def cb_unblock(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    date_str = update.callback_query.data.replace("unblock_", "")
    svc(context, K_BOOKING).unblock_day(date_str)
    await _show_day(update, context, date_str)


# ════════════════════════════════════════════════════════════════════════════════
# CRM — КЛИЕНТЫ
# ════════════════════════════════════════════════════════════════════════════════

async def adm_crm(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    clients = svc(context, K_CLIENT).all_clients()
    count   = len(clients)
    buttons = [
        (f"👤 {c.display_name}  ·  {c.phone or '—'}", f"crm_{c.user_id}")
        for c in clients
    ]
    header = f"👥 <b>Клиенты ({count}):</b>" if count else "👥 <b>Клиентов пока нет</b>"
    mgmt   = [
        ("📤 Экспорт", "adm_export_clients"),
        ("📥 Импорт",  "adm_import_clients"),
    ]
    await edit_or_reply(update, header,
        kb(buttons + mgmt + back_master(), cols=1))


async def cb_crm_client(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    target_uid = int(update.callback_query.data.replace("crm_", ""))
    card       = svc(context, K_CLIENT).card(target_uid)
    if not card:
        await edit_or_reply(update, "Клиент не найден.", kb(back_master()))
        return

    c     = card.client
    today = local_today().isoformat()

    upcoming  = [b for b in card.bookings if b.status == BookingStatus.ACTIVE and b.date >= today]
    past      = [b for b in card.bookings
                 if b.status in (BookingStatus.ACTIVE, BookingStatus.COMPLETED) and b.date < today]
    cancelled = [b for b in card.bookings if b.status == BookingStatus.CANCELLED]

    text = (
        f"👤 <b>{c.display_name}</b>\n"
        f"📞 {c.phone}  |  {c.username or '—'}\n\n"
        f"Визитов: <b>{card.visits}</b>  ·  "
        f"Потрачено: <b>{format_eur(card.total_spent)}</b>\n"
        f"Предстоящих: <b>{card.upcoming}</b>  ·  "
        f"Отмен: {len(cancelled)}\n"
        f"Последний визит: {fmt_date(card.last_visit) if card.last_visit else '—'}\n"
    )
    if upcoming:
        text += "\n<b>📅 Предстоящие:</b>\n"
        for b in upcoming:
            text += f"  {fmt_date(b.date)} {fmt_slot(b.time)} — {b.service}\n"
    if past:
        text += "\n<b>📋 Последние визиты:</b>\n"
        for b in sorted(past, key=lambda x: x.date, reverse=True)[:5]:
            text += f"  {fmt_date(b.date)} — {b.service} ({format_eur(b.price)})\n"
    if card.notes:
        text += "\n<b>📝 Заметки:</b>\n"
        for n in card.notes[:3]:
            d = n.created_at.strftime("%d.%m.%Y") if n.created_at else "—"
            text += f"  [{d}] {n.text}\n"
    if card.reviews:
        text += "\n<b>⭐ Отзывы:</b>\n"
        for r in card.reviews[:3]:
            d = r.created_at.strftime("%d.%m.%Y") if r.created_at else "—"
            text += f"  {stars(r.rating)} {d}"
            if r.text:
                text += f" — {r.text[:60]}"
            text += "\n"

    is_banned = svc(context, K_BAN).is_banned(target_uid)
    ban_btn   = ("✅ Разблокировать", f"adm_unban_{target_uid}") if is_banned \
                else ("🚫 В чёрный список", f"adm_ban_{target_uid}")

    await edit_or_reply(update, text,
        kb([
            ("✏️ Заметка",         f"note_{target_uid}"),
            ban_btn,
            ("🗑 Удалить клиента", f"adm_delete_client_{target_uid}"),
            ("◀️ К клиентам",      "adm_crm"),
        ], cols=2))


async def cb_note_start(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    target = int(update.callback_query.data.replace("note_", ""))
    context.user_data["note_target"] = target
    set_step(context.user_data, "master_note")
    await edit_or_reply(update, "✏️ Введите заметку о клиенте:")


async def _step_note(update: Update, context: CallbackContext):
    target    = context.user_data.pop("note_target", None)
    author, _ = uid_uname(update)
    set_step(context.user_data, None)
    if not target:
        return
    svc(context, K_NOTE).add(target, author, update.message.text.strip())
    await update.message.reply_text(
        "✅ Заметка сохранена.",
        reply_markup=kb([(f"◀️ Карточка клиента", f"crm_{target}")] + back_master()))


# ════════════════════════════════════════════════════════════════════════════════
# ОТЗЫВЫ
# ════════════════════════════════════════════════════════════════════════════════

async def adm_reviews(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    reviews = svc(context, K_REVIEW).recent(20)
    if not reviews:
        await edit_or_reply(update, "Отзывов пока нет.", kb(back_master()))
        return
    text = f"⭐ <b>Последние отзывы ({len(reviews)}):</b>\n\n"
    for r in reviews:
        client = svc(context, K_CLIENT).get(r.user_id)
        name   = client.display_name if client else "Клиент"
        d      = r.created_at.strftime("%d.%m.%Y") if r.created_at else "—"
        text  += f"{stars(r.rating)}  <b>{name}</b>  <i>{d}</i>\n"
        if r.text:
            text += f"💬 {r.text}\n"
        if r.photo_file_id:
            text += "📸 фото прикреплено\n"
        text += "\n"
        if len(text) > 3500:
            text += "..."
            break
    await edit_or_reply(update, text, kb(back_master()))


# ════════════════════════════════════════════════════════════════════════════════
# СТАТИСТИКА
# ════════════════════════════════════════════════════════════════════════════════

async def adm_stats(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    s   = svc(context, K_BOOKING).stats()
    avg = f"{stars(round(s.avg_rating))} {s.avg_rating}" if s.avg_rating else "нет отзывов"
    await edit_or_reply(update,
        "\n".join(filter(None, [
            "📊 <b>Статистика</b>",
            "",
            f"👥 Клиентов: <b>{s.unique_clients}</b>",
            f"📋 Визитов всего: <b>{s.total_bookings}</b>",
            f"💰 Выручка: <b>{format_eur(s.total_revenue)}</b>",
            (f"💵 Средний чек: <b>{format_eur(s.total_revenue // s.total_bookings)}</b>"
             if s.total_bookings else None),
            f"❌ Отмен: {s.cancelled}",
            "",
            f"📅 <b>{s.month_label}:</b>",
            f"   Записей: {s.month_bookings}",
            f"   Выручка: {format_eur(s.month_revenue)}",
            "",
            f"⭐ Средняя оценка: {avg}",
        ])),
        kb(back_master()))


# ════════════════════════════════════════════════════════════════════════════════
# УСЛУГИ
# ════════════════════════════════════════════════════════════════════════════════

async def adm_services(update: Update, context: CallbackContext):
    if update.callback_query:
        await update.callback_query.answer()
    services = svc(context, K_SERVICE).all()
    if not services:
        await edit_or_reply(update,
            "💅 <b>Услуги</b>\n\nУслуг пока нет.",
            kb([("➕ Добавить услугу", "adm_srv_add")] + back_master()))
        return
    lines = ["💅 <b>Услуги</b>\n"]
    for name, price in services.items():
        lines.append(f"• {name} — <b>{format_eur(price)}</b>")
    text    = "\n".join(lines)
    buttons = [(f"✏️ {name}", f"adm_srv_edit_{name}") for name in services]
    buttons.append(("➕ Добавить услугу", "adm_srv_add"))
    await edit_or_reply(update, text, kb(buttons + back_master()))


async def adm_srv_edit(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    name     = update.callback_query.data.replace("adm_srv_edit_", "", 1)
    services = svc(context, K_SERVICE).all()
    price    = services.get(name)
    if price is None:
        await edit_or_reply(update, "Услуга не найдена.", kb(back_master()))
        return
    await edit_or_reply(update,
        f"✏️ <b>{name}</b> — {format_eur(price)}\n\nЧто изменить?",
        kb([
            ("💰 Изменить цену", f"adm_srv_price_{name}"),
            ("ℹ️ Пример ввода", "adm_srv_help"),
            ("🔤 Переименовать", f"adm_srv_rename_{name}"),
            ("🗑 Удалить",       f"adm_srv_delete_{name}"),
            ("◀️ К услугам",     "adm_services"),
        ]))


async def adm_srv_add(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "srv_add_name")
    await edit_or_reply(update,
        "➕ <b>Новая услуга</b>\n\n"
        "Введите услугу в формате <code>Название 25</code>\n"
        "или несколько услуг через <code>;</code>.\n\n"
        f"{_PRICE_HELP}",
        kb([("ℹ️ Пример ввода", "adm_srv_help"), ("◀️ Отмена", "adm_services")]))


async def _step_srv_add_name(update: Update, context: CallbackContext):
    raw = update.message.text.strip()
    items, err = _parse_services_batch(raw)
    if items is None:
        name = raw
        if not name or len(name) > 80:
            await update.message.reply_text("⚠️ Название должно быть от 1 до 80 символов.")
            return
        set_step(context.user_data, "srv_add_price")
        context.user_data["srv_new_name"] = name
        await update.message.reply_text(
            f"Услуга: <b>{name}</b>\n\nВведите цену в EUR.\n\n{_PRICE_HELP}",
            parse_mode="HTML",
        )
        return
    added: list[str] = []
    for name, cents in items:
        ok, msg = svc(context, K_SERVICE).add(name, cents)
        if not ok:
            await update.message.reply_text(f"⚠️ {msg}")
            return
        added.append(f"• {name} — <b>{format_eur(cents)}</b>")
    set_step(context.user_data, None)
    await update.message.reply_text(
        "✅ Услуги добавлены:\n" + "\n".join(added),
        parse_mode="HTML",
    )
    fake_update = types.SimpleNamespace(callback_query=None, message=update.message)
    await adm_services(fake_update, context)


async def _step_srv_add_price(update: Update, context: CallbackContext):
    set_step(context.user_data, None)
    name = context.user_data.pop("srv_new_name", None)
    if not name:
        return
    raw = update.message.text.strip()
    price = parse_eur_input_to_cents(raw)
    if price is None:
        await update.message.reply_text("⚠️ Неверный формат цены. Пример: 25, 25.50 или 25,50.")
        return
    ok, err = svc(context, K_SERVICE).add(name, price)
    if ok:
        await update.message.reply_text(
            f"✅ Услуга <b>{name}</b> — {format_eur(price)} добавлена.",
            parse_mode="HTML")
        fake_update = types.SimpleNamespace(callback_query=None, message=update.message)
        await adm_services(fake_update, context)
    else:
        await update.message.reply_text(f"⚠️ {err}")


async def adm_srv_price(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    name     = update.callback_query.data.replace("adm_srv_price_", "", 1)
    services = svc(context, K_SERVICE).all()
    current  = services.get(name)
    if current is None:
        await edit_or_reply(update, "Услуга не найдена.", kb(back_master()))
        return
    set_step(context.user_data, "srv_edit_price")
    context.user_data["srv_edit_name"] = name
    await edit_or_reply(update,
        f"💰 <b>{name}</b>\nТекущая цена: {format_eur(current)}\n\n"
        f"Введите новую цену в EUR.\n\n{_PRICE_HELP}",
        kb([("ℹ️ Пример ввода", "adm_srv_help"), ("◀️ Отмена", f"adm_srv_edit_{name}")]))


async def _step_srv_edit_price(update: Update, context: CallbackContext):
    set_step(context.user_data, None)
    name = context.user_data.pop("srv_edit_name", None)
    if not name:
        return
    raw = update.message.text.strip()
    price = parse_eur_input_to_cents(raw)
    if price is None:
        await update.message.reply_text("⚠️ Неверный формат цены. Пример: 25, 25.50 или 25,50.")
        return
    ok, err = svc(context, K_SERVICE).update_price(name, price)
    if ok:
        await update.message.reply_text(
            f"✅ Цена <b>{name}</b> обновлена: {format_eur(price)}.",
            parse_mode="HTML")
        fake_update = types.SimpleNamespace(callback_query=None, message=update.message)
        await adm_services(fake_update, context)
    else:
        await update.message.reply_text(f"⚠️ {err}")


async def adm_srv_help(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    await edit_or_reply(
        update,
        f"{_PRICE_HELP}",
        kb([("◀️ К услугам", "adm_services")]),
    )


async def adm_srv_rename(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    name = update.callback_query.data.replace("adm_srv_rename_", "", 1)
    set_step(context.user_data, "srv_rename")
    context.user_data["srv_rename_from"] = name
    await edit_or_reply(update,
        f"🔤 Переименовать <b>{name}</b>\n\nВведите новое название:",
        kb([("◀️ Отмена", f"adm_srv_edit_{name}")]))


async def _step_srv_rename(update: Update, context: CallbackContext):
    set_step(context.user_data, None)
    old_name = context.user_data.pop("srv_rename_from", None)
    if not old_name:
        return
    new_name = update.message.text.strip()
    if not new_name or len(new_name) > 80:
        await update.message.reply_text("⚠️ Некорректное название.")
        return
    ok, err = svc(context, K_SERVICE).rename(old_name, new_name)
    if ok:
        await update.message.reply_text(
            f"✅ Услуга переименована: <b>{old_name}</b> → <b>{new_name}</b>",
            reply_markup=kb([("◀️ К услугам", "adm_services")] + back_master()),
            parse_mode="HTML")
    else:
        await update.message.reply_text(f"⚠️ {err}")


async def adm_srv_delete(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    name = update.callback_query.data.replace("adm_srv_delete_", "", 1)
    await edit_or_reply(update,
        f"🗑 Удалить услугу <b>{name}</b>?\n\n"
        f"⚠️ Прошлые записи сохранятся, новые на эту услугу будут недоступны.",
        kb([
            ("✅ Да, удалить", f"adm_srv_delete_yes_{name}"),
            ("◀️ Отмена",      f"adm_srv_edit_{name}"),
        ]))


async def adm_srv_delete_yes(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    name   = update.callback_query.data.replace("adm_srv_delete_yes_", "", 1)
    ok, err = svc(context, K_SERVICE).delete(name)
    if ok:
        await edit_or_reply(update,
            f"✅ Услуга <b>{name}</b> удалена.",
            kb([("◀️ К услугам", "adm_services")] + back_master()))
    else:
        await edit_or_reply(update, f"⚠️ {err}",
            kb([("◀️ К услугам", "adm_services")] + back_master()))


# ════════════════════════════════════════════════════════════════════════════════
# РАСПИСАНИЕ — аккуратная сетка 2 кнопки в ряд (Вкл/Выкл + Удалить)
# ════════════════════════════════════════════════════════════════════════════════

async def adm_schedule(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    slots = _tsvc(context).all()
    if not slots:
        await edit_or_reply(update,
            "⏰ <b>Расписание</b>\n\nСлотов нет.\n\n"
            "Добавьте временные слоты — они будут доступны для записи каждый день.",
            kb([("➕ Добавить слот", "adm_sched_add")] + back_master()))
        return

    lines   = ["⏰ <b>Расписание рабочих слотов</b>\n"]
    buttons = []
    for start, end, active in slots:
        icon  = "✅" if active else "❌"
        lines.append(f"{icon} {start}")
        # Пара кнопок для каждого слота — выводим сеткой 2 в ряд через cols=2
        toggle_label = f"{'🔕 Выкл' if active else '🔔 Вкл'}  {start}"
        toggle_cb    = f"adm_sched_toggle_{'off' if active else 'on'}_{start}"
        buttons.append((toggle_label, toggle_cb))
        buttons.append((f"🗑 Удалить  {start}", f"adm_sched_del_{start}"))

    buttons.append(("➕ Добавить слот", "adm_sched_add"))
    await edit_or_reply(update,
        "\n".join(lines),
        # cols=2: каждая пара (Вкл/Выкл + Удалить) в одну строку
        kb(buttons + back_master(), cols=2))


async def adm_sched_add(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "slot_add_start")
    await edit_or_reply(update,
        "➕ <b>Новый слот</b>\n\nВведите время начала (ЧЧ:ММ, например <code>10:00</code>):",
        kb([("◀️ Отмена", "adm_schedule")]))


async def _step_slot_start(update: Update, context: CallbackContext):
    import re
    start = update.message.text.strip()
    if not re.match(r"^\d{2}:\d{2}$", start):
        await update.message.reply_text("⚠️ Формат: ЧЧ:ММ, например 10:00")
        return
    context.user_data["slot_start"] = start
    set_step(context.user_data, "slot_add_end")
    await update.message.reply_text(
        f"Начало: <b>{start}</b>\n\nВведите время окончания (ЧЧ:ММ):",
        parse_mode="HTML")


async def _step_slot_end(update: Update, context: CallbackContext):
    import re
    end   = update.message.text.strip()
    start = context.user_data.pop("slot_start", None)
    set_step(context.user_data, None)
    if not start:
        return
    if not re.match(r"^\d{2}:\d{2}$", end):
        await update.message.reply_text("⚠️ Формат: ЧЧ:ММ, например 12:00",
            reply_markup=kb([("◀️ К расписанию", "adm_schedule")]))
        return
    ok, err = _tsvc(context).add(start, end)
    if ok:
        await update.message.reply_text(
            f"✅ Слот <b>{start}–{end}</b> добавлен.",
            reply_markup=kb([("◀️ К расписанию", "adm_schedule")] + back_master()),
            parse_mode="HTML")
    else:
        await update.message.reply_text(f"⚠️ {err}",
            reply_markup=kb([("◀️ К расписанию", "adm_schedule")]))


async def adm_sched_toggle(update: Update, context: CallbackContext):
    """Вкл/выкл глобального слота из расписания."""
    await update.callback_query.answer()
    data = update.callback_query.data  # adm_sched_toggle_on_HH:MM / _off_HH:MM
    on   = "_on_" in data
    # Убираем префикс adm_sched_toggle_on_ или adm_sched_toggle_off_
    start = data.replace("adm_sched_toggle_on_", "").replace("adm_sched_toggle_off_", "")
    _tsvc(context).toggle(start, on)
    # Возвращаемся в расписание плавно
    await adm_schedule(update, context)


async def adm_sched_delete(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    start = update.callback_query.data.replace("adm_sched_del_", "")
    await edit_or_reply(update,
        f"🗑 Удалить слот <b>{start}</b>?",
        kb([
            ("✅ Да, удалить", f"adm_sched_del_yes_{start}"),
            ("◀️ Отмена",       "adm_schedule"),
        ]))


async def adm_sched_delete_yes(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    start  = update.callback_query.data.replace("adm_sched_del_yes_", "")
    ok, err = _tsvc(context).delete(start)
    if ok:
        await adm_schedule(update, context)  # плавный возврат в расписание
    else:
        await edit_or_reply(update, f"⚠️ {err}",
            kb([("◀️ К расписанию", "adm_schedule")]))


# Обратная совместимость со старыми callback adm_slot_add / adm_slot_del_*
async def adm_slot_add(update: Update, context: CallbackContext):
    await adm_sched_add(update, context)

async def adm_slot_toggle(update: Update, context: CallbackContext):
    """Обратная совместимость: adm_slot_on_HH:MM / adm_slot_off_HH:MM (глобальный слот).
    Паттерн в main.py: ^adm_slot_o(n|ff)_\\d{2}:\\d{2}$  — только 5-символьное HH:MM.
    """
    await update.callback_query.answer()
    data  = update.callback_query.data
    on    = data.startswith("adm_slot_on_")
    # Убираем оба возможных префикса
    start = data.removeprefix("adm_slot_on_").removeprefix("adm_slot_off_")
    # Дополнительная проверка: только HH:MM (5 символов), не дата
    if len(start) == 5 and ":" in start:
        _tsvc(context).toggle(start, on)
        await adm_schedule(update, context)
    else:
        # Что-то пошло не так — просто обновляем расписание
        await adm_schedule(update, context)

async def adm_slot_delete(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    start = update.callback_query.data.replace("adm_slot_del_", "")
    await edit_or_reply(update, f"🗑 Удалить слот <b>{start}</b>?",
        kb([
            ("✅ Да, удалить", f"adm_sched_del_yes_{start}"),
            ("◀️ Отмена",       "adm_schedule"),
        ]))

async def adm_slot_delete_yes(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    start  = update.callback_query.data.replace("adm_slot_del_yes_", "")
    ok, err = _tsvc(context).delete(start)
    if ok:
        await adm_schedule(update, context)
    else:
        await edit_or_reply(update, f"⚠️ {err}",
            kb([("◀️ К расписанию", "adm_schedule")]))


# ════════════════════════════════════════════════════════════════════════════════
# О МАСТЕРЕ
# ════════════════════════════════════════════════════════════════════════════════

def _ads_weekday_picker_markup() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Пн", callback_data=_cb_ads_wd(1)),
            InlineKeyboardButton("Вт", callback_data=_cb_ads_wd(2)),
            InlineKeyboardButton("Ср", callback_data=_cb_ads_wd(3)),
        ],
        [
            InlineKeyboardButton("Чт", callback_data=_cb_ads_wd(4)),
            InlineKeyboardButton("Пт", callback_data=_cb_ads_wd(5)),
            InlineKeyboardButton("Сб", callback_data=_cb_ads_wd(6)),
        ],
        [
            InlineKeyboardButton("Вс", callback_data=_cb_ads_wd(7)),
            InlineKeyboardButton("◀️ Меню мастера", callback_data="master_menu"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _ads_group_times(times: list[str]) -> list[list[str]]:
    morning = [t for t in times if int(t[:2]) < 12]
    noon = [t for t in times if 12 <= int(t[:2]) <= 18]
    evening = [t for t in times if int(t[:2]) > 18 or int(t[:2]) < 3]
    return [morning, noon, evening]


def _ads_time_in_closed(day: dict, hhmm: str) -> bool:
    if not day.get("closed_start") or not day.get("closed_end"):
        return False
    return day["closed_start"] <= hhmm < day["closed_end"]


def _ads_day_markup(weekday: int, day: dict) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    chips: list[InlineKeyboardButton] = []
    for hhmm in day["times"]:
        blocked = (not day["is_open"]) or _ads_time_in_closed(day, hhmm)
        icon = "🚫" if blocked else "🕒"
        chips.append(InlineKeyboardButton(
            text=f"{icon} {hhmm}",
            callback_data=_cb_ads_rm(weekday, hhmm),
        ))
    for i in range(0, len(chips), 4):
        rows.append(chips[i:i + 4])
    rows.append([
        InlineKeyboardButton("➕ Добавить время", callback_data=_cb_ads_add(weekday)),
        InlineKeyboardButton("📋 Скопировать шаблон", callback_data=_cb_ads_copy(weekday)),
    ])

    day_target = not day["is_open"]
    day_label = "🔓 Открыть день" if day_target else "🔒 Закрыть день"
    has_period = bool(day.get("closed_start") and day.get("closed_end"))
    per_label = "✅ Открыть период" if has_period else "🚫 Закрыть период"
    per_target_closed = not has_period
    rows.append([
        InlineKeyboardButton(day_label, callback_data=_cb_ads_day(weekday, day_target)),
        InlineKeyboardButton(per_label, callback_data=_cb_ads_per(weekday, per_target_closed)),
    ])
    rows.append([
        InlineKeyboardButton("◀️ Назад", callback_data=_cb_ads_root()),
        InlineKeyboardButton("🏠 Меню", callback_data="master_menu"),
    ])
    return InlineKeyboardMarkup(rows)


def _ads_day_text(weekday: int, day: dict, note: str = "") -> str:
    groups = _ads_group_times(day["times"])
    lines = [
        f"📅 <b>{_WD_FULL.get(weekday, str(weekday))}</b> (шаблон)",
        f"🎛 Режим: мастер",
        f"🕒 Таймзона: {settings.TIMEZONE}",
        f"📌 Статус: {'✅ День открыт' if day['is_open'] else '⛔ День закрыт (слоты скрыты)'}",
    ]
    if day.get("closed_start") and day.get("closed_end"):
        lines.append(f"🚫 Закрытый период: {day['closed_start']}–{day['closed_end']}")
    lines.append("")
    lines.append("Времена:")
    if day["times"]:
        lines.append("до 12:00    до 18:00    после 18:00")
        max_len = max(len(groups[0]), len(groups[1]), len(groups[2]))
        for i in range(max_len):
            c1 = groups[0][i] if i < len(groups[0]) else ""
            c2 = groups[1][i] if i < len(groups[1]) else ""
            c3 = groups[2][i] if i < len(groups[2]) else ""
            lines.append(f"{c1:<10}{c2:<10}{c3}")
    else:
        lines.append("—")
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


async def _ads_render_day(update: Update, context: CallbackContext, weekday: int, note: str = ""):
    day = _wsvc(context).get_day_template(weekday)
    await edit_or_reply(update, _ads_day_text(weekday, day, note), _ads_day_markup(weekday, day))


async def _ads_render_day_by_message(
    context: CallbackContext, chat_id: int, message_id: int, weekday: int, note: str = ""
):
    day = _wsvc(context).get_day_template(weekday)
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=_ads_day_text(weekday, day, note),
        reply_markup=_ads_day_markup(weekday, day),
        parse_mode="HTML",
    )


async def _ads_redirect_notice(update: Update, context: CallbackContext, note: str):
    await ads_root(update, context, notice=f"ℹ️ {note}")


async def ads_root(update: Update, context: CallbackContext, notice: str = ""):
    await update.callback_query.answer()
    text = (
        "⏰ <b>Расписание</b>\n"
        "Выберите день недели (шаблон):\n"
        f"Таймзона: {settings.TIMEZONE}"
    )
    if notice:
        text += f"\n\n{notice}"
    await edit_or_reply(update, text, _ads_weekday_picker_markup())


async def ads_wd(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    if len(parts) != 3:
        await update.callback_query.answer("Некорректный callback", show_alert=True)
        return
    try:
        weekday = int(parts[2])
    except ValueError:
        await update.callback_query.answer("Некорректный день", show_alert=True)
        return
    if weekday < 1 or weekday > 7:
        await update.callback_query.answer("День 1..7", show_alert=True)
        return
    await _ads_render_day(update, context, weekday)


async def ads_add(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    if len(parts) != 3:
        await update.callback_query.answer("Некорректный callback", show_alert=True)
        return
    weekday = int(parts[2])
    context.user_data["ads_weekday"] = weekday
    if update.callback_query and update.callback_query.message:
        context.user_data["ads_origin_chat_id"] = update.callback_query.message.chat_id
        context.user_data["ads_origin_msg_id"] = update.callback_query.message.message_id
    set_step(context.user_data, "ads_add_time_input")
    await edit_or_reply(
        update,
        "Введите время в формате ЧЧ:ММ\nПример: 10:00",
        kb([("◀️ Назад", _cb_ads_wd(weekday)), ("🏠 Меню", "master_menu")], cols=2),
    )


async def _step_ads_add_time_input(update: Update, context: CallbackContext):
    weekday = int(context.user_data.get("ads_weekday") or 0)
    hhmm = _normalize_hhmm(update.message.text or "")
    try:
        await update.message.delete()
    except Exception:
        pass
    if weekday < 1 or weekday > 7:
        set_step(context.user_data, None)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Сессия истекла.")
        return
    if not hhmm:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Формат времени: ЧЧ:ММ.")
        return
    ok, msg = _wsvc(context).add_weekly_time(weekday, hhmm)
    set_step(context.user_data, None)
    note = f"✅ Добавлено: {msg}" if ok else f"⚠️ {msg}"
    chat_id = context.user_data.get("ads_origin_chat_id")
    message_id = context.user_data.get("ads_origin_msg_id")
    if chat_id and message_id:
        try:
            await _ads_render_day_by_message(context, int(chat_id), int(message_id), weekday, note)
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id=update.effective_chat.id, text=note)


async def ads_rm(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    if len(parts) != 4:
        await update.callback_query.answer("Некорректный callback", show_alert=True)
        return
    weekday = int(parts[2])
    hhmm = _from_hhmm_token(parts[3])
    if not hhmm:
        await update.callback_query.answer("Некорректное время", show_alert=True)
        return
    await edit_or_reply(
        update,
        f"Удалить {hhmm}?",
        kb([
            ("✅ Да, удалить", _cb_ads_rmok(weekday, hhmm)),
            ("◀️ Нет", _cb_ads_wd(weekday)),
            ("🏠 Меню", "master_menu"),
        ]),
    )


async def ads_rmok(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    if len(parts) != 4:
        await update.callback_query.answer("Некорректный callback", show_alert=True)
        return
    weekday = int(parts[2])
    hhmm = _from_hhmm_token(parts[3])
    if not hhmm:
        await update.callback_query.answer("Некорректное время", show_alert=True)
        return
    _wsvc(context).remove_weekly_time(weekday, hhmm)
    await _ads_render_day(update, context, weekday, f"✅ Удалено: {hhmm}")


async def ads_day(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    if len(parts) != 4:
        await update.callback_query.answer("Некорректный callback", show_alert=True)
        return
    weekday = int(parts[2])
    target_open = parts[3] == "1"
    action = "открыть" if target_open else "закрыть"
    await edit_or_reply(
        update,
        f"Подтвердите: {action} день {_WD_SHORT.get(weekday, weekday)}?",
        kb([
            ("✅ Да", _cb_ads_dayok(weekday, target_open, True)),
            ("◀️ Отмена", _cb_ads_wd(weekday)),
        ], cols=2),
    )


async def ads_dayok(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    if len(parts) != 5:
        await update.callback_query.answer("Некорректный callback", show_alert=True)
        return
    weekday = int(parts[2])
    target_open = parts[3] == "1"
    is_ok = parts[4] == "1"
    if not is_ok:
        await _ads_render_day(update, context, weekday)
        return
    ok, msg = _wsvc(context).toggle_day_open(weekday, target_open)
    if not ok:
        await _ads_render_day(update, context, weekday, f"⚠️ {msg}")
        return
    await _ads_render_day(update, context, weekday, "✅ Статус дня обновлен")


async def ads_per(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    if len(parts) != 4:
        await update.callback_query.answer("Некорректный callback", show_alert=True)
        return
    weekday = int(parts[2])
    to_closed = parts[3] == "1"
    if not to_closed:
        _wsvc(context).clear_closed_period(weekday)
        await _ads_render_day(update, context, weekday, "✅ Период открыт")
        return
    context.user_data["ads_weekday"] = weekday
    if update.callback_query and update.callback_query.message:
        context.user_data["ads_origin_chat_id"] = update.callback_query.message.chat_id
        context.user_data["ads_origin_msg_id"] = update.callback_query.message.message_id
    set_step(context.user_data, "ads_add_period_input")
    await edit_or_reply(
        update,
        "Введите закрытый период в формате ЧЧ:ММ-ЧЧ:ММ\nПример: 14:00-16:00",
        kb([("◀️ Назад", _cb_ads_wd(weekday)), ("🏠 Меню", "master_menu")], cols=2),
    )


async def ads_copy(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    if len(parts) != 3:
        await update.callback_query.answer("Некорректный callback", show_alert=True)
        return
    try:
        source_weekday = int(parts[2])
    except ValueError:
        await update.callback_query.answer("Некорректный день", show_alert=True)
        return
    if source_weekday < 1 or source_weekday > 7:
        await update.callback_query.answer("День 1..7", show_alert=True)
        return
    target_buttons = []
    for wd in range(1, 8):
        if wd == source_weekday:
            continue
        target_buttons.append(
            InlineKeyboardButton(_WD_SHORT.get(wd, str(wd)), callback_data=_cb_ads_copyto(source_weekday, wd))
        )
    rows = [target_buttons[i:i + 3] for i in range(0, len(target_buttons), 3)]
    rows.append([
        InlineKeyboardButton("◀️ Назад", callback_data=_cb_ads_wd(source_weekday)),
        InlineKeyboardButton("🏠 Меню", callback_data="master_menu"),
    ])
    await edit_or_reply(
        update,
        f"📋 <b>Копировать шаблон: {_WD_FULL.get(source_weekday, str(source_weekday))}</b>\n\n"
        "Выберите день, в который скопировать расписание.\n"
        "⚠️ Все существующие слоты в целевом дне будут заменены.",
        InlineKeyboardMarkup(rows),
    )


async def ads_copyto(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    if len(parts) != 4:
        await update.callback_query.answer("Некорректный callback", show_alert=True)
        return
    try:
        source_weekday = int(parts[2])
        target_weekday = int(parts[3])
    except ValueError:
        await update.callback_query.answer("Некорректный день", show_alert=True)
        return
    ok, result = _wsvc(context).copy_day_template(source_weekday, target_weekday)
    if not ok:
        await _ads_render_day(update, context, source_weekday, f"⚠️ {result}")
        return
    await _ads_render_day(
        update,
        context,
        source_weekday,
        f"✅ Скопировано в {_WD_SHORT.get(target_weekday, str(target_weekday))}: {result} сл.",
    )


async def _step_ads_add_period_input(update: Update, context: CallbackContext):
    weekday = int(context.user_data.get("ads_weekday") or 0)
    parsed = _normalize_period(update.message.text or "")
    try:
        await update.message.delete()
    except Exception:
        pass
    if weekday < 1 or weekday > 7:
        set_step(context.user_data, None)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Сессия истекла.")
        return
    if not parsed:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Формат периода: ЧЧ:ММ-ЧЧ:ММ.")
        return
    ok, msg = _wsvc(context).set_closed_period(weekday, parsed[0], parsed[1])
    set_step(context.user_data, None)
    note = f"✅ Период установлен: {msg}" if ok else f"⚠️ {msg}"
    chat_id = context.user_data.get("ads_origin_chat_id")
    message_id = context.user_data.get("ads_origin_msg_id")
    if chat_id and message_id:
        try:
            await _ads_render_day_by_message(context, int(chat_id), int(message_id), weekday, note)
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id=update.effective_chat.id, text=note)


async def adm_schedule(update: Update, context: CallbackContext):
    await ads_root(update, context)


async def adm_sched_add(update: Update, context: CallbackContext):
    await _ads_redirect_notice(update, context, "Старый callback перенаправлен в новое расписание")


async def adm_sched_toggle(update: Update, context: CallbackContext):
    await _ads_redirect_notice(update, context, "Старый callback перенаправлен в новое расписание")


async def adm_sched_delete(update: Update, context: CallbackContext):
    await _ads_redirect_notice(update, context, "Старый callback перенаправлен в новое расписание")


async def adm_sched_delete_yes(update: Update, context: CallbackContext):
    await _ads_redirect_notice(update, context, "Старый callback перенаправлен в новое расписание")


async def adm_slot_add(update: Update, context: CallbackContext):
    await _ads_redirect_notice(update, context, "Старый callback перенаправлен в новое расписание")


async def adm_slot_toggle(update: Update, context: CallbackContext):
    await _ads_redirect_notice(update, context, "Старый callback перенаправлен в новое расписание")


async def adm_slot_delete(update: Update, context: CallbackContext):
    await _ads_redirect_notice(update, context, "Старый callback перенаправлен в новое расписание")


async def adm_slot_delete_yes(update: Update, context: CallbackContext):
    await _ads_redirect_notice(update, context, "Старый callback перенаправлен в новое расписание")


async def adm_about(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    await edit_or_reply(
        update,
        "⚙️ <b>Настройки профиля мастера</b>\n\n"
        "Здесь вы можете изменить информацию, которую видят клиенты.\n"
        "Нажмите на кнопку, которую хотите обновить:",
        kb([
            ("📷 Изменить фото", "adm_profile_photo"),
            ("✏️ Описание (О себе)", "adm_profile_bio"),
            ("📍 Управление адресом", "adm_address_settings"),
            ("📞 Изменить контакт", "adm_profile_contact"),
            ("👁 Предпросмотр профиля", "show_about_master"),
            ("◀️ В главное меню", "master_menu"),
        ], cols=1),
    )


async def adm_about_edit_bio(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "about_bio")
    await edit_or_reply(update,
        "✏️ Введите новый текст «О мастере».\n\nМожно использовать переносы строк.",
        kb([("◀️ Отмена", "adm_about")]))


async def _step_about_bio(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Текст не может быть пустым. Попробуйте ещё раз.")
        return
    if len(text) > 800:
        await update.message.reply_text(
            f"Слишком длинный текст ({len(text)} символов, макс. 800).")
        return
    set_step(context.user_data, None)
    settings.set_runtime_value("MASTER_BIO", text)
    await update.message.reply_text("✅ Текст «О мастере» обновлён.",
        reply_markup=kb([("◀️ К странице", "adm_about")] + back_master()))


async def adm_about_edit_photo(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "about_photo")
    await edit_or_reply(update,
        "📸 Отправьте фото для страницы «О мастере».",
        kb([("◀️ Отмена", "adm_about")]))


async def _step_about_photo(update: Update, context: CallbackContext):
    set_step(context.user_data, None)
    if not update.message or not update.message.photo:
        await update.message.reply_text("Ожидается фото. Отправьте фото или нажмите Отмена.",
            reply_markup=kb([("◀️ Отмена", "adm_about")]))
        return
    file_id = update.message.photo[-1].file_id
    settings.set_runtime_value("MASTER_PHOTO_ID", file_id)
    await update.message.reply_text("✅ Фото обновлено.",
        reply_markup=kb([("◀️ К странице", "adm_about")] + back_master()))


async def adm_about_clear_photo(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    settings.set_runtime_value("MASTER_PHOTO_ID", "")
    await edit_or_reply(update, "🗑 Фото удалено.",
        kb([("◀️ К странице", "adm_about")] + back_master()))


async def adm_about_edit_address(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "about_address")
    await edit_or_reply(update,
        f"📍 <b>Текущий адрес:</b>\n{settings.MASTER_ADDRESS}\n\nВведите новый адрес:",
        kb([("◀️ Отмена", "adm_about")]))


async def _step_about_address(update: Update, context: CallbackContext):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Адрес не может быть пустым.")
        return
    if len(text) > 200:
        await update.message.reply_text("Слишком длинный адрес (макс. 200 символов).")
        return
    set_step(context.user_data, None)
    settings.set_runtime_value("MASTER_ADDRESS", text)
    await update.message.reply_text(f"✅ Адрес обновлён:\n{text}",
        reply_markup=kb([("◀️ К странице", "adm_about")] + back_master()))


async def adm_about_edit_contact(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "about_contact")
    await edit_or_reply(update,
        f"📞 <b>Текущий контакт:</b>\n{settings.MASTER_CONTACT}\n\n"
        "Введите новый контакт (например @username или +7900...):",
        kb([("◀️ Отмена", "adm_about")]))


async def _step_about_contact(update: Update, context: CallbackContext):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Контакт не может быть пустым.")
        return
    if len(text) > 100:
        await update.message.reply_text("Слишком длинный контакт (макс. 100 символов).")
        return
    set_step(context.user_data, None)
    settings.set_runtime_value("MASTER_CONTACT", text)
    await update.message.reply_text(f"✅ Контакт обновлён:\n{text}",
        reply_markup=kb([("◀️ К странице", "adm_about")] + back_master()))


# ════════════════════════════════════════════════════════════════════════════════
# ПРИГЛАШЕНИЕ
# ════════════════════════════════════════════════════════════════════════════════

async def adm_profile_photo(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "profile_photo")
    await edit_or_reply(
        update,
        "📷 <b>Загрузка фото мастера</b>\n\n"
        "Отправьте фотографию файлом или картинкой.\n"
        "Она будет отображаться в разделе \"О мастере\".\n\n"
        "👇 Жду ваше фото:",
        kb([("❌ Отмена", "adm_about")]),
    )


async def _step_profile_photo(update: Update, context: CallbackContext):
    if not update.message:
        return
    if update.message.text and update.message.text.strip().lower() == "удалить":
        set_step(context.user_data, None)
        settings.set_runtime_value("MASTER_PHOTO_ID", "")
        await update.message.reply_text("✅ Фото профиля удалено.", reply_markup=kb([("◀️ Назад", "adm_about")]))
        return
    if not update.message.photo:
        await update.message.reply_text(
            "Ожидаю фото. Или напишите <code>удалить</code> для очистки.",
            parse_mode="HTML",
            reply_markup=kb([("◀️ Назад", "adm_about")]),
        )
        return
    set_step(context.user_data, None)
    settings.set_runtime_value("MASTER_PHOTO_ID", update.message.photo[-1].file_id)
    await update.message.reply_text("✅ Фото профиля обновлено.", reply_markup=kb([("◀️ Назад", "adm_about")]))


async def adm_profile_bio(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "profile_bio")
    await edit_or_reply(
        update,
        "✏️ <b>Редактирование описания (О себе)</b>\n\n"
        "Напишите новый текст о себе.\n"
        "Вы можете использовать жирный, курсив и эмодзи.\n\n"
        "💡 Примеры оформления:\n"
        "<code>Привет! Я Анна 💅\n✨ Опыт 5 лет\n🌿 Только эко-материалы\n📍 Центр города</code>\n\n"
        "<code>Мастер ногтевого сервиса.\nСтерильность 100%.\nРаботаю с 10:00 до 20:00.</code>\n\n"
        "👇 Напишите ваш текст ниже:",
        kb([("◀️ Назад", "adm_about")]),
    )


async def _step_profile_bio(update: Update, context: CallbackContext):
    if not update.message:
        return
    text_value = _normalize_profile_text(update)
    if not text_value:
        await update.message.reply_text("Текст не должен быть пустым.")
        return
    set_step(context.user_data, None)
    _save_runtime_text("MASTER_BIO", text_value)
    await update.message.reply_text("✅ Описание обновлено.", reply_markup=kb([("◀️ Назад", "adm_about")]))


async def adm_profile_contact(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "profile_contact")
    await edit_or_reply(
        update,
        "📞 <b>Редактирование контакта</b>\n\n"
        "Как клиентам связаться с вами?\n"
        "Это может быть:\n"
        "• Юзернейм телеграм (@username)\n"
        "• Номер телефона (+7...)\n"
        "• Ссылка на WhatsApp\n"
        "• Или любой другой текст.\n\n"
        "💡 Примеры:\n"
        "<code>@nail_master_anna</code>\n"
        "<code>+7 (999) 000-00-00 (Звонить с 10 до 18)</code>\n"
        "<code>Пишите сюда или в WhatsApp: wa.me/7999...</code>\n\n"
        "⚠️ Мы не проверяем формат, просто сохраняем ваш текст как есть.\n\n"
        "👇 Напишите контакт ниже:",
        kb([("◀️ Назад", "adm_about")]),
    )


async def _step_profile_contact(update: Update, context: CallbackContext):
    if not update.message:
        return
    text_value = _normalize_profile_text(update)
    if not text_value:
        await update.message.reply_text("Контакт не должен быть пустым.")
        return
    set_step(context.user_data, None)
    _save_runtime_text("MASTER_CONTACT", text_value)
    await update.message.reply_text("✅ Контакт обновлен.", reply_markup=kb([("◀️ Назад", "adm_about")]))


async def adm_address_settings(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    lines = [
        "📍 <b>Управление адресом</b>",
        "",
        "Текущий статус:",
        f"• Текст: {_is_filled(settings.MASTER_ADDRESS)}",
        f"• Google Maps: {_is_filled(settings.MASTER_ADDRESS_GOOGLE)}",
        f"• Apple Maps: {_is_filled(settings.MASTER_ADDRESS_APPLE)}",
        f"• Фото схемы: {_is_photo_filled(settings.MASTER_ADDRESS_PHOTO_ID)}",
        f"• Доп. инфо: {_is_filled(settings.MASTER_ADDRESS_EXTRA)}",
        "",
        "Выберите элемент для редактирования:",
    ]
    await edit_or_reply(
        update,
        "\n".join(lines),
        kb([
            ("✏️ Текст адреса", "adm_address_text"),
            ("🔗 Ссылка Google", "adm_address_google"),
            ("🔗 Ссылка Apple", "adm_address_apple"),
            ("📸 Фото схемы", "adm_address_photo"),
            ("ℹ️ Доп. инфо", "adm_address_extra"),
            ("👁 Предпросмотр адреса", "show_address"),
            ("◀️ Назад", "adm_about"),
        ], cols=2),
    )


async def adm_address_text(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "address_text")
    await edit_or_reply(
        update,
        "✏️ <b>Редактирование текста адреса</b>\n\n"
        "Напишите основной адрес ниже. Это увидит клиент в первую очередь.\n\n"
        "💡 Примеры оформления:\n"
        "<code>г. Москва, ул. Ленина, д. 10, оф. 5</code>\n"
        "<code>Санкт-Петербург, Невский проспект 20\nВход со двора, 3 этаж, код 1234</code>\n"
        "<code>Казань, Баумана 15\nТЦ 'Столица', 2 этаж, рядом с эскалатором</code>\n\n"
        "👇 Напишите ваш адрес ниже:",
        kb([("◀️ Назад", "adm_address_settings")]),
    )


async def _step_address_text(update: Update, context: CallbackContext):
    if not update.message:
        return
    text_value = _normalize_profile_text(update)
    if not text_value:
        await update.message.reply_text("Адрес не должен быть пустым.")
        return
    set_step(context.user_data, None)
    _save_runtime_text("MASTER_ADDRESS", text_value)
    await update.message.reply_text("✅ Текст адреса сохранен.", reply_markup=kb([("◀️ Назад", "adm_address_settings")]))


async def adm_address_google(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "address_google")
    await edit_or_reply(
        update,
        "🗺 <b>Ссылка на Google Maps</b>\n\n"
        "Отправьте полную ссылку на точку. У клиента она отобразится как кнопка.\n\n"
        "💡 Как получить ссылку:\n"
        "<code>Откройте приложение карт.\nНажмите \"Поделиться\" на вашей точке.\nСкопируйте ссылку и отправьте её сюда.</code>\n\n"
        "💡 Пример ссылки:\n"
        "<code>https://goo.gl/maps/AbCdEfGhIjKlMnOp</code>\n"
        "<code>https://www.google.com/maps/place/...</code>\n\n"
        "👇 Отправьте ссылку ниже:\n"
        "<blockquote>Напишите \"удалить\", чтобы убрать ссылку</blockquote>",
        kb([("◀️ Назад", "adm_address_settings")]),
    )


async def _step_address_google(update: Update, context: CallbackContext):
    if not update.message:
        return
    text_value = _normalize_profile_text(update)
    if not text_value:
        await update.message.reply_text("Введите ссылку или слово удалить.")
        return
    set_step(context.user_data, None)
    _save_runtime_text("MASTER_ADDRESS_GOOGLE", text_value)
    await update.message.reply_text("✅ Ссылка Google сохранена.", reply_markup=kb([("◀️ Назад", "adm_address_settings")]))


async def adm_address_apple(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "address_apple")
    await edit_or_reply(
        update,
        "🍏 <b>Ссылка на Apple Карты</b>\n\n"
        "Отправьте ссылку для пользователей iPhone.\n\n"
        "💡 Пример ссылки:\n"
        "<code>https://maps.apple.com/?q=...</code>\n\n"
        "👇 Отправьте ссылку ниже:\n"
        "<blockquote>Напишите \"удалить\", чтобы убрать ссылку</blockquote>",
        kb([("◀️ Назад", "adm_address_settings")]),
    )


async def _step_address_apple(update: Update, context: CallbackContext):
    if not update.message:
        return
    text_value = _normalize_profile_text(update)
    if not text_value:
        await update.message.reply_text("Введите ссылку или слово удалить.")
        return
    set_step(context.user_data, None)
    _save_runtime_text("MASTER_ADDRESS_APPLE", text_value)
    await update.message.reply_text("✅ Ссылка Apple сохранена.", reply_markup=kb([("◀️ Назад", "adm_address_settings")]))


async def adm_address_photo(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "address_photo")
    await edit_or_reply(
        update,
        "📸 <b>Фото схемы проезда</b>\n\n"
        "Загрузите фото, если вход сложно найти.\n"
        "Это может быть фото вывески, двери или скриншот карты.\n\n"
        "💡 Клиент увидит кнопку \"📸 Схема проезда\", при нажатии на которую откроется это фото.\n\n"
        "👇 Отправьте фото файлом или изображением:\n"
        "<blockquote>Напишите слово \"удалить\" текстом, чтобы удалить текущее фото</blockquote>",
        kb([("◀️ Назад", "adm_address_settings")]),
    )


async def _step_address_photo(update: Update, context: CallbackContext):
    if not update.message:
        return
    if update.message.text and update.message.text.strip().lower() == "удалить":
        set_step(context.user_data, None)
        settings.set_runtime_value("MASTER_ADDRESS_PHOTO_ID", "")
        await update.message.reply_text("✅ Фото схемы удалено.", reply_markup=kb([("◀️ Назад", "adm_address_settings")]))
        return
    if not update.message.photo:
        await update.message.reply_text(
            "Ожидаю фото. Или напишите <code>удалить</code> для очистки.",
            parse_mode="HTML",
            reply_markup=kb([("◀️ Назад", "adm_address_settings")]),
        )
        return
    set_step(context.user_data, None)
    settings.set_runtime_value("MASTER_ADDRESS_PHOTO_ID", update.message.photo[-1].file_id)
    await update.message.reply_text("✅ Фото схемы сохранено.", reply_markup=kb([("◀️ Назад", "adm_address_settings")]))


async def adm_address_extra(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "address_extra")
    await edit_or_reply(
        update,
        "ℹ️ <b>Дополнительная информация</b>\n\n"
        "Здесь можно написать важные детали: домофон, парковка, метро.\n"
        "Отображается мелким текстом под основным адресом.\n\n"
        "💡 Примеры:\n"
        "<code>⚠️ Домофон не работает, звоните в дверь!</code>\n"
        "<code>🅿️ Бесплатная парковка во дворе за шлагбаумом.</code>\n"
        "<code>🚇 Метро Пушкинская, выход в город последний вагон.</code>\n"
        "<code>🐕 Можно приходить с собаками небольших пород.</code>\n\n"
        "👇 Напишите текст ниже:\n"
        "<blockquote>Напишите \"удалить\", чтобы очистить поле</blockquote>",
        kb([("◀️ Назад", "adm_address_settings")]),
    )


async def _step_address_extra(update: Update, context: CallbackContext):
    if not update.message:
        return
    text_value = _normalize_profile_text(update)
    if not text_value:
        await update.message.reply_text("Введите текст или слово удалить.")
        return
    set_step(context.user_data, None)
    _save_runtime_text("MASTER_ADDRESS_EXTRA", text_value)
    await update.message.reply_text("✅ Дополнительная информация сохранена.", reply_markup=kb([("◀️ Назад", "adm_address_settings")]))


async def adm_invite(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    link = f"https://t.me/{context.bot.username}"
    text = (
        f"📨 <b>Пригласить клиента</b>\n\n"
        f"Скопируйте текст ниже и отправьте клиенту:\n\n"
        f"<code>"
        f"Привет! Теперь записаться ко мне на маникюр можно прямо в Telegram — "
        f"быстро и удобно, без звонков.\n\n"
        f"👉 {link}\n\n"
        f"Нажми /start и выбери удобное время 💅"
        f"</code>\n\n"
        f"💡 Нажмите на текст чтобы скопировать."
    )
    await edit_or_reply(update, text, kb(back_master()))


# ════════════════════════════════════════════════════════════════════════════════
# ЧЁРНЫЙ СПИСОК
# ════════════════════════════════════════════════════════════════════════════════

async def adm_blacklist(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    banned = svc(context, K_BAN).all()
    lines  = ["🚫 <b>Чёрный список</b>\n"]
    if banned:
        for row in banned:
            client = svc(context, K_CLIENT).get(row["user_id"])
            name   = client.display_name if client else f"id:{row['user_id']}"
            reason = f" — {row['reason']}" if row["reason"] else ""
            lines.append(f"• {name}{reason}")
        lines.append("")
    lines.append("Выберите клиента из базы чтобы добавить в чёрный список:")
    banned_ids = {row["user_id"] for row in banned}
    clients    = svc(context, K_CLIENT).all_clients()
    buttons    = [
        (f"🚫 {c.display_name}", f"adm_ban_{c.user_id}")
        for c in clients if c.user_id not in banned_ids
    ]
    await edit_or_reply(update, "\n".join(lines),
        kb(buttons + back_master()))


async def adm_ban_client(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    user_id = int(update.callback_query.data.replace("adm_ban_", ""))
    client  = svc(context, K_CLIENT).get(user_id)
    name    = client.display_name if client else f"id:{user_id}"
    await edit_or_reply(update,
        f"Добавить <b>{name}</b> в чёрный список?\n\n"
        f"Клиент не сможет взаимодействовать с ботом.",
        kb([
            ("✅ Да, заблокировать", f"adm_ban_yes_{user_id}"),
            ("◀️ Отмена",            "adm_blacklist"),
        ]))


async def adm_ban_confirmed(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    user_id = int(update.callback_query.data.replace("adm_ban_yes_", ""))
    client  = svc(context, K_CLIENT).get(user_id)
    name    = client.display_name if client else f"id:{user_id}"
    svc(context, K_BAN).ban(user_id)
    await edit_or_reply(update,
        f"🚫 <b>{name}</b> добавлен в чёрный список.",
        kb([("◀️ Чёрный список", "adm_blacklist")] + back_master()))


async def adm_unban_client(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    user_id = int(update.callback_query.data.replace("adm_unban_", ""))
    client  = svc(context, K_CLIENT).get(user_id)
    name    = client.display_name if client else f"id:{user_id}"
    svc(context, K_BAN).unban(user_id)
    await edit_or_reply(update,
        f"✅ <b>{name}</b> удалён из чёрного списка.",
        kb([("◀️ Чёрный список", "adm_blacklist")] + back_master()))


# ════════════════════════════════════════════════════════════════════════════════
# ЭКСПОРТ / ИМПОРТ КЛИЕНТОВ
# ════════════════════════════════════════════════════════════════════════════════

async def adm_export_clients(update, context):
    import io, csv
    from datetime import datetime
    if update.callback_query:
        await update.callback_query.answer()
    clients = svc(context, K_CLIENT).all_clients()
    if not clients:
        await edit_or_reply(update, "Клиентов пока нет.", kb(back_master()))
        return
    buf    = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Имя", "Телефон", "Username", "Зарегистрирован"])
    for c in clients:
        reg = c.registered_at.strftime("%d.%m.%Y") if c.registered_at else "—"
        writer.writerow([c.display_name, c.phone, c.username or "—", reg])
    data     = buf.getvalue().encode("utf-8-sig")
    filename = f"clients_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    msg_target = (update.callback_query.message if update.callback_query else update.message)
    await msg_target.reply_document(document=data, filename=filename,
        caption=f"👥 Клиентская база: {len(clients)} человек")


async def adm_import_clients(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, "import_clients")
    await edit_or_reply(update,
        "📥 <b>Импорт клиентов</b>\n\n"
        "Загрузите Excel-файл (.xlsx) с колонками:\n"
        "• <b>name</b> — имя клиента\n"
        "• <b>phone</b> — телефон (+7...)\n\n"
        "Отправьте файл:",
        kb([("◀️ Отмена", "adm_crm")]))


async def _step_import_clients(update: Update, context: CallbackContext):
    import io, openpyxl
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".xlsx"):
        await update.message.reply_text("⚠️ Нужен файл в формате .xlsx",
            reply_markup=kb([("◀️ Отмена", "adm_crm")]))
        return
    set_step(context.user_data, None)
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        buf     = io.BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)
        wb      = openpyxl.load_workbook(buf)
        ws      = wb.active
        headers = [str(c.value or "").strip().lower() for c in next(ws.iter_rows(min_row=1, max_row=1))]
        if "name" not in headers or "phone" not in headers:
            await update.message.reply_text(
                "⚠️ В файле нет колонок <b>name</b> и <b>phone</b>.",
                reply_markup=kb([("◀️ К клиентам", "adm_crm")]), parse_mode="HTML")
            return
        name_idx  = headers.index("name")
        phone_idx = headers.index("phone")
        rows = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            name  = str(row[name_idx]  or "").strip()
            phone = str(row[phone_idx] or "").strip()
            if name and phone:
                rows.append({"name": name, "phone": phone})
        added, updated = svc(context, K_CLIENT).import_clients(rows)
        await update.message.reply_text(
            f"✅ <b>Импорт завершён</b>\n\n"
            f"Добавлено: <b>{added}</b>\n"
            f"Обновлено: <b>{updated}</b>\n"
            f"Всего строк: {len(rows)}",
            reply_markup=kb([("👥 К клиентам", "adm_crm")] + back_master()),
            parse_mode="HTML")
    except Exception as e:
        logger.error("import_clients: %s", e)
        await update.message.reply_text("⚠️ Ошибка при обработке файла. Проверьте формат.",
            reply_markup=kb([("◀️ К клиентам", "adm_crm")]))


# ════════════════════════════════════════════════════════════════════════════════
# УДАЛЕНИЕ КЛИЕНТА
# ════════════════════════════════════════════════════════════════════════════════

async def adm_delete_client(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    user_id = int(update.callback_query.data.replace("adm_delete_client_", ""))
    client  = svc(context, K_CLIENT).get(user_id)
    name    = client.display_name if client else f"id:{user_id}"
    await edit_or_reply(update,
        f"🗑 Удалить профиль <b>{name}</b>?\n\n"
        "История записей сохранится. Удаляются только имя и телефон.",
        kb([
            ("✅ Да, удалить", f"adm_delete_client_yes_{user_id}"),
            ("◀️ Отмена",      f"crm_{user_id}"),
        ]))


async def adm_delete_client_confirmed(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    user_id = int(update.callback_query.data.replace("adm_delete_client_yes_", ""))
    client  = svc(context, K_CLIENT).get(user_id)
    name    = client.display_name if client else f"id:{user_id}"
    svc(context, K_CLIENT).delete_client(user_id)
    await edit_or_reply(update,
        f"🗑 Профиль <b>{name}</b> удалён.",
        kb([("◀️ К клиентам", "adm_crm")] + back_master()))


# ════════════════════════════════════════════════════════════════════════════════
# ПОРТФОЛИО МАСТЕРА
# ════════════════════════════════════════════════════════════════════════════════

async def adm_portfolio(update: Update, context: CallbackContext):
    """Список фото портфолио с кнопками удаления."""
    await update.callback_query.answer()
    photos = svc(context, K_PORTFOLIO).all()
    count  = len(photos)
    from app.repositories.repo import PortfolioRepo
    max_p  = PortfolioRepo.MAX_PHOTOS

    text = f"📸 <b>Портфолио</b>  {count}/{max_p} фото\n\n"
    if photos:
        text += "Нажмите на фото чтобы удалить:"
    else:
        text += "Фото пока нет. Добавьте первое!"

    buttons = [(f"🗑 Фото {i+1}", f"adm_portfolio_del_{p['id']}")
               for i, p in enumerate(photos)]

    if count < max_p:
        buttons.append(("➕ Добавить фото", "adm_portfolio_add"))

    await edit_or_reply(update, text, kb(buttons + back_master()))


async def adm_portfolio_add(update: Update, context: CallbackContext):
    """Мастер отправляет фото для добавления в портфолио."""
    await update.callback_query.answer()
    set_step(context.user_data, "portfolio_add")
    await edit_or_reply(update,
        "📸 Отправьте фото для добавления в портфолио:",
        kb([("◀️ Отмена", "adm_portfolio")]))


async def _step_portfolio_add(update: Update, context: CallbackContext):
    """Обрабатываем фото от мастера."""
    set_step(context.user_data, None)
    if not update.message.photo:
        await update.message.reply_text(
            "⚠️ Нужно отправить фото.",
            reply_markup=kb([("◀️ Отмена", "adm_portfolio")]))
        return
    file_id = update.message.photo[-1].file_id
    ok, err = svc(context, K_PORTFOLIO).add(file_id)
    if ok:
        count = svc(context, K_PORTFOLIO).count()
        await update.message.reply_text(
            f"✅ Фото добавлено! В портфолио {count} фото.",
            reply_markup=kb([("📸 Портфолио", "adm_portfolio")] + back_master()))
    else:
        await update.message.reply_text(
            f"⚠️ {err}",
            reply_markup=kb([("📸 Портфолио", "adm_portfolio")] + back_master()))


async def adm_portfolio_delete(update: Update, context: CallbackContext):
    """Подтверждение удаления фото."""
    await update.callback_query.answer()
    photo_id = int(update.callback_query.data.replace("adm_portfolio_del_", ""))
    await edit_or_reply(update,
        "🗑 Удалить это фото из портфолио?",
        kb([
            ("✅ Да, удалить", f"adm_portfolio_del_yes_{photo_id}"),
            ("◀️ Отмена",      "adm_portfolio"),
        ]))


async def adm_portfolio_delete_yes(update: Update, context: CallbackContext):
    """Удаляем фото."""
    await update.callback_query.answer()
    photo_id = int(update.callback_query.data.replace("adm_portfolio_del_yes_", ""))
    ok = svc(context, K_PORTFOLIO).delete(photo_id)
    msg = "✅ Фото удалено." if ok else "⚠️ Фото не найдено."
    await edit_or_reply(update, msg, kb([("📸 Портфолио", "adm_portfolio")] + back_master()))
