"""Тесты salon-бота (v14+v15 аудит)."""
import sys, types, unittest, os, re

# ── Мок telegram ──────────────────────────────────────────────────────────────
for mod in ["telegram", "telegram.ext", "telegram.error", "telegram.constants"]:
    sys.modules.setdefault(mod, types.ModuleType(mod))
_tg = sys.modules["telegram"]
_tg.Update = object
_tg.InlineKeyboardMarkup = lambda inline_keyboard: inline_keyboard
_tg.InlineKeyboardButton = lambda text, callback_data: (text, callback_data)
sys.modules["telegram"].constants = types.SimpleNamespace(
    ParseMode=types.SimpleNamespace(HTML="HTML")
)

os.environ.setdefault("BOT_TOKEN", "x:y")
os.environ.setdefault("ADMIN_IDS", "1")
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)


# ── helpers ───────────────────────────────────────────────────────────────────
class TestHelpers(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_BASE, "app/bot/helpers.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_parse_mode_html_is_string(self):
        bad = re.findall(r'parse_mode=HTML[^"\']', self.src)
        self.assertEqual(bad, [], f"parse_mode=HTML без кавычек: {bad}")

    def test_message_not_modified_is_string(self):
        bad = re.findall(r'if message is not modified', self.src)
        self.assertEqual(bad, [], f"Сломанная проверка: {bad}")

    def test_send_photo_or_edit_is_coroutine(self):
        import inspect
        from app.bot.helpers import send_photo_or_edit
        self.assertTrue(inspect.iscoroutinefunction(send_photo_or_edit))

    def test_build_calendar_markup_exists(self):
        from app.bot.helpers import build_calendar_markup
        self.assertTrue(callable(build_calendar_markup))

    def test_build_calendar_grid_master_exists(self):
        from app.bot.helpers import build_calendar_grid_master
        self.assertTrue(callable(build_calendar_grid_master))

    def test_day_cols_default_is_3(self):
        self.assertIn("day_cols: int = 3", self.src)

    def test_nav_labels_unified(self):
        self.assertIn("Пред. неделя", self.src)
        self.assertIn("След. неделя", self.src)

    def test_nav_single_row(self):
        self.assertIn("nav_row", self.src)
        self.assertIn("rows.append(nav_row)", self.src)

    def test_back_main_label(self):
        from app.bot.helpers import back_main
        label, cb = back_main()[0]
        self.assertEqual(label, "◀️ Назад")
        self.assertEqual(cb, "main_menu")


# ── client.py ─────────────────────────────────────────────────────────────────
class TestClientHandlers(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_BASE, "app/bot/handlers/client.py"), encoding="utf-8") as f:
            self.src = f.read()

    def _fn(self, name):
        m = re.search(rf'async def {name}.*?(?=\nasync def )', self.src, re.DOTALL)
        return m.group(0) if m else ""

    def test_cmd_start_uses_edit_on_callback(self):
        body = self._fn("cmd_start")
        self.assertIn("update.callback_query", body)
        self.assertIn("edit_or_reply", body)

    def test_cmd_start_uses_safe_reply(self):
        body = self._fn("cmd_start")
        self.assertIn("safe_reply", body)

    def test_client_uses_build_calendar_markup(self):
        self.assertIn("build_calendar_markup", self.src)

    def test_my_bookings_empty_has_book_button(self):
        body = self._fn("my_bookings")
        self.assertIn('"book_service"', body)
        self.assertIn("Записаться", body)

    def test_booking_menu_past_no_cancel_button(self):
        body = self._fn("cb_booking_menu")
        self.assertIn("can_manage", body)
        self.assertIn("is_past", body)


# ── master.py ─────────────────────────────────────────────────────────────────
class TestMasterHandlers(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_BASE, "app/bot/handlers/master.py"), encoding="utf-8") as f:
            self.src = f.read()

    def _fn(self, name):
        m = re.search(rf'async def {name}.*?(?=\nasync def )', self.src, re.DOTALL)
        return m.group(0) if m else ""

    def test_slot_label_start_only(self):
        self.assertIn('lines.append(f"{icon} {start}")', self.src)

    def test_slot_label_no_range(self):
        m = re.search(r'lines\.append\(f"\{icon\} \{start\}[–-]\{end\}"\)', self.src)
        self.assertIsNone(m)

    def test_header_has_datetime(self):
        body = self._fn("show_master_menu")
        self.assertIn("now_str", body)
        self.assertIn("local_now", body)

    def test_header_no_subtitle(self):
        body = self._fn("show_master_menu")
        self.assertNotIn("subtitle", body)

    def test_today_has_back_to_master_menu(self):
        body = self._fn("adm_today")
        self.assertIn('"master_menu"', body)

    def test_today_shows_booking_buttons(self):
        body = self._fn("adm_today")
        self.assertIn("adm_cancel_", body)

    def test_adm_calendar_uses_grid_master(self):
        body = self._fn("adm_calendar")
        self.assertIn("build_calendar_grid_master", body)

    def test_ads_namespace_handlers_present(self):
        self.assertIn("async def ads_root", self.src)
        self.assertIn("async def ads_wd", self.src)
        self.assertIn("async def ads_per", self.src)

    def test_calendar_day_has_template_button(self):
        body = self._fn("_show_day")
        self.assertIn("Шаблон этого дня", body)
        self.assertIn("calnav_", body)


# ── database.py ───────────────────────────────────────────────────────────────
class TestDatabaseMigrations(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_BASE, "app/core/database.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_v7_in_migrations_list(self):
        self.assertIn('(7, "add portfolio table', self.src)

    def test_portfolio_table_in_schema(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS portfolio", self.src)

    def test_v8_weekly_templates_in_migrations(self):
        self.assertIn('(8, "add weekly schedule templates"', self.src)
        self.assertIn("CREATE TABLE IF NOT EXISTS weekly_day_templates", self.src)
        self.assertIn("CREATE TABLE IF NOT EXISTS weekly_day_times", self.src)


# ── repo.py ───────────────────────────────────────────────────────────────────
class TestPortfolioRepo(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_BASE, "app/repositories/repo.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_add_atomic_exists(self):
        self.assertIn("def add_atomic", self.src)

    def test_delete_atomic_exists(self):
        self.assertIn("def delete_atomic", self.src)

    def test_no_commit_in_add_atomic(self):
        start = self.src.find("def add_atomic")
        end = self.src.find("\n    @staticmethod", start + 1)
        self.assertNotIn("conn.commit", self.src[start:end])


# ── services.py ───────────────────────────────────────────────────────────────
class TestServices(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_BASE, "app/services/services.py"), encoding="utf-8") as f:
            self.src = f.read()

    def _portfolio_class(self):
        start = self.src.find("class PortfolioService")
        end = self.src.find("\nclass ", start + 1)
        return self.src[start:end]

    def test_portfolio_no_sqlite_connect(self):
        self.assertNotIn("sqlite3.connect", self._portfolio_class())

    def test_portfolio_uses_get_db(self):
        self.assertIn("get_db()", self._portfolio_class())

    def test_timeslotrepo_global_import(self):
        first_class = self.src.find("\nclass ")
        self.assertIn("TimeSlotRepo", self.src[:first_class])

    def test_no_double_blocked_for_date(self):
        start = self.src.find("    def create(self,")
        end = self.src.find("\n    def ", start + 1)
        count = self.src[start:end].count("BlockedSlotRepo.blocked_for_date")
        self.assertLessEqual(count, 1)

    def test_weekly_schedule_service_exists(self):
        self.assertIn("class WeeklyScheduleService", self.src)
        self.assertIn("def times_for_date", self.src)

    def test_booking_uses_weekly_times(self):
        self.assertIn("self._weekly.times_for_date", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
