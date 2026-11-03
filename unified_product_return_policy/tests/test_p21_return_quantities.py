"""Return quantities checked when the return is created and when a quantity is entered,
not only at validation.

Odoo 20 standard: Delivery > Return (``stock.picking._create_return``) creates a draft return
with zero quantities whatever is left, and "Return All" fills the quantity of the original move
minus its own returns, ignoring the other open returns. For the customer returns under the
policy the module: leaves out (or refuses) a delivery line with nothing left, refuses a
quantity above what is still returnable when it is entered (delivered - returns done - other
open returns, original row locked), caps "Return All", shows "Still Returnable", and keeps
the final check at validation."""
from freezegun import freeze_time

from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tests import Form, tagged

from .test_p18_sales_exceptions import T1, UrpSalesExceptionCommon


@tagged("post_install", "-at_install", "urp", "urp_return_quantities")
class TestReturnQuantities(UrpSalesExceptionCommon):

    def _rp(self, so, user=None):
        return self._deliveries(so).with_user(user or self.iu)._create_return()

    def _qty(self, ret, qtys, user=None):
        for move in ret.move_ids:
            move.with_user(user or self.iu).product_uom_qty = qtys.get(self._code(move.product_id), 0)

    def test_q01_fully_returned_delivery_refused_at_return(self):
        so = self._so({"A": 2})
        with freeze_time(T1):
            ret = self._rp(so)
            self._qty(ret, {"A": 2})
            self.assertIs(self._receive(ret, self.iu), True)
            with self.assertRaisesRegex(UserError, "nothing left to return"):
                self._rp(so)                                                         # the reported case

    def test_q02_open_return_holds_the_quantity(self):
        so = self._so({"A": 5})
        with freeze_time(T1):
            r1 = self._rp(so)
            self._qty(r1, {"A": 2})
            r2 = self._rp(so)
            self.assertEqual(r2.move_ids.urp_returnable_qty, 3.0, "5 delivered - 2 in another open return")
            with self.assertRaisesRegex(UserError, "at most 3"):
                self._qty(r2, {"A": 4})
            for user in (self.sa_ia, self.ia):                                      # administrators included
                with self.assertRaisesRegex(UserError, "more than can still be returned"):
                    self._qty(r2, {"A": 4}, user)
            self._qty(r2, {"A": 3})
            with self.assertRaisesRegex(UserError, "nothing left to return"):
                self._rp(so)                                                         # 2 + 3 held by open returns
            r1.with_user(self.iu).action_cancel()                                    # cancelled: frees its 2
            r3 = self._rp(so)
            self.assertEqual(r3.move_ids.urp_returnable_qty, 2.0)
            self.assertIs(self._receive(r2, self.iu), True)
            self.assertEqual(r3.move_ids.urp_returnable_qty, 2.0, "3 done, 2 left")

    def test_q03_return_all_capped_and_partial_lines(self):
        so = self._so({"A": 4, "C": 2})                                              # C: period ended, kept out
        with freeze_time(T1):
            r1 = self._rp(so)
            self.assertEqual(r1.move_ids.product_id, self.prods["A"])
            self._qty(r1, {"A": 1})
            r2 = self._rp(so)
            r2.with_user(self.iu).action_return_all()
            self.assertEqual(r2.move_ids.product_uom_qty, 3.0, "standard would say 4; one unit is held by r1")
            self.assertIs(self._receive(r1, self.iu), True)
            self.assertIs(self._receive(r2, self.iu), True)
            with self.assertRaisesRegex(UserError, "nothing left to return"):
                self._rp(so)

    def test_q04_partial_receipt_and_backorder_keep_the_count(self):
        so = self._so({"A": 4})
        with freeze_time(T1):
            r1 = self._rp(so)
            self._qty(r1, {"A": 3})
            self.assertIs(self._receive(r1, self.iu, {"A": 1}), True)                 # 1 received, backorder of 2
            backorder = self._deliveries(so).move_ids.returned_move_ids.picking_id.filtered(
                lambda p: p.state not in ("done", "cancel"))
            self.assertEqual(backorder.move_ids.product_uom_qty, 2.0)
            r2 = self._rp(so)
            self.assertEqual(r2.move_ids.urp_returnable_qty, 1.0, "4 - 1 done - 2 in the backorder")
            with self.assertRaisesRegex(UserError, "at most 1"):
                self._qty(r2, {"A": 2})

    def test_q05_automatic_sales_return_is_checked_too(self):
        so, req = self._approved({"N": 2}, so=self._so({"N": 3}))
        with freeze_time(T1):
            auto = self._auto(req)
            with self.assertRaisesRegex(UserError, "at most 3"):
                auto.move_ids.with_user(self.iu).product_uom_qty = 4                 # above what was delivered
            manual = self._rp(so, self.sa_iu)                                        # the third unit (admin)
            with self.assertRaisesRegex(UserError, "at most 1"):
                self._qty(manual, {"N": 2}, self.sa_iu)

    def test_q06_other_transfers_untouched(self):
        """Receipts from vendors and their returns, internal transfers: no new check."""
        vendor = self.env.ref("stock.stock_location_suppliers")
        rec = self.env["stock.picking"].create({
            "picking_type_id": self.warehouse.in_type_id.id, "location_id": vendor.id,
            "location_dest_id": self.warehouse.lot_stock_id.id,
            "move_ids": [Command.create({"product_id": self.prods["A"].id, "product_uom_qty": 2, "location_id": vendor.id,
                                         "location_dest_id": self.warehouse.lot_stock_id.id})]})
        rec.action_confirm()
        rec.move_ids.write({"quantity": 2, "picked": True})
        rec.button_validate()
        for _ in range(2):                                                           # standard behaviour kept
            ret = rec._create_return()
            ret.move_ids.product_uom_qty = 2
        self.assertEqual(len(rec.move_ids.returned_move_ids), 2)
