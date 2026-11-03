from datetime import datetime, timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.unified_product_return_policy.models.urp_tools import urp_deadline_boundary
from .common import UrpCommon


@tagged("post_install", "-at_install", "urp", "urp_p1")
class TestP1Policy(UrpCommon):

    # ---------------------------------------------------------------- A. settings
    def test_a01_enable_requires_positive_days_and_tz(self):
        c = self.env["res.company"].create({"name": "C-A01"})
        with self.assertRaises(ValidationError):
            c.write({"urp_enabled": True, "urp_tz": "Asia/Riyadh", "urp_default_days": 0})
        with self.assertRaises(ValidationError):
            c.write({"urp_enabled": True, "urp_tz": "Asia/Riyadh", "urp_default_days": -3})
        c.partner_id.tz = False
        with self.assertRaises(ValidationError):
            c.write({"urp_enabled": True, "urp_default_days": 7})

    def test_a02_tz_defaults_from_company_partner(self):
        c = self.env["res.company"].create({"name": "C-A02"})
        c.partner_id.tz = "Europe/Berlin"
        c.write({"urp_enabled": True, "urp_default_days": 7})
        self.assertEqual(c.urp_tz, "Europe/Berlin")

    def test_a03_disable_keeps_product_settings(self):
        self.product.product_tmpl_id.write({"use_custom_return_period": True, "custom_return_period_days": 9})
        self.company.urp_enabled = False
        self.assertFalse(self.product._urp_resolve(self.company))
        self.assertEqual(self.product.product_tmpl_id.custom_return_period_days, 9)
        self.company.urp_enabled = True
        self.assertEqual(self.product._urp_resolve(self.company)["return_days"], 9)

    def test_a04_settings_form_writes_company(self):
        settings = self.env["res.config.settings"].create({"urp_default_days": 21})
        settings.execute()
        self.assertEqual(self.company.urp_default_days, 21)

    # ---------------------------------------------------------------- B. product / category
    def test_b01_validation_on_create_write_import(self):
        tmpl = self.product.product_tmpl_id
        # 0 days = non-returnable (accepted); negative values are refused
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 0})
        self.assertTrue(self.product._urp_resolve(self.company)["non_returnable"])
        with self.assertRaises(ValidationError):
            self.env["product.category"].create({"name": "bad", "use_custom_return_period": True, "custom_return_period_days": -1})
        res = self.env["product.template"].load(
            ["name", "use_custom_return_period", "custom_return_period_days"], [["Imported", "True", "-1"]])
        self.assertTrue(any(m["type"] == "error" for m in res["messages"]), res)
        # disabling keeps the stored value, which is then ignored
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 5})
        tmpl.write({"use_custom_return_period": False})
        self.assertEqual(tmpl.custom_return_period_days, 5)
        self.assertEqual(self.product._urp_resolve(self.company)["policy_source"], "company")

    def test_b02_priority_product_category_company(self):
        tmpl, categ = self.product.product_tmpl_id, self.categ
        cases = [
            ((True, 7), (True, 10), 7, "product"),
            ((False, 0), (True, 10), 10, "category"),
            ((False, 0), (False, 0), 14, "company"),
            ((True, 30), (False, 0), 30, "product"),
            ((True, 5), (True, 20), 5, "product"),
            ((True, 30), (True, 7), 30, "product"),   # no MIN between levels
        ]
        for (pu, pd), (cu, cd), days, source in cases:
            tmpl.write({"use_custom_return_period": pu, **({"custom_return_period_days": pd} if pd else {})})
            categ.write({"use_custom_return_period": cu, **({"custom_return_period_days": cd} if cd else {})})
            res = self.product._urp_resolve(self.company)
            self.assertEqual((res["return_days"], res["policy_source"]), (days, source), ((pu, pd), (cu, cd)))

    def test_b03_parent_category_not_inherited(self):
        parent = self.env["product.category"].create({"name": "Parent", "use_custom_return_period": True, "custom_return_period_days": 3})
        self.categ.parent_id = parent
        self.assertEqual(self.product._urp_resolve(self.company)["policy_source"], "company")

    def test_b04_variants_share_template_policy(self):
        tmpl = self.product.product_tmpl_id
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 11})
        other = self.env["product.product"].create({"product_tmpl_id": tmpl.id})
        self.assertEqual(other._urp_resolve(self.company)["return_days"], 11)

    def test_b05_portal_days_is_min_of_base_and_portal(self):
        tmpl = self.product.product_tmpl_id
        self.company.write({"allow_spontaneous_returns": True})
        for base, portal, expected in [(7, 10, 7), (15, 10, 10), (30, 7, 7), (5, 14, 5), (9, 10, 9), (10, 10, 10)]:
            tmpl.write({"use_custom_return_period": True, "custom_return_period_days": base})
            self.company.return_validity_days = portal
            self.assertEqual(self.product._urp_resolve(self.company)["portal_days"], expected)
        self.company.allow_spontaneous_returns = False
        res = self.product._urp_resolve(self.company)
        self.assertFalse(res["portal_eligible"])
        self.assertEqual(res["return_days"], 10, "disabling portal returns does not disable the base policy")

    def test_b06_company_default_is_per_company(self):
        c2 = self.env["res.company"].create({"name": "C-B06"})
        c2.write({"urp_tz": "Asia/Riyadh", "urp_default_days": 7, "urp_enabled": True})
        self.product.product_tmpl_id.company_id = False   # shared product
        self.assertEqual(self.product._urp_resolve(self.company)["return_days"], 14)
        self.assertEqual(self.product._urp_resolve(c2)["return_days"], 7)

    # ---------------------------------------------------------------- history
    def test_h01_baseline_and_change_rows(self):
        Hist = self.env["urp.policy.history"]
        self.assertTrue(Hist.search([("scope", "=", "company"), ("res_id", "=", self.company.id)]))
        tmpl = self.product.product_tmpl_id
        n = Hist.search_count([("scope", "=", "product"), ("res_id", "=", tmpl.id)])
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 8})
        tmpl.write({"name": "renamed"})            # not a policy field -> no row
        rows = Hist.search([("scope", "=", "product"), ("res_id", "=", tmpl.id)], order="valid_from, id")
        # first change of a product never logged: the previous state is recorded as a baseline first
        self.assertEqual(n, 0)
        self.assertEqual([(r.origin, r.use_custom, r.custom_days) for r in rows], [("baseline", False, 0), ("change", True, 8)])
        tmpl.write({"custom_return_period_days": 9})
        self.assertEqual(Hist.search_count([("scope", "=", "product"), ("res_id", "=", tmpl.id)]), 3, "no second baseline")
        row = rows[-1]
        with self.assertRaises(UserError):
            row.write({"custom_days": 99})
        with self.assertRaises(UserError):
            row.unlink()

    def test_h02_resolve_at_past_instant(self):
        tmpl = self.product.product_tmpl_id
        t0 = fields.Datetime.now()
        Hist = self.env["urp.policy.history"]
        # make the existing rows look older, then change the policy "now"
        self.env.cr.execute("UPDATE urp_policy_history SET valid_from = %s", [t0 - timedelta(hours=2)])
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 30})
        self.env.cr.execute("UPDATE urp_policy_history SET valid_from = %s WHERE scope='product' AND res_id=%s",
                            [t0 - timedelta(minutes=30), tmpl.id])
        Hist.invalidate_model()
        self.assertEqual(self.product._urp_resolve(self.company, at=t0 - timedelta(hours=1))["return_days"], 14)
        self.assertEqual(self.product._urp_resolve(self.company, at=t0)["return_days"], 30)
        self.assertTrue(Hist._urp_changes_between("product", tmpl.id, t0 - timedelta(hours=1), t0))

    def test_h03_category_move_logged(self):
        tmpl = self.product.product_tmpl_id
        cat2 = self.env["product.category"].create({"name": "Cat2", "use_custom_return_period": True, "custom_return_period_days": 4})
        tmpl.categ_id = cat2
        row = self.env["urp.policy.history"].search([("scope", "=", "product"), ("res_id", "=", tmpl.id)], limit=1)
        self.assertEqual(row.categ_id, cat2.id)

    def test_h04_sql_change_detected_by_cron(self):
        self.env.cr.execute("UPDATE res_company SET urp_default_days = 99 WHERE id = %s", [self.company.id])
        self.company.invalidate_recordset()
        rows = self.env["urp.policy.history"]._cron_urp_check_consistency()
        detected = rows.filtered(lambda r: r.scope == "company" and r.res_id == self.company.id)
        self.assertEqual(detected.origin, "detected")
        self.assertEqual(detected.default_days, 99)

    # ---------------------------------------------------------------- deadline helper (D2)
    def test_d01_boundary_examples(self):
        cases = [
            (datetime(2026, 10, 1, 7, 0), "Asia/Riyadh", 7, datetime(2026, 10, 8, 21, 0)),
            (datetime(2026, 10, 1, 22, 30), "Asia/Riyadh", 7, datetime(2026, 10, 9, 21, 0)),
            (datetime(2026, 10, 20, 8, 0), "Europe/Berlin", 7, datetime(2026, 10, 27, 23, 0)),   # DST end 25/10
            (datetime(2026, 3, 20, 12, 0), "Europe/Berlin", 7, datetime(2026, 3, 27, 23, 0)),
        ]
        for ref, tz, days, expected in cases:
            self.assertEqual(urp_deadline_boundary(ref, days, tz), expected, (ref, tz))
        # last instant of local day 7 is eligible, first instant of day 8 is not
        b = urp_deadline_boundary(datetime(2026, 10, 1, 7, 0), 7, "Asia/Riyadh")
        self.assertTrue(b - timedelta(microseconds=1) < b)
        self.assertFalse(b < b)
