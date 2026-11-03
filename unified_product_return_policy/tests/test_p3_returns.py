from datetime import timedelta

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged

from .common import UrpCommon
from ..models.urp_guard import urp_internal


@tagged("post_install", "-at_install", "urp", "urp_p3")
class TestP3Returns(UrpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        stock_user = cls.env.ref("stock.group_stock_user")
        cls.sales_user = Users.create({"name": "SU", "login": "urp_su", "group_ids": [(6, 0, [cls.env.ref("sales_team.group_sale_salesman_all_leads").id, stock_user.id])]})
        cls.sales_admin = Users.create({"name": "SA", "login": "urp_sa", "group_ids": [(6, 0, [cls.env.ref("sales_team.group_sale_manager").id, stock_user.id])]})
        cls.stock_admin = Users.create({"name": "IA", "login": "urp_ia", "group_ids": [(6, 0, [cls.env.ref("stock.group_stock_manager").id, cls.env.ref("sales_team.group_sale_salesman_all_leads").id])]})
        cls.pos_admin = Users.create({"name": "PA", "login": "urp_pa", "group_ids": [(6, 0, [cls.env.ref("point_of_sale.group_pos_manager").id, stock_user.id, cls.env.ref("sales_team.group_sale_salesman_all_leads").id])]})
        cls.product.product_tmpl_id.write({"use_custom_return_period": True, "custom_return_period_days": 7})

    def _delivered_so(self, qty, deliver=None):
        so = self.env["sale.order"].create({"partner_id": self.partner.id, "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": qty})]})
        so.action_confirm()
        for q in (deliver or [qty]):
            pick = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
            pick.move_ids.quantity = q
            pick.move_ids.picked = True
            res = pick.button_validate()
            if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
                Form.from_action(self.env, res).save().process()
        return so

    def _return(self, so, qty_by_origin, user=None):
        delivery = so.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing" and p.state == "done")[:1]
        ret = delivery._create_return() if len(qty_by_origin) == 1 and next(iter(qty_by_origin)).picking_id == delivery else None
        if ret is None:
            ret = self.env["stock.picking"].browse()
            for origin in qty_by_origin:
                r = origin.picking_id._create_return()
                ret |= r
        for move in ret.move_ids:
            q = qty_by_origin.get(move.origin_returned_move_id, 0)
            move.product_uom_qty = q
            move.quantity = q
            move.picked = bool(q)
        ret = ret.with_user(user) if user else ret
        return ret

    def _validate(self, picking):
        return picking.button_validate()

    def _origins(self, so):
        return so.order_line.move_ids.filtered(lambda m: m.state == "done" and m.location_dest_usage == "customer").sorted("id")

    def test_e01_return_within_period(self):
        so = self._delivered_so(5)
        ret = self._return(so, {self._origins(so): 2}, self.sales_user)
        self.assertTrue(self._validate(ret))
        self.assertEqual(ret.state, "done")

    def test_e02_expired_blocked_for_user_and_final_guard(self):
        so = self._delivered_so(5)
        origin = self._origins(so)
        with freeze_time(origin.urp_deadline + timedelta(seconds=1)):
            ret = self._return(so, {origin: 2}, self.sales_user)
            action = self._validate(ret)
            self.assertEqual(action["res_model"], "urp.return.check.wizard")
            wiz = self.env["urp.return.check.wizard"].with_user(self.sales_user).browse(action["res_id"])
            self.assertFalse(wiz.can_override)
            with self.assertRaises(AccessError):
                wiz.action_override()
            with self.assertRaises(UserError):
                ret._action_done()                      # direct call (RPC-like path)
        with freeze_time(origin.urp_deadline - timedelta(seconds=1)):
            self.assertTrue(ret.with_context(urp_checked=False).button_validate())   # last second is still OK

    def test_e03_admin_override_requires_reason_and_is_stored(self):
        so = self._delivered_so(5)
        origin = self._origins(so)
        with freeze_time(origin.urp_deadline + timedelta(days=3)):
            ret = self._return(so, {origin: 2}, self.sales_admin)
            action = self._validate(ret)
            wiz = self.env["urp.return.check.wizard"].with_user(self.sales_admin).browse(action["res_id"])
            self.assertTrue(wiz.can_override)
            with self.assertRaises(UserError):
                wiz.action_override()
            wiz.reason = "Manufacturing defect confirmed"
            wiz.action_override()
            self.assertEqual(ret.state, "done")
            ov = ret.urp_override_id
            self.assertEqual((ov.user_id, ov.channel, ov.reason), (self.sales_admin, "sale", "Manufacturing defect confirmed"))
            self.assertTrue(ov.consumed_at)
            with self.assertRaises(UserError):
                ov.with_user(self.sales_admin).write({"reason": "x"})

    def test_e04_mixed_eligible_and_expired_partial_deliveries(self):
        so = self._delivered_so(10, deliver=[4])
        first = self._origins(so)
        with freeze_time(first.urp_deadline + timedelta(days=1)):
            so2_pick = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
            so2_pick.move_ids.quantity = 6
            so2_pick.move_ids.picked = True
            so2_pick.button_validate()
            second = self._origins(so) - first
            self.assertTrue(second.urp_deadline > first.urp_deadline)
            ret = self._return(so, {first: 2, second: 3}, self.sales_user)
            action = self._validate(ret)
            wiz = self.env["urp.return.check.wizard"].with_user(self.sales_user).browse(action["res_id"])
            self.assertEqual(wiz.line_ids.mapped("kind"), ["expired"])
            self.assertTrue(wiz.can_return_eligible)
            wiz.action_return_eligible_only()
            returned = ret.move_ids.filtered(lambda m: m.state == "done")
            self.assertEqual(sum(returned.mapped("quantity")), 3)
            self.assertEqual(returned.origin_returned_move_id, second)

    def test_e05_over_quantity_never_overridable(self):
        so = self._delivered_so(5)
        origin = self._origins(so)
        with self.assertRaisesRegex(UserError, "at most 5"):                          # refused when entered
            self._return(so, {origin: 6}, self.sales_admin)
        ret = self._return(so, {origin: 5}, self.sales_admin)
        urp_internal(ret.move_ids).write({"product_uom_qty": 6, "quantity": 6})      # forced past the entry check
        action = self._validate(ret)
        wiz = self.env["urp.return.check.wizard"].with_user(self.sales_admin).browse(action["res_id"])
        self.assertEqual(wiz.line_ids.kind, "over_qty")
        self.assertFalse(wiz.can_override, "no administrator override above the delivered quantity")

    def test_e06_repeated_returns_cap(self):
        so = self._delivered_so(5)
        origin = self._origins(so)
        for _ in range(2):
            r = self._return(so, {origin: 2}, self.sales_user)
            self.assertTrue(self._validate(r))
        with self.assertRaisesRegex(UserError, "at most 1"):
            self._return(so, {origin: 2}, self.sales_user)
        r3 = self._return(so, {origin: 1}, self.sales_user)
        urp_internal(r3.move_ids).write({"product_uom_qty": 2, "quantity": 2})       # forced past the entry check
        action = self._validate(r3)
        wiz = self.env["urp.return.check.wizard"].browse(action["res_id"])
        self.assertEqual((wiz.line_ids.kind, wiz.line_ids.requested), ("over_qty", 2))

    def test_e07_inventory_admin_and_pos_admin_cannot_override_sales(self):
        so = self._delivered_so(5)
        origin = self._origins(so)
        with freeze_time(origin.urp_deadline + timedelta(days=1)):
            for user in (self.stock_admin, self.pos_admin):
                ret = self._return(so, {origin: 1}, user)
                action = self._validate(ret)
                wiz = self.env["urp.return.check.wizard"].with_user(user).browse(action["res_id"])
                self.assertFalse(wiz.can_override, user.name)

    def test_e08_purchase_and_internal_unaffected(self):
        receipt_type = self.warehouse.in_type_id
        supplier = self.env["res.partner"].create({"name": "Vendor"})
        receipt = self.env["stock.picking"].create({
            "picking_type_id": receipt_type.id, "partner_id": supplier.id,
            "location_id": self.env.ref("stock.stock_location_suppliers").id, "location_dest_id": self.warehouse.lot_stock_id.id,
            "move_ids": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 3, "location_id": self.env.ref("stock.stock_location_suppliers").id, "location_dest_id": self.warehouse.lot_stock_id.id})]})
        receipt.action_confirm()
        receipt.move_ids.quantity = 3
        receipt.move_ids.picked = True
        receipt.button_validate()
        with freeze_time(fields.Datetime.now() + timedelta(days=400)):
            ret = receipt._create_return()
            ret.move_ids.product_uom_qty = 3
            ret.move_ids.quantity = 3
            ret.move_ids.picked = True
            self.assertTrue(ret.button_validate() is True)

    def test_e09_legacy_delivery_unaffected(self):
        self.company.urp_enabled = False
        so = self._delivered_so(5)
        self.company.urp_enabled = True
        origin = self._origins(so)
        self.assertFalse(origin.urp_batch_id)
        with freeze_time(fields.Datetime.now() + timedelta(days=400)):
            ret = self._return(so, {origin: 5}, self.sales_user)
            self.assertTrue(self._validate(ret) is True)

    def test_k01_override_record_and_snapshot_are_read_only_for_users(self):
        so = self._delivered_so(1)
        with self.assertRaises((AccessError, UserError)):
            so.order_line.urp_batch_ids.with_user(self.sales_admin).write({"qty": 9})
        with self.assertRaises(AccessError):
            self.env["urp.return.override"].with_user(self.sales_admin).create({
                "channel": "sale", "user_id": self.sales_admin.id, "reason": "x", "company_id": self.company.id})
