"""Product / category return policy independent per company (company-dependent fields)."""
import json
import uuid
from datetime import timedelta

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Command
from odoo.tests import Form, HttpCase, tagged
from odoo.tests.common import TransactionCase

from odoo.addons.unified_product_return_policy.models.urp_guard import UrpPayload

from .test_p4_portal import UrpPortalCommon
from .test_p5_pos import UrpPosCommon


class UrpCompanyPolicyCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        cls.company_a.write({"urp_enabled": True, "urp_tz": "Asia/Riyadh", "urp_default_days": 10})
        cls.company_b = cls.env["res.company"].create({"name": "URP MC B"})
        cls.company_b.write({"urp_enabled": True, "urp_tz": "Africa/Cairo", "urp_default_days": 30})
        cls.env.user.company_ids |= cls.company_b
        cls.categ = cls.env["product.category"].create({"name": "URP MC Categ"})
        cls.categ.with_company(cls.company_a).write({"use_custom_return_period": True, "custom_return_period_days": 15})
        cls.categ.with_company(cls.company_b).write({"use_custom_return_period": True, "custom_return_period_days": 20})
        cls.tmpl = cls.env["product.template"].create({"name": "URP MC X", "is_storable": True, "categ_id": cls.categ.id})
        cls.tmpl.with_company(cls.company_a).write({"use_custom_return_period": True, "custom_return_period_days": 7})
        cls.tmpl.with_company(cls.company_b).write({"use_custom_return_period": True, "custom_return_period_days": 14})
        cls.x = cls.tmpl.product_variant_id
        cls.y = cls.env["product.product"].create({"name": "URP MC Y", "is_storable": True, "categ_id": cls.categ.id})
        cls.plain_categ = cls.env["product.category"].create({"name": "URP MC plain"})
        cls.z = cls.env["product.product"].create({"name": "URP MC Z", "is_storable": True, "categ_id": cls.plain_categ.id})
        cls.partner = cls.env["res.partner"].create({"name": "URP MC Customer"})
        for company in (cls.company_a, cls.company_b):
            wh = cls.env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
            for p in (cls.x, cls.y, cls.z):
                cls.env["stock.quant"].with_company(company)._update_available_quantity(p, wh.lot_stock_id, 100)

    def _days(self, product, company, **kw):
        return product._urp_resolve(company, **kw)["return_days"]

    def _so(self, company, product, qty=1, env=None):
        env = env or self.env
        so = env["sale.order"].with_company(company).create({
            "partner_id": self.partner.id, "company_id": company.id,
            "order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": qty})]})
        so.action_confirm()
        return so


@tagged("post_install", "-at_install", "urp", "urp_company_policy")
class TestCompanyPolicy(UrpCompanyPolicyCommon):

    def test_m01_same_product_two_periods(self):
        self.assertEqual((self._days(self.x, self.company_a), self._days(self.x, self.company_b)), (7, 14))
        self.assertEqual(self._so(self.company_a, self.x).urp_batch_ids.return_days, 7)
        self.assertEqual(self._so(self.company_b, self.x).urp_batch_ids.return_days, 14)

    def test_m02_same_category_two_periods(self):
        self.assertEqual((self._days(self.y, self.company_a), self._days(self.y, self.company_b)), (15, 20))
        self.assertEqual(self.y._urp_resolve(self.company_b)["policy_source"], "category")

    def test_m03_product_change_in_a_does_not_touch_b(self):
        Hist = self.env["urp.policy.history"]
        rows_b = Hist.search_count([("scope", "=", "product"), ("res_id", "=", self.tmpl.id), ("policy_company_id", "=", self.company_b.id)])
        self.tmpl.with_company(self.company_a).custom_return_period_days = 5
        self.assertEqual((self._days(self.x, self.company_a), self._days(self.x, self.company_b)), (5, 14))
        self.assertEqual(self.tmpl.with_company(self.company_b).custom_return_period_days, 14)
        self.assertEqual(Hist.search_count([("scope", "=", "product"), ("res_id", "=", self.tmpl.id),
                                            ("policy_company_id", "=", self.company_b.id)]), rows_b, "no phantom row for B")
        last_a = Hist.search([("scope", "=", "product"), ("res_id", "=", self.tmpl.id)], limit=1)
        self.assertEqual((last_a.policy_company_id, last_a.custom_days), (self.company_a, 5))

    def test_m04_category_change_in_a_does_not_touch_b(self):
        self.categ.with_company(self.company_a).custom_return_period_days = 3
        self.assertEqual((self._days(self.y, self.company_a), self._days(self.y, self.company_b)), (3, 20))

    def test_m05_custom_in_a_disabled_in_b(self):
        self.tmpl.with_company(self.company_b).use_custom_return_period = False
        self.assertEqual(self._days(self.x, self.company_a), 7)
        self.assertEqual(self.x._urp_resolve(self.company_b)["policy_source"], "category")
        self.assertEqual(self._days(self.x, self.company_b), 20, "B falls back to ITS category")

    def test_m06_m07_fallbacks_stay_inside_the_company(self):
        self.categ.with_company(self.company_b).use_custom_return_period = False
        self.tmpl.with_company(self.company_b).use_custom_return_period = False
        r = self.x._urp_resolve(self.company_b)
        self.assertEqual((r["return_days"], r["policy_source"]), (30, "company"), "B default, never A's values")
        self.assertEqual(self._days(self.z, self.company_a), 10)
        self.assertEqual(self._days(self.z, self.company_b), 30)

    def test_m08_product_form_per_company(self):
        with Form(self.tmpl.with_company(self.company_a)) as f:
            self.assertEqual((f.use_custom_return_period, f.custom_return_period_days), (True, 7))
        with Form(self.tmpl.with_company(self.company_b)) as f:
            self.assertEqual(f.custom_return_period_days, 14)
            f.custom_return_period_days = 21
        self.assertEqual(self.tmpl.with_company(self.company_a).custom_return_period_days, 7)
        self.assertEqual(self.tmpl.with_company(self.company_b).custom_return_period_days, 21)
        self.assertEqual((self.tmpl.name, self.tmpl.categ_id), ("URP MC X", self.categ), "shared data unchanged")

    def test_m09_user_on_b_confirms_order_of_a(self):
        user = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "MC AB", "login": "urp_mc_ab", "company_id": self.company_b.id,
            "company_ids": [(6, 0, (self.company_a | self.company_b).ids)],
            "group_ids": [(6, 0, [self.env.ref("sales_team.group_sale_salesman_all_leads").id])]})
        env = self.env(user=user, context=dict(self.env.context, allowed_company_ids=[self.company_b.id, self.company_a.id]))
        self.assertEqual(env.company, self.company_b)
        so = env["sale.order"].create({"partner_id": self.partner.id, "company_id": self.company_a.id,
                                       "order_line": [(0, 0, {"product_id": self.x.id, "product_uom_qty": 1})]})
        so.action_confirm()
        self.assertEqual(so.urp_batch_ids.return_days, 7, "policy of the document company A")

    def test_m10_snapshot_unchanged_after_policy_change(self):
        so = self._so(self.company_b, self.x)
        self.tmpl.with_company(self.company_b).custom_return_period_days = 60
        self.categ.with_company(self.company_b).custom_return_period_days = 60
        self.company_b.urp_default_days = 60
        self.assertEqual(so.urp_batch_ids.return_days, 14)

    def test_m11_history_per_company(self):
        t0 = fields.Datetime.now()
        with freeze_time(t0 + timedelta(hours=1)):
            self.tmpl.with_company(self.company_a).custom_return_period_days = 5
        with freeze_time(t0 + timedelta(hours=3)):
            self.tmpl.with_company(self.company_b).custom_return_period_days = 9
        at = t0 + timedelta(hours=2)
        self.assertEqual((self._days(self.x, self.company_a, at=at), self._days(self.x, self.company_b, at=at)), (5, 14))
        at2 = t0 + timedelta(hours=4)
        self.assertEqual((self._days(self.x, self.company_a, at=at2), self._days(self.x, self.company_b, at=at2)), (5, 9))
        # legacy rows without a policy company (shared policy) apply to every company that existed
        # when the shared policy ended (A and B here); without that marker (fresh install) they apply to none
        Hist = self.env["urp.policy.history"].sudo()
        self.env["ir.config_parameter"].sudo().set_int(
            "unified_product_return_policy.shared_policy_max_company_id", max((self.company_a | self.company_b).ids))
        old = self.env["product.template"].create({"name": "URP MC legacy", "categ_id": self.plain_categ.id})
        Hist.create({"scope": "product", "res_id": old.id, "valid_from": t0 + timedelta(minutes=10), "origin": "change",
                     "use_custom": True, "custom_days": 4, "categ_id": self.plain_categ.id})
        prod = old.product_variant_id
        at3 = t0 + timedelta(minutes=20)
        self.assertEqual((self._days(prod, self.company_a, at=at3), self._days(prod, self.company_b, at=at3)), (4, 4))

    def test_m14b_constraint_per_company(self):
        with self.assertRaises(ValidationError):
            self.tmpl.with_company(self.company_b).write({"use_custom_return_period": True, "custom_return_period_days": -1})
        self.assertEqual(self.tmpl.with_company(self.company_a).custom_return_period_days, 7)
        # 0 = non-returnable, in that company only
        self.tmpl.with_company(self.company_b).write({"use_custom_return_period": True, "custom_return_period_days": 0})
        self.assertEqual((self.tmpl.with_company(self.company_a).custom_return_period_days,
                          self.tmpl.with_company(self.company_b).custom_return_period_days), (7, 0))

    def test_m14c_variants_use_template_policy_per_company(self):
        attr = self.env["product.attribute"].create({"name": "URP MC Size", "value_ids": [
            Command.create({"name": "S"}), Command.create({"name": "L"})]})
        self.tmpl.attribute_line_ids = [Command.create({"attribute_id": attr.id, "value_ids": [Command.set(attr.value_ids.ids)]})]
        variants = self.tmpl.product_variant_ids
        self.assertEqual(len(variants), 2)
        for v in variants:
            self.assertEqual((self._days(v, self.company_a), self._days(v, self.company_b)), (7, 14))

    def test_m14d_import_export_per_company(self):
        tmpl = self.tmpl.with_company(self.company_b)
        xmlid = tmpl.export_data(["id"])["datas"][0][0]
        tmpl.load(["id", "use_custom_return_period", "custom_return_period_days"], [[xmlid, "True", "40"]])
        self.assertEqual((self.tmpl.with_company(self.company_a).custom_return_period_days,
                          self.tmpl.with_company(self.company_b).custom_return_period_days), (7, 40))
        self.assertEqual(self.tmpl.with_company(self.company_a).export_data(["custom_return_period_days"])["datas"][0][0], 7)
        res = tmpl.load(["id", "use_custom_return_period", "custom_return_period_days"], [[xmlid, "True", "-1"]])
        self.assertTrue(res["messages"], "import with a negative period refused in that company")
        self.assertEqual(self.tmpl.with_company(self.company_b).custom_return_period_days, 40)
        res = tmpl.load(["id", "use_custom_return_period", "custom_return_period_days"], [[xmlid, "True", "0"]])
        self.assertFalse(res["messages"], "0 = non-returnable, accepted")
        self.assertEqual((self.tmpl.with_company(self.company_a).custom_return_period_days,
                          self.tmpl.with_company(self.company_b).custom_return_period_days), (7, 0))

    def test_m15_company_access_rights(self):
        user_b = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "MC B only", "login": "urp_mc_b", "company_id": self.company_b.id,
            "company_ids": [(6, 0, self.company_b.ids)],
            "group_ids": [(6, 0, [self.env.ref("sales_team.group_sale_manager").id,
                                  self.env.ref("product.group_product_manager").id])]})
        tmpl_b = self.tmpl.with_user(user_b).with_context(allowed_company_ids=[self.company_b.id])
        tmpl_b.custom_return_period_days = 18
        self.assertEqual((self.tmpl.with_company(self.company_a).custom_return_period_days,
                          self.tmpl.with_company(self.company_b).custom_return_period_days), (7, 18))
        with self.assertRaises(AccessError):
            self.tmpl.with_user(user_b).with_context(allowed_company_ids=[self.company_a.id]).write({"custom_return_period_days": 1})
        hist = self.env["urp.policy.history"].with_user(user_b).with_context(allowed_company_ids=[self.company_b.id])
        self.assertFalse(hist.search([("policy_company_id", "=", self.company_a.id)]), "A's policy history hidden from B")

    def test_m16_new_company_inherits_nothing(self):
        c = self.env["res.company"].create({"name": "URP MC C"})
        c.write({"urp_enabled": True, "urp_tz": "Asia/Riyadh", "urp_default_days": 45})
        self.assertFalse(self.tmpl.with_company(c).use_custom_return_period)
        self.assertFalse(self.categ.with_company(c).use_custom_return_period)
        r = self.x._urp_resolve(c)
        self.assertEqual((r["return_days"], r["policy_source"]), (45, "company"))

    def test_m17_consistency_cron_per_company(self):
        Hist = self.env["urp.policy.history"]
        Hist._cron_urp_check_consistency()
        n = Hist.search_count([])
        self.assertFalse(Hist._cron_urp_check_consistency(), "nothing to report: no false positives")
        self.assertEqual(Hist.search_count([]), n)
        # value changed by SQL in company B only -> detected row for B only
        self.env.cr.execute("""UPDATE product_template SET custom_return_period_days =
                               custom_return_period_days || jsonb_build_object(%s::text, 99) WHERE id = %s""",
                            (self.company_b.id, self.tmpl.id))
        self.tmpl.invalidate_recordset()
        rows = Hist._cron_urp_check_consistency()
        self.assertEqual((rows.scope, rows.policy_company_id, rows.custom_days, rows.origin),
                         ("product", self.company_b, 99, "detected"))


@tagged("post_install", "-at_install", "urp", "urp_company_policy")
class TestCompanyPolicyPortal(UrpPortalCommon):

    def test_m13_portal_uses_order_company_policy(self):
        other = self.env["res.company"].create({"name": "URP MC Portal other"})
        self.product.product_tmpl_id.with_company(other).write({"use_custom_return_period": True, "custom_return_period_days": 2})
        so, move = self._delivered(3)
        b = move.urp_batch_id
        self.assertEqual((b.return_days, b.portal_days), (7, 7), "own company: product 7, MIN(7, portal 10)")
        r = self.make_jsonrpc_request(f"/my/orders/{so.id}/urp_return_request", {
            "access_token": so.access_token, "idempotency_key": str(uuid.uuid4()), "return_details": {str(move.id): 1}})
        self.assertNotIn("error", r)


@tagged("post_install", "-at_install", "urp", "urp_company_policy")
class TestCompanyPolicyPos(UrpPosCommon):

    def test_m12_pos_online_and_offline_per_company(self):
        other = self.env["res.company"].create({"name": "URP MC POS other"})
        other.write({"urp_enabled": True, "urp_tz": "Asia/Riyadh", "urp_default_days": 3})
        tmpl = self.p.product_tmpl_id
        tmpl.with_company(other).write({"use_custom_return_period": True, "custom_return_period_days": 2})
        online = self._sale(1)
        self.assertEqual(online.lines.urp_return_days, 7, "POS company policy, not the other company's")
        sess = self._session()
        order = self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": sess.id,
            "lines": [Command.create({"name": "O", "product_id": self.p.id, "price_unit": 10.0, "qty": 1,
                                     "price_subtotal": 10.0, "price_subtotal_incl": 10.0, "discount": 0})],
            "amount_tax": 0, "amount_total": 10.0, "amount_paid": 0, "amount_return": 0})
        start = sess.start_at
        with freeze_time(start + timedelta(minutes=30)):
            tmpl.with_company(other).custom_return_period_days = 1   # change in ANOTHER company during the offline window
        with freeze_time(start + timedelta(hours=1)):
            meta = {"uuid": order.uuid, "orig_session_id": sess.id,
                    "device_date": fields.Datetime.to_string(start + timedelta(seconds=1)), "server_time": False}
            order.with_context(urp_sync_meta=UrpPayload(meta))._urp_snapshot_lines()
        line = order.lines
        self.assertEqual((line.urp_return_days, line.urp_date_trust, line.urp_policy_ambiguous), (7, "bounded", False),
                         "a change in another company does not make this sale ambiguous")
        order2 = order.copy({"lines": [Command.create({"name": "O2", "product_id": self.p.id, "price_unit": 10.0, "qty": 1,
                                                       "price_subtotal": 10.0, "price_subtotal_incl": 10.0, "discount": 0})]})
        with freeze_time(start + timedelta(minutes=40)):
            tmpl.with_company(self.company).custom_return_period_days = 9   # change in the POS company
        with freeze_time(start + timedelta(hours=1)):
            meta2 = {"uuid": order2.uuid, "orig_session_id": sess.id,
                     "device_date": fields.Datetime.to_string(start + timedelta(seconds=1)), "server_time": False}
            order2.with_context(urp_sync_meta=UrpPayload(meta2))._urp_snapshot_lines()
        l2 = order2.lines.filtered(lambda l: l.qty > 0)
        self.assertEqual((l2.urp_return_days, l2.urp_policy_ambiguous), (7, True), "historical value + flagged")


@tagged("post_install", "-at_install", "urp", "urp_company_policy")
class TestHistorySameSecond(UrpCompanyPolicyCommon):

    def test_m18_baseline_never_hides_a_change_of_the_same_second(self):
        """A baseline row and a change made in the same second: a past instant resolves to the
        change, whatever the microseconds of the baseline row."""
        Hist = self.env["urp.policy.history"].sudo()
        tmpl = self.env["product.template"].create({"name": "URP MC same second", "categ_id": self.plain_categ.id})
        prod = tmpl.product_variant_id
        tmpl.with_company(self.company_b).write({"use_custom_return_period": True, "custom_return_period_days": 11})
        rows = Hist.search([("scope", "=", "product"), ("res_id", "=", tmpl.id)])
        self.assertEqual(sorted(rows.mapped("origin")), ["baseline", "change"])
        self.assertFalse(any(r.valid_from.microsecond for r in rows), "whole seconds")
        at = max(rows.mapped("valid_from")) + timedelta(seconds=30)
        r = prod._urp_resolve(self.company_b, at=at)
        self.assertEqual((r["return_days"], r["policy_source"]), (11, "product"))
        # legacy rows: baseline first (lower id) WITH microseconds, then the change
        # of the same second in whole seconds -> the change must win
        tmpl2 = self.env["product.template"].create({"name": "URP MC legacy rows", "categ_id": self.plain_categ.id})
        sec = fields.Datetime.now().replace(microsecond=0) + timedelta(minutes=5)   # after company B was set up
        common = {"scope": "product", "res_id": tmpl2.id, "policy_company_id": self.company_b.id,
                  "company_id": self.company_b.id, "categ_id": self.plain_categ.id}
        base = Hist.create(dict(common, origin="baseline", use_custom=False, custom_days=0,
                                valid_from=sec.replace(microsecond=900000)))
        chg = Hist.create(dict(common, origin="change", use_custom=True, custom_days=12, valid_from=sec))
        self.assertLess(base.id, chg.id)
        r = tmpl2.product_variant_id._urp_resolve(self.company_b, at=sec + timedelta(seconds=30))
        self.assertEqual((r["return_days"], r["policy_source"]), (12, "product"))
