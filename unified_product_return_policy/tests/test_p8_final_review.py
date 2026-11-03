"""Regression tests: portal request cancellation rights (cross-company,
through JSON-RPC) and POS session-aggregated deliveries (no policy leak onto legacy sales)."""
import json
from datetime import timedelta

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tests import tagged

from .test_p4_portal import UrpPortalCommon
from .test_p5_pos import UrpPosCommon


@tagged("post_install", "-at_install", "urp", "urp_regr")
class TestF1PortalCancelRights(UrpPortalCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_b = cls.env["res.company"].create({"name": "URP Portal Co B"})
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        all_leads = cls.env.ref("sales_team.group_sale_salesman_all_leads").id
        own_docs = cls.env.ref("sales_team.group_sale_salesman").id
        cls.user_b = Users.create({"name": "Sales B", "login": "urp_f1_b", "password": "F1-Pass-B-123!",
                                   "group_ids": [(6, 0, [all_leads])], "company_id": cls.company_b.id,
                                   "company_ids": [(6, 0, cls.company_b.ids)]})
        cls.user_ab = Users.create({"name": "Sales AB", "login": "urp_f1_ab", "password": "F1-Pass-AB-123!",
                                    "group_ids": [(6, 0, [all_leads])], "company_id": cls.company_b.id,
                                    "company_ids": [(6, 0, (cls.company | cls.company_b).ids)]})
        cls.user_own = Users.create({"name": "Sales own", "login": "urp_f1_own", "password": "F1-Pass-OWN-123!",
                                     "group_ids": [(6, 0, [own_docs])], "company_id": cls.company.id,
                                     "company_ids": [(6, 0, cls.company.ids)]})
        cls.portal = Users.create({"name": "Portal", "login": "urp_f1_portal", "password": "F1-Pass-P-123!",
                                   "group_ids": [(6, 0, [cls.env.ref("base.group_portal").id])]})

    def _open_request(self):
        so, move = self._delivered()
        r = self._request(so, {move: 2})
        return self.env["urp.portal.return.request"].browse(r["request_id"])

    def _rpc_cancel(self, req, login, password, context=None):
        self.authenticate(login, password)
        res = self.url_open("/web/dataset/call_kw", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"model": "urp.portal.return.request", "method": "action_cancel_remaining",
                       "args": [req.ids], "kwargs": {"context": context or {}}}}),
            headers={"Content-Type": "application/json"}).json()
        req.invalidate_recordset()
        return res

    def test_f1_01_other_company_user_refused(self):
        req = self._open_request()
        res = self._rpc_cancel(req, "urp_f1_b", "F1-Pass-B-123!")
        self.assertIn("error", res)
        self.assertEqual(req.state, "active")
        self.assertEqual(req.line_ids.open_qty, 2)

    def test_f1_02_forged_company_context_refused(self):
        req = self._open_request()
        # user B tries to add company A to the allowed companies of the call
        res = self._rpc_cancel(req, "urp_f1_b", "F1-Pass-B-123!", {"allowed_company_ids": [self.company_b.id, self.company.id]})
        self.assertIn("error", res)
        self.assertEqual(req.state, "active")

    def test_f1_03_multi_company_user_needs_the_company_active(self):
        req = self._open_request()
        res = self._rpc_cancel(req, "urp_f1_ab", "F1-Pass-AB-123!", {"allowed_company_ids": [self.company_b.id]})
        self.assertIn("error", res, "company A not active in this call")
        self.assertEqual(req.state, "active")
        res = self._rpc_cancel(req, "urp_f1_ab", "F1-Pass-AB-123!", {"allowed_company_ids": [self.company_b.id, self.company.id]})
        self.assertNotIn("error", res, res)
        self.assertEqual(req.state, "cancelled")

    def test_f1_04_salesman_own_documents_and_portal_refused(self):
        req = self._open_request()
        self.assertIn("error", self._rpc_cancel(req, "urp_f1_own", "F1-Pass-OWN-123!"), "order of another salesperson")
        self.assertIn("error", self._rpc_cancel(req, "urp_f1_portal", "F1-Pass-P-123!"))
        self.assertEqual(req.state, "active")


@tagged("post_install", "-at_install", "urp", "urp_regr")
class TestF2PosAggregatedSession(UrpPosCommon):
    """Session pickings (stock updated at closing) carry the quantities of several orders:
    a returned quantity cannot be linked to one receipt, so Inventory may only return the
    legacy share; governed quantity goes through POS Refund with each receipt's own policy."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.point_of_sale_update_stock_quantities = "closing"

    def _close_stock(self):
        sess = self._session()
        self.assertTrue(sess.update_stock_at_closing)
        sess._create_picking_at_end_of_session()
        picking = sess.picking_ids.filtered(lambda p: not p.pos_order_id and p.state == "done")
        self.assertEqual(len(picking), 1)
        return picking.move_ids.filtered(lambda m: m.product_id == self.p)

    def _manual_return(self, move, qty):
        ret = move.picking_id._create_return()
        ret.move_ids.write({"product_uom_qty": qty, "quantity": qty, "picked": True})
        return ret, ret.button_validate()

    def _legacy_sale(self, qty):
        self.company.urp_enabled = False
        order = self._sale(qty)
        self.company.urp_enabled = True
        self.assertFalse(order.urp_governed)
        return order

    def _wizard(self, action):
        self.assertIsInstance(action, dict)
        self.assertEqual(action["res_model"], "urp.return.check.wizard")
        return self.env["urp.return.check.wizard"].browse(action["res_id"])

    # ---------------------------------------------------------------- problem 1: mixed picking
    def test_f2_01_mixed_legacy_share_returnable_without_policy(self):
        self._legacy_sale(2)
        self._sale(1)
        move = self._close_stock()
        self.assertEqual(move._urp_origin_channel(), "pos")
        self.assertEqual(move._urp_unattributable_limit(), 2)
        with freeze_time(fields.Datetime.now() + timedelta(days=400)):
            _ret, res = self._manual_return(move, 2)
            self.assertIs(res, True, "legacy quantity: standard behaviour, no deadline")

    def test_f2_02_mixed_governed_share_cannot_leave_through_inventory(self):
        self._legacy_sale(2)
        self._sale(1)
        move = self._close_stock()
        with freeze_time(fields.Datetime.now() + timedelta(days=400)):
            ret, res = self._manual_return(move, 3)
            wiz = self._wizard(res)
            self.assertEqual((wiz.line_ids.kind, wiz.line_ids.eligible), ("unattributable", 2))
            self.assertIn("POS Refund", wiz.message)
            self.assertFalse(wiz.can_override, "no override: the receipt must be used")
            with self.assertRaises(UserError):
                ret.move_ids._action_done()                                 # final guard
            wiz.action_return_eligible_only()
            self.assertEqual(ret.state, "done")
            self.assertEqual(ret.move_ids.filtered(lambda m: m.state == "done").quantity, 2)
            # legacy share used up: nothing returnable from Inventory any more, refused at creation
            with self.assertRaisesRegex(UserError, "POS Refund"):
                move.picking_id._create_return()

    def test_f2_03_mixed_even_in_time_governed_share_goes_to_pos(self):
        self._legacy_sale(1)
        self._sale(2)
        move = self._close_stock()
        _ret, res = self._manual_return(move, 2)                            # still inside every period
        self.assertEqual(self._wizard(res).line_ids.kind, "unattributable")

    def test_f2_04_legacy_pos_refund_reduces_legacy_share(self):
        legacy = self._legacy_sale(2)
        self._sale(1)
        move = self._close_stock()
        self._refund(legacy, 1)                                             # standard POS refund
        self.assertEqual(move._urp_unattributable_limit(), 1)
        _ret, res = self._manual_return(move, 2)
        self.assertEqual(self._wizard(res).line_ids.eligible, 1)

    def test_f2_05_only_legacy_orders_fully_standard(self):
        self._legacy_sale(1)
        self._legacy_sale(1)
        move = self._close_stock()
        self.assertFalse(move._urp_origin_channel())
        with freeze_time(fields.Datetime.now() + timedelta(days=400)):
            _ret, res = self._manual_return(move, 2)
            self.assertIs(res, True)

    def test_f2_06_ship_later_order_of_same_session_does_not_count(self):
        self._legacy_sale(1)
        self._sale(1, ship_later=True)
        move = self._close_stock()
        self.assertFalse(move._urp_origin_channel())

    # ---------------------------------------------------------------- problem 2: different deadlines
    def test_f2_07_each_receipt_keeps_its_own_deadline(self):
        short = self._sale(1)                                    # product period 7 days
        self.p.product_tmpl_id.custom_return_period_days = 30
        long_ = self._sale(1)                                    # 30 days, same session, same move
        move = self._close_stock()
        self.assertFalse(move._urp_delivery_deadline(), "no common deadline invented for a grouped move")
        self.assertNotEqual(short.lines.urp_deadline, long_.lines.urp_deadline)
        with freeze_time(short.lines.urp_deadline + timedelta(days=1)):
            # the 30-day sale is still eligible: POS Refund on its receipt works without override
            self.assertEqual(self._refund(long_, 1).state, "paid")
            # the 7-day sale is expired: its own policy applies
            with self.assertRaises(UserError):
                self._refund(short, 1)
            # and the grouped delivery cannot be used to bypass it from Inventory (refused at creation)
            with self.assertRaisesRegex(UserError, "POS Refund"):
                move.picking_id._create_return()

    def test_f2_08_single_order_picking_goes_to_pos_receipt(self):
        # real-time stock update: one picking per order -> attributable, normal deadline check
        self.company.point_of_sale_update_stock_quantities = "real"
        sess = self.pos_config_usd.current_session_id
        if sess:
            sess.update_stock_at_closing = False
        order = self._sale(2)
        move = order.picking_ids.move_ids.filtered(lambda m: m.product_id == self.p)
        self.assertEqual(len(move), 1)
        # POS Direct governed quantity is returned through POS Refund on its receipt only
        # (period, manager override and refund in one place), in time or not, for every user
        self.assertEqual(move._urp_unattributable_limit(), 0)
        self.assertEqual(move._urp_delivery_deadline(), order.lines.urp_deadline)
        for when in (fields.Datetime.now(), order.lines.urp_deadline + timedelta(seconds=1)):
            with freeze_time(when), self.assertRaisesRegex(UserError, "POS Refund on that receipt"):
                move.picking_id._create_return()
