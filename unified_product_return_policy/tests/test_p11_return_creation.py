"""Early return-period check at return creation + documented in-time return requests."""
import json
import uuid
from datetime import datetime, timedelta

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import Form, HttpCase, tagged

from .common import UrpCommon
from .test_p4_portal import UrpPortalCommon
from .test_p5_pos import UrpPosCommon

VALIDATED = datetime(2026, 10, 25, 9, 0)           # delivery validated 25 Oct (12:00 Riyadh)
NOV_2 = datetime(2026, 11, 2, 9, 0)                # inside the 10-day period
NOV_8 = datetime(2026, 11, 8, 9, 0)                # after the end of 4 November
END_NOV_4 = datetime(2026, 11, 4, 21, 0)           # exclusive boundary = 2026-11-05 00:00 Riyadh
FORGED = {"urp_internal": True, "urp_skip_guard": True, "urp_checked": True}


class UrpReturnCreationCommon(UrpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product.product_tmpl_id.write({"use_custom_return_period": True, "custom_return_period_days": 10})
        cls.product_long = cls.env["product.product"].create({
            "name": "URP Long", "is_storable": True, "use_custom_return_period": True, "custom_return_period_days": 30})
        cls.env["stock.quant"]._update_available_quantity(cls.product_long, cls.warehouse.lot_stock_id, 100)
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        stock_user = cls.env.ref("stock.group_stock_user").id
        cls.sales_user = Users.create({"name": "RC Sales", "login": "urp_rc_su", "password": "Rc-Sales-Pass-1!",
                                       "group_ids": [(6, 0, [cls.env.ref("sales_team.group_sale_salesman_all_leads").id, stock_user])]})
        cls.sales_admin = Users.create({"name": "RC Admin", "login": "urp_rc_sa",
                                        "group_ids": [(6, 0, [cls.env.ref("sales_team.group_sale_manager").id, stock_user])]})

    def _delivered(self, lines=None, at=VALIDATED):
        lines = lines or [(self.product, 5)]
        so = self.env["sale.order"].create({"partner_id": self.partner.id, "order_line": [
            (0, 0, {"product_id": p.id, "product_uom_qty": q}) for p, q in lines]})
        so.action_confirm()
        with freeze_time(at):
            pick = so.picking_ids
            for move in pick.move_ids:
                move.quantity = move.product_uom_qty
                move.picked = True
            pick.button_validate()
        return pick

    def _create(self, delivery, user):
        return delivery.with_user(user)._create_return()

    def _set_qty(self, ret, qty_by_product):
        for move in ret.move_ids:
            q = qty_by_product.get(move.product_id, 0)
            move.write({"product_uom_qty": q})

    def _validate(self, ret):
        for move in ret.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        return ret.button_validate()


@tagged("post_install", "-at_install", "urp", "urp_return_creation")
class TestReturnCreation(UrpReturnCreationCommon):

    def test_r01_created_and_validated_in_time(self):
        pick = self._delivered()
        with freeze_time(NOV_2):
            ret = self._create(pick, self.sales_user)
            self._set_qty(ret, {self.product: 2})
            self.assertEqual((ret.move_ids.urp_claim_qty, ret.move_ids.urp_claimed_at), (2, NOV_2))
            self.assertIs(self._validate(ret), True)

    def test_r02_requested_in_time_validated_after_deadline(self):
        pick = self._delivered()
        with freeze_time(NOV_2):
            ret = self._create(pick, self.sales_user)
            self._set_qty(ret, {self.product: 2})
        with freeze_time(NOV_8):
            self.assertIs(self._validate(ret), True, "documented request made within the period")
        self.assertEqual(ret.state, "done")

    def test_r03_creation_after_deadline_refused_with_date(self):
        pick = self._delivered()
        with freeze_time(NOV_8):
            with self.assertRaises(UserError) as cm:
                self._create(pick, self.sales_user)
            self.assertIn("2026-11-04 23:59", str(cm.exception), "deadline shown (end of 4 November)")
            self.assertFalse(pick.return_ids)

    def test_r04_admin_override_with_mandatory_reason(self):
        pick = self._delivered()
        with freeze_time(NOV_8):
            ret = self._create(pick, self.sales_admin)
            self.assertTrue(ret.message_ids.filtered(lambda m: "override with a reason required" in (m.body or "")))
            self._set_qty(ret, {self.product: 2})
            self.assertEqual(ret.move_ids.urp_claim_qty, 0, "no right recorded after the deadline")
            action = self._validate(ret.with_user(self.sales_admin))
            wiz = self.env["urp.return.check.wizard"].with_user(self.sales_admin).browse(action["res_id"])
            self.assertTrue(wiz.can_override)
            with self.assertRaises(UserError):
                wiz.action_override()                                  # reason missing
            wiz.reason = "Defective, confirmed by quality"
            wiz.action_override()
        self.assertEqual(ret.state, "done")
        self.assertEqual((ret.urp_override_id.user_id, ret.urp_override_id.reason), (self.sales_admin, "Defective, confirmed by quality"))

    def test_r05_partial_return_then_another(self):
        pick = self._delivered()
        with freeze_time(NOV_2):
            r1 = self._create(pick, self.sales_user)
            self._set_qty(r1, {self.product: 2})
            self.assertIs(self._validate(r1), True)
            r2 = self._create(pick, self.sales_user)
            self._set_qty(r2, {self.product: 1})
        with freeze_time(NOV_8):
            self.assertIs(self._validate(r2), True, "second request also made in time")
            with self.assertRaises(UserError):
                self._create(pick, self.sales_user)                    # a third one after the deadline

    def test_r06_quantity_increased_after_deadline(self):
        pick = self._delivered()
        with freeze_time(NOV_2):
            ret = self._create(pick, self.sales_user)
            self._set_qty(ret, {self.product: 1})
        with freeze_time(NOV_8):
            self._set_qty(ret, {self.product: 3})
            self.assertEqual(ret.move_ids.urp_claim_qty, 1, "increase after the deadline not documented")
            wiz = self.env["urp.return.check.wizard"].with_user(self.sales_user).browse(
                self._validate(ret.with_user(self.sales_user))["res_id"])
            self.assertEqual((wiz.line_ids.kind, wiz.line_ids.eligible), ("expired", 1))
            self.assertFalse(wiz.can_override)
            with self.assertRaises(UserError):
                ret.move_ids._action_done()                            # final guard
            wiz.action_return_eligible_only()
        self.assertEqual((ret.state, ret.move_ids.filtered(lambda m: m.state == "done").quantity), ("done", 1))

    def test_r07_cancelled_request_not_reusable(self):
        pick = self._delivered()
        with freeze_time(NOV_2):
            ret = self._create(pick, self.sales_user)
            self._set_qty(ret, {self.product: 2})
            ret.action_cancel()
        self.assertEqual(ret.move_ids.urp_claim_qty, 0)
        with freeze_time(NOV_8):
            with self.assertRaises(UserError):
                self._create(pick, self.sales_user)
            dup = ret.copy()                                           # duplicate of the cancelled return
            self.assertFalse(any(dup.move_ids.mapped("urp_claim_qty")), "the old claim is not copied")
            # re-linked to the delivery by hand, after the deadline: no right
            dup.move_ids.write({"origin_returned_move_id": pick.move_ids.id, "product_uom_qty": 2})
            self.assertEqual(dup.move_ids.urp_claim_qty, 0)
            dup.action_confirm()
            action = self._validate(dup)
            self.assertEqual(action["res_model"], "urp.return.check.wizard")

    def test_r08_effective_date_before_and_after_the_request(self):
        self.company.urp_date_basis = "effective"
        pick_a = self._delivered()
        pick_a.date_done = datetime(2026, 10, 5, 9, 0)                 # corrected BEFORE any request
        with freeze_time(NOV_2):
            with self.assertRaises(UserError):
                self._create(pick_a, self.sales_user)                  # period already over (15 Oct)
        pick_b = self._delivered()
        with freeze_time(NOV_2):
            ret = self._create(pick_b, self.sales_user)
            self._set_qty(ret, {self.product: 2})                      # documented in time
            pick_b.date_done = datetime(2026, 10, 5, 9, 0)             # corrected AFTER the request
            self.assertLess(pick_b.move_ids.urp_deadline, fields.Datetime.now())
            self.assertIs(self._validate(ret), True, "request keeps its right")

    def test_r09_manual_return_and_forged_claims(self):
        pick = self._delivered()
        origin = pick.move_ids
        with freeze_time(NOV_2):
            # draft created in time but quantity entered only after the deadline = undocumented
            undocumented = self._create(pick, self.sales_user)
        with freeze_time(NOV_8):
            self._set_qty(undocumented, {self.product: 1})
            self.assertEqual(undocumented.move_ids.urp_claim_qty, 0)
            self.assertEqual(self._validate(undocumented)["res_model"], "urp.return.check.wizard")
            # stock return built by hand (RPC-like), with forged claim values
            manual = self.env["stock.picking"].with_context(**FORGED).create({
                "picking_type_id": pick.picking_type_id.return_picking_type_id.id or pick.picking_type_id.id,
                "location_id": origin.location_dest_id.id, "location_dest_id": origin.location_id.id,
                "move_ids": [Command.create({"product_id": self.product.id, "product_uom_qty": 1,
                                             "location_id": origin.location_dest_id.id, "location_dest_id": origin.location_id.id,
                                             "origin_returned_move_id": origin.id, "urp_claim_qty": 5,
                                             "urp_claimed_at": "2026-10-26 09:00:00"})]})
            self.assertEqual((manual.move_ids.urp_claim_qty, manual.move_ids.urp_claimed_at), (0, False), "stripped")
            with self.assertRaises(UserError):
                manual.move_ids.with_context(**FORGED).write({"urp_claim_qty": 5})
            manual.action_confirm()
            manual.move_ids.quantity = 1
            manual.move_ids.picked = True
            with self.assertRaises(UserError):
                manual.move_ids._action_done()                         # final guard unchanged

    def test_r09b_claim_not_transferable_to_another_delivery(self):
        pick_old = self._delivered(at=VALIDATED - timedelta(days=30))   # long expired delivery
        pick = self._delivered()
        with freeze_time(NOV_2):
            ret = self._create(pick, self.sales_user)
            self._set_qty(ret, {self.product: 2})
        with freeze_time(NOV_8):
            ret.move_ids.write({"origin_returned_move_id": pick_old.move_ids.id})
            self.assertEqual(ret.move_ids.urp_claim_qty, 0)
            self.assertEqual(self._validate(ret)["res_model"], "urp.return.check.wizard")

    def test_r10_mixed_products_eligible_only(self):
        pick = self._delivered([(self.product, 2), (self.product_long, 2)])
        with freeze_time(NOV_8):                                       # 10-day product over, 30-day still open
            ret = self._create(pick, self.sales_user)
            self.assertEqual(ret.move_ids.product_id, self.product_long)
            self.assertTrue(ret.message_ids.filtered(lambda m: "Not added to this return" in (m.body or "")
                                                     and self.product.display_name in (m.body or "")))
            self._set_qty(ret, {self.product_long: 2})
            self.assertIs(self._validate(ret), True)

    def test_r11_legacy_delivery_unaffected(self):
        self.company.urp_enabled = False
        pick = self._delivered()
        self.company.urp_enabled = True
        with freeze_time(VALIDATED + timedelta(days=400)):
            ret = self._create(pick, self.sales_user)
            self._set_qty(ret, {self.product: 5})
            self.assertIs(self._validate(ret), True)


@tagged("post_install", "-at_install", "urp", "urp_return_creation")
class TestReturnCreationRpc(UrpReturnCreationCommon, HttpCase):

    def _call(self, model, method, args, context=None):
        return self.url_open("/web/dataset/call_kw", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"model": model, "method": method, "args": args, "kwargs": {"context": context or {}}}}),
            headers={"Content-Type": "application/json"}).json()

    def test_r12_return_button_through_rpc(self):
        pick = self._delivered()
        self.authenticate("urp_rc_su", "Rc-Sales-Pass-1!")
        with freeze_time(NOV_8):
            res = self._call("stock.picking", "action_return", [pick.ids], FORGED)
            self.assertIn("error", res)
            self.assertIn("return period has ended", json.dumps(res["error"]))
        pick.invalidate_recordset()
        self.assertFalse(pick.return_ids)


@tagged("post_install", "-at_install", "urp", "urp_return_creation")
class TestReturnCreationPortal(UrpPortalCommon):

    def test_r13_portal_request_still_allows_staff_return_after_deadline(self):
        so, move = self._delivered(5)
        r = self.make_jsonrpc_request(f"/my/orders/{so.id}/urp_return_request", {
            "access_token": so.access_token, "idempotency_key": str(uuid.uuid4()), "return_details": {str(move.id): 2}})
        self.assertNotIn("error", r)
        req = self.env["urp.portal.return.request"].browse(r["request_id"])
        with freeze_time(move.urp_deadline + timedelta(days=2)):
            ret = move.picking_id._create_return()                      # portal request = documented right
            # the right goes to the return line that executes the request, selected by the
            # Inventory user (an unlinked return gets none: test_p25_portal_link)
            ret.move_ids.write({"urp_portal_line_id": req.line_ids.id, "product_uom_qty": 3, "quantity": 3, "picked": True})
            wiz = self.env["urp.return.check.wizard"].browse(ret.button_validate()["res_id"])
            self.assertEqual(wiz.line_ids.eligible, 2, "portal request only, not portal + staff claim")


@tagged("post_install", "-at_install", "urp", "urp_return_creation")
class TestReturnCreationPos(UrpPosCommon):

    def test_r14_pos_refund_path_unchanged(self):
        self.p.product_tmpl_id.custom_return_period_days = 10
        order = self._sale(2, ship_later=True)
        with freeze_time(VALIDATED):
            self._deliver(order, 2)
        move = order.picking_ids.move_ids.filtered(lambda m: m.state == "done")
        with freeze_time(NOV_8):
            with self.assertRaises(UserError):
                self._refund(order, 1)                                  # POS refund: own check, unchanged
            self.assertEqual(self._refund(order, 1, authorize=True).state, "paid")
            ret = move.picking_id._create_return()                      # admin env: allowed, override later
            self.assertTrue(ret.move_ids)
