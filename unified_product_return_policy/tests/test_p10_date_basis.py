"""Return Period Calculation Based On: Actual Validation Date vs Effective Date."""
import uuid
from datetime import datetime, timedelta

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tests import Form, tagged

from .common import UrpCommon
from ..models.urp_guard import urp_internal
from .test_p4_portal import UrpPortalCommon
from .test_p5_pos import UrpPosCommon

VALIDATED = datetime(2026, 10, 25, 9, 0)          # 12:00 in Riyadh, 25 October 2026
EFFECTIVE_PAST = datetime(2026, 10, 5, 9, 0)      # Effective Date corrected 20 days back
EFFECTIVE_FUTURE = datetime(2026, 11, 10, 9, 0)
# day 0 = local delivery day, right ends at the end of local day 10 (Asia/Riyadh = UTC+3)
END_NOV_4 = datetime(2026, 11, 4, 21, 0)          # 2026-11-05 00:00 Riyadh
END_OCT_15 = datetime(2026, 10, 15, 21, 0)        # 2026-10-16 00:00 Riyadh
END_NOV_20 = datetime(2026, 11, 20, 21, 0)
FORGED = {"urp_internal": True, "urp_skip_guard": True, "urp_checked": True}


class UrpDateBasisCommon(UrpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product.product_tmpl_id.write({"use_custom_return_period": True, "custom_return_period_days": 10})

    def _basis(self, basis, company=None):
        (company or self.company).urp_date_basis = basis

    def _so(self, qty=2, company=None):
        company = company or self.company
        so = self.env["sale.order"].with_company(company).create({
            "partner_id": self.partner.id, "company_id": company.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": qty})]})
        so.action_confirm()
        return so

    def _deliver(self, so, qty=None):
        pick = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        for move in pick.move_ids:
            move.quantity = qty if qty is not None else move.product_uom_qty
            move.picked = True
        res = pick.button_validate()
        if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
            Form.from_action(self.env, res).save().process()
        return pick, pick.move_ids.filtered(lambda m: m.state == "done")

    def _return(self, origin, qty):
        ret = origin.picking_id._create_return()
        ret.move_ids.write({"product_uom_qty": qty, "quantity": qty, "picked": True})
        return ret, ret.button_validate()


@tagged("post_install", "-at_install", "urp", "urp_date_basis")
class TestDateBasisSales(UrpDateBasisCommon):

    # ---------------------------------------------------------------- the worked example
    def test_d01_validation_date_ignores_effective_date_change(self):
        self._basis("validation")
        with freeze_time(VALIDATED):
            pick, move = self._deliver(self._so())
        self.assertEqual((move.urp_date_basis, move.urp_validated_at, move.urp_deadline),
                         ("validation", VALIDATED, END_NOV_4))
        pick.date_done = EFFECTIVE_PAST
        self.assertEqual(move.date, EFFECTIVE_PAST, "standard: effective date propagated to the move")
        self.assertEqual((move.urp_validated_at, move.urp_deadline), (VALIDATED, END_NOV_4), "deadline unchanged")
        self.assertFalse(self.env["urp.deadline.log"].search([("move_id", "=", move.id)]))
        with freeze_time(datetime(2026, 11, 1, 9, 0)):
            self.assertIs(self._return(move, 1)[1], True, "still inside the period")

    def test_d02_effective_date_moved_to_the_past(self):
        self._basis("effective")
        with freeze_time(VALIDATED):
            pick, move = self._deliver(self._so())
        self.assertEqual((move.urp_date_basis, move.urp_validated_at, move.urp_deadline),
                         ("effective", VALIDATED, END_NOV_4))
        with freeze_time(datetime(2026, 10, 26, 9, 0)):
            pick.date_done = EFFECTIVE_PAST
            self.assertEqual(move.urp_deadline, END_OCT_15, "end of 15 October (Riyadh)")
            self.assertEqual(move.urp_validated_at, VALIDATED, "server validation time never changes")
            _ret, res = self._return(move, 1)
            self.assertEqual(res["res_model"], "urp.return.check.wizard", "period now ended")
        log = self.env["urp.deadline.log"].search([("move_id", "=", move.id)])
        self.assertEqual((log.old_date, log.new_date, log.old_deadline, log.new_deadline),
                         (VALIDATED, EFFECTIVE_PAST, END_NOV_4, END_OCT_15))
        self.assertTrue(pick.message_ids.filtered(lambda m: "recalculated from the corrected Effective Date" in (m.body or "")))

    def test_d03_effective_date_moved_to_the_future(self):
        self._basis("effective")
        with freeze_time(VALIDATED):
            pick, move = self._deliver(self._so())
        pick.date_done = EFFECTIVE_FUTURE
        self.assertEqual(move.urp_deadline, END_NOV_20)
        with freeze_time(datetime(2026, 11, 15, 9, 0)):
            self.assertIs(self._return(move, 1)[1], True)

    def test_d04_historical_days_used_not_current_settings(self):
        self._basis("effective")
        with freeze_time(VALIDATED):
            pick, move = self._deliver(self._so())
        self.product.product_tmpl_id.custom_return_period_days = 60
        self.company.urp_default_days = 90
        pick.date_done = EFFECTIVE_PAST
        self.assertEqual(move.urp_deadline, END_OCT_15, "10 days frozen at the sale")

    def test_d05_setting_change_does_not_touch_past_sales(self):
        self._basis("validation")
        so_v = self._so()                               # sold under "validation"
        self._basis("effective")
        so_e = self._so()                               # sold under "effective"
        self._basis("validation")                       # setting changed again before delivery
        with freeze_time(VALIDATED):
            pick_v, move_v = self._deliver(so_v)
            pick_e, move_e = self._deliver(so_e)
        self.assertEqual((move_v.urp_date_basis, move_e.urp_date_basis), ("validation", "effective"))
        pick_v.date_done = EFFECTIVE_PAST
        pick_e.date_done = EFFECTIVE_PAST
        self.assertEqual((move_v.urp_deadline, move_e.urp_deadline), (END_NOV_4, END_OCT_15))

    def test_d06_partial_deliveries_each_follow_their_own_date(self):
        self._basis("effective")
        so = self._so(5)
        with freeze_time(VALIDATED):
            pick1, move1 = self._deliver(so, 2)
        with freeze_time(VALIDATED + timedelta(days=3)):
            pick2, move2 = self._deliver(so)
        self.assertEqual(move2.urp_deadline, END_NOV_4 + timedelta(days=3))
        pick1.date_done = EFFECTIVE_PAST
        self.assertEqual((move1.urp_deadline, move2.urp_deadline), (END_OCT_15, END_NOV_4 + timedelta(days=3)))

    def test_d07_multi_company_each_its_own_basis(self):
        company_b = self.env["res.company"].create({"name": "URP DB B"})
        company_b.write({"urp_enabled": True, "urp_tz": "Asia/Riyadh", "urp_default_days": 10, "urp_date_basis": "effective"})
        wh_b = self.env["stock.warehouse"].search([("company_id", "=", company_b.id)], limit=1)
        self.env["stock.quant"].with_company(company_b)._update_available_quantity(self.product, wh_b.lot_stock_id, 10)
        self._basis("validation")
        with freeze_time(VALIDATED):
            pick_a, move_a = self._deliver(self._so())
            pick_b, move_b = self._deliver(self._so(company=company_b))
        self.assertEqual((move_a.urp_date_basis, move_b.urp_date_basis), ("validation", "effective"))
        pick_a.date_done = EFFECTIVE_PAST
        pick_b.date_done = EFFECTIVE_PAST
        self.assertEqual((move_a.urp_deadline, move_b.urp_deadline), (END_NOV_4, END_OCT_15))

    def test_d08_validation_date_and_basis_protected(self):
        self._basis("effective")
        with freeze_time(VALIDATED):
            pick, move = self._deliver(self._so())
        for vals in ({"urp_validated_at": EFFECTIVE_PAST}, {"urp_date_basis": "validation"}):
            with self.assertRaises(UserError):
                move.with_context(**FORGED).sudo().write(vals)
        self.assertEqual((move.urp_validated_at, move.urp_date_basis), (VALIDATED, "effective"))
        new = self.env["stock.move"].with_context(**FORGED).create({
            "product_id": self.product.id, "product_uom_qty": 1, "location_id": move.location_id.id,
            "location_dest_id": move.location_dest_id.id, "urp_validated_at": EFFECTIVE_PAST, "urp_date_basis": "effective"})
        self.assertEqual((new.urp_validated_at, new.urp_date_basis), (False, False), "stripped on create")
        pick.date_done = EFFECTIVE_PAST
        log = self.env["urp.deadline.log"].search([("move_id", "=", move.id)])
        with self.assertRaises(UserError):
            log.with_context(**FORGED).write({"new_deadline": END_NOV_20})
        with self.assertRaises(UserError):
            log.with_context(**FORGED).unlink()

    def test_d09_settings_screen_and_history(self):
        settings = self.env["res.config.settings"].create({"urp_date_basis": "effective"})
        settings.execute()
        self.assertEqual(self.company.urp_date_basis, "effective")
        row = self.env["urp.policy.history"].search([("scope", "=", "company"), ("res_id", "=", self.company.id)], limit=1)
        self.assertEqual(row.date_basis, "effective")
        self.assertEqual(self.product._urp_resolve(self.company)["date_basis"], "effective")
        self.assertEqual(self.product._urp_resolve(self.company, at=row.valid_from - timedelta(seconds=1))["date_basis"],
                         "validation", "historical basis at a past instant")

    def test_d10_default_is_validation(self):
        self.assertEqual(self.env["res.company"].create({"name": "URP DB new"}).urp_date_basis, "validation")
        self.assertEqual(self.company.urp_date_basis, "validation")

    def test_d11_effective_date_correction_does_not_reopen_returned_quantity(self):
        self._basis("effective")
        with freeze_time(VALIDATED):
            pick, move = self._deliver(self._so(2))
            ret, res = self._return(move, 2)
            self.assertIs(res, True)
        pick.date_done = EFFECTIVE_FUTURE
        with self.assertRaisesRegex(UserError, "nothing left to return"):            # refused at Return
            self._return(move, 1)
        # final check kept: a quantity forced past the entry check is still refused at validation
        forced = self.env["stock.picking"].create({
            "picking_type_id": ret.picking_type_id.id, "location_id": ret.location_id.id,
            "location_dest_id": ret.location_dest_id.id,
            "move_ids": [Command.create({"product_id": move.product_id.id, "product_uom_qty": 0,
                                         "origin_returned_move_id": move.id, "location_id": ret.location_id.id,
                                         "location_dest_id": ret.location_dest_id.id})]})
        forced.action_confirm()
        urp_internal(forced.move_ids).write({"product_uom_qty": 1, "quantity": 1, "picked": True})
        res2 = forced.button_validate()
        wiz = self.env["urp.return.check.wizard"].browse(res2["res_id"])
        self.assertEqual(wiz.line_ids.kind, "over_qty", "delivered quantity cap unchanged")


@tagged("post_install", "-at_install", "urp", "urp_date_basis")
class TestDateBasisPortal(UrpPortalCommon):

    def _request(self, so, details, key=None):
        return self.make_jsonrpc_request(f"/my/orders/{so.id}/urp_return_request", {
            "access_token": so.access_token, "idempotency_key": key or str(uuid.uuid4()),
            "return_details": {str(k.id): v for k, v in details.items()}})

    def test_p01_request_keeps_its_right_after_effective_date_moves_back(self):
        self.company.urp_date_basis = "effective"
        with freeze_time(VALIDATED):
            so, move = self._delivered(5)
        self.assertEqual(move.urp_portal_deadline, min(move.urp_deadline, move.urp_portal_deadline), "MIN kept")
        with freeze_time(VALIDATED + timedelta(days=1)):
            r = self._request(so, {move: 2})
            self.assertNotIn("error", r)
            req = self.env["urp.portal.return.request"].browse(r["request_id"])
            self.assertTrue(req.line_ids.in_time)
            move.picking_id.date_done = EFFECTIVE_PAST            # period now ended
            self.assertLess(move.urp_deadline, fields.Datetime.now())
            self.assertIn("error", self._request(so, {move: 1}), "no new portal request")
            # the 2 units requested in time can still be returned by the return that executes the request
            # (selected on its line; an unlinked return gets no right from it); more cannot
            ret = move.picking_id._create_return()
            ret.move_ids.write({"urp_portal_line_id": req.line_ids.id, "product_uom_qty": 3, "quantity": 3, "picked": True})
            wiz = self.env["urp.return.check.wizard"].browse(ret.button_validate()["res_id"])
            self.assertEqual((wiz.line_ids.kind, wiz.line_ids.eligible), ("expired", 2))
            wiz.action_return_eligible_only()
            self.assertEqual(ret.state, "done")
            self.assertEqual(req.state, "done")

    def test_p02_cancelled_request_not_revived(self):
        self.company.urp_date_basis = "effective"
        with freeze_time(VALIDATED):
            so, move = self._delivered(5)
        with freeze_time(VALIDATED + timedelta(days=1)):
            r = self._request(so, {move: 2})
            req = self.env["urp.portal.return.request"].browse(r["request_id"])
            req.action_cancel_remaining()
            move.picking_id.date_done = EFFECTIVE_PAST
            ret = move.picking_id._create_return()
            ret.move_ids.write({"product_uom_qty": 1, "quantity": 1, "picked": True})
            wiz = self.env["urp.return.check.wizard"].browse(ret.button_validate()["res_id"])
            self.assertEqual(wiz.line_ids.eligible, 0, "cancelled request gives no right")


@tagged("post_install", "-at_install", "urp", "urp_date_basis")
class TestDateBasisPos(UrpPosCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.p.product_tmpl_id.custom_return_period_days = 10

    def test_s01_ship_later_starts_at_delivery_and_follows_effective_date(self):
        self.company.urp_date_basis = "effective"
        order = self._sale(2, ship_later=True)
        self.assertEqual(order.lines.urp_date_basis, "effective")
        with freeze_time(VALIDATED):
            self._deliver(order, 2)
        move = order.picking_ids.move_ids.filtered(lambda m: m.state == "done")
        self.assertEqual((move.urp_date_basis, move.urp_validated_at, move.urp_deadline), ("effective", VALIDATED, END_NOV_4))
        move.picking_id.date_done = EFFECTIVE_PAST
        self.assertEqual(move.urp_deadline, END_OCT_15)
        with freeze_time(datetime(2026, 10, 26, 9, 0)):
            with self.assertRaises(UserError):
                self._refund(order, 1)                           # now needs an override
            self.assertEqual(self._refund(order, 1, authorize=True).state, "paid")

    def test_s02_ship_later_validation_basis_ignores_correction(self):
        self.company.urp_date_basis = "validation"
        order = self._sale(2, ship_later=True)
        with freeze_time(VALIDATED):
            self._deliver(order, 2)
        move = order.picking_ids.move_ids.filtered(lambda m: m.state == "done")
        move.picking_id.date_done = EFFECTIVE_PAST
        self.assertEqual(move.urp_deadline, END_NOV_4)
        with freeze_time(datetime(2026, 10, 26, 9, 0)):
            self.assertEqual(self._refund(order, 1).state, "paid")

    def test_s03_without_ship_later_deadline_stays_from_the_sale(self):
        self.company.urp_date_basis = "effective"
        with freeze_time(VALIDATED):
            order = self._sale(2)
        deadline = order.lines.urp_deadline
        self.assertEqual(deadline, END_NOV_4, "from the sale itself")
        pick = order.picking_ids.filtered(lambda p: p.state == "done")
        self.assertEqual(len(pick), 1, "per-order picking (stock updated in real time)")
        pick.date_done = EFFECTIVE_PAST
        self.assertFalse(pick.move_ids.urp_deadline, "no delivery deadline for a POS sale without ship later")
        self.assertEqual(order.lines.urp_deadline, deadline)
        with freeze_time(datetime(2026, 11, 1, 9, 0)):
            self.assertEqual(self._refund(order, 1).state, "paid")
