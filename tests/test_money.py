import os
import sys
import types
import unittest

for mod in ["telegram", "telegram.ext", "telegram.error", "telegram.constants"]:
    sys.modules.setdefault(mod, types.ModuleType(mod))
_tg = sys.modules["telegram"]
_tg.Update = object
_tg.InlineKeyboardMarkup = lambda inline_keyboard: inline_keyboard
_tg.InlineKeyboardButton = lambda text, callback_data: (text, callback_data)
sys.modules["telegram"].constants = types.SimpleNamespace(
    ParseMode=types.SimpleNamespace(HTML="HTML")
)
sys.modules["telegram.ext"].CallbackContext = object

os.environ.setdefault("BOT_TOKEN", "x:y")
os.environ.setdefault("ADMIN_IDS", "1")
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)

from app.core.money import format_eur, parse_eur_input_to_cents
from app.bot.handlers.master import _parse_services_batch


class TestMoney(unittest.TestCase):
    def test_parse_eur_input_to_cents(self):
        self.assertEqual(parse_eur_input_to_cents("25"), 2500)
        self.assertEqual(parse_eur_input_to_cents("25.5"), 2550)
        self.assertEqual(parse_eur_input_to_cents("25.50"), 2550)
        self.assertEqual(parse_eur_input_to_cents("25,50"), 2550)
        self.assertIsNone(parse_eur_input_to_cents("0"))
        self.assertIsNone(parse_eur_input_to_cents("-1"))
        self.assertIsNone(parse_eur_input_to_cents("12.345"))
        self.assertIsNone(parse_eur_input_to_cents("abc"))

    def test_format_eur(self):
        self.assertEqual(format_eur(2500), "25 €")
        self.assertEqual(format_eur(2550), "25.50 €")

    def test_parse_services_batch(self):
        ok, err = _parse_services_batch("Маникюр 25; Гель-лак 35.50; Наращивание 40")
        self.assertEqual(err, "")
        self.assertEqual(ok, [("Маникюр", 2500), ("Гель-лак", 3550), ("Наращивание", 4000)])

        bad, err = _parse_services_batch("Маникюр 25; Гель-лак xx")
        self.assertIsNone(bad)
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
