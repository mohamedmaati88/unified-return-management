"""Enabling the policy from Inventory > Settings (res.config.settings), saving
the three values together, for a company without any timezone stored beforehand."""
from odoo.exceptions import ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "urp", "urp_regr")
class TestP9SettingsSave(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env["res.company"].create({"name": "URP Settings A"})
        cls.company_b = cls.env["res.company"].create({"name": "URP Settings B"})
        for c in (cls.company_a, cls.company_b):
            c.partner_id.tz = False                    # like "My Company" on a fresh database
            c.write({"urp_enabled": False, "urp_tz": False})
        cls.env.user.company_ids |= cls.company_a | cls.company_b

    def _settings(self, company):
        return self.env["res.config.settings"].with_company(company).with_context(
            allowed_company_ids=[company.id])

    def _history(self, company):
        return self.env["urp.policy.history"].search(
            [("scope", "=", "company"), ("res_id", "=", company.id)], order="valid_from, id")

    def test_s01_enable_from_settings_with_all_values(self):
        c = self.company_a
        self.assertFalse(c.urp_tz)
        n = len(self._history(c))
        self._settings(c).create({"urp_enabled": True, "urp_default_days": 14, "urp_tz": "Asia/Riyadh"}).execute()
        self.assertEqual((c.urp_enabled, c.urp_default_days, c.urp_tz), (True, 14, "Asia/Riyadh"))
        rows = self._history(c)[n:]
        self.assertEqual(len(rows), 1, "one consistent history row, not one per field")
        self.assertEqual((rows.enabled, rows.default_days, rows.tz), (True, 14, "Asia/Riyadh"))

    def test_s02_enable_through_the_settings_form(self):
        c = self.company_a
        with Form(self._settings(c).create({})) as form:
            form.urp_enabled = True
            form.urp_default_days = 30
            form.urp_tz = "Asia/Riyadh"
        form.record.execute()
        self.assertEqual((c.urp_enabled, c.urp_default_days, c.urp_tz), (True, 30, "Asia/Riyadh"))

    def test_s03_timezone_and_days_still_mandatory(self):
        c = self.company_a
        with self.assertRaises(ValidationError):
            self._settings(c).create({"urp_enabled": True, "urp_default_days": 14, "urp_tz": False}).execute()
        with self.assertRaises(ValidationError):
            self._settings(c).create({"urp_enabled": True, "urp_default_days": 0, "urp_tz": "Asia/Riyadh"}).execute()
        self.assertFalse(c.urp_enabled)

    def test_s04_settings_form_shows_and_keeps_current_values(self):
        c = self.company_a
        c.write({"urp_enabled": True, "urp_default_days": 9, "urp_tz": "Africa/Cairo"})
        s = self._settings(c).create({})
        self.assertEqual((s.urp_enabled, s.urp_default_days, s.urp_tz), (True, 9, "Africa/Cairo"))
        n = len(self._history(c))
        s.execute()                                            # saving other settings: no change, no history row
        self.assertEqual(len(self._history(c)), n)
        self._settings(c).create({"urp_default_days": 12}).execute()
        self.assertEqual((c.urp_enabled, c.urp_default_days, c.urp_tz), (True, 12, "Africa/Cairo"))
        self._settings(c).create({"urp_enabled": False}).execute()
        self.assertFalse(c.urp_enabled)
        self.assertEqual(c.urp_tz, "Africa/Cairo", "disabling keeps the stored values")

    def test_s05_multi_company_isolated(self):
        a, b = self.company_a, self.company_b
        hist_a = len(self._history(a))
        self._settings(b).create({"urp_enabled": True, "urp_default_days": 7, "urp_tz": "Europe/Berlin"}).execute()
        self.assertEqual((b.urp_enabled, b.urp_default_days, b.urp_tz), (True, 7, "Europe/Berlin"))
        self.assertEqual((a.urp_enabled, a.urp_tz), (False, False), "other company untouched")
        self.assertEqual(len(self._history(a)), hist_a)
