import base64
import time
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tests import Form, HttpCase, tagged
from odoo.addons.auth_totp.models.totp import hotp
from ..models.urp_guard import urp_internal

from odoo.addons.pos_stock.tests.common import CommonPosStockTest
from odoo.addons.unified_product_return_policy.models.urp_guard import UrpPayload


class UrpPosCommon(CommonPosStockTest):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.pos_config_usd.company_id
        cls.company.write({"urp_tz": "Asia/Riyadh", "urp_default_days": 14, "urp_enabled": True})
        cls.p = cls.ten_dollars_no_tax.product_variant_id
        cls.p.write({"is_storable": True})
        cls.p.product_tmpl_id.write({"use_custom_return_period": True, "custom_return_period_days": 7})
        cls.pos_config_usd.write({"payment_method_ids": [(6, 0, cls.cash_payment_method.ids)]})
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.manager = Users.create({"name": "POS Mgr", "login": "urp_pos_mgr", "password": "Mgr-Pass-123!",
                                    "group_ids": [(6, 0, [cls.env.ref("point_of_sale.group_pos_manager").id])],
                                    "company_ids": [(6, 0, cls.company.ids)], "company_id": cls.company.id})
        cls.cashier = Users.create({"name": "POS Cash", "login": "urp_pos_cash", "password": "Cash-Pass-123!",
                                    "group_ids": [(6, 0, [cls.env.ref("point_of_sale.group_pos_user").id])],
                                    "company_ids": [(6, 0, cls.company.ids)], "company_id": cls.company.id})

    def _session(self):
        if not self.pos_config_usd.current_session_id:
            self.pos_config_usd.open_ui()
        sess = self.pos_config_usd.current_session_id
        if sess.state == "opening_control":
            sess.set_opening_control(0, "")
        return sess

    def _pay(self, order, amount):
        ctx = {"active_ids": order.ids, "active_id": order.id}
        self.env["pos.make.payment"].with_context(**ctx).create({"amount": amount, "payment_method_id": self.cash_payment_method.id}).check()
        return order

    def _sale(self, qty, ship_later=False, product=None):
        product = product or self.p
        if ship_later:
            self.pos_config_usd.ship_later = True
        sess = self._session()
        order = self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": sess.id, "partner_id": self.partner.id,
            "lines": [Command.create({"name": "L", "product_id": product.id, "price_unit": 10.0, "qty": qty,
                                     "price_subtotal": 10.0 * qty, "price_subtotal_incl": 10.0 * qty, "discount": 0})],
            "amount_tax": 0, "amount_total": 10.0 * qty, "amount_paid": 0, "amount_return": 0, "to_invoice": False,
            "shipping_date": fields.Date.to_string(fields.Date.today() + relativedelta(days=1)) if ship_later else False,
        })
        return self._pay(order, 10.0 * qty)

    def _draft_refund(self, order, qty):
        action = order.refund()
        ref = self.env["pos.order"].browse(action["res_id"])
        ref.lines.write({"qty": -qty})
        ref.lines._onchange_amount_line_all()
        ref._compute_prices()
        return ref

    def _pay_refund(self, ref, token=None):
        if token:
            ref.urp_override_token = token
        return self._pay(ref, ref.amount_total)

    def _refund(self, order, qty, authorize=False):
        """Refund ``qty``; with ``authorize`` the manager authorises this very refund order first."""
        ref = self._draft_refund(order, qty)
        token = None
        if authorize:
            tok = self._token(self._lines(ref), ref.uuid)
            self.assertTrue(tok["ok"], tok)
            token = tok["token"]
        return self._pay_refund(ref, token)

    @staticmethod
    def _lines(order):
        return [{"product_id": l.product_id.id, "refunded_line_id": l.refunded_orderline_id.id or False, "qty": -l.qty}
                for l in order.lines if l.qty < 0]

    def _token(self, lines, order_uuid="urp-test-uuid", reason="Damaged on arrival", login="urp_pos_mgr",
               password="Mgr-Pass-123!", session=None):
        return (session or self._session()).with_user(self.cashier).urp_request_override(
            login, password, None, reason, lines, order_uuid)

    def _stock(self, product=None):
        loc = self.pos_config_usd.picking_type_id.default_location_src_id
        return (product or self.p).with_context(location=loc.id).qty_available

    def _deliver(self, order, qty):
        pick = order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        pick.move_ids.quantity = qty
        pick.move_ids.picked = True
        res = pick.button_validate()
        if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
            Form.from_action(self.env, res).save().process()


@tagged("post_install", "-at_install", "urp", "urp_p5")
class TestP5Pos(UrpPosCommon):

    # ---------------------------------------------------------------- F. POS returns
    def test_f01_snapshot_on_sale(self):
        order = self._sale(2)
        line = order.lines
        self.assertTrue(order.urp_governed)
        self.assertEqual((line.urp_return_days, line.urp_policy_source, line.urp_date_trust), (7, "product", "server"))
        self.assertTrue(line.urp_deadline > line.urp_sale_at + timedelta(days=7))
        self.p.product_tmpl_id.custom_return_period_days = 30
        self.assertEqual(line.urp_return_days, 7, "no retroactive change")
        line.write({"urp_return_days": 99, "urp_deadline": fields.Datetime.now() + timedelta(days=400)})
        self.assertEqual(line.urp_return_days, 7, "snapshot fields ignored on write")

    def test_f02_refund_in_time_ok_expired_needs_token(self):
        order = self._sale(3)
        self.assertEqual(self._refund(order, 1).state, "paid")
        with freeze_time(order.lines.urp_deadline + timedelta(seconds=1)):
            with self.assertRaises(UserError):
                self._refund(order, 1)
            ref = self._draft_refund(order, 1)
            tok = self._token(self._lines(ref), ref.uuid)
            self.assertTrue(tok["ok"], tok)
            self._pay_refund(ref, tok["token"])
            self.assertEqual(ref.state, "paid")
            self.assertEqual((ref.urp_override_id.user_id, ref.urp_override_id.reason), (self.manager, "Damaged on arrival"))
            self.assertEqual(ref.urp_override_id.pos_order_uuid, ref.uuid)
            self.assertFalse(ref.urp_override_token, "token never stored")
            with self.assertRaises(UserError):
                self._pay_refund(self._draft_refund(order, 1), tok["token"])          # reuse

    def test_f03_token_scope_and_expiry(self):
        order = self._sale(3)
        with freeze_time(order.lines.urp_deadline + timedelta(days=1)):
            ref = self._draft_refund(order, 2)
            tok = self._token([{"product_id": self.p.id, "refunded_line_id": order.lines.id, "qty": 1}], ref.uuid)
            with self.assertRaises(UserError):
                self._pay_refund(ref, tok["token"])                  # more than authorised
            ref2 = self._draft_refund(order, 1)
            tok2 = self._token(self._lines(ref2), ref2.uuid)
        with freeze_time(order.lines.urp_deadline + timedelta(days=1, minutes=11)):
            with self.assertRaises(UserError):
                self._pay_refund(ref2, tok2["token"])                # expired

    def test_f04_server_quantity_cap(self):
        order = self._sale(2)
        self._refund(order, 2)
        # a client that bypasses the UI check sends one more refund line linked to the same sale line
        ref_b = self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": self._session().id,
            "lines": [Command.create({"name": "R", "product_id": self.p.id, "price_unit": 10.0, "qty": -1,
                                     "refunded_orderline_id": order.lines.id,
                                     "price_subtotal": -10.0, "price_subtotal_incl": -10.0, "discount": 0})],
            "amount_tax": 0, "amount_total": -10.0, "amount_paid": 0, "amount_return": 0})
        with self.assertRaises(UserError):
            self._pay(ref_b, -10.0)

    def test_f05_cashier_cannot_issue_token(self):
        res = self._token([{"product_id": self.p.id, "qty": 1}], login="urp_pos_cash", password="Cash-Pass-123!")
        self.assertEqual(res, {"ok": False, "error": "invalid"})
        self.assertFalse(self._token([{"product_id": self.p.id, "qty": 1}], reason=" ")["ok"])

    def test_f06_lockout_after_five_failures(self):
        for _ in range(5):
            self.assertFalse(self._token([{"product_id": self.p.id, "qty": 1}], password="wrong")["ok"])
        self.assertEqual(self._token([{"product_id": self.p.id, "qty": 1}])["error"], "locked")

    def test_f07_legacy_sale_unaffected(self):
        self.company.urp_enabled = False
        order = self._sale(2)
        self.company.urp_enabled = True
        self.assertFalse(order.lines.urp_has_snapshot)
        with freeze_time(fields.Datetime.now() + timedelta(days=400)):
            self.assertEqual(self._refund(order, 1).state, "paid")

    # ---------------------------------------------------------------- G. standalone
    def test_g01_standalone_refund(self):
        sess = self._session()

        def standalone(token=None, uuid=None):
            o = self.env["pos.order"].create({
                "company_id": self.company.id, "session_id": sess.id, **({"uuid": uuid} if uuid else {}),
                "lines": [Command.create({"name": "S", "product_id": self.p.id, "price_unit": 10.0, "qty": -1,
                                         "price_subtotal": -10.0, "price_subtotal_incl": -10.0, "discount": 0})],
                "amount_tax": 0, "amount_total": -10.0, "amount_paid": 0, "amount_return": 0, "urp_override_token": token})
            return self._pay(o, -10.0)
        with self.assertRaises(UserError):
            standalone()
        tok = self._token([{"product_id": self.p.id, "qty": 1}], "urp-standalone-1", reason="Receipt lost, verified by manager")
        self.assertEqual(standalone(tok["token"], "urp-standalone-1").state, "paid")
        self.company.urp_enabled = False
        self.assertEqual(standalone().state, "paid", "policy disabled: standard behaviour")

    # ---------------------------------------------------------------- ship later
    def test_s01_ship_later_guard_and_stock(self):
        order = self._sale(5, ship_later=True)
        s0 = self._stock()
        self._deliver(order, 2)
        with self.assertRaises(UserError) as cm:
            self._refund(order, 3)
        self.assertIn("URP-OPS-01", str(cm.exception))
        self.assertEqual(self._stock(), s0 - 2)
        ref = self._refund(order, 2)
        self.assertEqual(ref.lines.urp_refund_kind, "return")
        self.assertEqual(self._stock(), s0)
        self.assertEqual(sum(ref.lines.urp_allocation_ids.mapped("qty")), 2)
        with self.assertRaises(UserError):
            self._refund(order, 1)

    def test_s02_ship_later_predelivery_cancellation(self):
        order = self._sale(5, ship_later=True)
        ref = self._refund(order, 2)
        self.assertEqual(ref.lines.urp_refund_kind, "cancel")
        self.assertEqual(order.picking_ids.move_ids.filtered(lambda m: m.state != "cancel").product_uom_qty, 3)

    def test_s03_ship_later_deadline_per_delivery(self):
        order = self._sale(4, ship_later=True)
        self._deliver(order, 4)
        move = order.picking_ids.move_ids.filtered(lambda m: m.state == "done")
        self.assertTrue(move.urp_deadline)
        with freeze_time(move.urp_deadline + timedelta(seconds=1)):
            with self.assertRaises(UserError):
                self._refund(order, 1)
            self.assertEqual(self._refund(order, 1, authorize=True).state, "paid")

    def test_s04_legacy_ship_later_untouched(self):
        self.company.urp_enabled = False
        order = self._sale(5, ship_later=True)
        self.company.urp_enabled = True
        self._deliver(order, 2)
        self.assertEqual(self._refund(order, 3).state, "paid")

    # ---------------------------------------------------------------- D10 inventory return of POS delivery
    def test_d10_manual_return_of_pos_delivery(self):
        order = self._sale(4, ship_later=True)
        self._deliver(order, 4)
        move = order.picking_ids.move_ids.filtered(lambda m: m.state == "done")
        ret = move.picking_id._create_return()
        ret.move_ids.product_uom_qty = 1
        ret.move_ids.quantity = 1
        ret.move_ids.picked = True
        self.assertTrue(ret.button_validate() is True, "in time: allowed")
        self._refund(order, 2)
        ret2 = move.picking_id._create_return()
        with self.assertRaisesRegex(UserError, "at most 1"), self.env.cr.savepoint():
            ret2.move_ids.product_uom_qty = 2                                   # refused when entered
        urp_internal(ret2.move_ids).write({"product_uom_qty": 2, "quantity": 2, "picked": True})
        action = ret2.button_validate()
        self.assertEqual(action["res_model"], "urp.return.check.wizard", "1 returned + 2 refunded -> only 1 left")

    # ---------------------------------------------------------------- offline
    def _unsnapshotted_order(self, qty=1):
        sess = self._session()
        order = self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": sess.id,
            "lines": [Command.create({"name": "O", "product_id": self.p.id, "price_unit": 10.0, "qty": qty,
                                     "price_subtotal": 10.0 * qty, "price_subtotal_incl": 10.0 * qty, "discount": 0})],
            "amount_tax": 0, "amount_total": 10.0 * qty, "amount_paid": 0, "amount_return": 0})
        return order, sess

    def test_a1_offline_trust_and_ambiguity(self):
        order, sess = self._unsnapshotted_order()
        start = sess.start_at
        frozen = freeze_time(start + timedelta(hours=1))
        frozen.start()
        self.addCleanup(frozen.stop)
        # bounded: device date inside the session, policy unchanged
        meta = {"uuid": order.uuid, "orig_session_id": sess.id, "device_date": fields.Datetime.to_string(start + timedelta(seconds=1))}
        order.with_context(urp_sync_meta=UrpPayload(meta))._urp_snapshot_lines()
        line = order.lines
        self.assertEqual((line.urp_date_trust, line.urp_policy_ambiguous), ("bounded", False))
        # clamped: device date before session start
        order2, _ = self._unsnapshotted_order()
        meta2 = {"uuid": order2.uuid, "orig_session_id": sess.id, "device_date": fields.Datetime.to_string(start - timedelta(days=2))}
        order2.with_context(urp_sync_meta=UrpPayload(meta2))._urp_snapshot_lines()
        self.assertEqual((order2.lines.urp_date_trust, order2.lines.urp_sale_at), ("clamped", start))
        # policy change inside the window -> flagged, applied policy = the one at the sale time
        order3, _ = self._unsnapshotted_order()
        self.p.product_tmpl_id.custom_return_period_days = 30
        meta3 = {"uuid": order3.uuid, "orig_session_id": sess.id, "device_date": fields.Datetime.to_string(start)}
        order3.with_context(urp_sync_meta=UrpPayload(meta3))._urp_snapshot_lines()
        self.assertTrue(order3.lines.urp_policy_ambiguous)
        self.assertTrue(order3.lines.urp_ambiguous_history_ids)
        self.assertEqual(order3.lines.urp_return_days, 7, "policy in force at the (bounded) sale time")
        # online sync (current order) -> server time, no ambiguity
        order4, _ = self._unsnapshotted_order()
        order4.with_context(urp_sync_meta=UrpPayload({"uuid": order4.uuid, "orig_session_id": sess.id, "device_date": False,
                                                      "server_time": True}))._urp_snapshot_lines()
        self.assertEqual((order4.lines.urp_date_trust, order4.lines.urp_return_days), ("server", 30))


SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


@tagged("post_install", "-at_install", "urp", "urp_p5")
class TestP5PosAuthHttp(UrpPosCommon, HttpCase):
    """POS Administrator re-authentication through a real HTTP JSON-RPC call, as the POS front end does (TOTP needs a request)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager.sudo().totp_secret = SECRET

    def _rpc(self, login, password, totp=None):
        sess = self._session()
        return self.make_jsonrpc_request("/web/dataset/call_kw/pos.session/urp_request_override", {
            "model": "pos.session", "method": "urp_request_override", "args": [sess.ids, login, password, totp,
                                                                                "Damaged", [{"product_id": self.p.id, "qty": 1}], "urp-http-uuid"],
            "kwargs": {}})

    def _code(self):
        return hotp(base64.b32decode(SECRET), int(time.time() / 30))

    def test_t01_totp_required_wrong_valid_replay(self):
        self.assertTrue(self.manager.totp_enabled)
        self.authenticate("urp_pos_cash", "Cash-Pass-123!")
        self.assertEqual(self._rpc("urp_pos_mgr", "Mgr-Pass-123!")["error"], "invalid")
        code = self._code()
        self.assertFalse(self._rpc("urp_pos_mgr", "Mgr-Pass-123!", 654321 if code == 123456 else 123456)["ok"])
        ok = self._rpc("urp_pos_mgr", "Mgr-Pass-123!", code)
        self.assertTrue(ok["ok"], ok)
        self.assertFalse(self._rpc("urp_pos_mgr", "Mgr-Pass-123!", code)["ok"], "replayed code rejected")
        self.assertEqual(self.env["urp.auth.attempt"].search_count([("user_id", "=", self.manager.id), ("success", "=", True)]), 1)
