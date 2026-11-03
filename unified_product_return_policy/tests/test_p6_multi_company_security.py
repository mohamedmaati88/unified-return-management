from datetime import timedelta

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged

from .common import UrpCommon


@tagged("post_install", "-at_install", "urp", "urp_p6")
class TestP6MultiCompanySecurity(UrpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_b = cls.env["res.company"].create({"name": "URP Company B"})
        cls.company_b.partner_id.tz = "Africa/Cairo"
        cls.company_b.write({"urp_enabled": True, "urp_default_days": 30})
        cls.wh_b = cls.env["stock.warehouse"].search([("company_id", "=", cls.company_b.id)], limit=1)
        cls.env["stock.quant"].with_company(cls.company_b)._update_available_quantity(cls.product, cls.wh_b.lot_stock_id, 100)
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        groups = [cls.env.ref("sales_team.group_sale_salesman_all_leads").id, cls.env.ref("stock.group_stock_user").id]
        cls.user_b = Users.create({"name": "User B", "login": "urp_user_b", "group_ids": [(6, 0, groups)],
                                   "company_id": cls.company_b.id, "company_ids": [(6, 0, cls.company_b.ids)]})
        cls.user_ab = Users.create({"name": "User AB", "login": "urp_user_ab", "group_ids": [(6, 0, groups)],
                                    "company_id": cls.company_b.id, "company_ids": [(6, 0, (cls.company | cls.company_b).ids)]})
        cls.salesman = Users.create({"name": "Salesman", "login": "urp_salesman", "group_ids": [(6, 0, groups)],
                                     "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)]})

    def _so(self, company, qty=2, user=None):
        env = self.env(user=user) if user else self.env
        so = env["sale.order"].with_company(company).create({
            "partner_id": self.partner.id, "company_id": company.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": qty})]})
        so.action_confirm()
        return so

    def _deliver(self, so):
        pick = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        pick.move_ids.quantity = pick.move_ids.product_uom_qty
        pick.move_ids.picked = True
        pick.button_validate()
        return so.order_line.move_ids.filtered(lambda m: m.state == "done")

    # ---------------------------------------------------------------- J. multi-company
    def test_j01_each_company_uses_its_own_default_and_tz(self):
        so_a, so_b = self._so(self.company), self._so(self.company_b)
        ba, bb = so_a.urp_batch_ids, so_b.urp_batch_ids
        self.assertEqual((ba.return_days, ba.policy_source, ba.tz, ba.company_id), (14, "company", "Asia/Riyadh", self.company))
        self.assertEqual((bb.return_days, bb.policy_source, bb.tz, bb.company_id), (30, "company", "Africa/Cairo", self.company_b))

    def test_j02_disabled_company_stays_standard(self):
        self.company_b.urp_enabled = False
        so_b = self._so(self.company_b)
        self.assertFalse(so_b.urp_batch_ids)
        move = self._deliver(so_b)
        self.assertFalse(move.urp_deadline)
        with freeze_time(fields.Datetime.now() + timedelta(days=400)):
            ret = move.picking_id._create_return()
            ret.move_ids.write({"product_uom_qty": 2, "quantity": 2, "picked": True})
            self.assertTrue(ret.button_validate() is True)
        self.assertTrue(self._so(self.company).urp_batch_ids, "company A still governed")

    def test_j03_document_company_not_active_company(self):
        # user AB works with company B active but confirms an order of company A
        so = self.env["sale.order"].with_user(self.user_ab).with_context(allowed_company_ids=[self.company_b.id, self.company.id]).create({
            "partner_id": self.partner.id, "company_id": self.company.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1})]})
        so.action_confirm()
        self.assertEqual((so.urp_batch_ids.return_days, so.urp_batch_ids.tz), (14, "Asia/Riyadh"))

    def test_j04_company_tz_change_after_delivery_keeps_deadline(self):
        move = self._deliver(self._so(self.company_b))
        deadline = move.urp_deadline
        self.company_b.write({"urp_tz": "Asia/Tokyo", "urp_default_days": 3})
        move.invalidate_recordset()
        self.assertEqual(move.urp_deadline, deadline)
        self.assertEqual(move.urp_batch_id.tz, "Africa/Cairo")

    def test_j05_record_rules_isolate_companies(self):
        so_a = self._so(self.company)
        self._so(self.company_b)
        Batch = self.env["urp.sale.batch"].with_user(self.user_b)
        self.assertFalse(Batch.search([("id", "in", so_a.urp_batch_ids.ids)]))
        with self.assertRaises(AccessError):
            so_a.urp_batch_ids.with_user(self.user_b).read(["qty"])
        mgr_b = self.user_b.copy({"login": "urp_mgr_b", "group_ids": [(4, self.env.ref("sales_team.group_sale_manager").id)]})
        hist = self.env["urp.policy.history"].with_user(mgr_b)
        self.assertTrue(hist.search([("company_id", "=", self.company_b.id)]))
        self.assertFalse(hist.search([("company_id", "=", self.company.id)]))
        with self.assertRaises(AccessError):
            self.env["urp.policy.history"].with_user(self.user_b).search([])      # no read right without manager role

    def test_j06_product_policy_is_independent_per_company(self):
        """Replaces the former j06 (shared policy): each company has its own product policy."""
        self.product.product_tmpl_id.with_company(self.company).write(
            {"use_custom_return_period": True, "custom_return_period_days": 5})
        self.assertEqual(self._so(self.company).urp_batch_ids.return_days, 5)
        b = self._so(self.company_b).urp_batch_ids
        self.assertEqual((b.return_days, b.policy_source), (30, "company"), "B keeps its own default")

    # ---------------------------------------------------------------- K. security
    def test_k02_salesman_cannot_override_expired_return(self):
        so = self._so(self.company, 1)
        move = self._deliver(so)
        with freeze_time(move.urp_deadline + timedelta(seconds=1)):
            ret = move.picking_id._create_return().with_user(self.salesman)
            ret.move_ids.quantity = 1
            ret.move_ids.picked = True
            action = ret.button_validate()
            wiz = self.env["urp.return.check.wizard"].with_user(self.salesman).browse(action["res_id"])
            self.assertFalse(wiz.can_override)
            wiz.reason = "please"
            with self.assertRaises(AccessError):
                wiz.action_override()
            self.assertEqual(ret.with_context(urp_checked=True).button_validate()["res_model"], "urp.return.check.wizard",
                             "a client-sent urp_checked flag does not skip the check")
            with self.assertRaises(UserError):
                ret.move_ids._action_done()                                # final guard in _action_done
            self.assertNotEqual(ret.state, "done")

    def test_k03_history_is_append_only_even_for_admin(self):
        row = self.env["urp.policy.history"].search([], limit=1)
        with self.assertRaises(UserError):
            row.write({"default_days": 1})
        with self.assertRaises(UserError):
            row.unlink()

    def test_k04_snapshots_protected_even_with_sudo(self):
        move = self._deliver(self._so(self.company))
        with self.assertRaises(UserError):
            move.sudo().write({"urp_deadline": fields.Datetime.now() + timedelta(days=999)})
        with self.assertRaises(UserError):
            move.urp_batch_id.sudo().write({"return_days": 999})

    def test_k05_auth_attempts_and_overrides_not_writable_by_users(self):
        with self.assertRaises(AccessError):
            self.env["urp.auth.attempt"].with_user(self.salesman).search([])
        with self.assertRaises(AccessError):
            self.env["urp.return.override"].with_user(self.salesman).create({
                "channel": "sale", "user_id": self.salesman.id, "reason": "x", "company_id": self.company.id})

    def test_k06_consistency_cron_runs_clean(self):
        self.env["urp.policy.history"]._cron_urp_check_consistency()
        self.company_b.urp_default_days = 45
        self.env["urp.policy.history"]._cron_urp_check_consistency()
        last = self.env["urp.policy.history"].search([("scope", "=", "company"), ("res_id", "=", self.company_b.id)],
                                                    order="id desc", limit=1)
        self.assertEqual((last.default_days, last.origin), (45, "change"))
