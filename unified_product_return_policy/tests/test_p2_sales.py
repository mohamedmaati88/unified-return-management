from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from .common import UrpCommon


@tagged("post_install", "-at_install", "urp", "urp_p2")
class TestP2Sales(UrpCommon):

    def _so(self, qty, days=7):
        return self._make_so(qty, days)

    def _make_so(self, qty, days=7):
        self._set_days(days)
        so = self.env["sale.order"].create({"partner_id": self.partner.id, "order_line": [
            (0, 0, {"product_id": self.product.id, "product_uom_qty": qty})]})
        so.action_confirm()
        return so, so.order_line

    def _set_days(self, days):
        self.product.product_tmpl_id.write({"use_custom_return_period": True, "custom_return_period_days": days})

    def _out(self, line):
        return line.move_ids.filtered(lambda m: m.location_dest_usage == "customer" and m.state != "cancel")

    def _open(self, line):
        return sorted(self._out(line).filtered(lambda m: m.state != "done").mapped(
            lambda m: (m.urp_batch_id.return_days, m.product_uom_qty)))

    def _deliver(self, picking, qty_by_move, backorder=True):
        for move, q in qty_by_move.items():
            move.quantity = q
            move.picked = True
        res = picking.button_validate()
        if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
            wiz = Form.from_action(self.env, res).save()
            wiz.process() if backorder else wiz.process_cancel_backorder()

    def _invariant(self, line):
        for b in line.urp_batch_ids:
            moves = b._urp_customer_moves()
            done = sum(moves.filtered(lambda m: m.state == "done").mapped("quantity"))
            open_ = sum(moves.filtered(lambda m: m.state not in ("done", "cancel")).mapped("product_uom_qty"))
            self.assertAlmostEqual(b.qty, done + open_ + b.qty_cancelled,
                                   msg="batch %s approved=%s done=%s open=%s cancelled=%s" % (b.id, b.qty, done, open_, b.qty_cancelled))

    # ---------------------------------------------------------------- C/D
    def test_c01_confirm_creates_linked_batch(self):
        so, line = self._so(5)
        b = line.urp_batch_ids
        self.assertEqual((b.qty, b.return_days, b.policy_source, b.tz), (5, 7, "product", "Asia/Riyadh"))
        self.assertEqual(self._out(line).urp_batch_id, b)
        self._invariant(line)

    def test_c02_policy_change_after_sale_does_not_touch_snapshot(self):
        so, line = self._so(5)
        self._set_days(30)
        self.categ.write({"use_custom_return_period": True, "custom_return_period_days": 2})
        self.company.urp_default_days = 99
        self.company.urp_tz = "Europe/Berlin"
        self.assertEqual((line.urp_batch_ids.return_days, line.urp_batch_ids.tz), (7, "Asia/Riyadh"))
        with self.assertRaises(UserError):
            line.urp_batch_ids.write({"return_days": 60})
        with self.assertRaises(UserError):
            self._out(line).write({"urp_deadline": fields.Datetime.now()})

    def test_c03_policy_disabled_at_sale_means_legacy(self):
        self.company.urp_enabled = False
        so, line = self._so(5)
        self.assertFalse(line.urp_batch_ids)
        self.company.urp_enabled = True
        self.assertFalse(line.urp_batch_ids, "re-enabling does not create retroactive snapshots")

    def test_d01_increase_creates_separate_move(self):
        so, line = self._so(5)
        self._set_days(14)
        line.product_uom_qty = 8
        self.assertEqual(self._open(line), [(7, 5), (14, 3)])
        self.assertEqual(len(self._out(line).picking_id), 1)
        self._invariant(line)

    def test_d02_decrease_lifo_and_spanning(self):
        so, line = self._so(5)
        self._set_days(14)
        line.product_uom_qty = 8
        line.product_uom_qty = 6
        self.assertEqual(self._open(line), [(7, 5), (14, 1)])
        line.product_uom_qty = 3
        self.assertEqual(self._open(line), [(7, 3)])
        self.assertFalse(line.move_ids.filtered(lambda m: m.product_uom_qty < 0))
        self._invariant(line)

    def test_d03_decrease_below_delivered_still_blocked_by_standard(self):
        so, line = self._so(5)
        self._deliver(so.picking_ids, {self._out(line): 4})
        with self.assertRaises(UserError):
            line.product_uom_qty = 3

    def test_d04_new_line_on_confirmed_order(self):
        so, line = self._so(5)
        self._set_days(21)
        so.write({"order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 2})]})
        new = so.order_line - line
        self.assertEqual((new.urp_batch_ids.qty, new.urp_batch_ids.return_days), (2, 21))
        self.assertEqual(self._out(new).urp_batch_id, new.urp_batch_ids)

    def test_d05_reconfirm_keeps_each_batch_policy(self):
        so, line = self._so(5)
        b1 = line.urp_batch_ids
        self._set_days(30)
        line.product_uom_qty = 8
        b2 = line.urp_batch_ids - b1
        mv1 = self._out(line).filtered(lambda m: m.urp_batch_id == b1)
        mv2 = self._out(line).filtered(lambda m: m.urp_batch_id == b2)
        self._deliver(so.picking_ids, {mv1: 2, mv2: 0})
        so._action_cancel()
        so.action_draft()
        self._set_days(14)
        so.action_confirm()
        self.assertEqual(sorted(self._out(line).filtered(lambda m: m.state != "done").mapped(
            lambda m: (m.urp_batch_id.id, m.product_uom_qty))), sorted([(b1.id, 3), (b2.id, 3)]))
        self.assertEqual(len(line.urp_batch_ids), 2)
        self._invariant(line)

    def test_d06_reconfirm_more_and_less(self):
        so, line = self._so(5)
        b1 = line.urp_batch_ids
        self._set_days(30)
        line.product_uom_qty = 8
        b2 = line.urp_batch_ids - b1
        so._action_cancel()
        so.action_draft()
        line.product_uom_qty = 10
        self._set_days(14)
        so.action_confirm()
        b3 = line.urp_batch_ids - b1 - b2
        self.assertEqual(self._open(line), [(7, 5), (14, 2), (30, 3)])
        self.assertEqual(b3.return_days, 14)
        self._invariant(line)
        so2, line2 = self._so(5)
        c1 = line2.urp_batch_ids
        self._set_days(30)
        line2.product_uom_qty = 8
        so2._action_cancel()
        so2.action_draft()
        line2.product_uom_qty = 6
        so2.action_confirm()
        self.assertEqual(self._open(line2), [(7, 5), (30, 1)])
        self.assertEqual(len(line2.urp_batch_ids), 2)
        self._invariant(line2)

    # ---------------------------------------------------------------- I. deliveries & deadlines
    def test_i01_partial_deliveries_stamp_own_deadlines(self):
        so, line = self._so(10)
        b1 = line.urp_batch_ids
        self._deliver(so.picking_ids, {self._out(line): 4})
        first = line.move_ids.filtered(lambda m: m.state == "done")
        # second delivery three days later
        bo = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        self._deliver(bo, {bo.move_ids: 6})
        done = line.move_ids.filtered(lambda m: m.state == "done").sorted("id")
        self.assertEqual(done.mapped("quantity"), [4, 6])
        self.assertEqual(done.urp_batch_id, b1)
        for m in done:
            self.assertTrue(m.urp_deadline > m.date + timedelta(days=7))
            self.assertTrue(m.urp_deadline <= m.date + timedelta(days=8, hours=24))

    def test_i02_return_and_duplicate_do_not_copy_snapshot(self):
        so, line = self._so(5)
        pick = so.picking_ids
        self._deliver(pick, {self._out(line): 5})
        ret = pick._create_return()
        self.assertFalse(ret.move_ids.urp_batch_id)
        self.assertFalse(ret.move_ids.urp_deadline)
        self.assertEqual(ret.move_ids.origin_returned_move_id.urp_batch_id, line.urp_batch_ids)
        self.assertFalse(pick.copy().move_ids.urp_batch_id)
        self.assertFalse(so.copy().order_line.urp_batch_ids)

    def test_i03_ambiguous_batches_raise(self):
        so, line = self._so(5)
        extra = line._urp_create_batch(1)
        extra2 = line._urp_create_batch(1)
        with self.assertRaises(UserError):
            line.with_context(urp_batch_map={})._prepare_procurement_values()
