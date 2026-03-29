"""
main.py — Salon Bot v14.
Точка входа. Все роуты обновлены под новые callback_data.
"""
from __future__ import annotations

import datetime
import logging

import pytz
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.bot.handlers import client as ch
from app.bot.handlers.client import handle_contact
from app.bot.handlers import master as mh
from app.bot.helpers import (
    K_BAN,
    K_BOOKING, K_CLIENT, K_DISPATCH, K_LIMITERS, K_NOTE, K_PORTFOLIO, K_REVIEW, K_SERVICE,
    StepDispatcher, fmt_booking, kb, notify_admins, safe_send, stars,
)
from app.core.callback_dedup import is_duplicate
from app.core.database import run_migrations, seed_services
from app.core.healthcheck import start as hc_start, stop as hc_stop, set_ready
from app.core.rate_limiter import Limiters, setup_logging
from app.core.settings import settings
from app.core.time_utils import fmt_date, fmt_slot
from app.services import excel_worker
from app.services.reminder_guard import send_with_mark
from app.services.services import (
    BlacklistService, BookingService, ClientService,
    NoteService, PortfolioService, ReviewService, ServiceService, TimeSlotService,
)

logger: logging.Logger

_SIGN = f"\n\n— {settings.MASTER_USERNAME}"
_TZ   = pytz.timezone(settings.TIMEZONE)


# ════════════════════════════════════════════════════════════════════════════════
# ФОНОВЫЕ ЗАДАЧИ
# ════════════════════════════════════════════════════════════════════════════════

async def job_complete_past(context):
    """00:05 МСК — переводит active → completed для прошедших записей."""
    try:
        svc: BookingService = context.bot_data[K_BOOKING]
        svc.complete_past_bookings()
    except Exception:
        logger.exception("job_complete_past failed")


async def job_reminders(context):
    """Каждые 5 мин — напоминания за 24ч и за 2ч."""
    try:
        booking_svc: BookingService = context.bot_data[K_BOOKING]
        due = booking_svc.pending_reminders()
    except Exception:
        logger.exception("job_reminders: failed to fetch candidates")
        return

    for b in due["24h"]:
        async def _send_24h(b=b):
            await safe_send(
                context.bot, b.user_id,
                f"🔔 <b>Напоминание!</b> Завтра у вас запись:\n\n"
                f"{fmt_booking(b)}\n\n"
                f"📍 {settings.MASTER_ADDRESS}{_SIGN}",
                kb([
                    ("❌ Отменить запись", f"cancel_ask_{b.id}"),
                    ("🔄 Перенести",       f"reschedule_{b.id}"),
                ]),
            )
        await send_with_mark(
            bot=context.bot, booking=b, field="notified_24h",
            mark_fn=booking_svc.mark, send_fn=_send_24h,
        )

    for b in due["2h"]:
        async def _send_2h(b=b):
            await safe_send(
                context.bot, b.user_id,
                f"⏰ <b>До вашей записи 2 часа!</b>\n\n"
                f"{fmt_booking(b)}\n\n"
                f"📍 {settings.MASTER_ADDRESS}{_SIGN}",
                kb([("❌ Отменить запись", f"cancel_ask_{b.id}")]),
            )
        await send_with_mark(
            bot=context.bot, booking=b, field="notified_1h",
            mark_fn=booking_svc.mark, send_fn=_send_2h,
        )


async def job_return_notify(context):
    """Каждый час — приглашение на повторный визит через 21 день."""
    try:
        booking_svc: BookingService = context.bot_data[K_BOOKING]
        client_svc:  ClientService  = context.bot_data[K_CLIENT]
        candidates = booking_svc.return_candidates()
    except Exception:
        logger.exception("job_return_notify: failed to fetch candidates")
        return

    for b in candidates:
        client = None
        try:
            client = client_svc.get(b.user_id)
        except Exception:
            pass
        name = client.display_name if client else "Клиент"

        async def _send_return(b=b, name=name):
            await safe_send(
                context.bot, b.user_id,
                f"{name}, твои ноготочки уже соскучились! 💗\n\n"
                f"Записывайся на коррекцию — жду тебя! 💅{_SIGN}",
                kb([("📅 Записаться", "book_service")]),
            )

        sent = await send_with_mark(
            bot=context.bot, booking=b, field="notified_return",
            mark_fn=booking_svc.mark, send_fn=_send_return,
        )
        if sent:
            logger.info("return_notify uid=%s", b.user_id)


async def job_review_requests(context):
    """Каждые 15 мин — запрос отзыва через 4ч после визита."""
    try:
        booking_svc: BookingService = context.bot_data[K_BOOKING]
        client_svc:  ClientService  = context.bot_data[K_CLIENT]
        candidates = booking_svc.review_candidates()
    except Exception:
        logger.exception("job_review_requests: failed to fetch candidates")
        return

    for b in candidates:
        client = None
        try:
            client = client_svc.get(b.user_id)
        except Exception:
            pass
        name = client.display_name if client else "Клиент"

        async def _send_review(b=b, name=name):
            await safe_send(
                context.bot, b.user_id,
                f"{name}, как вам результат? 💅\n\nОцените, пожалуйста, работу:",
                kb([
                    ("⭐⭐⭐⭐⭐ 5",  f"review_{b.id}_5"),
                    ("⭐⭐⭐⭐ 4",    f"review_{b.id}_4"),
                    ("⭐⭐⭐ 3",      f"review_{b.id}_3"),
                    ("⭐⭐ 2",        f"review_{b.id}_2"),
                    ("⭐ 1",          f"review_{b.id}_1"),
                    ("⏭ Пропустить", f"review_skip_{b.id}_0"),
                ]),
            )

        sent = await send_with_mark(
            bot=context.bot, booking=b, field="review_sent",
            mark_fn=booking_svc.mark, send_fn=_send_review,
        )
        if sent:
            logger.info("review_request bid=%s uid=%s", b.id, b.user_id)


async def job_expire_reschedules(context):
    """Каждый час — отклоняем просроченные запросы на перенос."""
    try:
        booking_svc: BookingService = context.bot_data[K_BOOKING]
        expired = booking_svc.expire_reschedules()
    except Exception:
        logger.exception("job_expire_reschedules: failed")
        return

    for rr in expired:
        try:
            b = booking_svc.get(rr.booking_id)
            if b:
                await safe_send(
                    context.bot, b.user_id,
                    f"❌ Запрос на перенос истёк — мастер не ответил "
                    f"в течение {settings.RESCHEDULE_TIMEOUT_HOURS} ч.\n\n"
                    f"Ваша запись остаётся: {fmt_date(b.date)} {fmt_slot(b.time)}\n"
                    f"По вопросам: {settings.MASTER_USERNAME}",
                )
        except Exception:
            logger.exception("job_expire_reschedules: rr_id=%s", rr.id)


# ════════════════════════════════════════════════════════════════════════════════
# СБОРКА ПРИЛОЖЕНИЯ
# ════════════════════════════════════════════════════════════════════════════════



async def error_handler(update, context):
    """Логирует все необработанные исключения и уведомляет мастера."""
    logger.exception("Unhandled exception", exc_info=context.error)
    try:
        err_text = "ERR: " + type(context.error).__name__ + ": " + str(context.error)
        for admin_id in settings.ADMIN_IDS:
            await context.bot.send_message(
                chat_id=admin_id,
                text=err_text,
            )
    except Exception:
        pass


async def job_wal_checkpoint(context):
    """Каждые 6 часов сбрасывает WAL-лог SQLite в основной файл."""
    try:
        from app.core.database import get_connection
        get_connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.debug("WAL checkpoint done")
    except Exception:
        logger.exception("WAL checkpoint failed")

def build_application() -> Application:
    app = Application.builder().token(settings.BOT_TOKEN).build()

    dispatcher = StepDispatcher()
    app.bot_data[K_BAN]          = BlacklistService()
    app.bot_data[K_CLIENT]       = ClientService()
    app.bot_data[K_BOOKING]      = BookingService()
    app.bot_data[K_NOTE]         = NoteService()
    app.bot_data[K_REVIEW]       = ReviewService()
    app.bot_data[K_SERVICE]      = ServiceService()
    app.bot_data[K_PORTFOLIO]    = PortfolioService(settings.DB_PATH)
    app.bot_data["svc_timeslot"] = TimeSlotService()
    app.bot_data[K_LIMITERS]     = Limiters(settings)
    app.bot_data[K_DISPATCH]     = dispatcher

    ch.register_steps(dispatcher)
    mh.register_steps(dispatcher)

    jq = app.job_queue
    jq.run_daily(job_complete_past,          time=datetime.time(0, 5, tzinfo=_TZ))
    jq.run_repeating(job_reminders,          interval=300,  first=30)
    jq.run_repeating(job_return_notify,      interval=3600, first=60)
    jq.run_repeating(job_review_requests,    interval=900,  first=120)
    jq.run_repeating(job_expire_reschedules, interval=3600, first=180)
    jq.run_repeating(job_wal_checkpoint,    interval=21600, first=300)

    # ── Deduplication middleware ─────────────────────────────────────────────
    # noop: заглушка для неактивных кнопок (заголовки сетки-календаря)
    async def _noop(update, context):
        if update.callback_query:
            await update.callback_query.answer()
    app.add_handler(CallbackQueryHandler(_noop, pattern="^noop$"))

    from telegram.ext import TypeHandler, ApplicationHandlerStop
    from telegram import Update as _Update

    async def _dedup_middleware(update: _Update, context):
        if update.callback_query and is_duplicate(update.callback_query.id):
            try:
                await update.callback_query.answer()
            except Exception:
                pass
            raise ApplicationHandlerStop

    app.add_handler(TypeHandler(_Update, _dedup_middleware), group=-1)

    # ════════════════════════════════════════════════════════════════════════
    # КЛИЕНТ — навигация
    # ════════════════════════════════════════════════════════════════════════
    app.add_handler(CommandHandler(["start", "menu"], ch.cmd_start))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, ch.handle_text))
    app.add_handler(MessageHandler(filters.TEXT  & ~filters.COMMAND, ch.handle_text))
    app.add_handler(CallbackQueryHandler(ch.cmd_start,    pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(ch.show_prices,      pattern="^prices$"))
    app.add_handler(CallbackQueryHandler(ch.show_portfolio,   pattern="^portfolio$"))
    app.add_handler(CallbackQueryHandler(ch.cb_portfolio_nav,  pattern=r"^portfolio_\d+$"))
    app.add_handler(CallbackQueryHandler(ch.cb_portfolio_back, pattern="^portfolio_back$"))
    app.add_handler(CallbackQueryHandler(ch.show_address, pattern="^show_address$"))
    app.add_handler(CallbackQueryHandler(ch.show_contact, pattern="^contact$"))
    app.add_handler(CallbackQueryHandler(ch.show_about,   pattern="^about$"))

    # ════════════════════════════════════════════════════════════════════════
    # КЛИЕНТ — запись
    # ════════════════════════════════════════════════════════════════════════
    app.add_handler(CallbackQueryHandler(ch.book_service,        pattern="^book_service$"))
    app.add_handler(CallbackQueryHandler(ch.cb_service,          pattern="^srv_"))
    app.add_handler(CallbackQueryHandler(ch.cb_date_week,        pattern=r"^date_week_\d+$"))
    app.add_handler(CallbackQueryHandler(ch.cb_date,             pattern=r"^date_\d{4}-\d{2}-\d{2}$"))
    app.add_handler(CallbackQueryHandler(ch.cb_time,             pattern=r"^time_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(ch.cb_book_confirm,     pattern="^book_confirm$"))
    app.add_handler(CallbackQueryHandler(ch.cb_book_change_time, pattern="^book_change_time$"))
    app.add_handler(CallbackQueryHandler(ch.cb_book_cancel_draft,pattern="^book_cancel_draft$"))

    # ════════════════════════════════════════════════════════════════════════
    # КЛИЕНТ — мои записи + перенос
    # ════════════════════════════════════════════════════════════════════════
    app.add_handler(CallbackQueryHandler(ch.my_bookings,       pattern="^my_bookings$"))
    app.add_handler(CallbackQueryHandler(ch.cb_booking_menu,   pattern=r"^bm_\d+$"))
    app.add_handler(CallbackQueryHandler(ch.cb_cancel_ask,     pattern=r"^cancel_ask_\d+$"))
    app.add_handler(CallbackQueryHandler(ch.cb_cancel_yes,     pattern=r"^cancel_yes_\d+$"))
    app.add_handler(CallbackQueryHandler(ch.cb_reschedule,     pattern=r"^reschedule_\d+$"))
    # ← НОВЫЙ: навигация по неделям при переносе
    app.add_handler(CallbackQueryHandler(ch.cb_reschedule_week,pattern=r"^rweek_\d+$"))
    app.add_handler(CallbackQueryHandler(ch.cb_rdate,          pattern=r"^rdate_\d{4}-\d{2}-\d{2}$"))
    app.add_handler(CallbackQueryHandler(ch.cb_rtime,          pattern=r"^rtime_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(ch.cb_reschedule_yes, pattern=r"^ryes_\d+_\d{4}-\d{2}-\d{2}_\d{2}:\d{2}$"))

    # ════════════════════════════════════════════════════════════════════════
    # КЛИЕНТ — отзыв
    # ════════════════════════════════════════════════════════════════════════
    app.add_handler(CallbackQueryHandler(ch.cb_rating,           pattern=r"^review_\d+_[1-5]$"))
    app.add_handler(CallbackQueryHandler(ch.cb_review_skip,      pattern=r"^review_skip_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(ch.cb_review_skip_text, pattern="^review_skip_text$"))
    app.add_handler(CallbackQueryHandler(ch.cb_review_photo_skip,pattern="^review_photo_skip$"))

    # ════════════════════════════════════════════════════════════════════════
    # МАСТЕР — меню и календарь
    # ════════════════════════════════════════════════════════════════════════
    app.add_handler(CallbackQueryHandler(mh.show_master_menu,   pattern="^master_menu$"))
    app.add_handler(CallbackQueryHandler(mh.adm_today,          pattern="^adm_today$"))
    app.add_handler(CallbackQueryHandler(mh.adm_calendar,       pattern="^adm_calendar$"))
    app.add_handler(CallbackQueryHandler(mh.adm_calendar_week,  pattern=r"^cal_week_\d+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_calendar_day,   pattern=r"^calday_\d{4}-\d{2}-\d{2}$"))
    app.add_handler(CallbackQueryHandler(mh.adm_cancel_booking, pattern=r"^adm_cancel_\d+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_cancel_confirmed,pattern=r"^adm_cancel_yes_\d+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_rconfirm,       pattern=r"^adm_rconfirm_\d+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_rdecline,       pattern=r"^adm_rdecline_\d+$"))

    # Обратная совместимость (старые callback из кэша)
    app.add_handler(CallbackQueryHandler(mh.adm_block,    pattern="^adm_block$"))
    app.add_handler(CallbackQueryHandler(mh.cb_block_day, pattern=r"^block_\d{4}-\d{2}-\d{2}$"))
    app.add_handler(CallbackQueryHandler(mh.adm_unblock,  pattern="^adm_unblock$"))
    app.add_handler(CallbackQueryHandler(mh.cb_unblock,   pattern="^unblock_"))

    # ════════════════════════════════════════════════════════════════════════
    # МАСТЕР — CRM
    # ════════════════════════════════════════════════════════════════════════
    app.add_handler(CallbackQueryHandler(mh.adm_crm,        pattern="^adm_crm$"))
    app.add_handler(CallbackQueryHandler(mh.cb_crm_client,  pattern=r"^crm_\d+$"))
    app.add_handler(CallbackQueryHandler(mh.cb_note_start,  pattern=r"^note_\d+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_reviews,    pattern="^adm_reviews$"))
    app.add_handler(CallbackQueryHandler(mh.adm_stats,      pattern="^adm_stats$"))

    # ════════════════════════════════════════════════════════════════════════
    # МАСТЕР — услуги
    # ════════════════════════════════════════════════════════════════════════
    app.add_handler(CallbackQueryHandler(mh.adm_services,       pattern="^adm_services$"))
    app.add_handler(CallbackQueryHandler(mh.adm_srv_add,        pattern="^adm_srv_add$"))
    app.add_handler(CallbackQueryHandler(mh.adm_srv_edit,       pattern=r"^adm_srv_edit_.+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_srv_price,      pattern=r"^adm_srv_price_.+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_srv_rename,     pattern=r"^adm_srv_rename_.+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_srv_delete,     pattern=r"^adm_srv_delete_(?!yes_).+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_srv_delete_yes, pattern=r"^adm_srv_delete_yes_.+$"))

    # ════════════════════════════════════════════════════════════════════════
    # МАСТЕР — расписание (обновлённые callback: adm_sched_*)
    # Старые adm_slot_* сохранены для обратной совместимости.
    # ════════════════════════════════════════════════════════════════════════
    app.add_handler(CallbackQueryHandler(mh.adm_schedule,         pattern="^adm_schedule$"))
    # Новые callback
    app.add_handler(CallbackQueryHandler(mh.adm_sched_add,        pattern="^adm_sched_add$"))
    app.add_handler(CallbackQueryHandler(mh.adm_sched_toggle,     pattern=r"^adm_sched_toggle_(on|off)_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(mh.adm_sched_delete,     pattern=r"^adm_sched_del_(?!yes_)\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(mh.adm_sched_delete_yes, pattern=r"^adm_sched_del_yes_\d{2}:\d{2}$"))
    # Обратная совместимость — старые callback
    app.add_handler(CallbackQueryHandler(mh.adm_slot_add,        pattern="^adm_slot_add$"))
    app.add_handler(CallbackQueryHandler(mh.adm_slot_toggle,     pattern=r"^adm_slot_o(n|ff)_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(mh.adm_slot_delete,     pattern=r"^adm_slot_del_(?!yes_)\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(mh.adm_slot_delete_yes, pattern=r"^adm_slot_del_yes_\d{2}:\d{2}$"))

    # ════════════════════════════════════════════════════════════════════════
    # МАСТЕР — блокировка дней и слотов
    # ВАЖНО: adm_slot_off_/adm_slot_on_ с датой идут ПЕРЕД голыми HH:MM
    # ════════════════════════════════════════════════════════════════════════
    app.add_handler(CallbackQueryHandler(mh.adm_slot_off,   pattern=r"^adm_slot_off_\d{4}-\d{2}-\d{2}_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(mh.adm_slot_on,    pattern=r"^adm_slot_on_\d{4}-\d{2}-\d{2}_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(mh.adm_day_close,  pattern="^adm_day_close_"))
    app.add_handler(CallbackQueryHandler(mh.adm_day_open,   pattern="^adm_day_open_"))

    # ════════════════════════════════════════════════════════════════════════
    # МАСТЕР — О мастере
    # ════════════════════════════════════════════════════════════════════════
    app.add_handler(CallbackQueryHandler(mh.adm_portfolio,             pattern="^adm_portfolio$"))
    app.add_handler(CallbackQueryHandler(mh.adm_portfolio_add,         pattern="^adm_portfolio_add$"))
    app.add_handler(CallbackQueryHandler(mh.adm_portfolio_delete,      pattern=r"^adm_portfolio_del_(?!yes_)\d+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_portfolio_delete_yes,  pattern=r"^adm_portfolio_del_yes_\d+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_about,                 pattern="^adm_about$"))
    app.add_handler(CallbackQueryHandler(mh.adm_about_edit_bio,     pattern="^adm_about_edit_bio$"))
    app.add_handler(CallbackQueryHandler(mh.adm_about_edit_photo,   pattern="^adm_about_edit_photo$"))
    app.add_handler(CallbackQueryHandler(mh.adm_about_clear_photo,  pattern="^adm_about_clear_photo$"))
    app.add_handler(CallbackQueryHandler(mh.adm_about_edit_address, pattern="^adm_about_edit_address$"))
    app.add_handler(CallbackQueryHandler(mh.adm_about_edit_contact, pattern="^adm_about_edit_contact$"))

    # ════════════════════════════════════════════════════════════════════════
    # МАСТЕР — прочее
    # ════════════════════════════════════════════════════════════════════════
    app.add_handler(CallbackQueryHandler(mh.adm_export_clients,          pattern="^adm_export_clients$"))
    app.add_handler(CallbackQueryHandler(mh.adm_import_clients,          pattern="^adm_import_clients$"))
    app.add_handler(CallbackQueryHandler(mh.adm_invite,                  pattern="^adm_invite$"))
    app.add_handler(CallbackQueryHandler(mh.adm_blacklist,               pattern="^adm_blacklist$"))
    app.add_handler(CallbackQueryHandler(mh.adm_ban_client,              pattern=r"^adm_ban_\d+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_ban_confirmed,           pattern=r"^adm_ban_yes_\d+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_unban_client,            pattern=r"^adm_unban_\d+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_delete_client,           pattern=r"^adm_delete_client_\d+$"))
    app.add_handler(CallbackQueryHandler(mh.adm_delete_client_confirmed, pattern=r"^adm_delete_client_yes_\d+$"))

    app.add_error_handler(error_handler)
    return app


# ════════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ════════════════════════════════════════════════════════════════════════════════

def main():
    global logger
    logger = setup_logging(settings.LOGS_DIR)
    logger.info("Запуск Salon Bot v14...")

    # 1. Миграции БД
    run_migrations()

    # 2. Seed — заполняем дефолтами если таблицы пустые
    seed_services(settings.SERVICES)
    from app.repositories.repo import TimeSlotRepo
    from app.core.database import atomic as _atomic, get_db as _get_db
    with _get_db() as db:
        if not TimeSlotRepo.all_active(db):
            with _atomic() as db2:
                TimeSlotRepo.seed(db2, settings.TIME_SLOTS)

    # 3. Синхронизируем settings из БД
    ServiceService().sync()
    TimeSlotService().sync()

    excel_worker.start()
    app = build_application()
    hc_start(port=8080)
    logger.info("✅ Бот запущен.")
    try:
        set_ready(True)
        app.run_polling(drop_pending_updates=True)
    finally:
        set_ready(False)
        excel_worker.stop()
        hc_stop()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    main()
