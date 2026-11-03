"""POS Direct (paid and delivered at once, no ship later): the Inventory Return of the POS picking
must not bypass the POS return policy.

Sale on 1 Oct 2026 (Asia/Riyadh) of A (7 days), B (15 days), C (30 days); day 0 = sale day, so the
last allowed days are 8 Oct (A), 16 Oct (B) and 31 Oct (C). Time is simulated with freeze_time
only: no POS order date, effective date or deadline is ever written in the database."""
from unittest.mock import patch

from freezegun import freeze_time

from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tests import Form, tagged

from .test_p5_pos import UrpPosCommon

SALE = "2026-10-01 07:00:00"                       # 10:00 Riyadh
OCT5, OCT12, OCT20, NOV1 = ("2026-10-05 09:00:00", "2026-10-12 09:00:00",
                            "2026-10-20 09:00:00", "2026-11-01 09:00:00")
# products whose POS Refund is still within the period, per day (POS order date + own period)
IN_TIME = {OCT5: {"A", "B", "C"}, OCT12: {"B", "C"}, OCT20: {"C"}, NOV1: set()}


class UrpPosDirectCommon(UrpPosCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.point_of_sale_update_stock_quantities = "real"
        stock_user = cls.env.ref("stock.group_stock_user")
        cls.manager.group_ids |= stock_user
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        # warehouse user WITHOUT any Point of Sale right (cannot read POS order lines)
        cls.wh_user = Users.create({"name": "Warehouse User", "login": "urp_wh_user", "password": "Wh-Pass-123!",
                                    "group_ids": [(6, 0, stock_user.ids)],
                                    "company_ids": [(6, 0, cls.company.ids)], "company_id": cls.company.id})
        cls.prods = {}
        for code, days in (("A", 7), ("B", 15), ("C", 30)):
            tmpl = cls.env["product.template"].create({"name": "Direct " + code, "is_storable": True,
                                                      "available_in_pos": True, "list_price": 10, "taxes_id": [(5, 0)]})
            tmpl.write({"use_custom_return_period": True, "custom_return_period_days": days})
            cls.prods[code] = tmpl.product_variant_id
        cls.code_of = {p: c for c, p in cls.prods.items()}

    # ------------------------------------------------------------------ helpers
    def _order(self, qtys=None, prods=None):
        prods = prods or self.prods
        qtys = qtys or {c: 2 for c in prods}
        sess = self._session()
        total = 10.0 * sum(qtys.values())
        order = self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": sess.id, "partner_id": self.partner.id,
            "lines": [Command.create({"name": c, "product_id": prods[c].id, "price_unit": 10.0, "qty": q,
                                     "price_subtotal": 10.0 * q, "price_subtotal_incl": 10.0 * q, "discount": 0})
                      for c, q in qtys.items()],
            "amount_tax": 0, "amount_total": total, "amount_paid": 0, "amount_return": 0, "to_invoice": False})
        return self._pay(order, total)

    def _pos_refund(self, order, qtys, token=False):
        """POS Refund linked to the original receipt (cashier, no override unless ``token``)."""
        ref = self.env["pos.order"].browse(order.refund()["res_id"])
        for line in ref.lines:
            code = self.code_of.get(line.product_id)
            if code in qtys:
                line.qty = -qtys[code]
            else:
                line.unlink()
        ref.lines._onchange_amount_line_all()
        ref._compute_prices()
        if token:
            tok = self._token(self._lines(ref), ref.uuid)
            self.assertTrue(tok["ok"], tok)
            ref.urp_override_token = tok["token"]
        return self._pay(ref, ref.amount_total)

    def _pick(self, order):
        return order.picking_ids.filtered(lambda p: p.state == "done" and p.location_dest_id.usage == "customer")

    def _move(self, order, code):
        return self._pick(order).move_ids.filtered(lambda m: m.product_id == self.prods[code])

    def _forged_return(self, order, user, qtys):
        """Return picking built directly (RPC / import), without the Return button: only the
        validation guard can stop it."""
        pick = self._pick(order)
        ptype = pick.picking_type_id.return_picking_type_id or pick.picking_type_id
        ret = self.env["stock.picking"].with_user(user).create({
            "picking_type_id": ptype.id, "location_id": pick.location_dest_id.id, "location_dest_id": pick.location_id.id,
            "move_ids": [Command.create({
                "product_id": self.prods[c].id, "product_uom_qty": q, "origin_returned_move_id": self._move(order, c).id,
                "location_id": pick.location_dest_id.id, "location_dest_id": pick.location_id.id}) for c, q in qtys.items()]})
        ret.action_confirm()
        for move in ret.move_ids:
            move.quantity = move.product_uom_qty
        ret.move_ids.picked = True
        return ret

    def _wizard(self, action, user=None):
        self.assertIsInstance(action, dict)
        self.assertEqual(action["res_model"], "urp.return.check.wizard")
        wiz = self.env["urp.return.check.wizard"].browse(action["res_id"])
        return wiz.with_user(user) if user else wiz


@tagged("post_install", "-at_install", "urp", "urp_pos_direct")
class TestPosDirectReturns(UrpPosDirectCommon):

    # ---------------------------------------------------------------- 1. POS Refund, cashier
    def test_x01_pos_refund_per_product_period_from_pos_order_date(self):
        with freeze_time(SALE):
            orders = {day: self._order() for day in IN_TIME}
            o = next(iter(orders.values()))
            self.assertEqual({self.code_of[l.product_id]: str(l.urp_deadline) for l in o.lines},
                             {"A": "2026-10-08 21:00:00", "B": "2026-10-16 21:00:00", "C": "2026-10-31 21:00:00"})
        for day, in_time in IN_TIME.items():
            with freeze_time(day):
                for code in self.prods:
                    if code in in_time:
                        self.assertEqual(self._pos_refund(orders[day], {code: 1}).state, "paid", (day, code))
                    else:
                        with self.assertRaisesRegex(UserError, "return period ended", msg=(day, code)):
                            self._pos_refund(orders[day], {code: 1})

    def test_x02_one_refund_order_checked_line_by_line(self):
        with freeze_time(SALE):
            order = self._order()
        with freeze_time(OCT20):
            with self.assertRaises(UserError) as err:
                self._pos_refund(order, {"A": 1, "B": 1, "C": 1})
            msg = str(err.exception)
            self.assertIn("Direct A", msg)
            self.assertIn("Direct B", msg)
            self.assertNotIn("Direct C", msg, "C is still within its own 30 days")
            self.assertEqual(self._pos_refund(order, {"C": 2}).state, "paid", "the picking is never blocked as a whole")

    # ---------------------------------------------------------------- 4. POS Manager with the approved override
    def test_x03_pos_manager_override_on_the_receipt(self):
        with freeze_time(SALE):
            o12, o01 = self._order(), self._order()
        with freeze_time(OCT12):
            self.assertEqual(self._pos_refund(o12, {"A": 2}, token=True).state, "paid")
        with freeze_time(NOV1):
            ref = self._pos_refund(o01, {"A": 1, "B": 1, "C": 1}, token=True)
            self.assertEqual(ref.state, "paid")
            self.assertTrue(ref.urp_override_id.consumed_at)
            self.assertEqual(ref.urp_override_id.user_id, self.manager)

    # ---------------------------------------------------------------- 2/3. Inventory Return, normal user
    def test_x04_inventory_return_refused_for_normal_user_every_day(self):
        with freeze_time(SALE):
            order = self._order()
        move = self._move(order, "A")
        # root cause: without POS rights the user could not read the POS lines -> "not governed"
        self.assertEqual(move.with_user(self.wh_user)._urp_origin_channel(), "pos")
        self.assertEqual(move.with_user(self.wh_user)._urp_unattributable_limit(), 0)
        for day in IN_TIME:
            with freeze_time(day):
                with self.assertRaisesRegex(UserError, "POS Refund on that receipt", msg=day) as err:
                    self._pick(order).with_user(self.wh_user)._create_return()
                self.assertIn(order.pos_reference or order.name, str(err.exception))
        self.assertFalse(self._pick(order).move_ids.returned_move_ids)

    def test_x05_validation_guard_for_forged_return(self):
        """Return built without the Return button (RPC): refused at validation, no override."""
        with freeze_time(SALE):
            order = self._order()
        with freeze_time(OCT5):                                        # even within every period
            ret = self._forged_return(order, self.wh_user, {"A": 1, "C": 2})
            wiz = self._wizard(ret.with_user(self.wh_user).button_validate(), self.wh_user)
            self.assertEqual(set(wiz.line_ids.mapped("kind")), {"pos_receipt"})
            self.assertEqual(wiz.line_ids.mapped("eligible"), [0.0, 0.0])
            self.assertFalse(wiz.can_override)
            self.assertIn("POS Refund on that receipt", wiz.message)
            with self.assertRaisesRegex(UserError, "POS Refund"):
                ret.move_ids.with_user(self.wh_user)._action_done()   # final guard (barcode, RPC...)
            self.assertNotEqual(ret.state, "done")

    # ---------------------------------------------------------------- 4. Inventory Return, POS Manager
    def test_x06_inventory_return_refused_for_pos_manager_no_override(self):
        with freeze_time(SALE):
            order = self._order()
        for day in (OCT5, NOV1):
            with freeze_time(day):
                with self.assertRaisesRegex(UserError, "POS Refund on that receipt"):
                    self._pick(order).with_user(self.manager)._create_return()
        with freeze_time(NOV1):
            ret = self._forged_return(order, self.manager, {"A": 1})
            wiz = self._wizard(ret.with_user(self.manager).button_validate(), self.manager)
            self.assertEqual(wiz.line_ids.kind, "pos_receipt")
            self.assertFalse(wiz.can_override, "the override is given on the receipt, not in Inventory")
            wiz.reason = "try"
            with self.assertRaises(Exception):
                wiz.action_override()
            self.assertNotEqual(ret.state, "done")
            # the manager does it on the receipt instead
            self.assertEqual(self._pos_refund(order, {"A": 1}, token=True).state, "paid")

    # ---------------------------------------------------------------- period from the POS order, not the picking
    def test_x07_period_from_pos_sale_not_effective_date(self):
        """The POS picking is validated 9 days after the sale (automatic validation failed:
        lot missing). Its Effective Date does not move the POS return period."""
        self.company.urp_date_basis = "effective"
        tmpl = self.env["product.template"].create({"name": "Direct Lot", "is_storable": True, "tracking": "lot",
                                                   "available_in_pos": True, "list_price": 10, "taxes_id": [(5, 0)]})
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 7})
        lot_p = tmpl.product_variant_id
        self.code_of[lot_p] = "L"
        with freeze_time(SALE):
            order = self._order({"L": 1}, {"L": lot_p})
            pick = order.picking_ids
            self.assertEqual(len(pick), 1)
            self.assertNotEqual(pick.state, "done", "automatic validation failed (no lot)")
            deadline = order.lines.urp_deadline
            self.assertEqual(str(deadline), "2026-10-08 21:00:00")
        with freeze_time("2026-10-10 09:00:00"):
            lot = self.env["stock.lot"].create({"name": "L-1", "product_id": lot_p.id})
            pick.move_ids.quantity = 1
            pick.move_ids.move_line_ids.lot_id = lot
            pick.move_ids.picked = True
            res = pick.button_validate()
            if isinstance(res, dict):
                Form.from_action(self.env, res).save().process()
            self.assertEqual(pick.state, "done")
            self.assertEqual(str(pick.date_done), "2026-10-10 09:00:00")
        move = pick.move_ids
        self.assertFalse(move.urp_deadline, "no deadline stamped on a POS Direct delivery")
        self.assertEqual(move._urp_delivery_deadline(), deadline, "the sale's own deadline")
        self.assertEqual(order.lines.urp_deadline, deadline)
        with freeze_time(OCT12):     # within 7 days of the Effective Date, but not of the POS order date
            with self.assertRaisesRegex(UserError, "return period ended"):
                self._pos_refund(order, {"L": 1})
            with self.assertRaisesRegex(UserError, "POS Refund"):
                pick.with_user(self.wh_user)._create_return()

    # ---------------------------------------------------------------- partial quantities
    def test_x08_partial_quantities_on_the_receipt(self):
        with freeze_time(SALE):
            order = self._order()
        with freeze_time(OCT5):
            self.assertEqual(self._pos_refund(order, {"A": 1, "B": 1}).state, "paid")
            with self.assertRaisesRegex(UserError, "exceeds the quantity"):
                self._pos_refund(order, {"A": 2})                                  # 1 left
            self.assertEqual(self._pos_refund(order, {"A": 1}).state, "paid")
        with freeze_time(OCT20):
            with self.assertRaisesRegex(UserError, "return period ended"):
                self._pos_refund(order, {"B": 1})
            self.assertEqual(self._pos_refund(order, {"B": 1}, token=True).state, "paid")
            self.assertEqual(self._pos_refund(order, {"C": 2}).state, "paid")

    # ---------------------------------------------------------------- no double return across channels
    def test_x09_inventory_return_of_previous_version_counts_on_the_receipt(self):
        """Returns made from Inventory before the POS Direct rule applied (allowed within the period) are
        deducted from what the receipt can still refund: the same goods are never returned twice."""
        with freeze_time(SALE):
            order = self._order()
        StockMove = type(self.env["stock.move"])
        with freeze_time(OCT5):
            with patch.object(StockMove, "_urp_unattributable_limit", lambda self: None):     # previous behaviour
                ret = self._pick(order).with_user(self.wh_user)._create_return()
                for move in ret.move_ids:
                    q = {"A": 2, "B": 1, "C": 0}[self.code_of[move.product_id]]
                    move.write({"product_uom_qty": q, "quantity": q, "picked": True})
                self.assertIs(ret.with_user(self.wh_user).button_validate(), True)
            with self.assertRaisesRegex(UserError, "already returned through Inventory"):
                self._pos_refund(order, {"A": 1})
            self.assertEqual(self._pos_refund(order, {"B": 1}).state, "paid")
            with self.assertRaisesRegex(UserError, "already returned through Inventory"):
                self._pos_refund(order, {"B": 1})
            self.assertEqual(self._pos_refund(order, {"C": 2}).state, "paid")

    def test_x10_pos_refund_then_inventory(self):
        with freeze_time(SALE):
            order = self._order()
        with freeze_time(OCT5):
            self.assertEqual(self._pos_refund(order, {"A": 2}).state, "paid")
            ret = self._forged_return(order, self.manager, {"A": 1})
            wiz = self._wizard(ret.button_validate())
            self.assertEqual((wiz.line_ids.kind, wiz.line_ids.eligible), ("pos_receipt", 0.0))

    # ---------------------------------------------------------------- legacy sales unaffected
    def test_x11_legacy_pos_direct_sale_standard(self):
        with freeze_time(SALE):
            self.company.urp_enabled = False
            order = self._order()
            self.company.urp_enabled = True
            self.assertFalse(order.urp_governed)
        with freeze_time(NOV1):
            ret = self._pick(order).with_user(self.wh_user)._create_return()
            self.assertEqual(len(ret.move_ids), 3)
            ret.move_ids.write({"product_uom_qty": 1, "quantity": 1, "picked": True})
            self.assertIs(ret.with_user(self.wh_user).button_validate(), True)
            self.assertEqual(self._pos_refund(order, {"A": 1}).state, "paid", "legacy: standard POS refund")


@tagged("post_install", "-at_install", "urp", "urp_pos_direct")
class TestPosShipLaterStockUser(UrpPosDirectCommon):
    """Same root cause on ship-later deliveries: a warehouse user without POS rights saw the
    delivery as not governed. Ship later keeps its Inventory return within the period."""

    def _ship_later(self, qty):
        order = self._sale(qty, ship_later=True, product=self.prods["A"])
        pick = order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        pick.move_ids.quantity = qty
        pick.move_ids.picked = True
        self.assertIs(pick.with_user(self.wh_user).button_validate(), True)
        return order, pick

    def test_y01_ship_later_expired_refused_for_stock_user(self):
        with freeze_time(SALE):
            order, pick = self._ship_later(2)
            self.assertEqual(str(pick.move_ids.urp_deadline), "2026-10-08 21:00:00")
        with freeze_time(OCT5):
            self.assertEqual(pick.move_ids.with_user(self.wh_user)._urp_origin_channel(), "pos")
        with freeze_time(NOV1):
            with self.assertRaisesRegex(UserError, "return period has ended"):
                pick.with_user(self.wh_user)._create_return()

    def test_y02_ship_later_inventory_then_pos_refund_no_double(self):
        with freeze_time(SALE):
            order, pick = self._ship_later(4)
        with freeze_time(OCT5):
            ret = pick.with_user(self.wh_user)._create_return()
            ret.move_ids.write({"product_uom_qty": 3, "quantity": 3, "picked": True})
            self.assertIs(ret.with_user(self.wh_user).button_validate(), True, "ship later: Inventory allowed in time")
            with self.assertRaisesRegex(UserError, "already returned through Inventory"):
                self._pos_refund(order, {"A": 2})
            self.assertEqual(self._pos_refund(order, {"A": 1}).state, "paid")
            with self.assertRaisesRegex(UserError, "already returned through Inventory"):
                self._pos_refund(order, {"A": 1})


@tagged("post_install", "-at_install", "urp", "urp_pos_direct")
class TestPosAggregatedStockUser(UrpPosDirectCommon):
    """Aggregated session picking (stock updated at closing) with legacy and governed sales:
    the legacy share stays returnable from Inventory, separately; the governed share does not."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.point_of_sale_update_stock_quantities = "closing"

    def test_z01_mixed_picking_legacy_share_for_stock_user(self):
        with freeze_time(SALE):
            self.company.urp_enabled = False
            self._order({"A": 2, "B": 1})                                    # legacy sale
            self.company.urp_enabled = True
            self._order({"A": 1, "C": 1})                                    # governed sale
            sess = self._session()
            sess._create_picking_at_end_of_session()
            pick = sess.picking_ids.filtered(lambda p: not p.pos_order_id and p.state == "done")
            self.assertEqual(len(pick), 1)
        moves = {self.code_of[m.product_id]: m for m in pick.move_ids}
        as_user = {c: m.with_user(self.wh_user) for c, m in moves.items()}
        self.assertEqual({c: m._urp_unattributable_limit() for c, m in as_user.items() if m._urp_origin_channel()},
                         {"A": 2, "C": 0})
        self.assertFalse(as_user["B"]._urp_origin_channel(), "B was only sold before the policy")
        with freeze_time(NOV1):
            ret = pick.with_user(self.wh_user)._create_return()
            # C (governed only) is not added; A (mixed) and B (legacy) are
            self.assertEqual({self.code_of[m.product_id] for m in ret.move_ids}, {"A", "B"})
            for move in ret.move_ids:
                q = {"A": 3, "B": 1}[self.code_of[move.product_id]]
                move.write({"product_uom_qty": q, "quantity": q, "picked": True})
            wiz = self._wizard(ret.with_user(self.wh_user).button_validate(), self.wh_user)
            self.assertEqual([(l.product_id, l.kind, l.eligible) for l in wiz.line_ids],
                             [(self.prods["A"], "unattributable", 2.0)])
            wiz.action_return_eligible_only()
            self.assertEqual(ret.state, "done")
            self.assertEqual({self.code_of[m.product_id]: m.quantity for m in ret.move_ids if m.state == "done"},
                             {"A": 2.0, "B": 1.0})
