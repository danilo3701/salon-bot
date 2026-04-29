"""
app/bot/handlers/client.py  — v13

Флоу клиента. Плавные переходы через edit_or_reply.
Календарь унифицирован с мастером (build_calendar_buttons).
Запись и перенос — на 60 дней вперёд.
"""
from __future__ import annotations

import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

from app.bot.helpers import (
    set_step,
    K_BAN, K_BOOKING, K_CLIENT, K_DISPATCH, K_LIMITERS, K_NOTE, K_PORTFOLIO, K_REVIEW, K_SERVICE,
    back_main, build_calendar_buttons, build_calendar_grid_markup, build_calendar_markup, edit_or_reply, fmt_booking, kb, kb_row,
    send_photo_or_edit,
    normalize_phone, notify_admins, notify_admins_photo,
    safe_reply, safe_send, stars, svc, uid_uname, validate_phone,
)
from app.core.settings import settings
from app.core.money import format_eur
from app.core.time_utils import fmt_date, fmt_slot
from app.models.domain import BookingStatus

logger = logging.getLogger("salon.client")

_SIGN = f"\n\n— {settings.MASTER_USERNAME}"


def register_steps(dispatcher):
    dispatcher.register("await_name",         _step_name)
    dispatcher.register("await_phone",        _step_phone)
    dispatcher.register("await_review_text",  _step_review_text)
    dispatcher.register("await_review_photo", _step_review_photo)


# ════════════════════════════════════════════════════════════════════════════════
# ГЛАВНОЕ МЕНЮ
# ════════════════════════════════════════════════════════════════════════════════

async def _is_banned(update, context) -> bool:
    uid, _ = uid_uname(update)
    if svc(context, K_BAN).is_banned(uid):
        if update.callback_query:
            await update.callback_query.answer("Доступ ограничен.", show_alert=True)
        elif update.message:
            await update.message.reply_text("К сожалению, доступ к боту ограничен.")
        return True
    return False


async def cmd_start(update: Update, context: CallbackContext):
    if await _is_banned(update, context):
        return
    uid, uname = uid_uname(update)
    first = (update.effective_user.first_name or "Клиент") if update.effective_user else "Клиент"
    svc(context, K_CLIENT).touch(uid, uname, first)
    set_step(context.user_data, None)

    buttons  = [
        ("\U0001F485 Записаться", "book_service"),
        ("\U0001F4CB Мои записи", "my_bookings"),
        ("\U0001F4B0 Прайс-лист", "prices"),
        ("\U0001F4CD Адрес", "show_address"),
        ("\U0001F469\u200D\U0001F3A8 О мастере", "show_about_master"),
        ("\U0001F4F8 Портфолио", "portfolio"),
    ]

    client = svc(context, K_CLIENT).get(uid)
    name   = client.display_name if client else first
    studio_name = settings.MASTER_USERNAME.lstrip("@") or name
    text   = (
        f'\U0001F485 <b>Студия Маникюра "{escape(studio_name)}"</b>\n'
        "Добро пожаловать!\n"
        "Выберите действие в меню ниже:"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001F485 Записаться", callback_data="book_service")],
        [
            InlineKeyboardButton("\U0001F4CB Мои записи", callback_data="my_bookings"),
            InlineKeyboardButton("\U0001F4B0 Прайс-лист", callback_data="prices"),
        ],
        [
            InlineKeyboardButton("\U0001F4CD Адрес", callback_data="show_address"),
            InlineKeyboardButton("\U0001F469\u200D\U0001F3A8 О мастере", callback_data="show_about_master"),
        ],
        [InlineKeyboardButton("\U0001F4F8 Портфолио", callback_data="portfolio")],
    ])
    # При нажатии кнопки «Назад» — редактируем плавно; при /start — новое сообщение
    if update.callback_query:
        await edit_or_reply(update, text, markup)
    else:
        await safe_reply(update, text, markup)


async def handle_text(update: Update, context: CallbackContext):
    if await _is_banned(update, context):
        return
    dispatcher = context.bot_data.get(K_DISPATCH)
    if dispatcher:
        handled = await dispatcher.dispatch(update, context)
        if handled:
            return
    await cmd_start(update, context)


# ════════════════════════════════════════════════════════════════════════════════
# ИНФОРМАЦИОННЫЕ РАЗДЕЛЫ
# ════════════════════════════════════════════════════════════════════════════════

async def show_prices(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, None)  # сброс любого незавершённого ввода
    services = svc(context, K_SERVICE).all()
    lines = "\n".join(
        f"• {name}: <b>{format_eur(price)}</b>"
        for name, price in services.items()
    )
    await edit_or_reply(update,
        f"💰 <b>Прайс-лист</b>\n\n{lines}",
        kb(back_main()))


def _address_client_markup() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    map_row: list[InlineKeyboardButton] = []
    if settings.MASTER_ADDRESS_GOOGLE:
        map_row.append(InlineKeyboardButton("\U0001F5FA Google Maps", url=settings.MASTER_ADDRESS_GOOGLE))
    if settings.MASTER_ADDRESS_APPLE:
        map_row.append(InlineKeyboardButton("\U0001F34F Apple Карты", url=settings.MASTER_ADDRESS_APPLE))
    if map_row:
        rows.append(map_row)
    if settings.MASTER_ADDRESS_PHOTO_ID:
        rows.append([InlineKeyboardButton("\U0001F4F8 Схема проезда", callback_data="show_address_photo")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def _master_contact_html() -> str:
    contact = (settings.MASTER_CONTACT or "").strip()
    if not contact:
        return "Контакт не указан"
    if contact.startswith("@"):
        username = contact.lstrip("@")
        return f"<a href='https://t.me/{escape(username)}'>{escape(contact)}</a>"
    if contact.startswith(("http://", "https://", "wa.me/", "t.me/")):
        href = contact if contact.startswith(("http://", "https://")) else f"https://{contact}"
        return f"<a href='{escape(href)}'>{escape(contact)}</a>"
    return escape(contact).replace("\n", "<br>")


def _master_contact_button() -> InlineKeyboardButton:
    contact = (settings.MASTER_CONTACT or "").strip()
    if contact.startswith("@"):
        return InlineKeyboardButton("✍️ Написать мастеру", url=f"https://t.me/{contact.lstrip('@')}")
    if contact.startswith(("http://", "https://", "wa.me/", "t.me/")):
        href = contact if contact.startswith(("http://", "https://")) else f"https://{contact}"
        return InlineKeyboardButton("✍️ Написать мастеру", url=href)
    return InlineKeyboardButton("✍️ Написать мастеру", callback_data="contact")


async def show_address(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, None)
    lines = [
        "\U0001F4CD <b>Наш адрес</b>",
        escape(settings.MASTER_ADDRESS or "Адрес пока не заполнен").replace("\n", "<br>"),
    ]
    extra = (settings.MASTER_ADDRESS_EXTRA or "").strip()
    if extra:
        lines.append(f"<blockquote>{escape(extra).replace(chr(10), '<br>')}</blockquote>")
    await edit_or_reply(update, "\n".join(lines), _address_client_markup(), disable_web_page_preview=True)


async def show_address_photo(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, None)
    photo_id = settings.MASTER_ADDRESS_PHOTO_ID
    if not photo_id:
        await edit_or_reply(update, "\U0001F4F8 Схема проезда пока не добавлена.", kb(back_main()))
        return
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="show_address")]])
    await send_photo_or_edit(update, context, photo_id, "\U0001F4F8 <b>Схема проезда</b>", markup)


async def show_contact(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, None)
    await edit_or_reply(
        update,
        f"\U0001F4DE <b>Контакт мастера</b>\n\n{_master_contact_html()}",
        kb(back_main()),
        disable_web_page_preview=True,
    )


async def show_about(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    set_step(context.user_data, None)
    master_name = settings.MASTER_USERNAME.lstrip("@") or "Мастер"
    bio = settings.MASTER_BIO or (
        "Привет! Я сертифицированный мастер ногтевого сервиса.\n"
        "✨ Работаю на качественных материалах.\n"
        "✨ Соблюдаю стерильность и чистоту.\n"
        "✨ Помогу подобрать удобную форму и оттенок."
    )
    bio = bio.replace("\\n", "\n")
    text = f"\U0001F469\u200D\U0001F3A8 <b>Мастер: {escape(master_name)}</b>\n\n{escape(bio).replace(chr(10), '<br>')}"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001F4CD Показать адрес", callback_data="show_address")],
        [_master_contact_button()],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")],
    ])
    if settings.MASTER_PHOTO_ID:
        try:
            await send_photo_or_edit(update, context, settings.MASTER_PHOTO_ID, text, markup)
            return
        except Exception:
            pass
    await edit_or_reply(update, text, markup)


async def show_about_master(update: Update, context: CallbackContext):
    await show_about(update, context)


async def book_service(update: Update, context: CallbackContext):
    if await _is_banned(update, context):
        return
    await update.callback_query.answer()
    uid, _ = uid_uname(update)
    lim    = svc(context, K_LIMITERS)
    ok, w  = lim.booking.check(uid)
    if not ok:
        await edit_or_reply(update,
            f"⏳ Слишком много запросов. Подождите {w} сек.",
            kb(back_main()))
        return
    buttons = [(name, f"srv_{name}") for name in svc(context, K_SERVICE).all()]
    await edit_or_reply(update,
        "💅 <b>Выберите услугу:</b>",
        kb(buttons + back_main()))


async def cb_service(update: Update, context: CallbackContext):
    if await _is_banned(update, context):
        return
    await update.callback_query.answer()
    service  = update.callback_query.data.replace("srv_", "")
    services = svc(context, K_SERVICE).all()
    if service not in services:
        await edit_or_reply(update, "Услуга не найдена.", kb(back_main()))
        return
    context.user_data["book_service"] = service
    context.user_data["book_price"]   = services[service]
    context.user_data["date_week_offset"] = 0

    dates = svc(context, K_BOOKING).available_dates()
    if not dates:
        await edit_or_reply(update,
            "К сожалению, свободных дат нет. Попробуйте позже или напишите мастеру.",
            kb([("📞 Контакт", "contact")] + back_main()))
        return
    await _show_date_week(update, context, service, services[service], dates, week_offset=0)


# ════════════════════════════════════════════════════════════════════════════════
# ВЫБОР ДАТЫ — НЕДЕЛЬНЫЙ КАЛЕНДАРЬ (унифицирован с мастером)
# ════════════════════════════════════════════════════════════════════════════════

async def _show_date_week(update, context, service: str, price: int,
                          all_dates, week_offset: int = 0):
    markup, week_header = build_calendar_grid_markup(
        all_dates       = all_dates,
        prefix          = "date_",
        week_offset     = week_offset,
        week_nav_prefix = "date_week_",
        back_buttons    = [("◀️ Назад", "book_service")],
    )
    btn_data  = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    has_dates = any(d.startswith("date_2") for d in btn_data)
    nl2 = chr(10) + chr(10)
    if not has_dates:
        note = nl2 + "<i>На этой неделе свободных дат нет — листайте вперёд ►️</i>"
    else:
        note = nl2 + "<i>Тапните на число, чтобы выбрать день</i>"
    text = (
        "💅 <b>" + service + "</b> — " + format_eur(price)
        + chr(10) + chr(10) + week_header + note
    )
    await edit_or_reply(update, text, markup)

async def cb_date_week(update: Update, context: CallbackContext):
    """Переключение недели в календаре клиента: date_week_0, date_week_1..."""
    await update.callback_query.answer()
    offset  = int(update.callback_query.data.replace("date_week_", ""))
    service = context.user_data.get("book_service", "")
    price   = context.user_data.get("book_price", 0)
    dates   = svc(context, K_BOOKING).available_dates()
    context.user_data["date_week_offset"] = offset
    await _show_date_week(update, context, service, price, dates, week_offset=offset)


# ════════════════════════════════════════════════════════════════════════════════
# ВЫБОР ВРЕМЕНИ
# ════════════════════════════════════════════════════════════════════════════════

async def cb_date(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    date_str = update.callback_query.data.replace("date_", "")
    context.user_data["book_date"] = date_str

    slots = svc(context, K_BOOKING).free_slots(date_str)
    if not slots:
        await edit_or_reply(update,
            "Этот день уже занят. Выберите другой.",
            kb([("◀️ Назад", f"srv_{context.user_data.get('book_service', '')}")]))
        return
    buttons = [(fmt_slot(t), f"time_{t}") for t in slots]
    await edit_or_reply(update,
        f"📅 <b>{fmt_date(date_str)}</b>\n\nВыберите время:",
        kb(buttons + [("◀️ Назад", f"srv_{context.user_data.get('book_service', '')}")]))


async def cb_time(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    time_str = update.callback_query.data.replace("time_", "")
    context.user_data["book_time"] = time_str

    uid, _  = uid_uname(update)
    client  = svc(context, K_CLIENT).get(uid)

    if client and client.is_registered:
        await _show_confirm(update, context, client.display_name)
    else:
        set_step(context.user_data, "await_name")
        await edit_or_reply(update,
            "👤 Как вас зовут?")


async def _show_confirm(update, context, name: str):
    service      = context.user_data.get("book_service", "")
    date_str     = context.user_data.get("book_date", "")
    time_str     = context.user_data.get("book_time", "")
    price        = context.user_data.get("book_price", 0)
    week_offset  = context.user_data.get("date_week_offset", 0)
    await edit_or_reply(update,
        f"📋 <b>Подтверждение записи</b>\n\n"
        f"👤 {name}\n"
        f"💅 {service} — {format_eur(price)}\n"
        f"📅 {fmt_date(date_str)}\n"
        f"⏰ {fmt_slot(time_str)}\n\n"
        f"Всё верно?",
        kb([
            ("✅ Подтвердить",     "book_confirm"),
            # Изменить время — возврат к слотам той же даты
            ("◀️ Изменить время", "book_change_time"),
            # Изменить дату — возврат к неделе где была выбрана дата
            ("◀️ Изменить дату",  f"date_week_{week_offset}"),
        ]))


# ════════════════════════════════════════════════════════════════════════════════
# ОНБОРДИНГ — имя и телефон
# ════════════════════════════════════════════════════════════════════════════════

async def _step_name(update: Update, context: CallbackContext):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Введите имя (минимум 2 символа):")
        return
    context.user_data["onboard_name"] = name
    set_step(context.user_data, "await_phone")
    await update.message.reply_text(
        "📞 Введите ваш номер телефона:\n"
        "Например: +79001234567",
    )


async def handle_contact(update: Update, context: CallbackContext):
    contact = update.message.contact
    if not contact:
        return
    uid, _ = uid_uname(update)
    if not contact.user_id or contact.user_id != uid:
        return
    client = svc(context, K_CLIENT).get(uid)
    if client and client.is_registered:
        phone = normalize_phone(contact.phone_number)
        svc(context, K_CLIENT).set_profile(uid, client.name, phone)
        await update.message.reply_text("📱 Номер телефона обновлён.")


async def _step_phone(update: Update, context: CallbackContext):
    raw = (update.message.text or "").strip()
    if not validate_phone(raw):
        await update.message.reply_text(
            "Некорректный номер. Попробуйте ещё раз.\n"
            "Например: +79001234567",
        )
        return
    uid, _  = uid_uname(update)
    name    = context.user_data.pop("onboard_name", "Клиент")
    phone   = normalize_phone(raw)
    set_step(context.user_data, None)
    svc(context, K_CLIENT).set_profile(uid, name, phone)
    await _show_confirm(update, context, name)


# ════════════════════════════════════════════════════════════════════════════════
# ПОДТВЕРЖДЕНИЕ И ОТМЕНА ЧЕРНОВИКА
# ════════════════════════════════════════════════════════════════════════════════

async def cb_book_confirm(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    uid, _   = uid_uname(update)
    service  = context.user_data.get("book_service", "")
    date_str = context.user_data.get("book_date", "")
    time_str = context.user_data.get("book_time", "")

    result = svc(context, K_BOOKING).create(uid, service, date_str, time_str)
    if not result.ok:
        await edit_or_reply(update,
            f"⚠️ {result.error}",
            kb([("📅 Выбрать другое время", "book_service")] + back_main()))
        return

    b      = result.booking
    client = svc(context, K_CLIENT).get(uid)
    name   = client.display_name if client else "Клиент"
    phone  = client.phone if client else "—"

    await edit_or_reply(update,
        f"✅ <b>Запись подтверждена!</b>\n\n"
        f"{fmt_booking(b)}\n\n"
        f"Ждём вас! 💅\n\n"
        f"Если нужно перенести или отменить — откройте «📋 Мои записи».{_SIGN}",
        kb([("📋 Мои записи", "my_bookings")] + back_main()))

    await notify_admins(
        context.bot,
        f"📩 <b>Новая запись #{b.id}</b>\n\n"
        f"👤 {name}  📞 {phone}\n"
        f"{fmt_booking(b)}",
        kb([
            ("✅ Подтвердить", f"adm_req_confirm_{b.id}"),
            ("🔄 Перенести", f"reschedule_{b.id}"),
            ("👤 Карточка клиента", f"crm_{uid}"),
            ("📋 Мои записи", "adm_requests"),
        ], cols=2),
    )

    for k in ("book_service", "book_date", "book_time", "book_price"):
        context.user_data.pop(k, None)


async def cb_book_change_time(update: Update, context: CallbackContext):
    """Клиент нажал «Изменить время» — возвращаем к слотам той же даты."""
    await update.callback_query.answer()
    date_str    = context.user_data.get("book_date", "")
    week_offset = context.user_data.get("date_week_offset", 0)
    context.user_data.pop("book_time", None)

    if not date_str:
        await edit_or_reply(update, "Выберите дату заново:",
            kb([(f"◀️ К выбору даты", f"date_week_{week_offset}")] + back_main()))
        return

    slots = svc(context, K_BOOKING).free_slots(date_str)
    if not slots:
        await edit_or_reply(update,
            "На эту дату слотов больше нет. Выберите другую дату:",
            kb([(f"◀️ К выбору даты", f"date_week_{week_offset}")] + back_main()))
        return

    buttons = [(fmt_slot(t), f"time_{t}") for t in slots]
    await edit_or_reply(update,
        f"📅 <b>{fmt_date(date_str)}</b>\n\nВыберите другое время:",
        kb(buttons + [(f"◀️ К выбору даты", f"date_week_{week_offset}")]))


async def cb_book_cancel_draft(update: Update, context: CallbackContext):
    """Полная отмена черновика — возврат в главное меню."""
    await update.callback_query.answer()
    set_step(context.user_data, None)
    for k in ("book_service", "book_date", "book_time", "book_price"):
        context.user_data.pop(k, None)
    await edit_or_reply(update, "Хорошо, запись не создана.", kb(back_main()))


# ════════════════════════════════════════════════════════════════════════════════
# МОИ ЗАПИСИ
# ════════════════════════════════════════════════════════════════════════════════

async def my_bookings(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    uid, _ = uid_uname(update)
    rows   = svc(context, K_BOOKING).user_active(uid)
    if not rows:
        past  = svc(context, K_BOOKING).user_past(uid)
        extra = [(f"🕐 {fmt_date(b.date)} — {b.service}", f"bm_{b.id}") for b in past[:3]]
        msg   = ("У вас нет предстоящих записей.\n\n📋 <b>Прошлые визиты:</b>"
                 if extra else "У вас пока нет записей.")
        await edit_or_reply(update, msg,
            kb([("💅 Записаться", "book_service")] + extra + back_main()))
        return
    buttons = [
        (f"{fmt_date(b.date)} {fmt_slot(b.time)} — {b.service}", f"bm_{b.id}")
        for b in rows
    ]
    await edit_or_reply(update,
        "📋 <b>Ваши записи:</b>",
        kb(buttons + back_main()))


async def cb_booking_menu(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    bid       = int(update.callback_query.data.replace("bm_", ""))
    uid_bm, _ = uid_uname(update)
    b         = svc(context, K_BOOKING).get(bid)
    if not b:
        await edit_or_reply(update, "Запись не найдена.", kb(back_main()))
        return
    if b.user_id != uid_bm and uid_bm not in settings.ADMIN_IDS:
        await edit_or_reply(update, "Нет доступа к этой записи.", kb(back_main()))
        return
    from app.core.time_utils import local_today
    today = local_today().isoformat()
    is_past      = b.date < today
    is_active    = b.status.value == "active"
    can_manage   = is_active and not is_past
    info = fmt_booking(b)
    if is_past and is_active:
        info += chr(10) + chr(10) + "<i>Визит состоялся — изменения недоступны</i>"
    elif not is_active:
        info += chr(10) + chr(10) + "<i>Запись завершена — изменения недоступны</i>"
    if can_manage:
        buttons = [
            ("🔄 Перенести",       f"reschedule_{bid}"),
            ("❌ Отменить запись", f"cancel_ask_{bid}"),
            ("◀️ Назад",           "my_bookings"),
        ]
    else:
        buttons = [("◀️ Назад", "my_bookings")]
    await edit_or_reply(update, info, kb(buttons))

async def cb_cancel_ask(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    bid = int(update.callback_query.data.replace("cancel_ask_", ""))
    b   = svc(context, K_BOOKING).get(bid)
    if not b:
        await edit_or_reply(update, "Запись не найдена.", kb(back_main()))
        return
    await edit_or_reply(update,
        f"Отменить запись?\n\n{fmt_booking(b)}",
        kb([
            ("✅ Да, отменить", f"cancel_yes_{bid}"),
            ("◀️ Назад",        f"bm_{bid}"),
        ]))


async def cb_cancel_yes(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    bid    = int(update.callback_query.data.replace("cancel_yes_", ""))
    uid, _ = uid_uname(update)
    result = svc(context, K_BOOKING).cancel(bid, uid)
    if not result.ok:
        await edit_or_reply(update, f"⚠️ {result.error}", kb(back_main()))
        return
    b = result.booking
    await edit_or_reply(update,
        f"✅ Запись отменена.\n\n{fmt_booking(b)}",
        kb([("💅 Записаться снова", "book_service")] + back_main()))
    await notify_admins(context.bot,
        f"❌ Клиент отменил запись:\n\n{fmt_booking(b)}")


# ════════════════════════════════════════════════════════════════════════════════
# ПЕРЕНОС — КЛИЕНТ  (60 дней, с недельным календарём)
# ════════════════════════════════════════════════════════════════════════════════

async def cb_reschedule(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    uid, _ = uid_uname(update)
    lim    = svc(context, K_LIMITERS)
    ok, w  = lim.reschedule.check(uid)
    if not ok:
        await edit_or_reply(update,
            f"⏳ Слишком много запросов на перенос. Подождите {w} сек.",
            kb(back_main()))
        return
    bid  = int(update.callback_query.data.replace("reschedule_", ""))
    b    = svc(context, K_BOOKING).get(bid)
    if not b:
        await edit_or_reply(update, "Запись не найдена.", kb(back_main()))
        return
    context.user_data["reschedule_bid"]         = bid
    context.user_data["reschedule_week_offset"] = 0

    dates = svc(context, K_BOOKING).available_dates()  # уже 60 дней после фикса settings
    if not dates:
        await edit_or_reply(update, "Свободных дат нет.",
            kb([("◀️ Назад", f"bm_{bid}")]))
        return
    await _show_reschedule_week(update, context, bid, dates, week_offset=0)


async def _show_reschedule_week(update, context, bid: int, all_dates, week_offset: int):
    markup, week_header = build_calendar_markup(
        all_dates       = all_dates,
        prefix          = "rdate_",
        week_offset     = week_offset,
        week_nav_prefix = "rweek_",
        back_buttons    = [("◀️ Назад", f"bm_{bid}")],
    )
    await edit_or_reply(update,
        f"🔄 <b>Перенос записи</b>\n\n{week_header}\n\nВыберите новую дату:",
        markup)


async def cb_reschedule_week(update: Update, context: CallbackContext):
    """Переключение недели в календаре переноса: rweek_0, rweek_1..."""
    await update.callback_query.answer()
    offset = int(update.callback_query.data.replace("rweek_", ""))
    bid    = context.user_data.get("reschedule_bid", 0)
    dates  = svc(context, K_BOOKING).available_dates()
    context.user_data["reschedule_week_offset"] = offset
    await _show_reschedule_week(update, context, bid, dates, week_offset=offset)


async def cb_rdate(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    date_str = update.callback_query.data.replace("rdate_", "")
    context.user_data["reschedule_date"] = date_str
    bid   = context.user_data.get("reschedule_bid", 0)
    slots = svc(context, K_BOOKING).free_slots(date_str)
    if not slots:
        await edit_or_reply(update,
            "Этот день занят. Выберите другой.",
            kb([("◀️ Назад", f"reschedule_{bid}")]))
        return
    buttons = [(fmt_slot(t), f"rtime_{t}") for t in slots]
    await edit_or_reply(update,
        f"📅 <b>{fmt_date(date_str)}</b>\n\nВыберите время:",
        kb(buttons + [("◀️ Назад", f"reschedule_{bid}")]))


async def cb_rtime(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    time_str = update.callback_query.data.replace("rtime_", "")
    bid      = context.user_data.get("reschedule_bid", 0)
    date_str = context.user_data.get("reschedule_date", "")
    await edit_or_reply(update,
        f"🔄 Перенести запись на:\n"
        f"📅 {fmt_date(date_str)}  ⏰ {fmt_slot(time_str)}\n\nПодтвердить?",
        kb([
            ("✅ Да", f"ryes_{bid}_{date_str}_{time_str}"),
            ("◀️ Назад", f"rdate_{date_str}"),
        ]))


async def cb_reschedule_yes(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    parts    = update.callback_query.data.split("_", 3)
    # ryes_<bid>_<date>_<time>
    bid      = int(parts[1])
    date_str = parts[2]
    time_str = parts[3]
    b_before = svc(context, K_BOOKING).get(bid)
    if not b_before:
        await edit_or_reply(update, "Запись не найдена.", kb(back_main()))
        return

    uid, _ = uid_uname(update)
    result = svc(context, K_BOOKING).reschedule_by_client(bid, uid, date_str, time_str)
    if not result.ok:
        await edit_or_reply(
            update,
            f"⚠️ {result.error}",
            kb([("◀️ Выбрать другое время", f"rdate_{date_str}"), ("📋 Мои записи", "my_bookings")]),
        )
        return

    b_after = result.booking
    await edit_or_reply(
        update,
        f"✅ <b>Запись перенесена!</b>\n\n"
        f"{fmt_booking(b_after)}\n\n"
        f"{_SIGN}",
        kb([("📋 Мои записи", "my_bookings")] + back_main()),
    )

    actor_is_admin = uid in settings.ADMIN_IDS
    actor_client = svc(context, K_CLIENT).get(b_before.user_id)
    actor_name = actor_client.display_name if actor_client else "Клиент"
    action_label = "Мастер перенёс запись" if actor_is_admin else "Клиент перенёс запись"
    await notify_admins(
        context.bot,
        f"🔄 <b>{action_label}</b>\n\n"
        f"👤 {actor_name}\n"
        f"Было: {fmt_date(b_before.date)} {fmt_slot(b_before.time)}\n"
        f"Стало: {fmt_date(b_after.date)} {fmt_slot(b_after.time)}\n"
        f"💅 {b_after.service}",
    )

# ════════════════════════════════════════════════════════════════════════════════
# ОТЗЫВ
# ════════════════════════════════════════════════════════════════════════════════

async def cb_rating(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    data   = update.callback_query.data  # review_<bid>_<rating>
    parts  = data.split("_")
    bid    = int(parts[1])
    rating = int(parts[2])

    context.user_data["review_bid"]    = bid
    context.user_data["review_rating"] = rating
    set_step(context.user_data, "await_review_text")

    await edit_or_reply(update,
        f"{stars(rating)} Спасибо за оценку!\n\n"
        f"Напишите пару слов о визите:",
        kb([("⏭ Пропустить", "review_skip_text")]))


async def _step_review_text(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    if text.startswith("/"):
        text = ""

    uid, _  = uid_uname(update)
    bid     = context.user_data.pop("review_bid", None)
    rating  = context.user_data.pop("review_rating", 5)
    set_step(context.user_data, None)

    if bid is None:
        return

    rid = svc(context, K_REVIEW).add(uid, bid, rating, text)
    context.user_data["review_id"] = rid

    client   = svc(context, K_CLIENT).get(uid)
    name     = client.display_name if client else "Клиент"
    b        = svc(context, K_BOOKING).get(bid)
    svc_name = b.service if b else "—"
    caption  = (
        f"⭐ <b>Новый отзыв</b>\n\n"
        f"👤 {name}\n"
        f"💅 {svc_name}\n"
        f"{stars(rating)} ({rating}/5)\n"
    )
    if text:
        caption += f"💬 {text}\n"
    await notify_admins(context.bot, caption)

    set_step(context.user_data, "await_review_photo")
    await update.message.reply_text(
        f"✅ Отзыв сохранён! {stars(rating)}\n\n"
        "📸 Хотите прикрепить фото результата? Отправьте фото или нажмите «Пропустить».",
        reply_markup=kb([("⏭ Пропустить", "review_photo_skip")])
    )


async def _step_review_photo(update: Update, context: CallbackContext):
    if not update.message or not update.message.photo:
        await update.message.reply_text(
            "Отправьте фото или нажмите «Пропустить».",
            reply_markup=kb([("⏭ Пропустить", "review_photo_skip")])
        )
        return

    photo_file_id = update.message.photo[-1].file_id
    rid           = context.user_data.pop("review_id", None)
    set_step(context.user_data, None)

    if rid:
        svc(context, K_REVIEW).attach_photo(rid, photo_file_id)

    uid, _  = uid_uname(update)
    client  = svc(context, K_CLIENT).get(uid)
    name    = client.display_name if client else "Клиент"
    await notify_admins_photo(context.bot, photo_file_id, f"📸 Фото от клиента {name}")

    await update.message.reply_text(
        "📸 Фото прикреплено к отзыву! Спасибо 🙏",
        reply_markup=kb(back_main())
    )


async def cb_review_photo_skip(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    context.user_data.pop("review_id", None)
    set_step(context.user_data, None)
    await edit_or_reply(update, "Спасибо за отзыв! 🙏", kb(back_main()))


async def cb_review_skip(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    context.user_data.pop("review_bid", None)
    context.user_data.pop("review_rating", None)
    set_step(context.user_data, None)
    await edit_or_reply(update, "Хорошо, в следующий раз 😊", kb(back_main()))


async def cb_review_skip_text(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    uid, _  = uid_uname(update)
    bid     = context.user_data.pop("review_bid", None)
    rating  = context.user_data.pop("review_rating", 5)
    set_step(context.user_data, None)

    if bid is None:
        return

    rid = svc(context, K_REVIEW).add(uid, bid, rating, "")
    context.user_data["review_id"] = rid

    client   = svc(context, K_CLIENT).get(uid)
    name     = client.display_name if client else "Клиент"
    b        = svc(context, K_BOOKING).get(bid)
    svc_name = b.service if b else "—"
    await notify_admins(context.bot,
        f"⭐ <b>Новый отзыв</b>\n\n"
        f"👤 {name}\n💅 {svc_name}\n{stars(rating)} ({rating}/5)\n")

    set_step(context.user_data, "await_review_photo")
    await edit_or_reply(update,
        f"✅ Оценка сохранена! {stars(rating)}\n\n"
        "📸 Хотите прикрепить фото результата?",
        kb([("⏭ Пропустить", "review_photo_skip")]))


# ════════════════════════════════════════════════════════════════════════════════
# ПОРТФОЛИО
# ════════════════════════════════════════════════════════════════════════════════

async def show_portfolio(update: Update, context: CallbackContext):
    """Показываем первое фото портфолио с кнопками навигации."""
    await update.callback_query.answer()
    await _show_portfolio_photo(update, context, index=0)


async def cb_portfolio_back(update: Update, context: CallbackContext):
    """Выход из портфолио — возврат в главное меню."""
    await update.callback_query.answer()
    set_step(context.user_data, None)
    await cmd_start(update, context)

async def cb_portfolio_nav(update: Update, context: CallbackContext):
    """Навигация по портфолио: portfolio_1, portfolio_2 ..."""
    await update.callback_query.answer()
    index = int(update.callback_query.data.replace("portfolio_", ""))
    await _show_portfolio_photo(update, context, index=index)


async def _show_portfolio_photo(update, context, index: int):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    photos = svc(context, K_PORTFOLIO).all()
    if not photos:
        await edit_or_reply(update,
            "📸 Портфолио пока пусто.",
            kb(back_main()))
        return

    total   = len(photos)
    index   = index % total          # бесконечная прокрутка
    photo   = photos[index]
    caption = f"📸 <b>Портфолио</b>  {index + 1} / {total}"

    # Навигация — всегда обе кнопки (бесконечная прокрутка)
    prev_i = (index - 1) % total
    next_i = (index + 1) % total
    nav_row = [
        InlineKeyboardButton("◀️", callback_data=f"portfolio_{prev_i}"),
        InlineKeyboardButton("▶️", callback_data=f"portfolio_{next_i}"),
    ]
    markup = InlineKeyboardMarkup([
        nav_row,
        [InlineKeyboardButton("◀️ Назад", callback_data="portfolio_back")],
    ])

    cq = update.callback_query
    # Если уже показываем фото — редактируем медиа (плавно, без нового сообщения)
    if cq.message.photo:
        from telegram import InputMediaPhoto
        try:
            await cq.message.edit_media(
                media=InputMediaPhoto(
                    media=photo["file_id"],
                    caption=caption,
                    parse_mode="HTML",
                ),
                reply_markup=markup,
            )
            return
        except Exception:
            pass
    # Первый показ — удаляем текстовое сообщение, отправляем фото
    await send_photo_or_edit(update, context, photo["file_id"], caption, markup)
