"""app/bot/helpers.py  — v13"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Callable, Coroutine, Optional

from app.core.step_guard import set_step, check_and_reset_if_expired  # noqa: F401

logger = logging.getLogger("salon.bot")

# bot_data ключи
K_CLIENT   = "svc_client"
K_BOOKING  = "svc_booking"
K_NOTE     = "svc_note"
K_REVIEW   = "svc_review"
K_SERVICE  = "svc_service"
K_BAN      = "svc_ban"
K_LIMITERS = "limiters"
K_DISPATCH   = "step_dispatcher"
K_PORTFOLIO  = "svc_portfolio"

AsyncHandler = Callable[..., Coroutine[Any, Any, None]]


def svc(context, key: str):
    v = context.bot_data.get(key)
    if v is None:
        raise RuntimeError(f"bot_data['{key}'] не инициализирован.")
    return v


class StepDispatcher:
    def __init__(self):
        self._handlers: dict[str, AsyncHandler] = {}

    def register(self, step: str, fn: AsyncHandler):
        self._handlers[step] = fn

    async def dispatch(self, update, context) -> bool:
        if await check_and_reset_if_expired(update, context):
            return True
        step    = context.user_data.get("step")
        handler = self._handlers.get(step)
        if handler:
            await handler(update, context)
            return True
        return False


# ════════════════════════════════════════════════════════════════════════════════
# КЛАВИАТУРА
# ════════════════════════════════════════════════════════════════════════════════

def kb(buttons: list[tuple[str, str]], cols: int = 1):
    """Строит InlineKeyboardMarkup.
    cols=2 — сетка 2 кнопки в ряд.
    Кнопки-одиночки (◀️ / 🏠) всегда идут отдельной строкой.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    def _is_nav(text: str) -> bool:
        return text.startswith(("◀️", "🏠"))

    rows: list[list] = []
    buf:  list       = []
    for text, data in buttons:
        btn = InlineKeyboardButton(text, callback_data=data)
        if _is_nav(text) or cols == 1:
            if buf:
                rows.append(buf); buf = []
            rows.append([btn])
        else:
            buf.append(btn)
            if len(buf) == cols:
                rows.append(buf); buf = []
    if buf:
        rows.append(buf)
    return InlineKeyboardMarkup(rows)


def kb_row(*pairs: tuple[str, str], extra=None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(t, callback_data=d) for t, d in pairs]]
    if extra:
        for row in extra:
            rows.append([InlineKeyboardButton(t, callback_data=d) for t, d in row])
    return InlineKeyboardMarkup(rows)


def back_main() -> list[tuple[str, str]]:
    return [("◀️ Назад", "main_menu")]


def back_master() -> list[tuple[str, str]]:
    return [("◀️ Меню мастера", "master_menu")]


# ════════════════════════════════════════════════════════════════════════════════
# ТЕЛЕФОН
# ════════════════════════════════════════════════════════════════════════════════

from app.core.phone import normalize_phone, validate_phone  # noqa: F401


# ════════════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ
# ════════════════════════════════════════════════════════════════════════════════

def uid_uname(update) -> tuple[int, str]:
    user  = update.effective_user
    uid   = user.id if user else 0
    uname = (f"@{user.username}" if user and user.username
             else (user.first_name if user else "unknown"))
    return uid, uname


def fmt_booking(b) -> str:
    from app.core.time_utils import fmt_date, fmt_slot
    icon = {"active": "✅", "cancelled": "❌", "completed": "✔️"}.get(b.status.value, "•")
    return (
        f"{icon} <b>{b.service}</b> — {b.price:,} руб.\n"
        f"📅 {fmt_date(b.date)}  ⏰ {fmt_slot(b.time)}"
    )


def stars(rating: int) -> str:
    return "⭐" * rating + "☆" * (5 - rating)


def plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 19:
        return many
    r = n % 10
    if r == 1:      return one
    if 2 <= r <= 4: return few
    return many


# ════════════════════════════════════════════════════════════════════════════════
# ОТПРАВКА СООБЩЕНИЙ — плавные переходы через edit_or_reply
# ════════════════════════════════════════════════════════════════════════════════



async def send_photo_or_edit(update, context, photo_id: str, caption: str, markup):
    """Плавный показ фото:
    - фото → фото: edit_media (без мигания)
    - текст → фото: delete + send_photo
    """
    from telegram import InputMediaPhoto
    cq = update.callback_query
    if cq.message.photo:
        try:
            await cq.message.edit_media(
                media=InputMediaPhoto(media=photo_id, caption=caption, parse_mode="HTML"),
                reply_markup=markup,
            )
            return
        except Exception as e:
            if "message is not modified" in str(e).lower():
                return
    try:
        await cq.message.delete()
    except Exception:
        pass
    await context.bot.send_photo(
        chat_id=cq.message.chat_id,
        photo=photo_id,
        caption=caption,
        parse_mode="HTML",
        reply_markup=markup,
    )

async def safe_reply(update, text: str, markup=None):
    """Отправляет НОВОЕ сообщение. Используется для главного меню."""
    try:
        msg = (update.callback_query.message
               if update.callback_query else update.message)
        if msg:
            await msg.reply_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.error("safe_reply: %s", e)


async def edit_or_reply(update, text: str, markup=None, **kwargs):
    """Плавный переход: редактирует текущее сообщение (callback) или
    отправляет новое (текстовое сообщение). Не добавляет лишних сообщений в чат.
    """
    if "reply_markup" in kwargs:
        markup = kwargs["reply_markup"]
    parse_mode      = kwargs.get("parse_mode", "HTML")
    disable_preview = kwargs.get("disable_web_page_preview", False)

    try:
        if update.callback_query:
            msg = update.callback_query.message
            # Если текущее сообщение с медиа (фото/видео) — нельзя edit_text.
            # Удаляем его и отправляем новое текстовое.
            if msg.photo or msg.video or msg.document or msg.audio:
                try:
                    await msg.delete()
                except Exception:
                    pass
                await msg.reply_text(
                    text, reply_markup=markup, parse_mode=parse_mode,
                    disable_web_page_preview=disable_preview,
                )
            else:
                try:
                    await msg.edit_text(
                        text, reply_markup=markup, parse_mode=parse_mode,
                        disable_web_page_preview=disable_preview,
                    )
                except Exception as e:
                    if "message is not modified" in str(e).lower():
                        return  # контент не изменился — норма, ничего не делаем
                    await msg.reply_text(
                        text, reply_markup=markup, parse_mode=parse_mode,
                        disable_web_page_preview=disable_preview,
                    )
        elif update.message:
            await update.message.reply_text(
                text, reply_markup=markup, parse_mode=parse_mode,
                disable_web_page_preview=disable_preview,
            )
    except Exception as e:
        logger.error("edit_or_reply: %s", e)


async def safe_send(bot, chat_id: int, text: str, markup=None):
    try:
        await bot.send_message(chat_id=chat_id, text=text,
                               reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.error("safe_send %s: %s", chat_id, e)


async def safe_send_photo(bot, chat_id: int, photo_file_id: str,
                          caption: str = "", markup=None):
    try:
        await bot.send_photo(chat_id=chat_id, photo=photo_file_id,
                             caption=caption, parse_mode="HTML",
                             reply_markup=markup)
    except Exception as e:
        logger.error("safe_send_photo %s: %s", chat_id, e)


def _in_working_hours() -> bool:
    from app.core.time_utils import local_now
    h = local_now().hour
    return 9 <= h < 21


async def notify_admins(bot, text: str, markup=None):
    if not _in_working_hours():
        return
    from app.core.settings import settings
    for aid in settings.ADMIN_IDS:
        await safe_send(bot, aid, text, markup)


async def notify_admins_photo(bot, photo_file_id: str, caption: str = ""):
    if not _in_working_hours():
        return
    from app.core.settings import settings
    for aid in settings.ADMIN_IDS:
        await safe_send_photo(bot, aid, photo_file_id, caption)


# ════════════════════════════════════════════════════════════════════════════════
# УНИФИЦИРОВАННЫЙ НЕДЕЛЬНЫЙ КАЛЕНДАРЬ
# Используется одинаково и у клиента, и у мастера.
# ════════════════════════════════════════════════════════════════════════════════

def build_calendar_grid_markup(
    all_dates,
    prefix: str,
    week_offset: int,
    week_nav_prefix: str,
    back_buttons: list[tuple[str, str]],
):
    """Красивая сетка-календарь 7 колонок (Пн–Вс).

    Строки:
      • Заголовок: Пн Вт Ср Чт Пт Сб Вс  (неактивные кнопки)
      • 7 кнопок-дней: число если доступно, «·» если нет
      • Навигация [◀️ | ▶️]
      • Кнопки «Назад»
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from app.core.time_utils import local_today

    _DAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    _MONTHS_SHORT = ["янв","фев","мар","апр","май","июн",
                     "июл","авг","сен","окт","ноя","дек"]

    today      = local_today()
    week_start = today + _dt.timedelta(weeks=week_offset)
    # Приводим к понедельнику
    week_start = week_start - _dt.timedelta(days=week_start.weekday())
    week_end   = week_start + _dt.timedelta(days=6)

    def _ds(d) -> str:
        return d.date if hasattr(d, "date") else d

    available = {_ds(d) for d in all_dates}

    # Заголовок: название месяца (или двух, если неделя на стыке)
    s_m = week_start.month
    e_m = week_end.month
    s_y = week_start.year
    e_y = week_end.year
    if s_m == e_m:
        header_month = f"{_MONTHS_SHORT[s_m-1].capitalize()} {s_y}"
    else:
        ym_end = f" {e_y}" if s_y != e_y else ""
        header_month = f"{_MONTHS_SHORT[s_m-1].capitalize()} – {_MONTHS_SHORT[e_m-1].capitalize()}{ym_end} {e_y}"

    if week_offset == 0:
        header = f"📅 <b>Эта неделя</b> · {header_month}"
    else:
        header = f"📅 <b>{header_month}</b>"

    rows = []

    # Строка заголовка дней недели
    rows.append([
        InlineKeyboardButton(label, callback_data="noop")
        for label in _DAY_LABELS
    ])

    # 7 кнопок-дней
    day_row = []
    for i in range(7):
        day = week_start + _dt.timedelta(days=i)
        ds  = day.isoformat()
        if ds in available:
            label = str(day.day)
            day_row.append(InlineKeyboardButton(label, callback_data=f"{prefix}{ds}"))
        else:
            # Серая точка — день недоступен или в прошлом
            day_row.append(InlineKeyboardButton("·", callback_data="noop"))
    rows.append(day_row)

    # Навигация
    next_start = week_end + _dt.timedelta(days=1)
    has_next   = any(_ds(d) >= next_start.isoformat() for d in all_dates)
    nav_row    = []
    if week_offset > 0:
        nav_row.append(InlineKeyboardButton(
            "◀️ Назад", callback_data=f"{week_nav_prefix}{week_offset - 1}"
        ))
    if has_next:
        nav_row.append(InlineKeyboardButton(
            "Вперёд ▶️", callback_data=f"{week_nav_prefix}{week_offset + 1}"
        ))
    if nav_row:
        rows.append(nav_row)

    for text, data in back_buttons:
        rows.append([InlineKeyboardButton(text, callback_data=data)])

    return InlineKeyboardMarkup(rows), header



def build_calendar_grid_master(
    all_dates,
    prefix: str,
    week_offset: int,
    week_nav_prefix: str,
    back_buttons: list[tuple[str, str]],
):
    """Сетка-календарь 7 колонок для панели мастера.

    Каждый день показывает:
      🟢 свободен  🟡 частично  🔴 полный  🚫 закрыт
    Прошедшие и неизвестные дни — серая точка (noop).
    Все будущие дни кликабельны для просмотра.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from app.core.time_utils import local_today

    _DAY_LABELS = [chr(1055)+chr(1085), chr(1042)+chr(1090),
                   chr(1057)+chr(1088), chr(1063)+chr(1090),
                   chr(1055)+chr(1090), chr(1057)+chr(1073),
                   chr(1042)+chr(1089)]
    _MONTHS_SHORT = [chr(1103)+chr(1085)+chr(1074), chr(1092)+chr(1077)+chr(1074),
                     chr(1084)+chr(1072)+chr(1088), chr(1072)+chr(1087)+chr(1088),
                     chr(1084)+chr(1072)+chr(1081), chr(1080)+chr(1102)+chr(1085),
                     chr(1080)+chr(1102)+chr(1083), chr(1072)+chr(1074)+chr(1075),
                     chr(1089)+chr(1077)+chr(1085), chr(1086)+chr(1082)+chr(1090),
                     chr(1085)+chr(1086)+chr(1103), chr(1076)+chr(1077)+chr(1082)]

    today      = local_today()
    week_start = today + _dt.timedelta(weeks=week_offset)
    week_start = week_start - _dt.timedelta(days=week_start.weekday())
    week_end   = week_start + _dt.timedelta(days=6)

    def _ds(d):
        return d.date if hasattr(d, "date") else d

    # Словарь date_str -> SlotInfo
    slot_map = {_ds(d): d for d in all_dates}

    # Заголовок
    s_m, e_m = week_start.month, week_end.month
    s_y, e_y = week_start.year, week_end.year
    if s_m == e_m:
        header_month = f"{_MONTHS_SHORT[s_m-1].capitalize()} {s_y}"
    else:
        ym_end = f" {e_y}" if s_y != e_y else ""
        header_month = (f"{_MONTHS_SHORT[s_m-1].capitalize()}"
                        f" – {_MONTHS_SHORT[e_m-1].capitalize()}{ym_end} {e_y}")

    if week_offset == 0:
        header = f"📅 <b>Эта неделя</b> · {header_month}"
    else:
        header = f"📅 <b>{header_month}</b>"

    rows = []

    # Строка заголовков дней
    rows.append([
        InlineKeyboardButton(label, callback_data="noop")
        for label in _DAY_LABELS
    ])

    # 7 кнопок дней
    day_row = []
    for i in range(7):
        day = week_start + _dt.timedelta(days=i)
        ds  = day.isoformat()
        if day < today:
            day_row.append(InlineKeyboardButton("·", callback_data="noop"))
            continue
        slot = slot_map.get(ds)
        if slot is None:
            # Нет данных (выходит за горизонт 60 дней)
            day_row.append(InlineKeyboardButton(str(day.day), callback_data=f"{prefix}{ds}"))
            continue
        if slot.is_blocked:
            emoji = "🚫"
        elif slot.is_full:
            emoji = "🔴"
        elif slot.booked_count > 0:
            emoji = "🟡"
        else:
            emoji = "🟢"
        label = f"{emoji}{day.day}"
        day_row.append(InlineKeyboardButton(label, callback_data=f"{prefix}{ds}"))
    rows.append(day_row)

    # Навигация
    next_start = week_end + _dt.timedelta(days=1)
    all_ds = {_ds(d) for d in all_dates}
    has_next = any(d >= next_start.isoformat() for d in all_ds)
    nav_row = []
    if week_offset > 0:
        nav_row.append(InlineKeyboardButton(
            "◀️ Назад",
            callback_data=f"{week_nav_prefix}{week_offset - 1}"
        ))
    if has_next:
        nav_row.append(InlineKeyboardButton(
            "Вперёд ▶️",
            callback_data=f"{week_nav_prefix}{week_offset + 1}"
        ))
    if nav_row:
        rows.append(nav_row)

    for text, data in back_buttons:
        rows.append([InlineKeyboardButton(text, callback_data=data)])

    return InlineKeyboardMarkup(rows), header


def build_calendar_markup(
    all_dates,
    prefix: str,
    week_offset: int,
    week_nav_prefix: str,
    back_buttons: list[tuple[str, str]],
    label_fn=None,
    day_cols: int = 3,
):
    """Строит InlineKeyboardMarkup для недельного календаря.

    Структура:
      • Кнопки дней — сетка day_cols (по умолчанию 3 в ряд)
      • Навигация [◀️ Пред. неделя | След. неделя ▶️] — всегда одна строка
      • back_buttons — каждая отдельной строкой снизу
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from app.core.time_utils import fmt_date, fmt_date_btn, local_today

    today      = local_today()
    week_start = today + _dt.timedelta(weeks=week_offset)
    week_end   = week_start + _dt.timedelta(days=6)

    def _ds(d) -> str:
        return d.date if hasattr(d, "date") else d

    week_dates = [d for d in all_dates
                  if week_start.isoformat() <= _ds(d) <= week_end.isoformat()]

    # Заголовок
    s_fmt = week_start.strftime("%d.%m")
    e_fmt = week_end.strftime("%d.%m.%Y")
    if week_offset == 0:
        header = f"📅 <b>Эта неделя</b>  <i>({s_fmt} – {e_fmt})</i>"
    else:
        header = f"📅 <b>{s_fmt} – {e_fmt}</b>"

    rows = []

    # Кнопки дней — сетка day_cols
    day_btns = []
    for d in week_dates:
        ds    = _ds(d)
        label = label_fn(ds, d) if label_fn else fmt_date_btn(ds)
        day_btns.append(InlineKeyboardButton(label, callback_data=f"{prefix}{ds}"))
    for i in range(0, len(day_btns), day_cols):
        rows.append(day_btns[i:i + day_cols])

    # Навигация — всегда одна строка [◀️ | ▶️]
    next_start = week_end + _dt.timedelta(days=1)
    has_next   = any(_ds(d) >= next_start.isoformat() for d in all_dates)
    nav_row    = []
    if week_offset > 0:
        nav_row.append(InlineKeyboardButton("◀️ Пред. неделя",
                                            callback_data=f"{week_nav_prefix}{week_offset - 1}"))
    if has_next:
        nav_row.append(InlineKeyboardButton("След. неделя ▶️",
                                            callback_data=f"{week_nav_prefix}{week_offset + 1}"))
    if nav_row:
        rows.append(nav_row)

    # Кнопки «Назад» — каждая отдельной строкой
    for text, data in back_buttons:
        rows.append([InlineKeyboardButton(text, callback_data=data)])

    return InlineKeyboardMarkup(rows), header


def build_calendar_buttons(
    all_dates,
    prefix: str,
    week_offset: int,
    week_nav_prefix: str,
    back_buttons: list[tuple[str, str]],
    label_fn=None,
) -> tuple[list[tuple[str, str]], str]:
    """Устаревший API — используется мастером (calday_/cal_week_).
    Возвращает (плоский список кнопок для kb(), заголовок).
    Для клиентского календаря используй build_calendar_markup().
    """
    from app.core.time_utils import fmt_date, local_today

    today      = local_today()
    week_start = today + _dt.timedelta(weeks=week_offset)
    week_end   = week_start + _dt.timedelta(days=6)

    def _ds(d) -> str:
        return d.date if hasattr(d, "date") else d

    week_dates = [d for d in all_dates
                  if week_start.isoformat() <= _ds(d) <= week_end.isoformat()]

    day_buttons = []
    for d in week_dates:
        ds    = _ds(d)
        label = label_fn(ds, d) if label_fn else fmt_date(ds)
        day_buttons.append((label, f"{prefix}{ds}"))

    nav = []
    if week_offset > 0:
        nav.append(("◀️ Пред. неделя", f"{week_nav_prefix}{week_offset - 1}"))
    next_start = week_end + _dt.timedelta(days=1)
    has_next   = any(_ds(d) >= next_start.isoformat() for d in all_dates)
    if has_next:
        nav.append(("След. неделя ▶️", f"{week_nav_prefix}{week_offset + 1}"))

    s_fmt = week_start.strftime("%d.%m")
    e_fmt = week_end.strftime("%d.%m.%Y")
    if week_offset == 0:
        header = f"📅 <b>Эта неделя</b>  <i>({s_fmt} – {e_fmt})</i>"
    else:
        header = f"📅 <b>{s_fmt} – {e_fmt}</b>"

    return day_buttons + nav + back_buttons, header
