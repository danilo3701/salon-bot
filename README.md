# Salon Bot — Appointment Booking for Beauty Salons

> Production Telegram bot for booking nail appointments, managing schedules, and automating client communications — deployed for a real salon in St. Petersburg.

[![CI](https://github.com/pavel-ai-dev/salon-bot-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/pavel-ai-dev/salon-bot-ai/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://python.org)
[![PTB](https://img.shields.io/badge/python--telegram--bot-21.x-green)](https://python-telegram-bot.org)
[![Tests](https://img.shields.io/badge/Tests-30%2F30-brightgreen)](tests/)
[![Production](https://img.shields.io/badge/Status-Live-brightgreen)](https://t.me)

## Problem

Beauty salon masters spend 2–3 hours/day on manual booking: answering messages, maintaining Excel sheets, sending reminders. Clients forget appointments. No-shows cost 30–40% of potential revenue.

## Solution

A Telegram bot that replaces the entire booking workflow: clients pick a date/time slot → master gets notified → both receive automatic reminders (24h and 2h before) → master manages all bookings from an admin panel inside Telegram.

## Real-World Usage

- **Live deployment**: serving a nail salon master in St. Petersburg
- **Real clients**: active daily bookings from Telegram users
- **Server**: production VPS with systemd service, 24/7 uptime
- **Database**: SQLite v7 with months of booking history
- **Tests**: 30/30 passing

## Architecture

```
Client → Telegram → Bot (polling)
              │
   python-telegram-bot 21.x
              │
    ┌─────────┴──────────────────┐
    │      Handler Layer          │
    │  client.py   master.py     │  ← two roles: client + admin
    └─────────────────────────────┘
              │
    ┌─────────┴──────────────────┐
    │      Service Layer          │
    │  services.py               │  ← business logic
    │  reminder_guard.py         │  ← deduplication for reminders
    │  excel_worker.py           │  ← Excel export
    └─────────────────────────────┘
              │
    ┌─────────┴──────────────────┐
    │      Repository Layer       │
    │  repo.py ← SQLite          │  ← all DB operations
    └─────────────────────────────┘

Background:
  APScheduler → 24h reminder + 2h reminder per booking

System: systemd service + logrotate + daily backup 03:00
```

## Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11 |
| Bot framework | python-telegram-bot 21.x |
| Database | SQLite (v7 schema) |
| Scheduling | APScheduler (reminders) |
| Process manager | systemd |
| Log rotation | logrotate |
| Testing | pytest (30 tests) |
| Backup | cron → daily 03:00 |

## What's Implemented

### Client Flow
- **Booking calendar**: date picker → available slot picker → confirmation
- **Appointment management**: view and cancel bookings
- **Portfolio**: master's photo gallery in Telegram
- **Reviews**: leave and read client reviews
- **Phone validation**: E.164 format, country code handling

### Master (Admin) Panel
- **Schedule view**: all bookings for any selected date
- **Booking management**: confirm, cancel, reschedule any slot
- **Client blacklist**: block problematic clients with reason
- **Excel export**: full booking history as .xlsx
- **Statistics**: booking counts, popular time slots

### Reliability & Safety
- **Rate limiting**: per-user request throttle (anti-spam)
- **Callback deduplication**: prevents double-processing on slow networks
- **Step guard**: conversation state machine, prevents invalid transitions
- **Reminder deduplication**: `reminder_guard` — no duplicate reminders after restart
- **Healthcheck**: bot reports its own alive status

## Architecture Decisions

**Why SQLite over PostgreSQL?**
Single admin (1 master), ~50 bookings/day. SQLite has 0ms latency and zero infra cost. Repository pattern isolates DB calls — migration to PostgreSQL needs only `repo.py` changes.

**Why python-telegram-bot over aiogram?**
PTB 21.x has excellent sync test support. No async concurrency needed at this scale. Cleaner test code, easier onboarding for non-async devs.

**Why systemd over Docker?**
Single-service app on VPS. systemd provides auto-restart, log rotation, and OS-level integration without Docker overhead.

## Trade-offs

| Decision | Pro | Con |
|----------|-----|-----|
| SQLite | Zero infra, fast, easy backup | Not for concurrent writes |
| Systemd | Simple, system-integrated | Not portable |
| Polling mode | No SSL/webhook needed | ~1s extra latency |
| Repository pattern | Swap DB with no handler changes | Extra abstraction layer |

## How It Scales

```
Current: 1 bot, SQLite, 1 master, ~50 clients/day

Scale path:
- Multi-master: add master_id column, route by Telegram ID
- Concurrent writes: repo.py → PostgreSQL (handlers unchanged)
- Multiple salons: add salon_id, tenant routing
- Async: migrate to aiogram 3 (same handler structure)
- Crash-safe reminders: APScheduler → Celery
```

## Failure Handling

```python
# Reminder deduplication — survives restart
if reminder_guard.already_sent(booking_id, "24h"):
    return  # skip duplicate

# Callback deduplication — Telegram delivers twice on slow networks
if dedup.is_duplicate(callback_query.id):
    await callback_query.answer()
    return

# Step guard — user can't skip booking steps
if not step_guard.can_proceed(user_id, Step.CONFIRM):
    await message.reply("Please select a time slot first")
    return

# Rate limiter — silent drop (no error spam to user)
if rate_limiter.is_throttled(user_id):
    return
```

## Testing

```bash
pytest tests/ -v
# 30 tests: booking flow, reminders, deduplication,
# rate limiting, phone validation, Excel export, blacklist
```

## Metrics

| Metric | Value |
|--------|-------|
| Live users | real clients, St. Petersburg |
| Tests | 30/30 passing |
| Bot uptime | 99%+ (systemd restart=always) |
| Daily backup | 03:00 cron, 30 days retention |
| Booking flow steps | 4 (date → time → confirm → done) |

## Quick Start

```bash
git clone https://github.com/yourusername/salon-bot-ai
cd salon-bot-ai
cp .env.example .env
# set BOT_TOKEN, MASTER_CHAT_ID in .env

pip install -r requirements.txt
python main.py

# Tests
pytest tests/ -v

# Deploy (Linux systemd)
cp infra/salon_bot.service /etc/systemd/system/
systemctl enable --now salon_bot
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Telegram Bot API token |
| `MASTER_CHAT_ID` | Telegram ID of the master (admin) |
| `MASTER_USERNAME` | Master's display name for clients |
| `MASTER_ADDRESS` | Address shown in reminders |
| `TIMEZONE` | Timezone (e.g. `Europe/Moscow`) |
| `DB_PATH` | Path to SQLite database file |

## Project Structure

```
salon-bot-ai/
├── main.py
├── app/
│   ├── bot/handlers/
│   │   ├── client.py           # Booking flow (date/time/confirm)
│   │   └── master.py           # Admin panel
│   ├── core/
│   │   ├── database.py         # SQLite init + connection
│   │   ├── settings.py         # Config from .env
│   │   ├── rate_limiter.py     # Per-user throttle
│   │   ├── callback_dedup.py   # Callback deduplication
│   │   ├── step_guard.py       # Conversation state machine
│   │   ├── reminder_guard.py   # Reminder deduplication
│   │   ├── time_utils.py       # Timezone helpers
│   │   ├── phone.py            # Phone validation
│   │   └── healthcheck.py      # Liveness probe
│   ├── models/domain.py        # Booking, Client, TimeSlot
│   ├── repositories/repo.py    # All DB operations
│   └── services/
│       ├── services.py         # Business logic
│       ├── excel_worker.py     # .xlsx export
│       └── reminder_guard.py
├── tests/test_bot.py           # 30 pytest tests
├── infra/salon_bot.service     # systemd unit
├── .env.example
└── requirements.txt
```

## Why This Matters for Business

- **ROI**: saves 2–3h/day of manual booking work
- **No-show reduction**: automatic 24h + 2h reminders
- **Zero friction**: clients book in 30 seconds inside Telegram
- **Master control**: full schedule + Excel for accounting
- **Extensible**: adding a new salon takes less than 1 day
