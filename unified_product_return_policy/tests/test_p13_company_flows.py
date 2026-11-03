"""End-to-end flows per company: Sales/Inventory, Portal, POS (two companies, two sessions),
plus companies created after the end of the shared policy (legacy shared history rows)."""
import json
import uuid
from datetime import datetime, timedelta

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import Form, HttpCase, tagged

from odoo.addons.unified_product_return_policy.models.urp_policy_history import SHARED_POLICY_COMPANY_PARAM

from .test_p12_company_policy import UrpCompanyPolicyCommon
from .test_p5_pos import UrpPosCommon

DELIVERED = datetime(2026, 10, 25, 9, 0)      # 12:00 Riyadh / 11:00 Cairo


def _upgraded_after(env, companies):
    """Mark the end of the shared policy when ``companies`` were the existing ones."""
    env["ir.config_parameter"].sudo().set_int(SHARED_POLICY_COMPANY_PARAM, max(companies.ids))


class UrpFlowCommon(UrpCompanyPolicyCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        stock_user = cls.env.ref("stock.group_stock_user").id
        both = (cls.company_a | cls.company_b).ids
        cls.salesman = Users.create({"name": "Flow Sales", "login": "urp_flow_su", "company_id": cls.company_a.id,
                                     "company_ids": [(6, 0, both)],
                                     "group_ids": [(6, 0, [cls.env.ref("sales_team.group_sale_salesman_all_leads").id, stock_user])]})
        cls.manager = Users.create({"name": "Flow Mgr", "login": "urp_flow_sm", "company_id": cls.company_a.id,
                                    "company_ids": [(6, 0, both)],
                                    "group_ids": [(6, 0, [cls.env.ref("sales_team.group_sale_manager").id, stock_user])]})

    def _deliver(self, so, qty=None):
        with freeze_time(DELIVERED):
            pick = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
            for m in pick.move_ids:
                m.quantity = qty if qty is not None else m.product_uom_qty
                m.picked = True
            res = pick.button_validate()
            if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
                Form.from_action(self.env, res).save().process()
        return pick

    def _ret(self, pick, qty, user):
        ret = pick.with_user(user).with_context(allowed_company_ids=[pick.company_id.id])._create_return()
        ret.move_ids.write({"product_uom_qty": qty})
        return ret

    def _validate(self, ret):
        for m in ret.move_ids:
            m.quantity = m.product_uom_qty
            m.picked = True
        return ret.button_validate()


@tagged("post_install", "-at_install", "urp", "urp_company_flows")
class TestCompanySalesFlows(UrpFlowCommon):

    def test_w01_sale_delivery_returns_per_company(self):
        so_a, so_b = self._so(self.company_a, self.x, 3), self._so(self.company_b, self.x, 3)
        pick_a, pick_b = self._deliver(so_a), self._deliver(so_b)
        # A: 7 days, Riyadh -> end of 1 Nov; B: 14 days, Cairo -> end of 8 Nov
        self.assertEqual(pick_a.move_ids.urp_deadline, datetime(2026, 11, 1, 21, 0))
        self.assertEqual(pick_b.move_ids.urp_deadline, datetime(2026, 11, 8, 22, 0))
        with freeze_time(datetime(2026, 10, 30, 9, 0)):                # in time for both: documented requests
            ra = self._ret(pick_a, 1, self.salesman)
            rb = self._ret(pick_b, 1, self.salesman)
        with freeze_time(datetime(2026, 11, 4, 9, 0)):                 # A over, B still open
            self.assertIs(self._validate(ra), True, "A request made in time")
            self.assertIs(self._validate(rb), True)
            with self.assertRaises(UserError):
                self._ret(pick_a, 1, self.salesman)                     # new request in A refused
            rb2 = self._ret(pick_b, 1, self.salesman)                   # B still within its 14 days
            self.assertIs(self._validate(rb2), True)
            # admin override in A with its reason
            ra2 = self._ret(pick_a, 1, self.manager)
            wiz = self.env["urp.return.check.wizard"].with_user(self.manager).browse(self._validate(ra2.with_user(self.manager))["res_id"])
            wiz.reason = "Customer complaint accepted"
            wiz.action_override()
            self.assertEqual((ra2.state, ra2.urp_override_id.reason), ("done", "Customer complaint accepted"))

    def test_w02_policy_change_in_a_after_sales(self):
        so_a, so_b = self._so(self.company_a, self.x, 2), self._so(self.company_b, self.x, 2)
        pick_a, pick_b = self._deliver(so_a), self._deliver(so_b)
        deadlines = (pick_a.move_ids.urp_deadline, pick_b.move_ids.urp_deadline)
        self.tmpl.with_company(self.company_a).custom_return_period_days = 60
        self.assertEqual((pick_a.move_ids.urp_deadline, pick_b.move_ids.urp_deadline), deadlines, "no retroactive change")
        self.assertEqual(self._so(self.company_b, self.x).urp_batch_ids.return_days, 14, "B unaffected")
        self.assertEqual(self._so(self.company_a, self.x).urp_batch_ids.return_days, 60)

    def test_w03_active_company_differs_from_document(self):
        env_b = self.env(user=self.salesman, context=dict(self.env.context, allowed_company_ids=[self.company_b.id, self.company_a.id]))
        self.assertEqual(env_b.company, self.company_b)
        so = env_b["sale.order"].create({"partner_id": self.partner.id, "company_id": self.company_a.id,
                                         "order_line": [(0, 0, {"product_id": self.x.id, "product_uom_qty": 1})]})
        so.action_confirm()
        b = so.urp_batch_ids
        self.assertEqual((b.return_days, b.tz, b.company_id), (7, "Asia/Riyadh", self.company_a))
        pick = self._deliver(so.sudo())
        self.assertEqual(pick.move_ids.urp_deadline, datetime(2026, 11, 1, 21, 0), "A policy and A timezone")

    def test_w04_company_created_after_upgrade_sales(self):
        """Legacy shared history rows do not reach a company created after the shared policy ended."""
        Hist = self.env["urp.policy.history"].sudo()
        legacy_tmpl = self.env["product.template"].create({"name": "URP Flow legacy", "is_storable": True,
                                                            "categ_id": self.plain_categ.id})
        Hist.create({"scope": "product", "res_id": legacy_tmpl.id, "valid_from": fields.Datetime.now() - timedelta(days=1),
                     "origin": "change", "use_custom": True, "custom_days": 4, "categ_id": self.plain_categ.id})
        # the upgrade happened when A and B existed; C is created afterwards
        _upgraded_after(self.env, self.company_a | self.company_b)
        c = self.env["res.company"].create({"name": "URP Flow C (after upgrade)"})
        c.write({"urp_enabled": True, "urp_tz": "Asia/Riyadh", "urp_default_days": 30})
        p = legacy_tmpl.product_variant_id
        now = fields.Datetime.now() + timedelta(seconds=5)
        self.assertEqual(p._urp_resolve(self.company_a, at=now)["return_days"], 4, "A existed: shared value applies")
        r = p._urp_resolve(c, at=now)
        self.assertEqual((r["return_days"], r["policy_source"]), (30, "company"), "C never had the shared value")
        self.assertEqual(p._urp_resolve(c)["return_days"], 30, "online = historical for C")
        # C's own change is logged for C only (baseline + change), never inherited from the shared row
        legacy_tmpl.with_company(c).write({"use_custom_return_period": True, "custom_return_period_days": 9})
        rows_c = Hist.search([("scope", "=", "product"), ("res_id", "=", legacy_tmpl.id), ("policy_company_id", "=", c.id)])
        self.assertEqual(sorted(rows_c.mapped("origin")), ["baseline", "change"])
        self.assertEqual(p._urp_resolve(self.company_a, at=fields.Datetime.now() + timedelta(seconds=5))["return_days"], 4)


@tagged("post_install", "-at_install", "urp", "urp_company_flows")
class TestCompanyPortalFlows(UrpFlowCommon, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for c, days in ((cls.company_a, 10), (cls.company_b, 10)):
            c.write({"allow_spontaneous_returns": True, "return_validity_days": days})
        cls.portal_partner = cls.env["res.partner"].create({"name": "URP Flow portal", "email": "flow@example.com"})

    def _portal_so(self, company, qty=3):
        so = self.env["sale.order"].with_company(company).create({
            "partner_id": self.portal_partner.id, "company_id": company.id,
            "order_line": [(0, 0, {"product_id": self.x.id, "product_uom_qty": qty})]})
        so.action_confirm()
        pick = so.picking_ids
        pick.move_ids.quantity = qty
        pick.move_ids.picked = True
        pick.button_validate()
        so._portal_ensure_token()
        return so, pick.move_ids

    def _request(self, so, move, qty, token=None):
        return self.make_jsonrpc_request(f"/my/orders/{so.id}/urp_return_request", {
            "access_token": token or so.access_token, "idempotency_key": str(uuid.uuid4()),
            "return_details": {str(move.id): qty}})

    def test_w05_portal_per_company(self):
        so_a, mv_a = self._portal_so(self.company_a)
        so_b, mv_b = self._portal_so(self.company_b)
        self.assertEqual((mv_a.urp_batch_id.portal_days, mv_b.urp_batch_id.portal_days), (7, 10),
                         "MIN(product, portal): A min(7,10), B min(14,10)")
        r = self._request(so_a, mv_a, 2)
        self.assertNotIn("error", r)
        self.assertIn("error", self._request(so_a, mv_a, 2), "only 1 unit left after the reservation")
        self.assertIn("error", self._request(so_b, mv_b, 1, token=so_a.access_token), "other order's token refused")
        with freeze_time(mv_a.urp_portal_deadline + timedelta(hours=1)):
            self.assertIn("error", self._request(so_a, mv_a, 1), "A portal period over")
            self.assertNotIn("error", self._request(so_b, mv_b, 1), "B portal period still open")
            ret = mv_a.picking_id._create_return()                          # request made in time keeps its right
            # for the return line that executes it (selected by the Inventory user)
            ret.move_ids.write({"urp_portal_line_id": self.env["urp.portal.return.request"].browse(r["request_id"]).line_ids.id,
                                "product_uom_qty": 2, "quantity": 2, "picked": True})
            self.assertIs(ret.button_validate(), True)


class UrpPosTwoCompanies(UrpPosCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        data_b = cls.setup_other_company(name="URP POS Co B")
        cls.company_b = data_b["company"]
        cls.company_b.write({"urp_tz": "Africa/Cairo", "urp_default_days": 30, "urp_enabled": True})
        cls.p.product_tmpl_id.with_company(cls.company_b).write({"use_custom_return_period": True, "custom_return_period_days": 14})
        cls.pm_b = cls.env["pos.payment.method"].create({"name": "Cash B", "company_id": cls.company_b.id, "type": "cash",
                                                         "journal_id": data_b["default_journal_cash"].id})
        cls.config_b = cls.env["pos.config"].with_company(cls.company_b).create({
            "name": "URP POS B", "company_id": cls.company_b.id, "payment_method_ids": [(6, 0, cls.pm_b.ids)]})
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.manager_b = Users.create({"name": "POS Mgr B", "login": "urp_pos_mgr_b", "password": "MgrB-Pass-123!",
                                      "group_ids": [(6, 0, [cls.env.ref("point_of_sale.group_pos_manager").id])],
                                      "company_ids": [(6, 0, cls.company_b.ids)], "company_id": cls.company_b.id})
        cls.env.user.company_ids |= cls.company_b

    def _session_b(self):
        cfg = self.config_b.with_company(self.company_b)
        if not cfg.current_session_id:
            cfg.open_ui()
        s = cfg.current_session_id
        if s.state == "opening_control":
            s.set_opening_control(0, "")
        return s

    def _sync(self, session, pm, lines, online=True, device_date=None, token=None):
        """Real POS synchronisation (sync_from_ui). online=False = order sent later by the device."""
        uid = str(uuid.uuid4())
        total = sum(q * 10.0 for _orig, q in lines)
        data = {
            "uuid": uid, "company_id": session.company_id.id, "session_id": session.id, "partner_id": self.partner.id,
            "date_order": fields.Datetime.to_string(device_date or fields.Datetime.now()),
            "lines": [[0, 0, {"product_id": self.p.id, "full_product_name": "P", "price_unit": 10.0, "qty": q,
                              "price_subtotal": 10.0 * q, "price_subtotal_incl": 10.0 * q, "discount": 0,
                              **({"refunded_orderline_id": orig.id} if orig else {})}] for orig, q in lines],
            "payment_ids": [[0, 0, {"amount": total, "payment_method_id": pm.id}]],
            "amount_paid": total, "amount_total": total, "amount_tax": 0.0, "amount_return": 0.0,
            "to_invoice": False, "state": "paid",
        }
        if token:
            data["urp_override_token"] = token
        ctx = {"current_order_uuid": uid} if online else {}
        self.env["pos.order"].with_company(session.company_id).with_context(**ctx).sync_from_ui([data])
        return self.env["pos.order"].search([("uuid", "=", uid)])


@tagged("post_install", "-at_install", "urp", "urp_company_flows")
class TestCompanyPosFlows(UrpPosTwoCompanies):

    def test_w06_two_sessions_two_periods_refunds(self):
        sa, sb = self._session(), self._session_b()
        with freeze_time(DELIVERED):
            oa = self._sync(sa, self.cash_payment_method, [(False, 2)])
            ob = self._sync(sb, self.pm_b, [(False, 2)])
        self.assertEqual((oa.lines.urp_return_days, ob.lines.urp_return_days), (7, 14))
        self.assertEqual((oa.lines.urp_tz, ob.lines.urp_tz), ("Asia/Riyadh", "Africa/Cairo"))
        with freeze_time(DELIVERED + timedelta(days=3)):
            self.assertEqual(self._sync(sa, self.cash_payment_method, [(oa.lines, -1)]).state, "paid", "A in time")
        with freeze_time(DELIVERED + timedelta(days=10)):              # A over (7), B open (14)
            with self.assertRaises(UserError):
                self._sync(sa, self.cash_payment_method, [(oa.lines, -1)])
            self.assertEqual(self._sync(sb, self.pm_b, [(ob.lines, -1)]).state, "paid", "B still in time")
        with freeze_time(DELIVERED + timedelta(days=20)):              # B over: manager B override
            uid = str(uuid.uuid4())
            tok = sb.with_user(self.manager_b).with_context(allowed_company_ids=[self.company_b.id]).urp_request_override(
                "urp_pos_mgr_b", "MgrB-Pass-123!", None, "Damaged", [{"product_id": self.p.id, "refunded_line_id": ob.lines.id, "qty": 1}], uid)
            self.assertTrue(tok["ok"], tok)
            # token issued for B's session cannot be used in A's session
            with self.assertRaises(UserError):
                self._sync(sa, self.cash_payment_method, [(oa.lines, -1)], token=tok["token"])

    def test_w07_manager_of_a_cannot_authorise_b(self):
        sb = self._session_b()
        # the manager of A has no access to company B: refused before any token is issued
        with self.assertRaises(AccessError):
            sb.with_user(self.manager).urp_request_override(
                "urp_pos_mgr", "Mgr-Pass-123!", None, "x", [{"product_id": self.p.id, "qty": 1}], str(uuid.uuid4()))
        self.assertFalse(self.env["urp.return.override"].sudo().search([("pos_session_id", "=", sb.id)]))

    def test_w08_offline_policy_change_per_company(self):
        sa, sb = self._session(), self._session_b()
        start_b = sb.start_at
        with freeze_time(start_b + timedelta(minutes=30)):
            self.p.product_tmpl_id.with_company(self.company).custom_return_period_days = 3   # change in A only
        with freeze_time(start_b + timedelta(hours=1)):
            ob = self._sync(sb, self.pm_b, [(False, 1)], online=False, device_date=start_b + timedelta(minutes=5))
        self.assertEqual((ob.lines.urp_date_trust, ob.lines.urp_return_days, ob.lines.urp_policy_ambiguous),
                         ("bounded", 14, False), "B offline sale: B policy, A's change is irrelevant")
        with freeze_time(start_b + timedelta(hours=1, minutes=30)):
            self.p.product_tmpl_id.with_company(self.company_b).custom_return_period_days = 20  # change in B
        with freeze_time(start_b + timedelta(hours=2)):
            ob2 = self._sync(sb, self.pm_b, [(False, 1)], online=False, device_date=start_b + timedelta(minutes=10))
        self.assertEqual((ob2.lines.urp_return_days, ob2.lines.urp_policy_ambiguous), (14, True),
                         "historical B value at the sale time, flagged")

    def test_w09_ship_later_partial_in_company_b(self):
        sb = self._session_b()
        self.config_b.ship_later = True
        order = self.env["pos.order"].with_company(self.company_b).create({
            "company_id": self.company_b.id, "session_id": sb.id, "partner_id": self.partner.id,
            "lines": [Command.create({"name": "L", "product_id": self.p.id, "price_unit": 10.0, "qty": 4,
                                     "price_subtotal": 40.0, "price_subtotal_incl": 40.0, "discount": 0})],
            "amount_tax": 0, "amount_total": 40.0, "amount_paid": 0, "amount_return": 0, "to_invoice": False,
            "shipping_date": fields.Date.to_string(fields.Date.today() + timedelta(days=1))})
        self.env["pos.make.payment"].with_context(active_ids=order.ids, active_id=order.id).create(
            {"amount": 40.0, "payment_method_id": self.pm_b.id}).check()
        pick = order.picking_ids
        with freeze_time(DELIVERED):
            pick.move_ids.quantity = 2
            pick.move_ids.picked = True
            res = pick.button_validate()
            if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
                Form.from_action(self.env, res).save().process()
        done = order.picking_ids.move_ids.filtered(lambda m: m.state == "done")
        self.assertEqual(done.urp_deadline, datetime(2026, 11, 8, 22, 0), "B: 14 days from delivery, Cairo")
        with freeze_time(DELIVERED + timedelta(days=1)):
            with self.assertRaises(UserError) as cm:
                self._sync(sb, self.pm_b, [(order.lines, -3)])            # 2 delivered only
            self.assertIn("URP-OPS-01", str(cm.exception))
            self.assertEqual(self._sync(sb, self.pm_b, [(order.lines, -2)]).state, "paid")

    def test_w10_offline_sale_in_company_created_after_upgrade(self):
        """POS offline: a shared pre-upgrade row applies to A (existed) but not to B (created after)."""
        Hist = self.env["urp.policy.history"].sudo()
        tmpl = self.env["product.template"].create({"name": "URP POS legacy", "available_in_pos": True})
        prod = tmpl.product_variant_id
        sa, sb = self._session(), self._session_b()
        Hist.create({"scope": "product", "res_id": tmpl.id, "valid_from": sa.start_at - timedelta(days=1),
                     "origin": "change", "use_custom": True, "custom_days": 4, "categ_id": tmpl.categ_id.id})
        _upgraded_after(self.env, self.company)          # upgrade when only A existed; B created later
        self.assertLess(self.company.id, self.company_b.id)
        self.p = prod
        with freeze_time(sb.start_at + timedelta(hours=1)):
            oa = self._sync(sa, self.cash_payment_method, [(False, 1)], online=False, device_date=sb.start_at + timedelta(minutes=1))
            ob = self._sync(sb, self.pm_b, [(False, 1)], online=False, device_date=sb.start_at + timedelta(minutes=1))
        self.assertEqual(oa.lines.urp_return_days, 4, "A existed before the upgrade: shared value")
        self.assertEqual((ob.lines.urp_return_days, ob.lines.urp_policy_source), (30, "company"), "B: its own default")

    def test_w11_refund_linked_to_a_sale_of_another_company_refused(self):
        """A refund in company B cannot refer to a receipt line of company A, whichever company has
        the policy enabled: refused at payment, nothing recorded in B."""
        sa, sb = self._session(), self._session_b()
        with freeze_time(DELIVERED):
            oa = self._sync(sa, self.cash_payment_method, [(False, 2)])
        self.assertTrue(oa.lines.urp_has_snapshot)
        stock_before = self.p.with_company(self.company_b).qty_available

        def refused():
            before = self.env["pos.order"].search_count([("session_id", "=", sb.id)])
            with self.assertRaisesRegex(UserError, "does not match the receipt line"), self.env.cr.savepoint():
                self._sync(sb, self.pm_b, [(oa.lines, -1)])
            self.assertEqual(self.env["pos.order"].search_count([("session_id", "=", sb.id)]), before,
                             "no refund order recorded in B")

        with freeze_time(DELIVERED + timedelta(days=3)):                 # A's sale still within its period
            refused()                                                     # policy enabled in A and B
            self.company_b.urp_enabled = False
            refused()                                                     # enabled in the original company only
            self.company_b.urp_enabled = True
            self.company.urp_enabled = False
            refused()                                                     # enabled in the refund company only
            self.company.urp_enabled = True
            self.p.invalidate_recordset(["qty_available"])
            self.assertEqual(self.p.with_company(self.company_b).qty_available, stock_before, "no stock moved back in B")
            # the genuine refund in A, on A's receipt, still goes through
            self.assertEqual(self._sync(sa, self.cash_payment_method, [(oa.lines, -1)]).state, "paid")
