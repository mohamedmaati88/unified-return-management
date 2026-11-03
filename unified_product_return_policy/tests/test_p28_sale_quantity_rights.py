"""Quantity of a confirmed sales order line changed by real Sales users.

Odoo 20 lets a confirmed line go down to the quantity it counts as delivered (``qty_delivered``)
and refuses below it. The module follows that rule and only books, in the return policy batch of
the line, the undelivered quantity it cancels (``qty_cancelled``). Every group reads the batches
only, so that bookkeeping is done by the module itself: every test below that changes a quantity or
cancels an order runs as a real user, who must not get an Access Error."""
from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import Form, tagged

from .common import UrpCommon


@tagged("post_install", "-at_install", "urp", "urp_p28")
class TestSaleQuantityRights(UrpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        g = lambda xid: cls.env.ref(xid).id                       # noqa: E731
        Users = cls.env["res.users"].with_context(no_reset_password=True)

        def user(login, groups, company=None):
            company = company or cls.company
            return Users.create({"name": login, "login": login, "group_ids": [(6, 0, [g(x) for x in groups])],
                                 "company_ids": [(6, 0, company.ids)], "company_id": company.id})
        cls.su = user("urp_q_su", ["sales_team.group_sale_salesman_all_leads", "stock.group_stock_user"])
        cls.sa = user("urp_q_sa", ["sales_team.group_sale_manager", "stock.group_stock_user"])
        cls.su_only = user("urp_q_su_only", ["sales_team.group_sale_salesman_all_leads"])
        cls.iu_only = user("urp_q_iu_only", ["stock.group_stock_user"])
        cls.internal = user("urp_q_internal", ["base.group_user"])
        cls.company_b = cls.env["res.company"].create({"name": "URP Q company B"})
        cls.su_b = user("urp_q_su_b", ["sales_team.group_sale_salesman_all_leads", "stock.group_stock_user"],
                        cls.company_b)
        cls.product.product_tmpl_id.write({"use_custom_return_period": True, "custom_return_period_days": 7})
        cls.product_2 = cls.env["product.product"].create({
            "name": "URP Widget 2", "type": "consu", "is_storable": True, "categ_id": cls.categ.id, "list_price": 5.0,
            "use_custom_return_period": True, "custom_return_period_days": 7})
        cls.env["stock.quant"]._update_available_quantity(cls.product_2, cls.warehouse.lot_stock_id, 1000)

    # ------------------------------------------------------------------ helpers
    def _so(self, lines, uom=None):
        """Confirmed order, ``lines`` = {product: quantity} in ``uom`` (the product unit by default)."""
        vals = []
        for product, qty in lines.items():
            line = {"product_id": product.id, "product_uom_qty": qty}
            if uom:
                line["product_uom_id"] = uom.id
            vals.append(Command.create(line))
        so = self.env["sale.order"].create({"partner_id": self.partner.id, "order_line": vals})
        so.action_confirm()
        return so

    def _line(self, so, product=None):
        return so.order_line.filtered(lambda l: l.product_id == (product or self.product))

    def _out(self, line):
        return line.move_ids.filtered(lambda m: m.location_dest_usage == "customer" and m.state != "cancel")

    def _open_qty(self, line):
        """Undelivered demand of the line, in the line's unit."""
        return sum(m.uom_id._compute_quantity(m.product_uom_qty, line.product_uom_id)
                   for m in self._out(line).filtered(lambda m: m.state != "done"))

    def _done_qty(self, line):
        return sum(m.uom_id._compute_quantity(m.quantity, line.product_uom_id)
                   for m in self._out(line).filtered(lambda m: m.state == "done"))

    def _deliver(self, so, qtys):
        """Validate the open delivery with ``qtys`` = {product: quantity in the line's unit}, backorder kept."""
        pick = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel") and p.picking_type_code == "outgoing")
        for move in pick.move_ids:
            line = move.sale_line_id
            move.quantity = line.product_uom_id._compute_quantity(qtys.get(move.product_id, 0), move.uom_id)
            move.picked = True
        res = pick.button_validate()
        if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
            Form.from_action(self.env, res).save().process()

    def _cancel(self, so, user):
        res = so.with_user(user).with_context(disable_cancel_warning=True).action_cancel()
        if isinstance(res, dict) and res.get("res_model"):
            Form.from_action(self.env, res).save().with_user(user).action_cancel()
        self.assertEqual(so.state, "cancel")

    def _invariant(self, line):
        """Approved = delivered + still open + cancelled, per batch (no quantity lost or counted twice)."""
        for b in line.urp_batch_ids:
            moves = b._urp_customer_moves()
            done = sum(m.uom_id._compute_quantity(m.quantity, line.product_uom_id)
                       for m in moves.filtered(lambda m: m.state == "done"))
            open_ = sum(m.uom_id._compute_quantity(m.product_uom_qty, line.product_uom_id)
                        for m in moves.filtered(lambda m: m.state not in ("done", "cancel")))
            self.assertAlmostEqual(b.qty, done + open_ + b.qty_cancelled,
                                   msg="batch %s approved=%s done=%s open=%s cancelled=%s" % (b.id, b.qty, done, open_, b.qty_cancelled))

    def _snapshot(self, line):
        """What a reduction must never change: the policy of each batch and the delivered moves."""
        batches = [(b.id, b.qty, b.return_days, b.policy_source, b.approved_at, b.tz, b.date_basis)
                   for b in line.urp_batch_ids.sorted("id")]
        delivered = [(m.id, m.quantity, m.urp_batch_id.id, m.urp_deadline)
                     for m in self._out(line).filtered(lambda m: m.state == "done").sorted("id")]
        return batches, delivered

    def _returnable(self, line):
        """Return right left on the delivered moves of the line (delivered - returns done)."""
        return sum(m.quantity - m._urp_returned_qty(m)
                   for m in self._out(line).filtered(lambda m: m.state == "done"))

    def _return(self, so, qty, to_refund=True):
        delivery = so.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing" and p.state == "done")[:1]
        ret = delivery._create_return()
        vals = {"product_uom_qty": qty, "quantity": qty, "picked": True}
        if "to_refund" in ret.move_ids._fields:
            vals["to_refund"] = to_refund
        ret.move_ids.write(vals)
        self.assertIs(ret.button_validate(), True)
        return ret

    def _ten_six(self):
        so = self._so({self.product: 10})
        self._deliver(so, {self.product: 6})
        line = self._line(so)
        self.assertEqual((line.qty_delivered, self._done_qty(line), self._open_qty(line)), (6, 6, 4))
        return so, line

    def _lower(self, line, qty, user, cancelled, open_qty):
        before = self._snapshot(line)
        line.with_user(user).product_uom_qty = qty
        self.assertEqual(line.product_uom_qty, qty)
        self.assertAlmostEqual(self._open_qty(line), open_qty)
        self.assertAlmostEqual(sum(line.urp_batch_ids.mapped("qty_cancelled")), cancelled)
        self.assertFalse(line.move_ids.filtered(lambda m: m.product_uom_qty < 0), "no negative procurement")
        self.assertEqual(self._snapshot(line), before, "batches' policy and delivered moves unchanged")
        self._invariant(line)

    # ------------------------------------------------------------------ 10 ordered, 6 delivered
    def test_q01_lower_to_9(self):
        so, line = self._ten_six()
        self._lower(line, 9, self.su, cancelled=1, open_qty=3)

    def test_q02_lower_to_8(self):
        so, line = self._ten_six()
        self._lower(line, 8, self.su, cancelled=2, open_qty=2)

    def test_q03_lower_to_delivered(self):
        so, line = self._ten_six()
        self._lower(line, 6, self.su, cancelled=4, open_qty=0)
        self.assertFalse(so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")),
                         "nothing left to deliver")

    def test_q04_below_delivered_refused_as_standard(self):
        so, line = self._ten_six()
        before = self._snapshot(line)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            line.with_user(self.su).product_uom_qty = 5
        line.invalidate_recordset()
        line.urp_batch_ids.invalidate_recordset()
        self.assertEqual((line.product_uom_qty, self._open_qty(line), sum(line.urp_batch_ids.mapped("qty_cancelled"))),
                         (10, 4, 0), "refused: nothing changed")
        self.assertEqual(self._snapshot(line), before)

    def test_q05_increase_after_partial_delivery(self):
        so, line = self._ten_six()
        first = line.urp_batch_ids
        before = self._snapshot(line)
        self.product.product_tmpl_id.custom_return_period_days = 30          # policy changed after the sale
        line.with_user(self.su).product_uom_qty = 12
        new = line.urp_batch_ids - first
        self.assertEqual((new.qty, new.return_days, first.return_days), (2, 30, 7))
        self.assertEqual(sorted((m.urp_batch_id.id, m.product_uom_qty)
                                for m in self._out(line).filtered(lambda m: m.state != "done")),
                         sorted([(first.id, 4), (new.id, 2)]), "own move per batch, never merged")
        self.assertEqual(self._snapshot(line)[1], before[1], "delivered moves unchanged")
        self._invariant(line)

    def test_q06_sales_administrator(self):
        so, line = self._ten_six()
        self._lower(line, 8, self.sa, cancelled=2, open_qty=2)

    def test_q07_sales_user_without_inventory_rights(self):
        """Standard Odoo 20 lets a Sales user without Inventory rights lower the line: so does the module."""
        so, line = self._ten_six()
        self._lower(line, 8, self.su_only, cancelled=2, open_qty=2)

    def test_q08_cancel_draft_reconfirm(self):
        so, line = self._ten_six()
        batch = line.urp_batch_ids
        delivered = self._snapshot(line)[1]
        self._cancel(so, self.su)
        self.assertEqual((batch.qty_cancelled, self._open_qty(line)), (4, 0))
        self.assertEqual(self._snapshot(line)[1], delivered, "delivered goods untouched by the cancellation")
        self._invariant(line)
        so.with_user(self.su).action_draft()
        self.product.product_tmpl_id.custom_return_period_days = 30
        so.with_user(self.su).action_confirm()
        self.assertEqual(line.urp_batch_ids, batch, "the open quantity comes back with its own batch")
        self.assertEqual((batch.qty_cancelled, batch.return_days, self._open_qty(line)), (0, 7, 4))
        self.assertEqual(self._out(line).filtered(lambda m: m.state != "done").urp_batch_id, batch)
        self.assertEqual(self._snapshot(line)[1], delivered)
        self._invariant(line)

    def test_q09_unit_of_measure(self):
        """Sold by the dozen, stocked by the unit: every quantity in the line's unit."""
        dozen = self.env.ref("uom.product_uom_dozen")
        so = self._so({self.product: 10}, uom=dozen)
        self._deliver(so, {self.product: 6})
        line = self._line(so)
        self.assertEqual((line.product_uom_id, line.qty_delivered, self._open_qty(line)), (dozen, 6, 4))
        self._lower(line, 8, self.su, cancelled=2, open_qty=2)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            line.with_user(self.su).product_uom_qty = 5

    def test_q09b_unit_of_measure_cancel_and_reconfirm(self):
        dozen = self.env.ref("uom.product_uom_dozen")
        so = self._so({self.product: 10}, uom=dozen)
        self._deliver(so, {self.product: 6})
        line = self._line(so)
        batch = line.urp_batch_ids
        delivered = self._snapshot(line)[1]
        self._cancel(so, self.su)
        self.assertEqual((batch.qty_cancelled, self._open_qty(line)), (4, 0), "4 dozen cancelled, in the line's unit")
        self._invariant(line)
        so.with_user(self.su).action_draft()
        so.with_user(self.su).action_confirm()
        self.assertEqual((line.urp_batch_ids, batch.qty_cancelled, self._open_qty(line)), (batch, 0, 4))
        self.assertEqual(self._snapshot(line)[1], delivered)
        self._invariant(line)

    def test_q09c_unit_of_measure_several_batches(self):
        """Two batches by the dozen: reduction, cancellation and the split at re-confirmation."""
        dozen = self.env.ref("uom.product_uom_dozen")
        so = self._so({self.product: 5}, uom=dozen)
        line = self._line(so)
        b1 = line.urp_batch_ids
        self.product.product_tmpl_id.custom_return_period_days = 14
        line.product_uom_qty = 8
        b2 = line.urp_batch_ids - b1
        pick = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        for move in pick.move_ids:
            move.quantity = dozen._compute_quantity(2 if move.urp_batch_id == b1 else 0, move.uom_id)
            move.picked = True
        res = pick.button_validate()
        if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
            Form.from_action(self.env, res).save().process()
        self.assertEqual((line.qty_delivered, self._open_qty(line)), (2, 6))
        self._lower(line, 7, self.su, cancelled=1, open_qty=5)
        self.assertEqual((b1.qty_cancelled, b2.qty_cancelled), (0, 1), "newest batch reduced first")
        self._cancel(so, self.su)
        self.assertEqual((b1.qty_cancelled, b2.qty_cancelled), (3, 3), "open dozens of each batch cancelled")
        self._invariant(line)
        so.with_user(self.su).action_draft()
        so.with_user(self.su).action_confirm()
        self.assertEqual((b1.qty_cancelled, b2.qty_cancelled), (0, 1),
                         "7 ordered, 2 delivered: 5 restored oldest batch first, the reduced dozen stays cancelled")
        self.assertEqual(line.urp_batch_ids, b1 | b2)
        by_batch = {b: sum(m.uom_id._compute_quantity(m.product_uom_qty, dozen)
                           for m in self._out(line).filtered(lambda m: m.state != "done" and m.urp_batch_id == b))
                    for b in (b1, b2)}
        self.assertEqual((by_batch[b1], by_batch[b2]), (3, 2), "open quantity split per batch, in dozens")
        self._invariant(line)

    def test_q10_after_a_refunded_return(self):
        """6 delivered, 2 returned (to refund): the floor is Odoo's own qty_delivered, never the module's."""
        so, line = self._ten_six()
        self._return(so, 2)
        floor = line.qty_delivered
        returnable = self._returnable(line)
        self.assertEqual(returnable, 4)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            line.with_user(self.su).product_uom_qty = floor - 1
        self._lower(line, floor, self.su, cancelled=4, open_qty=0)
        self.assertEqual(self._returnable(line), returnable, "return right unchanged by the reduction")

    def test_q11_batch_still_protected(self):
        so, line = self._ten_six()
        line.with_user(self.su).product_uom_qty = 8
        batch = line.urp_batch_ids
        for user in (self.su, self.sa, self.su_only):
            for vals in ({"qty_cancelled": 0}, {"return_days": 60}, {"qty": 100}):
                with self.assertRaises((AccessError, UserError)), self.env.cr.savepoint():
                    batch.with_user(user).write(vals)
                with self.assertRaises((AccessError, UserError)), self.env.cr.savepoint():
                    batch.with_user(user).with_context(urp_internal=True).write(vals)      # forged marker
        with self.assertRaises(UserError), self.env.cr.savepoint():
            batch.write({"qty_cancelled": 0})                                              # even as superuser
        self.assertEqual((batch.qty_cancelled, batch.return_days, batch.qty), (2, 7, 10))

    def test_q12_return_right_not_increased(self):
        so, line = self._ten_six()
        self.assertEqual(self._returnable(line), 6)
        line.with_user(self.su).product_uom_qty = 6
        self.assertEqual(self._returnable(line), 6, "only delivered goods give a return right")
        ret = so.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing" and p.state == "done")[:1]._create_return()
        self.assertEqual(ret.move_ids.urp_returnable_qty, 6, "Still Returnable: the delivered quantity only")
        with self.assertRaises(UserError), self.env.cr.savepoint():
            ret.move_ids.write({"product_uom_qty": 7})

    # ------------------------------------------------------------------ other shapes
    def test_q13_lower_before_any_delivery(self):
        so = self._so({self.product: 10})
        line = self._line(so)
        self._lower(line, 7, self.su, cancelled=3, open_qty=7)

    def test_q14_several_partial_deliveries(self):
        so = self._so({self.product: 10})
        self._deliver(so, {self.product: 3})
        self._deliver(so, {self.product: 3})
        line = self._line(so)
        self.assertEqual((line.qty_delivered, self._open_qty(line)), (6, 4))
        self._lower(line, 8, self.su, cancelled=2, open_qty=2)

    def test_q15_several_batches_newest_first(self):
        so = self._so({self.product: 5})
        line = self._line(so)
        b1 = line.urp_batch_ids
        self.product.product_tmpl_id.custom_return_period_days = 14
        line.product_uom_qty = 8
        b2 = line.urp_batch_ids - b1
        pick = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        for move in pick.move_ids:
            move.quantity = 5 if move.urp_batch_id == b1 else 1
            move.picked = True
        res = pick.button_validate()
        if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
            Form.from_action(self.env, res).save().process()
        self.assertEqual((line.qty_delivered, self._open_qty(line)), (6, 2))
        self._lower(line, 7, self.su, cancelled=1, open_qty=1)
        self.assertEqual((b1.qty_cancelled, b2.qty_cancelled), (0, 1), "newest batch reduced first")

    def test_q16_other_lines_untouched(self):
        so = self._so({self.product: 10, self.product_2: 5})
        self._deliver(so, {self.product: 6, self.product_2: 2})
        line, other = self._line(so), self._line(so, self.product_2)
        other_before = (self._snapshot(other), self._open_qty(other), other.urp_batch_ids.qty_cancelled)
        self._lower(line, 8, self.su, cancelled=2, open_qty=2)
        self.assertEqual((self._snapshot(other), self._open_qty(other), other.urp_batch_ids.qty_cancelled), other_before)

    def test_q17_repeated_reductions(self):
        so, line = self._ten_six()
        self._lower(line, 9, self.su, cancelled=1, open_qty=3)
        self._lower(line, 8, self.su, cancelled=2, open_qty=2)
        self._lower(line, 6, self.su, cancelled=4, open_qty=0)

    def test_q18_after_a_full_return(self):
        so, line = self._ten_six()
        self._return(so, 6)
        floor = line.qty_delivered
        self._lower(line, floor, self.su, cancelled=4, open_qty=0)
        self.assertEqual(self._returnable(line), 0)

    def test_q19_other_company_and_other_rights(self):
        so, line = self._ten_six()
        with self.assertRaises(AccessError, msg="user of another company"), self.env.cr.savepoint():
            line.with_user(self.su_b).product_uom_qty = 8
        with self.assertRaises(AccessError, msg="internal user without Sales or Inventory rights"), self.env.cr.savepoint():
            line.with_user(self.internal).product_uom_qty = 8
        line.invalidate_recordset()
        line.urp_batch_ids.invalidate_recordset()
        self.assertEqual((line.product_uom_qty, sum(line.urp_batch_ids.mapped("qty_cancelled"))), (10, 0),
                         "refused: nothing changed")
        # standard Odoo 20 (sale_stock) lets an Inventory User update sales order lines: same here
        self._lower(line, 8, self.iu_only, cancelled=2, open_qty=2)
