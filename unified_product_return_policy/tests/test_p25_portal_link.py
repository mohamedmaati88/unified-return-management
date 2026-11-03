"""Portal requests and Inventory returns of the same delivery line count against each other.

The portal takes open returns into account, a return takes open requests into account, and only
the request a return line executes is consumed and gives its in-time right: the Inventory user
selects the request a return line executes (optional "Portal Request" column of the standard return lines, nothing selected for
him), and one rule holds on every delivery line:
    returns done + open independent returns + sum(max(open request, open returns linked to it)) <= delivered.
"""
import json
from datetime import datetime

from freezegun import freeze_time

from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import Form, HttpCase, tagged

from ..models.urp_guard import urp_internal
from .test_p18_sales_exceptions import T1, VAL, UrpSalesExceptionCommon

AFTER = datetime(2026, 11, 20, 9, 0)          # every return period of these sales has ended


class UrpPortalLinkCommon(UrpSalesExceptionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.write({"allow_spontaneous_returns": True, "return_validity_days": 10})
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.own = Users.create({"name": "pl_own", "login": "pl_own", "password": "pl_own-Pass-1!",
                                "group_ids": [(6, 0, [cls.env.ref("sales_team.group_sale_salesman").id])],
                                "company_ids": [(6, 0, cls.company.ids)], "company_id": cls.company.id})

    # ------------------------------------------------------------------ helpers
    def _move(self, so, code="A", delivery=None):
        delivery = delivery or self._deliveries(so)[:1]
        return delivery.move_ids.filtered(lambda m: m.product_id == self.prods[code])

    def _preq(self, so, qty, code="A", at=T1, delivery=None):
        """A portal request as the portal controller records it (submission time = ``at``)."""
        with freeze_time(at):
            req = self.env["urp.portal.return.request"].sudo().create({
                "order_id": so.id, "submitted_at": at, "idempotency_key": "%s-%s-%s" % (so.id, code, at.isoformat()),
                "line_ids": [Command.create({"move_id": self._move(so, code, delivery).id, "requested_qty": qty})]})
        return req

    def _ret(self, so, qty, line=None, user=None, code="A", delivery=None):
        """Delivery > Return (standard button), then the return line edited as in the form: the
        Portal Request and the quantity are saved together."""
        user = user or self.iu
        ret = (delivery or self._deliveries(so)[:1]).with_user(user)._create_return()
        move = ret.move_ids.filtered(lambda m: m.product_id == self.prods[code])
        vals = {"product_uom_qty": qty}
        if line:
            vals["urp_portal_line_id"] = line.id
        move.with_user(user).write(vals)
        return ret

    def _done(self, ret, qty=None, backorder=True, user=None):
        user = user or self.iu
        for move in ret.move_ids:
            q = move.product_uom_qty if qty is None else qty
            move.with_user(user).write({"quantity": q, "picked": True})
        res = ret.with_user(user).button_validate()
        if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
            wiz = Form.from_action(self.env, res).save()
            res = (wiz.process() if backorder else wiz.process_cancel_backorder()) or True
        return res

    def _state(self, req):
        req.invalidate_recordset()
        req.line_ids.invalidate_recordset()
        return req.state, req.line_ids.open_qty, req.line_ids.processed_qty


@tagged("post_install", "-at_install", "urp", "urp_portal_link")
class TestPortalLink(UrpPortalLinkCommon):

    # ------------------------------------------------------------------ mandatory scenarios
    def test_l01_return_done_before_the_portal_request(self):
        so = self._so({"A": 10})
        with freeze_time(T1):
            self._done(self._ret(so, 3))
            self.assertEqual(self._move(so)._urp_portal_available(), 7.0, "10 delivered - 3 returned")

    def test_l02_open_return_before_the_portal_request(self):
        so = self._so({"A": 10})
        with freeze_time(T1):
            self._ret(so, 6)
            self.assertEqual(self._move(so)._urp_portal_available(), 4.0, "the open return of 6 is deducted (was 10)")

    def test_l03_portal_request_before_an_independent_return(self):
        so = self._so({"A": 10})
        req = self._preq(so, 6)
        with freeze_time(T1):
            with self.assertRaisesRegex(UserError, "at most 4.0 can still be returned without a portal request"):
                self._ret(so, 6)
            self.assertEqual(self._state(req), ("active", 6.0, 0.0), "the request is never closed by it")
            ret = self._ret(so, 4)                                           # independent, within what is free
            self.assertIs(self._done(ret), True)
        self.assertEqual(self._state(req), ("active", 6.0, 0.0), "an independent return consumes nothing")

    def test_l04_executing_the_right_request(self):
        so = self._so({"A": 10})
        req = self._preq(so, 6)
        with freeze_time(T1):
            ret = self._ret(so, 0)
            move = ret.move_ids
            self.assertEqual(move.urp_returnable_qty, 4.0, "unlinked: 6 reserved for the request")
            move.with_user(self.iu).urp_portal_line_id = req.line_ids
            self.assertEqual(move.urp_returnable_qty, 10.0, "linked: the request's 6 are its own")
            move.with_user(self.iu).product_uom_qty = 6
            self.assertIs(self._done(ret), True)
        self.assertEqual(self._state(req), ("done", 0.0, 6.0), "consumed once")
        with freeze_time(T1), self.assertRaisesRegex(UserError, "no open quantity left"):
            self._ret(so, 1, req.line_ids)                                    # never twice
        self.assertEqual(self._state(req), ("done", 0.0, 6.0))

    def test_l05_independent_return_with_an_open_request(self):
        so = self._so({"A": 10})
        req = self._preq(so, 4)
        with freeze_time(T1):
            self.assertIs(self._done(self._ret(so, 3)), True)
            self.assertEqual(self._state(req), ("active", 4.0, 0.0), "the request stays open for 4")
            self.assertEqual(self._move(so)._urp_portal_available(), 3.0, "10 - 3 returned - 4 requested")

    def test_l06_two_requests_only_the_linked_one_is_consumed(self):
        so = self._so({"A": 10})
        first = self._preq(so, 3, at=datetime(2026, 10, 29, 9, 0))
        second = self._preq(so, 4)
        with freeze_time(T1):
            ret = self._ret(so, 4, second.line_ids)
            self.assertIs(self._done(ret), True)
        self.assertEqual(self._state(second), ("done", 0.0, 4.0))
        self.assertEqual(self._state(first), ("active", 3.0, 0.0), "the older request is not consumed")

    def test_l07_partial_receipt(self):
        with freeze_time(T1):
            # with a backorder: it keeps the link, the rest is received later
            so = self._so({"A": 10})
            req = self._preq(so, 5)
            ret = self._ret(so, 5, req.line_ids)
            self.assertIs(self._done(ret, 3), True)
            self.assertEqual(self._state(req), ("active", 2.0, 3.0))
            backorder = ret.backorder_ids
            self.assertEqual((backorder.move_ids.product_uom_qty, backorder.move_ids.urp_portal_line_id),
                             (2.0, req.line_ids), "the backorder still executes the request")
            self.assertIs(self._done(backorder), True)
            self.assertEqual(self._state(req), ("done", 0.0, 5.0))
            with self.assertRaisesRegex(UserError, "no open quantity left"):
                self._ret(so, 1, req.line_ids)                                # the same quantity twice
            # without a backorder: the rest stays open (reserved) until received or cancelled
            so2 = self._so({"A": 10})
            req2 = self._preq(so2, 5)
            ret2 = self._ret(so2, 5, req2.line_ids)
            self.assertIs(self._done(ret2, 3, backorder=False), True)
            self.assertEqual(self._state(req2), ("active", 2.0, 3.0))
            self.assertEqual(self._move(so2)._urp_portal_available(), 5.0, "10 - 3 returned - 2 still requested")
            later = self._ret(so2, 2, req2.line_ids)                          # received later
            self.assertIs(self._done(later), True)
            self.assertEqual(self._state(req2), ("done", 0.0, 5.0))
            # cancelling the rest
            so3 = self._so({"A": 10})
            req3 = self._preq(so3, 5)
            self.assertIs(self._done(self._ret(so3, 5, req3.line_ids), 3, backorder=False), True)
            req3.with_user(self.su).action_cancel_remaining()
            self.assertEqual(self._state(req3), ("done", 0.0, 3.0))
            self.assertEqual(self._move(so3)._urp_portal_available(), 7.0)

    def test_l08_in_time_request_executed_after_the_deadline(self):
        so = self._so({"A": 10})
        req = self._preq(so, 6)
        self.assertTrue(req.line_ids.in_time)
        with freeze_time(AFTER):
            ret = self._ret(so, 3)                                            # independent: no right from it
            self.assertEqual(len(ret.move_ids), 1, "the line stays on the Return: the request can be selected")
            ret.move_ids.write({"quantity": 3, "picked": True})
            wiz = self._wizard(self._validate(ret, self.iu), self.iu)
            self.assertEqual((wiz.line_ids.kind, wiz.line_ids.eligible), ("expired", 0.0))
            self.assertIn("Portal Request", wiz.message, "the message points to the open request")
            ret.move_ids.with_user(self.iu).write({"urp_portal_line_id": req.line_ids.id, "product_uom_qty": 6})
            self.assertIs(self._done(ret, 6), True, "the request submitted in time keeps its right")
        self.assertEqual(self._state(req), ("done", 0.0, 6.0))
        # more than the request after the deadline: the usual period check for the extra units
        so2 = self._so({"A": 10})
        req2 = self._preq(so2, 6)
        with freeze_time(AFTER):
            ret2 = self._ret(so2, 8, req2.line_ids)
            for m in ret2.move_ids:
                m.write({"quantity": 8, "picked": True})
            wiz = self._wizard(self._validate(ret2, self.iu), self.iu)
            self.assertEqual((wiz.line_ids.kind, wiz.line_ids.eligible), ("expired", 6.0))
            wiz.action_return_eligible_only()
        self.assertEqual((ret2.state, self._state(req2)), ("done", ("done", 0.0, 6.0)))

    def test_l09_forgotten_selection(self):
        so = self._so({"A": 10})
        req = self._preq(so, 6)
        with freeze_time(T1):
            # it would exceed the delivery: refused, with the request named, nothing guessed
            with self.assertRaisesRegex(UserError, r"(?s)reserved by open portal requests 6.*%s.*Portal Request column"
                                        % req.name.replace("/", r"\/")):
                self._ret(so, 6)
            # it would not: an independent return, the request stays open (not consumed)
            self.assertIs(self._done(self._ret(so, 2)), True)
        self.assertEqual(self._state(req), ("active", 6.0, 0.0))

    def test_l10_two_users_cannot_execute_the_same_request(self):
        so = self._so({"A": 10})
        req = self._preq(so, 6)
        with freeze_time(T1):
            first = self._ret(so, 6, req.line_ids)
            with self.assertRaisesRegex(UserError, "already being executed by %s" % first.name):
                self._ret(so, 6, req.line_ids, user=self.ia)
            # cancelling the first return releases it
            first.with_user(self.iu).action_cancel()
            second = self._ret(so, 6, req.line_ids, user=self.ia)
            self.assertIs(self._done(second, user=self.ia), True)
        self.assertEqual(self._state(req), ("done", 0.0, 6.0))

    # ------------------------------------------------------------------ link checks
    def test_l11_link_checked_on_the_server(self):
        so = self._so({"A": 10, "C": 4})
        other = self._so({"A": 10})
        req = self._preq(so, 2)
        req_c = self._preq(so, 2, code="C")
        req_other = self._preq(other, 2)
        with freeze_time(T1):
            ret = self._ret(so, 0)
            move_a = ret.move_ids.filtered(lambda m: m.product_id == self.prods["A"])
            for wrong, label in ((req_c.line_ids, "other product"), (req_other.line_ids, "other delivery")):
                with self.assertRaisesRegex(UserError, "very delivery line", msg=label), self.env.cr.savepoint():
                    move_a.with_user(self.iu).urp_portal_line_id = wrong
                with self.assertRaisesRegex(UserError, "very delivery line", msg=label), self.env.cr.savepoint():
                    urp_internal(move_a).write({"urp_portal_line_id": wrong.id})        # module code included
            # a request cancelled, an Own Documents salesman, a forged context: refused
            req2 = self._preq(so, 1, at=datetime(2026, 10, 29, 9, 0))
            req2.with_user(self.su).action_cancel_remaining()
            with self.assertRaisesRegex(UserError, "no open quantity left"), self.env.cr.savepoint():
                move_a.with_user(self.iu).urp_portal_line_id = req2.line_ids
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                move_a.with_user(self.own).urp_portal_line_id = req.line_ids
            with self.assertRaisesRegex(UserError, "very delivery line"), self.env.cr.savepoint():
                move_a.with_user(self.iu).with_context(urp_split="split", urp_internal="internal").write(
                    {"urp_portal_line_id": req_other.line_ids.id})
            # change of the request before validation; frozen once done
            move_a.with_user(self.iu).write({"urp_portal_line_id": req.line_ids.id, "product_uom_qty": 2})
            move_a.with_user(self.iu).urp_portal_line_id = False
            move_a.with_user(self.iu).urp_portal_line_id = req.line_ids
            ret.move_ids.filtered(lambda m: m.product_id == self.prods["C"]).with_user(self.iu).product_uom_qty = 0
            self.assertIs(self._done(ret), True)
            for value in (False, req2.line_ids.id):
                with self.assertRaisesRegex(UserError, "cannot be changed"), self.env.cr.savepoint():
                    move_a.with_user(self.iu).write({"urp_portal_line_id": value})
                with self.assertRaisesRegex(UserError, "cannot be changed"), self.env.cr.savepoint():
                    move_a.with_user(self.ia).sudo().write({"urp_portal_line_id": value})
        self.assertEqual(self._state(req), ("done", 0.0, 2.0))

    def test_l12_origin_change_copy_return_of_return_exchange(self):
        so = self._so({"A": 10})
        other = self._so({"A": 10})
        req = self._preq(so, 4)
        with freeze_time(T1):
            ret = self._ret(so, 4, req.line_ids)
            move = ret.move_ids
            self.assertFalse(move.copy().urp_portal_line_id, "never copied")
            move.with_user(self.iu).origin_returned_move_id = self._move(other)
            self.assertFalse(move.urp_portal_line_id, "moved to another delivery line: link dropped")
            move.with_user(self.iu).origin_returned_move_id = self._move(so)
            move.with_user(self.iu).urp_portal_line_id = req.line_ids
            self.assertIs(self._done(ret), True)
            back = ret.with_user(self.iu)._create_return()
            self.assertFalse(back.move_ids.urp_portal_line_id, "a return of the return is not the request")
            # an exchange (moves not linked to the delivery) cannot execute a request
            exch = self._deliveries(other).with_user(self.iu)._create_return()
            exch.move_ids.write({"origin_returned_move_id": False})
            req_o = self._preq(other, 2)
            with self.assertRaisesRegex(UserError, "very delivery line"):
                exch.move_ids.with_user(self.iu).urp_portal_line_id = req_o.line_ids

    def test_l13_return_all_capped_by_the_request(self):
        so = self._so({"A": 10})
        req = self._preq(so, 6)
        with freeze_time(T1):
            ret = self._ret(so, 0, req.line_ids)
            ret.with_user(self.iu).action_return_all()
            self.assertEqual(ret.move_ids.product_uom_qty, 6.0, "linked: the request's open quantity, not 10")
            ret.move_ids.with_user(self.iu).write({"product_uom_qty": 0, "urp_portal_line_id": False})
            ret.with_user(self.iu).action_return_all()
            self.assertEqual(ret.move_ids.product_uom_qty, 4.0, "unlinked: what the open request leaves")
            # 2 of the 6 executed by this line: a second line linked to the request gets the other 4
            ret.move_ids.with_user(self.iu).write({"product_uom_qty": 2, "urp_portal_line_id": req.line_ids.id})
            other = self._ret(so, 0, req.line_ids)
            other.with_user(self.iu).action_return_all()
            self.assertEqual(other.move_ids.product_uom_qty, 4.0, "the open quantity not held by the first line")
            self.assertEqual(self._move(so)._urp_portal_available(), 4.0, "10 - max(6, 2 + 4)")
            self.assertIs(self._done(ret), True)
            self.assertIs(self._done(other), True)
        self.assertEqual(self._state(req), ("done", 0.0, 6.0), "6 consumed, never more")

    def test_l14_units_of_measure(self):
        """Odoo 20 delivers in the product unit (24 Units for 2 Dozens sold); the return line is
        entered in Dozens. Every quantity is compared in the unit of the delivery line."""
        dozen = self.env.ref("uom.product_uom_dozen")
        so = self._so({"A": 24})
        origin = self._move(so)
        req = self._preq(so, 12)                                              # 12 units requested
        with freeze_time(T1):
            ret = self._deliveries(so).with_user(self.iu)._create_return()
            move = ret.move_ids
            move.with_user(self.iu).uom_id = dozen
            with self.assertRaisesRegex(UserError, "at most 12.0 can still be returned without"), self.env.cr.savepoint():
                move.with_user(self.iu).product_uom_qty = 2                     # 24 units > the 12 not requested
            move.with_user(self.iu).urp_portal_line_id = req.line_ids
            ret.with_user(self.iu).action_return_all()
            self.assertEqual(move.product_uom_qty, 1.0, "Return All: the 12 requested units = 1 Dozen")
            self.assertEqual(origin._urp_portal_available(), 12.0, "24 - 12 being executed")
            self.assertIs(self._done(ret), True)
        self.assertEqual(self._state(req), ("done", 0.0, 12.0))

    def test_l15_several_deliveries_and_purchase_returns(self):
        so = self._so({"A": 10}, deliver=[(VAL, {"A": 6}), (VAL, {"A": 4})])
        first, second = self._deliveries(so)
        req = self._preq(so, 3, delivery=first)
        with freeze_time(T1):
            ret = second.with_user(self.iu)._create_return()
            with self.assertRaisesRegex(UserError, "very delivery line"):
                ret.move_ids.with_user(self.iu).urp_portal_line_id = req.line_ids
            self.assertEqual(self._move(so, delivery=second)._urp_portal_available(), 4.0, "the other delivery is free")
            self.assertEqual(self._move(so, delivery=first)._urp_portal_available(), 3.0)
            # a vendor receipt and its return: not a customer return, no request can be linked
            receipt = self.env["stock.picking"].create({
                "picking_type_id": self.warehouse.in_type_id.id, "partner_id": self.partner.id,
                "move_ids": [Command.create({"product_id": self.prods["A"].id, "product_uom_qty": 2})]})
            receipt.action_confirm()
            receipt.move_ids.write({"quantity": 2, "picked": True})
            receipt.button_validate()
            vret = receipt.with_user(self.iu)._create_return()
            with self.assertRaisesRegex(UserError, "very delivery line"):
                vret.move_ids.with_user(self.iu).urp_portal_line_id = req.line_ids

    def test_l16_sales_return_requests_see_the_portal_requests(self):
        so = self._so({"A": 10})
        self._preq(so, 6)
        with freeze_time(T1):
            req = self._request(so, self.su, {"A": 5})
            line = req.line_ids
            self.assertEqual(line.available_qty, 4.0, "10 delivered - 6 requested on the portal")
            with self.assertRaisesRegex(UserError, "only 4.0 delivered unit"):
                req.action_submit()
            line.requested_qty = 4
            req.action_submit()
            self.assertEqual(self._auto(req).move_ids.product_uom_qty, 4.0)
            self.assertEqual(self._move(so)._urp_portal_available(), 0.0, "10 = 4 open return + 6 requested")

    def test_l17_no_portal_request_means_no_change(self):
        so = self._so({"A": 10})
        with freeze_time(T1):
            ret = self._ret(so, 0)
            self.assertEqual(ret.move_ids.urp_returnable_qty, 10.0)
            with self.assertRaisesRegex(UserError, "at most 10.0 can still be returned\\."):
                ret.move_ids.with_user(self.iu).product_uom_qty = 11
            ret.move_ids.with_user(self.iu).product_uom_qty = 10
            self.assertIs(self._done(ret), True)
        with freeze_time(AFTER):
            so2 = self._so({"A": 5})
            with self.assertRaisesRegex(UserError, "approved return exception is required"):
                self._deliveries(so2).with_user(self.iu)._create_return()     # unchanged: no in-time request


@tagged("post_install", "-at_install", "urp", "urp_portal_link")
class TestPortalLinkHttp(UrpPortalLinkCommon, HttpCase):
    """The customer's portal (the same pages and calls as before) and direct RPC access."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.iu.password = "pl_iu-Pass-1!"
        cls.company_b = cls.env["res.company"].create({"name": "Portal Link B"})
        cls.b_iu = Users.create({"name": "pl_b_iu", "login": "pl_b_iu", "password": "pl_b_iu-Pass-1!",
                                 "group_ids": [(6, 0, [cls.env.ref("stock.group_stock_manager").id])],
                                 "company_ids": [(6, 0, cls.company_b.ids)], "company_id": cls.company_b.id})
        cls.portal_user = Users.create({"name": "pl portal", "login": "pl_portal", "password": "pl_portal-Pass-1!",
                                        "group_ids": [(6, 0, [cls.env.ref("base.group_portal").id])]})

    def _portal(self, so, qty, code="A"):
        so._portal_ensure_token()
        return self.make_jsonrpc_request(f"/my/orders/{so.id}/urp_return_request", {
            "access_token": so.access_token, "idempotency_key": "k-%s-%s" % (so.id, qty),
            "return_details": {str(self._move(so, code).id): qty}})

    def _call(self, model, method, args, kwargs=None):
        return self.url_open("/web/dataset/call_kw", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"model": model, "method": method, "args": args, "kwargs": kwargs or {}}}),
            headers={"Content-Type": "application/json"}, timeout=120).json()

    def test_h01_portal_sees_done_and_open_returns(self):
        so = self._so({"A": 10})
        so2 = self._so({"A": 10})
        with freeze_time(T1):
            self._done(self._ret(so, 3))
            self._ret(so2, 6)
            data = self.make_jsonrpc_request("/my/order/return_data", {"order_id": so2.id, "access_token": so2._portal_ensure_token()})
            self.assertEqual([l["remaining_delivered_qty"] for l in data["returnable_lines"]], [4.0])
            self.assertIn("Only 7.0 unit(s)", self._portal(so, 8)["error"], "scenario 1: 8 refused")
            self.assertNotIn("error", self._portal(so, 7), "at most 7")
            self.assertIn("Only 4.0 unit(s)", self._portal(so2, 7)["error"], "scenario 2: 7 refused")
            self.assertNotIn("error", self._portal(so2, 4), "at most 4")

    def test_h02_rpc(self):
        so = self._so({"A": 10})
        other = self._so({"A": 10})
        with freeze_time(T1):
            req = self.env["urp.portal.return.request"].browse(self._portal(so, 4)["request_id"])
            req_o = self.env["urp.portal.return.request"].browse(self._portal(other, 2)["request_id"])
            ret = self._ret(so, 0)
            move = ret.move_ids
        self.authenticate("urp_x_iu", "pl_iu-Pass-1!")
        res = self._call("stock.move", "write", [move.ids, {"urp_portal_line_id": req_o.line_ids.id}])
        self.assertIn("very delivery line", json.dumps(res.get("error")))
        res = self._call("stock.move", "write", [move.ids, {"urp_portal_line_id": req.line_ids.id, "product_uom_qty": 4}],
                         {"context": {"urp_internal": True, "urp_split": True}})
        self.assertNotIn("error", res)
        move.invalidate_recordset()
        self.assertEqual((move.urp_portal_line_id, move.product_uom_qty), (req.line_ids, 4.0))
        # another company, the customer himself: no access
        self.authenticate("pl_b_iu", "pl_b_iu-Pass-1!")
        res = self._call("stock.move", "write", [move.ids, {"urp_portal_line_id": False}])
        self.assertIn("AccessError", json.dumps(res.get("error")))
        self.authenticate("pl_portal", "pl_portal-Pass-1!")
        res = self._call("stock.move", "write", [move.ids, {"urp_portal_line_id": False}])
        self.assertIn("AccessError", json.dumps(res.get("error")))
        move.invalidate_recordset()
        self.assertEqual(move.urp_portal_line_id, req.line_ids)
