"""Manager approval of ship-later refunds in the POS.

A ship-later line has one deadline per delivery, unknown to the POS device, and the payment requires
a manager when the period ended or the product is non-returnable. The POS asks the server first
(``pos.session.urp_refund_needs``, read only) and opens the existing approval dialog. The payment
keeps its own checks and stays the only authority."""
from datetime import datetime

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import tagged

from odoo.addons.point_of_sale.tests.test_frontend import TestPointOfSaleHttpCommon
from .test_p5_pos import UrpPosCommon
from ..models.urp_guard import urp_internal

D1 = datetime(2026, 10, 1, 9, 0)        # first delivery: 7 days, right ends on 8 Oct (Asia/Riyadh)
D2 = datetime(2026, 10, 6, 9, 0)        # second delivery: right ends on 13 Oct
T = datetime(2026, 10, 10, 9, 0)        # first delivery expired, second one still in time


class _Rollback(Exception):
    pass


@tagged("post_install", "-at_install", "urp", "urp_ship_later_approval")
class TestShipLaterApproval(UrpPosCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.n = cls.env["product.product"].create({"name": "URP SL NoReturn", "is_storable": True, "list_price": 10.0,
                                                   "use_custom_return_period": True, "custom_return_period_days": 0})
        loc = cls.pos_config_usd.picking_type_id.default_location_src_id
        cls.env["stock.quant"]._update_available_quantity(cls.n, loc, 100)
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.company_b = cls.env["res.company"].create({"name": "URP SL other company"})
        cls.user_b = Users.create({"name": "SL B", "login": "urp_sl_b",
                                   "group_ids": [(6, 0, [cls.env.ref("point_of_sale.group_pos_user").id])],
                                   "company_ids": [(6, 0, cls.company_b.ids)], "company_id": cls.company_b.id})
        cls.internal = Users.create({"name": "SL internal", "login": "urp_sl_internal",
                                     "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])],
                                     "company_ids": [(6, 0, cls.company.ids)], "company_id": cls.company.id})

    # ------------------------------------------------------------------ helpers
    def _needs(self, lines, user=None):
        return self._session().with_user(user or self.cashier).urp_refund_needs(lines)

    def _manual_refund(self, specs):
        """Draft refund with several lines: specs = [(original line, qty)]."""
        lines = [Command.create({"name": "R", "product_id": orig.product_id.id, "price_unit": 10.0, "qty": -q,
                                 "refunded_orderline_id": orig.id, "price_subtotal": -10.0 * q,
                                 "price_subtotal_incl": -10.0 * q, "discount": 0}) for orig, q in specs]
        total = -10.0 * sum(q for _o, q in specs)
        return self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": self._session().id, "partner_id": self.partner.id,
            "lines": lines, "amount_tax": 0, "amount_total": total, "amount_paid": 0, "amount_return": 0})

    def _payment_needs(self, ref):
        """What the payment itself decides for this draft refund (nothing kept)."""
        got = None
        try:
            with self.env.cr.savepoint():
                got = sorted((n["kind"], n["orig"].id, n["qty"]) for n in ref._urp_check_refunds({}))
                raise _Rollback
        except _Rollback:
            pass
        self.env.invalidate_all()
        return got

    @staticmethod
    def _as_tuples(res):
        return sorted((n["kind"], n["refunded_line_id"], n["qty"]) for n in res["needs"])

    def _two_deliveries(self, qty=4, product=None):
        with freeze_time(D1):
            order = self._sale(qty, ship_later=True, product=product)
            self._deliver(order, qty // 2)
        with freeze_time(D2):
            self._deliver(order, qty - qty // 2)
        return order

    # ------------------------------------------------------------------ decisions
    def test_v01_in_time_needs_nothing(self):
        with freeze_time(D1):
            order = self._sale(3, ship_later=True)
            self._deliver(order, 3)
            ref = self._draft_refund(order, 1)
            self.assertEqual(self._needs(self._lines(ref)), {"needs": [], "blocked": False})
            self.assertEqual(self._pay_refund(ref).state, "paid", "no approval needed in time")

    def test_v02_expired_needs_the_manager_and_the_dialog_token_pays(self):
        with freeze_time(D1):
            order = self._sale(3, ship_later=True)
            self._deliver(order, 3)
        with freeze_time(T):
            ref = self._draft_refund(order, 2)
            res = self._needs(self._lines(ref))
            self.assertEqual(self._as_tuples(res), [("expired", order.lines.id, 2.0)])
            with self.assertRaisesRegex(UserError, "return period ended"), self.env.cr.savepoint():
                self._pay_refund(ref)                                     # no approval: refused, as before
            lines = [{"product_id": n["product_id"], "refunded_line_id": n["refunded_line_id"], "qty": n["qty"]}
                     for n in res["needs"]]
            tok = self._token(lines, ref.uuid)
            self.assertTrue(tok["ok"], tok)
            self.assertEqual(self._pay_refund(ref, tok["token"]).state, "paid")
        self.assertEqual((ref.urp_override_id.line_ids.exception_type, bool(ref.urp_override_id.consumed_at)),
                         ("expired", True))

    def test_v03_non_returnable(self):
        with freeze_time(D1):
            order = self._sale(2, ship_later=True, product=self.n)
            self._deliver(order, 2)
            ref = self._draft_refund(order, 1)
            self.assertEqual(self._as_tuples(self._needs(self._lines(ref))), [("non_returnable", order.lines.id, 1.0)])

    def test_v04_two_deliveries_with_different_deadlines(self):
        order = self._two_deliveries(4)
        with freeze_time(T):
            ref = self._draft_refund(order, 3)
            res = self._needs(self._lines(ref))
            # in-time delivery taken first (2), then 1 unit of the expired one
            self.assertEqual(self._as_tuples(res), [("expired", order.lines.id, 1.0)])
            tok = self._token([{"product_id": self.p.id, "refunded_line_id": order.lines.id, "qty": 1}], ref.uuid)
            self.assertEqual(self._pay_refund(ref, tok["token"]).state, "paid")
        by_move = {a.delivery_move_id.date: a.qty for a in ref.lines.urp_allocation_ids}
        self.assertEqual(sorted(by_move.values()), [1.0, 2.0])

    def test_v05_several_refund_lines_of_one_receipt_line(self):
        order = self._two_deliveries(4)
        with freeze_time(T):
            orig = order.lines
            one = self._needs([{"product_id": self.p.id, "refunded_line_id": orig.id, "qty": 3}])
            split = self._needs([{"product_id": self.p.id, "refunded_line_id": orig.id, "qty": 2},
                                 {"product_id": self.p.id, "refunded_line_id": orig.id, "qty": 1}])
            self.assertEqual(sum(n["qty"] for n in one["needs"]), sum(n["qty"] for n in split["needs"]))
            ref = self._manual_refund([(orig, 2), (orig, 1)])
            self.assertEqual(self._as_tuples(split), self._payment_needs(ref), "same decision as the payment")

    def test_v06_blocked_cases(self):
        with freeze_time(D1):
            order = self._sale(5, ship_later=True)
            self._deliver(order, 2)
            res = self._needs([{"product_id": self.p.id, "refunded_line_id": order.lines.id, "qty": 3}])
            self.assertIn("URP-OPS-01", res["blocked"])
            self.assertEqual(res["needs"], [])
        with freeze_time(D1):
            other = self._sale(4, ship_later=True)
            self._deliver(other, 4)
            pick = other.picking_ids.filtered(lambda p: p.state == "done")
            ret = pick._create_return()
            ret.move_ids.write({"product_uom_qty": 3, "quantity": 3, "picked": True})
            self.assertIs(ret.button_validate(), True)
            res = self._needs([{"product_id": self.p.id, "refunded_line_id": other.lines.id, "qty": 2}])
            self.assertIn("already returned through Inventory", res["blocked"])

    def test_v07_before_delivery_is_a_cancellation(self):
        with freeze_time(D1):
            order = self._sale(5, ship_later=True)
            self.assertEqual(self._needs([{"product_id": self.p.id, "refunded_line_id": order.lines.id, "qty": 2}]),
                             {"needs": [], "blocked": False})

    def test_v08_read_only(self):
        order = self._two_deliveries(4)
        with freeze_time(T):
            paid = self._draft_refund(order, 1)
            self._pay_refund(paid)                                                 # in time: allocation written
            kinds = paid.lines.mapped("urp_refund_kind")
            counts = lambda: tuple(self.env[m].sudo().search_count([]) for m in (                      # noqa: E731
                "urp.return.allocation", "urp.return.override", "urp.auth.attempt"))
            before = counts()
            for _ in range(3):
                self._needs([{"product_id": self.p.id, "refunded_line_id": order.lines.id, "qty": 3}])
            self.assertEqual(counts(), before, "nothing created")
            self.assertEqual(paid.lines.mapped("urp_refund_kind"), kinds, "nothing written")

    def test_v09_rights_company_and_input(self):
        with freeze_time(D1):
            order = self._sale(3, ship_later=True)
            self._deliver(order, 3)
        lines = [{"product_id": self.p.id, "refunded_line_id": order.lines.id, "qty": 1}]
        for user in (self.user_b, self.internal):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                self._needs(lines, user=user)
        with freeze_time(T):
            self.assertTrue(self._needs(lines)["needs"])
            for bad in ([{"product_id": self.n.id, "refunded_line_id": order.lines.id, "qty": 1}],   # other product
                        [{"product_id": self.p.id, "refunded_line_id": 999999999, "qty": 1}],          # no such line
                        [{"product_id": "x", "refunded_line_id": order.lines.id}], "garbage", None):
                self.assertEqual(self._needs(bad), {"needs": [], "blocked": False}, bad)
            direct = self._sale(1)                                                # not a ship-later line: ignored
            self.assertEqual(self._needs([{"product_id": self.p.id, "refunded_line_id": direct.lines.id, "qty": 1}]),
                             {"needs": [], "blocked": False})

    def test_v10_quantity_changed_after_the_approval(self):
        with freeze_time(D1):
            order = self._sale(3, ship_later=True)
            self._deliver(order, 3)
        with freeze_time(T):
            ref = self._draft_refund(order, 1)
            tok = self._token([{"product_id": self.p.id, "refunded_line_id": order.lines.id, "qty": 1}], ref.uuid)
            ref.lines.write({"qty": -2})
            ref.lines._onchange_amount_line_all()
            ref._compute_prices()
            with self.assertRaisesRegex(UserError, "does not cover"), self.env.cr.savepoint():
                self._pay_refund(ref, tok["token"])
            self.assertFalse(ref.urp_override_id, "the approval was not used")

    def test_v11_same_decision_as_the_payment(self):
        """The pre-check and the payment give the same answer (refactoring kept the behaviour)."""
        order = self._two_deliveries(4)
        nr = self._two_deliveries(4, product=self.n)
        for when in (D2, T):
            with freeze_time(when):
                for o, q in ((order, 1), (order, 3), (order, 4), (nr, 2)):
                    ref = self._draft_refund(o, q)
                    self.assertEqual(self._as_tuples(self._needs(self._lines(ref))), self._payment_needs(ref), (when, q))


class UrpShipLaterUiCommon(TestPointOfSaleHttpCommon):

    def _setup(self):
        config = self.main_pos_config
        config.company_id.write({"urp_tz": "Asia/Riyadh", "urp_default_days": 14, "urp_enabled": True})
        config.ship_later = True
        self.product = self.env["product.product"].create({
            "name": "URP ShipLater", "is_storable": True, "available_in_pos": True, "list_price": 10.0, "taxes_id": False,
            "use_custom_return_period": True, "custom_return_period_days": 7})
        self.env["stock.quant"]._update_available_quantity(
            self.product, config.picking_type_id.default_location_src_id, 100)
        # pos_stock refuses to validate a ship-later order unless the customer has a name, street, city
        # and country ("Incorrect address for shipping"), as Odoo's own ship-later tour uses partner_full
        self.env["res.partner"].create({"name": "URP SL Customer", "street": "1 Return Street", "city": "Riyadh",
                                        "country_id": self.env.ref("base.sa").id})
        config.with_user(self.pos_user).open_ui()
        self.start_pos_tour("UrpShipLaterSale", login="pos_user")
        sale = self.env["pos.order"].search([("session_id", "=", config.current_session_id.id)], limit=1)
        self.assertTrue(sale.shipping_date, "sold with Ship Later")
        # the warehouse ships 2 of the 3 units; that delivery's return period has ended since
        pick = sale.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        pick.move_ids.write({"quantity": 2, "picked": True})
        pick.with_context(skip_backorder=True, picking_ids_not_to_backorder=[]).button_validate()
        done = sale.picking_ids.move_ids.filtered(lambda m: m.state == "done")
        urp_internal(done).write({"urp_deadline": fields.Datetime.subtract(fields.Datetime.now(), days=1)})
        return config, sale


@tagged("post_install", "-at_install", "urp", "urp_ship_later_approval")
class TestShipLaterApprovalUi(UrpShipLaterUiCommon):

    def test_u06_dialog_opens_and_refund_is_paid(self):
        config, sale = self._setup()
        self.start_pos_tour("UrpShipLaterExpiredRefund", login="pos_user")
        refund = self.env["pos.order"].search([("session_id", "=", config.current_session_id.id), ("amount_total", "<", 0)])
        self.assertEqual((refund.state, refund.lines.refunded_orderline_id), ("paid", sale.lines))
        ov = refund.urp_override_id
        self.assertEqual((ov.user_id, ov.reason, ov.line_ids.exception_type),
                         (self.pos_admin, "Late return accepted by the manager", "expired"))

    def test_u07_no_answer_no_refund(self):
        config, sale = self._setup()
        self.start_pos_tour("UrpShipLaterPrecheckDown", login="pos_user")
        refund = self.env["pos.order"].search([("session_id", "=", config.current_session_id.id), ("amount_total", "<", 0),
                                               ("state", "=", "paid")])
        self.assertFalse(refund, "no refund paid without the pre-check")
