"""0 days = NON-RETURNABLE, with the existing fields of products and categories.

(enabled, 0) is a final policy, never "not set": no fallback to the category or the company, no
"returnable until the end of the sale day", and the return period override does not apply.
The policy frozen at the sale decides (non-retroactive), in every channel."""
from datetime import timedelta

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Command
from odoo.tests import Form, tagged

from odoo.addons.unified_product_return_policy.models.urp_guard import UrpPayload

from .common import UrpCommon
from .test_p4_portal import UrpPortalCommon
from .test_p11_return_creation import NOV_2, NOV_8, UrpReturnCreationCommon
from .test_p15_pos_direct import OCT5, OCT12, SALE, UrpPosDirectCommon


# ====================================================================== settings & priority
@tagged("post_install", "-at_install", "urp", "urp_non_returnable")
class TestNonReturnableSettings(UrpCommon):

    def _resolve(self, product=None):
        res = (product or self.product)._urp_resolve(self.company)
        return res["return_days"], res["policy_source"], res["non_returnable"]

    # 1-4 form behaviour
    def test_n01_first_activation_suggests_one_day(self):
        for model in ("product.template", "product.category"):
            with Form(self.env[model]) as f:
                f.name = "First " + model
                f.use_custom_return_period = True
                self.assertEqual(f.custom_return_period_days, 1, model)

    def test_n02_zero_saved_kept_on_reopen_and_reactivation(self):
        for model in ("product.template", "product.category"):
            with Form(self.env[model]) as f:
                f.name = "Zero " + model
                f.use_custom_return_period = True
                f.custom_return_period_days = 0
            rec = f.record
            self.assertEqual((rec.use_custom_return_period, rec.custom_return_period_days), (True, 0), model)
            self.assertTrue(rec.urp_non_returnable)
            with Form(rec) as f2:                                   # reopen
                self.assertEqual(f2.custom_return_period_days, 0)
                f2.use_custom_return_period = False
            self.assertEqual((rec.use_custom_return_period, rec.custom_return_period_days), (False, 0),
                             "disabled: value kept, not used")
            with Form(rec) as f3:
                f3.use_custom_return_period = True
                self.assertEqual(f3.custom_return_period_days, 0, "re-activation restores the deliberate 0")
            self.assertEqual((rec.use_custom_return_period, rec.custom_return_period_days), (True, 0))

    def test_n03_disable_reenable_through_orm_keeps_zero(self):
        tmpl = self.product.product_tmpl_id
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 0})
        tmpl.write({"use_custom_return_period": False})
        self.assertEqual(self._resolve()[1], "company")
        tmpl.write({"use_custom_return_period": True})
        self.assertEqual(tmpl.custom_return_period_days, 0)
        self.assertEqual(self._resolve(), (0, "product", True))

    # 5-6 ORM / import / negative values
    def test_n04_orm_and_import(self):
        Tmpl, Categ = self.env["product.template"], self.env["product.category"]
        self.assertEqual(Tmpl.create({"name": "A", "use_custom_return_period": True}).custom_return_period_days, 1)
        self.assertEqual(Categ.create({"name": "A", "use_custom_return_period": True}).custom_return_period_days, 1)
        self.assertEqual(Tmpl.create({"name": "B", "use_custom_return_period": True,
                                      "custom_return_period_days": 0}).custom_return_period_days, 0)
        # first activation through write, nothing set before -> 1
        never = Tmpl.create({"name": "C"})
        never.write({"use_custom_return_period": True})
        self.assertEqual(never.custom_return_period_days, 1)
        res = Tmpl.load(["name", "use_custom_return_period", "custom_return_period_days"], [["Imp0", "True", "0"]])
        self.assertFalse(res["messages"], res)
        self.assertEqual(Tmpl.browse(res["ids"]).custom_return_period_days, 0)
        res = Tmpl.load(["name", "use_custom_return_period"], [["Imp1", "True"]])
        self.assertEqual(Tmpl.browse(res["ids"]).custom_return_period_days, 1)

    def test_n05_negative_refused(self):
        with self.assertRaises(ValidationError):
            self.product.product_tmpl_id.write({"use_custom_return_period": True, "custom_return_period_days": -1})
        with self.assertRaises(ValidationError):
            self.env["product.category"].create({"name": "neg", "use_custom_return_period": True, "custom_return_period_days": -3})
        res = self.env["product.category"].load(["name", "use_custom_return_period", "custom_return_period_days"],
                                                [["NegImp", "True", "-1"]])
        self.assertTrue(any(m["type"] == "error" for m in res["messages"]), res)

    def test_n06_copy_keeps_the_values(self):
        tmpl = self.env["product.template"].create({"name": "Src", "use_custom_return_period": True,
                                                    "custom_return_period_days": 0})
        dup = tmpl.copy()
        self.assertNotEqual((dup.use_custom_return_period, dup.custom_return_period_days), (True, 1),
                            "copy never turns a deliberate 0 into the suggested 1")
        if dup.use_custom_return_period:
            self.assertEqual(dup.custom_return_period_days, 0)

    # 7-9 + 4th scenario: priority, zero is final
    def test_n07_priority_with_zero(self):
        tmpl, categ = self.product.product_tmpl_id, self.categ
        cases = [
            ((True, 0), (True, 10), (0, "product", True)),       # scenario 1
            ((False, 0), (True, 0), (0, "category", True)),      # scenario 2
            ((True, 7), (True, 0), (7, "product", False)),       # scenario 3
            ((False, 0), (False, 0), (14, "company", False)),    # scenario 4
        ]
        for (pu, pd), (cu, cd), expected in cases:
            tmpl.write({"use_custom_return_period": pu, "custom_return_period_days": pd})
            categ.write({"use_custom_return_period": cu, "custom_return_period_days": cd})
            self.assertEqual(self._resolve(), expected, ((pu, pd), (cu, cd)))

    def test_n08_history_resolves_zero_at_past_instant(self):
        tmpl = self.product.product_tmpl_id
        # after the policy was enabled in setUpClass (real time), whatever the day the test runs
        day1 = fields.Datetime.now().replace(microsecond=0) + timedelta(days=1)
        day2, day3 = day1 + timedelta(days=1), day1 + timedelta(days=2)
        with freeze_time(day1):
            tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 7})
        with freeze_time(day2):
            tmpl.write({"custom_return_period_days": 0})
        with freeze_time(day3):
            tmpl.write({"custom_return_period_days": 14})
        later = timedelta(hours=4)
        self.assertEqual(self.product._urp_resolve(self.company, at=day1 + later)["return_days"], 7)
        res = self.product._urp_resolve(self.company, at=day2 + later)
        self.assertEqual((res["return_days"], res["policy_source"], res["non_returnable"]), (0, "product", True))
        self.assertEqual(self.product._urp_resolve(self.company, at=day3 + later)["return_days"], 14)

    def test_n09_company_default_stays_positive(self):
        with self.assertRaises(ValidationError):
            self.company.urp_default_days = 0


# ====================================================================== sales / inventory
@tagged("post_install", "-at_install", "urp", "urp_non_returnable")
class TestNonReturnableSales(UrpReturnCreationCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.nr = cls.env["product.product"].create({"name": "URP No Return", "is_storable": True,
                                                   "use_custom_return_period": True, "custom_return_period_days": 0})
        cls.env["stock.quant"]._update_available_quantity(cls.nr, cls.warehouse.lot_stock_id, 100)

    def _forged(self, delivery, user, qty_by_product):
        """Return built without the Return button: only the validation guard can stop it."""
        ret = self.env["stock.picking"].with_user(user).create({
            "picking_type_id": delivery.picking_type_id.return_picking_type_id.id or delivery.picking_type_id.id,
            "location_id": delivery.location_dest_id.id, "location_dest_id": delivery.location_id.id,
            "move_ids": [Command.create({"product_id": m.product_id.id, "product_uom_qty": qty_by_product[m.product_id],
                                         "origin_returned_move_id": m.id, "location_id": m.location_dest_id.id,
                                         "location_dest_id": m.location_id.id})
                         for m in delivery.move_ids if m.product_id in qty_by_product]})
        ret.action_confirm()
        return ret

    def _wizard(self, action, user):
        self.assertEqual(action["res_model"], "urp.return.check.wizard")
        return self.env["urp.return.check.wizard"].with_user(user).browse(action["res_id"])

    # 10, 14, 15, 20
    def test_s01_sold_non_returnable_stays_so_unless_approved(self):
        """Non-returnable stays non-returnable for the normal return; only an explicit,
        reasoned approval of a Sales Administrator (typed Non-Returnable Exception) lets it pass."""
        pick = self._delivered([(self.nr, 3)])
        move = pick.move_ids
        self.assertEqual((move.urp_batch_id.return_days, move.urp_batch_id.policy_source), (0, "product"))
        self.assertEqual((move.urp_deadline, move.urp_portal_deadline), (False, False), "no expiry date at all")
        self.assertTrue(move.urp_validated_at)
        self.nr.product_tmpl_id.custom_return_period_days = 14                 # policy changed afterwards
        for when in (VALIDATED_SAME_DAY, NOV_2, NOV_8):
            with freeze_time(when):
                with self.assertRaisesRegex(UserError, "non-returnable", msg=when):
                    self._create(pick, self.sales_user)
        with freeze_time(NOV_2):
            ret = self._forged(pick, self.sales_user, {self.nr: 1})
            wiz = self._wizard(self._validate(ret.with_user(self.sales_user)), self.sales_user)
            self.assertEqual((wiz.line_ids.kind, wiz.line_ids.eligible), ("non_returnable", 0.0))
            self.assertFalse(wiz.can_override, "a sales user cannot approve")
            with self.assertRaisesRegex(UserError, "non-returnable"):
                ret.move_ids.with_user(self.sales_user)._action_done()
            self.assertNotEqual(ret.state, "done")
            # the Sales Administrator (with stock rights) approves and executes, with a reason
            ret2 = self._create(pick, self.sales_admin)
            self._set_qty(ret2, {self.nr: 1})
            wiz2 = self._wizard(self._validate(ret2.with_user(self.sales_admin)), self.sales_admin)
            self.assertTrue(wiz2.can_override)
            wiz2.reason = "Customer insists, approved by sales management"
            wiz2.action_override()
            self.assertEqual(ret2.state, "done")
            self.assertEqual(ret2.urp_override_id.line_ids.exception_type, "non_returnable")
        self.assertEqual(move.urp_batch_id.return_days, 0, "the snapshot never changes")

    # 11
    def test_s02_sold_returnable_keeps_its_period_after_change_to_zero(self):
        pick = self._delivered([(self.product, 5)])                             # 10 days at the sale
        self.product.product_tmpl_id.custom_return_period_days = 0
        with freeze_time(NOV_2):
            ret = self._create(pick, self.sales_user)
            self._set_qty(ret, {self.product: 2})
            self.assertIs(self._validate(ret), True, "historical 10 days still apply")
        with freeze_time(NOV_8):
            ret2 = self._create(pick, self.sales_admin)
            self._set_qty(ret2, {self.product: 1})
            wiz = self._wizard(self._validate(ret2.with_user(self.sales_admin)), self.sales_admin)
            self.assertEqual(wiz.line_ids.kind, "expired", "an ordinary expired period, not non-returnable")
            self.assertTrue(wiz.can_override)

    # 18, 19
    def test_s03_mixed_order_returnable_lines_unaffected(self):
        pick = self._delivered([(self.nr, 2), (self.product_long, 2)])
        with freeze_time(NOV_2):
            ret = self._create(pick, self.sales_user)
            self.assertEqual(ret.move_ids.product_id, self.product_long, "only the returnable product is added")
            self.assertTrue(any("non-returnable" in str(b) for b in ret.message_ids.mapped("body")))
            self._set_qty(ret, {self.product_long: 2})
            self.assertIs(self._validate(ret), True)
            ret2 = self._forged(pick, self.sales_user, {self.nr: 1})
            wiz = self._wizard(self._validate(ret2), self.sales_user)
            self.assertEqual(wiz.line_ids.kind, "non_returnable")

    # 21
    def test_s04_legacy_sale_standard(self):
        self.company.urp_enabled = False
        pick = self._delivered([(self.nr, 2)])
        self.company.urp_enabled = True
        self.assertFalse(pick.move_ids.urp_batch_id)
        with freeze_time(NOV_8):
            ret = self._create(pick, self.sales_user)
            self._set_qty(ret, {self.nr: 1})
            self.assertIs(self._validate(ret), True, "sold before the policy: standard behaviour")

    def test_s05_category_zero_applies_to_sales(self):
        categ = self.env["product.category"].create({"name": "NR Cat", "use_custom_return_period": True,
                                                     "custom_return_period_days": 0})
        p = self.env["product.product"].create({"name": "Cat NR", "is_storable": True, "categ_id": categ.id})
        self.env["stock.quant"]._update_available_quantity(p, self.warehouse.lot_stock_id, 10)
        pick = self._delivered([(p, 1)])
        self.assertEqual((pick.move_ids.urp_batch_id.return_days, pick.move_ids.urp_batch_id.policy_source), (0, "category"))
        with freeze_time(NOV_2), self.assertRaisesRegex(UserError, "non-returnable"):
            self._create(pick, self.sales_user)

    # scope: the non-returnable rule only applies to returns of customer deliveries
    def _done(self, ptype, src, dst, qty, user=None):
        pick = self.env["stock.picking"].with_user(user or self.env.user).create({
            "picking_type_id": ptype.id, "location_id": src.id, "location_dest_id": dst.id,
            "move_ids": [Command.create({"product_id": self.nr.id, "product_uom_qty": qty,
                                         "location_id": src.id, "location_dest_id": dst.id})]})
        pick.action_confirm()
        pick.action_assign()
        pick.move_ids.write({"quantity": qty, "picked": True})
        self.assertIs(pick.button_validate(), True, ptype.name)
        return pick

    def test_s06_scope_operations_not_linked_to_customer_returns(self):
        wh, stock_user = self.warehouse, self.sales_user
        supplier = self.env.ref("stock.stock_location_suppliers")
        customer = self.env.ref("stock.stock_location_customers")
        shelf = self.env["stock.location"].create({"name": "NR Shelf", "location_id": wh.lot_stock_id.id})
        with freeze_time(NOV_8):
            # vendor receipt and its return to the vendor (purchase return)
            receipt = self._done(wh.in_type_id, supplier, wh.lot_stock_id, 5)
            vret = receipt.with_user(stock_user)._create_return()
            self.assertEqual(vret.move_ids.product_id, self.nr)
            vret.move_ids.write({"product_uom_qty": 2})
            self.assertIs(self._validate(vret.with_user(stock_user)), True, "purchase return")
            # internal transfer and its reverse
            internal = self._done(wh.int_type_id, wh.lot_stock_id, shelf, 3, stock_user)
            iret = internal.with_user(stock_user)._create_return()
            iret.move_ids.write({"product_uom_qty": 3})
            self.assertIs(self._validate(iret.with_user(stock_user)), True, "internal transfer return")
            # scrap and inventory adjustment
            # scrap (Odoo 20: a move to the company scrap location)
            self._done(wh.int_type_id, wh.lot_stock_id, self.company.scrap_location_id, 1, stock_user)
            quant = self.env["stock.quant"].search([("product_id", "=", self.nr.id),
                                                    ("location_id", "=", wh.lot_stock_id.id)], limit=1)
            quant.inventory_quantity = quant.quantity + 4
            quant.action_apply_inventory()
            # receipt from the customer location NOT linked to a delivery: standard (not a sale return)
            self._done(wh.in_type_id, customer, wh.lot_stock_id, 1, stock_user)
            # a delivery of a non-returnable product is not blocked either
            pick = self._delivered([(self.nr, 1)], at=NOV_8)
            self.assertEqual(pick.state, "done")
            # and the return of a customer RETURN (sending it back to the customer) is not a sale return
            self.company.urp_enabled = False
            legacy = self._delivered([(self.nr, 2)], at=NOV_8)
            self.company.urp_enabled = True
            cret = legacy._create_return()
            cret.move_ids.write({"product_uom_qty": 1})
            self.assertIs(self._validate(cret), True)
            back = cret._create_return()
            back.move_ids.write({"product_uom_qty": 1})
            self.assertIs(self._validate(back), True, "re-delivery of a returned item")


VALIDATED_SAME_DAY = "2026-10-25 15:00:00"           # same local day as the delivery (25 Oct, Riyadh)


# ====================================================================== portal
@tagged("post_install", "-at_install", "urp", "urp_non_returnable")
class TestNonReturnablePortal(UrpPortalCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.nr = cls.env["product.product"].create({"name": "Portal No Return", "is_storable": True, "list_price": 10,
                                                   "use_custom_return_period": True, "custom_return_period_days": 0})
        wh = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.env["stock.quant"]._update_available_quantity(cls.nr, wh.lot_stock_id, 100)

    def _delivered_mixed(self):
        so = self.env["sale.order"].create({"partner_id": self.partner.id, "order_line": [
            (0, 0, {"product_id": self.nr.id, "product_uom_qty": 2}), (0, 0, {"product_id": self.product.id, "product_uom_qty": 3})]})
        so.action_confirm()
        pick = so.picking_ids
        for m in pick.move_ids:
            m.quantity = m.product_uom_qty
            m.picked = True
        pick.button_validate()
        so._portal_ensure_token()
        moves = so.order_line.move_ids.filtered(lambda m: m.state == "done")
        return so, moves.filtered(lambda m: m.product_id == self.nr), moves.filtered(lambda m: m.product_id == self.product)

    # 15, 16, 17, 18, 19
    def test_p01_not_offered_and_rpc_refused(self):
        so, nr_move, ok_move = self._delivered_mixed()
        self.assertEqual((nr_move.urp_batch_id.portal_days, nr_move.urp_portal_deadline), (0, False))
        self.assertFalse(nr_move._urp_portal_open())
        data = self._data(so)
        self.assertEqual([l["move_id"] for l in data["returnable_lines"]], ok_move.ids, "only the returnable product")
        res = self._request(so, {nr_move: 1})
        self.assertIn("not returnable", res.get("error", ""))
        res = self._request(so, {nr_move: 1, ok_move: 1})
        self.assertIn("error", res, "a mixed request cannot smuggle the non-returnable line")
        res = self._request(so, {ok_move: 2})
        self.assertIn("request_id", res, res)
        self.assertFalse(self.env["urp.portal.return.request.line"].search([("move_id", "=", nr_move.id)]))

    def test_p02_only_non_returnable_order_has_no_portal_return(self):
        so = self.env["sale.order"].create({"partner_id": self.partner.id, "order_line": [
            (0, 0, {"product_id": self.nr.id, "product_uom_qty": 1})]})
        so.action_confirm()
        so.picking_ids.move_ids.write({"quantity": 1, "picked": True})
        so.picking_ids.button_validate()
        self.assertFalse(so._is_portal_return_allowed())


# ====================================================================== POS
@tagged("post_install", "-at_install", "urp", "urp_non_returnable")
class TestNonReturnablePos(UrpPosDirectCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tmpl = cls.env["product.template"].create({"name": "Direct N", "is_storable": True, "available_in_pos": True,
                                                   "list_price": 10, "taxes_id": [(5, 0)]})
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 0})
        cls.prods["N"] = tmpl.product_variant_id
        cls.code_of[tmpl.product_variant_id] = "N"

    def _standalone(self, product, token=None, uuid=None):
        sess = self._session()
        o = self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": sess.id, **({"uuid": uuid} if uuid else {}),
            "lines": [Command.create({"name": "S", "product_id": product.id, "price_unit": 10.0, "qty": -1,
                                     "price_subtotal": -10.0, "price_subtotal_incl": -10.0, "discount": 0})],
            "amount_tax": 0, "amount_total": -10.0, "amount_paid": 0, "amount_return": 0, "urp_override_token": token})
        return self._pay(o, -10.0)

    # 12, 14, 18, 19, 20
    def test_q01_pos_direct_refund_needs_the_administrator(self):
        with freeze_time(SALE):
            order = self._order({"N": 2, "C": 2})
            n_line = order.lines.filtered(lambda l: l.product_id == self.prods["N"])
            self.assertEqual((n_line.urp_return_days, n_line.urp_deadline), (0, False), "no expiry date shown")
        with freeze_time(OCT5):
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._pos_refund(order, {"N": 1})
            with self.assertRaises(UserError) as err:
                self._pos_refund(order, {"N": 1, "C": 1})
            self.assertNotIn("Direct C", str(err.exception))
            self.assertEqual(self._pos_refund(order, {"C": 2}).state, "paid", "the returnable product is unaffected")
            with self.assertRaisesRegex(UserError, "POS Refund on that receipt"):
                self._pick(order).with_user(self.manager)._create_return()    # inventory: POS Refund only
            # Non-Returnable Exception of the POS Administrator, on the receipt
            ref = self._pos_refund(order, {"N": 1}, token=True)
            self.assertEqual((ref.state, ref.lines.refunded_orderline_id), ("paid", n_line))
            self.assertEqual(ref.urp_override_id.line_ids.exception_type, "non_returnable")
        self.assertEqual(n_line.urp_return_days, 0, "the snapshot never changes")

    # 13
    def test_q02_standalone_refund_needs_the_administrative_exception(self):
        """No receipt = no historical policy. The cashier is refused; a POS Administrator
        may approve an administrative exception with a reason, even for a product that is
        non-returnable today (see test_p17 for the security cases)."""
        with self.assertRaisesRegex(UserError, "currently non-returnable"):
            self._standalone(self.prods["N"])
        tok = self._token([{"product_id": self.prods["N"].id, "qty": 1}], "urp-nr-standalone", reason="No receipt")
        self.assertTrue(tok["ok"], tok)
        self.assertEqual(self._standalone(self.prods["N"], tok["token"], "urp-nr-standalone").state, "paid")
        tok2 = self._token([{"product_id": self.prods["C"].id, "qty": 1}], "urp-c-standalone", reason="No receipt")
        self.assertEqual(self._standalone(self.prods["C"], tok2["token"], "urp-c-standalone").state, "paid",
                         "returnable product: the existing administrator flow is unchanged")

    # 10 / 11 on POS
    def test_q03_non_retroactive_on_pos(self):
        with freeze_time(SALE):
            sold_n = self._order({"N": 1})
            sold_a = self._order({"A": 2})                                    # A = 7 days at the sale
        self.prods["N"].product_tmpl_id.custom_return_period_days = 14
        self.prods["A"].product_tmpl_id.custom_return_period_days = 0
        with freeze_time(OCT5):
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._pos_refund(sold_n, {"N": 1})
            self.assertEqual(self._pos_refund(sold_a, {"A": 1}).state, "paid", "sold with 7 days: still returnable")
        with freeze_time(OCT12):                                          # A's own 7 days are over
            with self.assertRaisesRegex(UserError, "return period ended"):
                self._pos_refund(sold_a, {"A": 1})
            self.assertEqual(self._pos_refund(sold_a, {"A": 1}, token=True).state, "paid",
                             "an expired period (not non-returnable): the override applies")

    # ship later: delivered = return refused; undelivered = cancellation still allowed
    def test_q04_ship_later(self):
        with freeze_time(SALE):
            delivered = self._sale(2, ship_later=True, product=self.prods["N"])
            self._deliver(delivered, 2)
            pending = self._sale(2, ship_later=True, product=self.prods["N"])
            dmove = delivered.picking_ids.move_ids.filtered(lambda m: m.state == "done")
            self.assertEqual((dmove.urp_deadline, dmove._urp_non_returnable()), (False, True))
        with freeze_time(OCT5):
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._pos_refund(delivered, {"N": 1})                          # cashier
            with self.assertRaisesRegex(UserError, "non-returnable"):
                dmove.picking_id.with_user(self.wh_user)._create_return()      # inventory, no approval
            ref_d = self._pos_refund(delivered, {"N": 1}, token=True)            # POS Administrator
            self.assertEqual(ref_d.urp_override_id.line_ids.exception_type, "non_returnable")
            ref = self._pos_refund(pending, {"N": 2})
            self.assertEqual((ref.state, ref.lines.urp_refund_kind), ("paid", "cancel"), "cancellation before delivery")
            self.assertEqual(pending.picking_ids.mapped("state"), ["cancel"])

    # 22: policy changes while the POS is offline -> historical policy, never the current one
    def _offline_snapshot(self, sess, product, device_at):
        order = self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": sess.id,
            "lines": [Command.create({"name": "O", "product_id": product.id, "price_unit": 10.0, "qty": 1,
                                     "price_subtotal": 10.0, "price_subtotal_incl": 10.0, "discount": 0})],
            "amount_tax": 0, "amount_total": 10.0, "amount_paid": 0, "amount_return": 0})
        meta = {"uuid": order.uuid, "orig_session_id": sess.id, "device_date": fields.Datetime.to_string(device_at)}
        order.with_context(urp_sync_meta=UrpPayload(meta))._urp_snapshot_lines()
        return order.lines

    def test_q05_offline_changes_to_and_from_zero(self):
        t0 = fields.Datetime.now() + timedelta(days=1)
        a, n = self.prods["A"], self.prods["N"]                               # A: 7 days, N: 0 days
        categ = self.env["product.category"].create({"name": "Offline cat"})
        d = self.env["product.product"].create({"name": "Offline D", "is_storable": True, "categ_id": categ.id})
        with freeze_time(t0):
            sess = self._session()
            if sess.start_at > t0:
                sess.start_at = t0
        with freeze_time(t0 + timedelta(minutes=10)):
            a.product_tmpl_id.custom_return_period_days = 0                    # 7 -> 0
            n.product_tmpl_id.custom_return_period_days = 5                    # 0 -> 5
            categ.write({"use_custom_return_period": True, "custom_return_period_days": 0})   # category -> 0
        with freeze_time(t0 + timedelta(minutes=20)):
            before = {p: self._offline_snapshot(sess, p, t0 + timedelta(minutes=5)) for p in (a, n, d)}
            after = {p: self._offline_snapshot(sess, p, t0 + timedelta(minutes=15)) for p in (a, n, d)}
        self.assertEqual([before[p].urp_return_days for p in (a, n, d)], [7, 0, 14], "policy at the sale time")
        self.assertEqual([after[p].urp_return_days for p in (a, n, d)], [0, 5, 0])
        self.assertTrue(all(before[p].urp_policy_ambiguous for p in (a, n, d)), "change inside the offline window flagged")
        self.assertEqual([bool(before[p].urp_deadline) for p in (a, n, d)], [True, False, True])
        self.assertEqual([bool(after[p].urp_deadline) for p in (a, n, d)], [False, True, False])
