"""Operational limits: the behaviour of the current version in borderline cases."""
from datetime import timedelta

from freezegun import freeze_time

from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tests import tagged

from .test_p11_return_creation import NOV_2, NOV_8, UrpReturnCreationCommon
from ..models.urp_guard import urp_internal


@tagged("post_install", "-at_install", "urp", "urp_operational")
class TestOperationalRisks(UrpReturnCreationCommon):

    def test_o01_quantity_from_detailed_operations_is_not_documented(self):
        """A quantity entered only on the move lines (Detailed Operations) is not recorded as an
        in-period request: validated after the deadline, it needs an approval (strict side)."""
        pick = self._delivered()
        with freeze_time(NOV_2):
            ret = self._create(pick, self.sales_user)
            move = ret.move_ids
            self.env["stock.move.line"].create({
                "move_id": move.id, "picking_id": ret.id, "product_id": self.product.id, "quantity": 2,
                "location_id": move.location_id.id, "location_dest_id": move.location_dest_id.id})
            self.assertEqual(move.quantity, 2)
            self.assertEqual((move.urp_claim_qty, move.urp_claimed_at), (0, False))
        with freeze_time(NOV_8):
            move.picked = True
            action = ret.button_validate()
            self.assertEqual(action["res_model"], "urp.return.check.wizard", "strict: needs override or new period")

    def test_o02_plain_receipt_from_customer_is_not_checked(self):
        """A receipt from the customer location NOT linked to the delivery (no
        origin_returned_move_id) is a standard receipt: the return policy does not apply to it."""
        pick = self._delivered()
        origin = pick.move_ids
        with freeze_time(NOV_8):
            receipt = self.env["stock.picking"].create({
                "picking_type_id": self.warehouse.in_type_id.id,
                "location_id": origin.location_dest_id.id, "location_dest_id": origin.location_id.id,
                "move_ids": [Command.create({"product_id": self.product.id, "product_uom_qty": 1,
                                             "location_id": origin.location_dest_id.id,
                                             "location_dest_id": origin.location_id.id})]})
            receipt.action_confirm()
            receipt.move_ids.quantity = 1
            receipt.move_ids.picked = True
            self.assertIs(receipt.button_validate(), True)

    def test_o03_two_returns_for_the_same_quantity(self):
        """Two drafts each documented for the full delivered quantity: the second one is
        refused at validation (quantity cap re-checked with the origin row locked)."""
        pick = self._delivered()                                        # 5 delivered
        with freeze_time(NOV_2):
            r1 = self._create(pick, self.sales_user)
            r2 = self._create(pick, self.sales_user)
            self._set_qty(r1, {self.product: 5})
            with self.assertRaisesRegex(UserError, "more than can still be returned"), self.env.cr.savepoint():
                self._set_qty(r2, {self.product: 5})                    # refused when entered
            self.assertEqual(r2.move_ids.urp_returnable_qty, 0.0, "the other open return holds everything")
            # final check kept: forced past the entry check (as a concurrent write could), refused at validation
            urp_internal(r2.move_ids).write({"product_uom_qty": 5, "quantity": 5, "picked": True})
        with freeze_time(NOV_8):
            self.assertIs(self._validate(r1), True)
            action = self._validate(r2)
            wiz = self.env["urp.return.check.wizard"].browse(action["res_id"])
            self.assertEqual((wiz.line_ids.kind, wiz.line_ids.eligible), ("over_qty", 0))
