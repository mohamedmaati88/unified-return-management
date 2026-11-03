"""Approvals visible per channel and document ownership, approval
PIN lockout of 15 full minutes from the fifth failure, approval token cleared from cancelled orders."""
import json
import uuid
from datetime import datetime, timedelta

from freezegun import freeze_time

from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import HttpCase, tagged

from ..models.urp_guard import urp_internal
from .test_p15_pos_direct import OCT5, SALE
from .test_p18_sales_exceptions import T1, UrpSalesExceptionCommon
from .test_p20_pos_pin import TestPosApprovalPin


class UrpApprovalVisibilityCommon(UrpSalesExceptionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        g = lambda xid: cls.env.ref(xid).id                       # noqa: E731

        def user(login, groups):
            return Users.create({"name": login, "login": login, "password": login + "-Pass-1!",
                                 "group_ids": [(6, 0, [g(x) for x in groups])],
                                 "company_ids": [(6, 0, cls.company.ids)], "company_id": cls.company.id})
        cls.pos_user = user("stb_pos_user", ["point_of_sale.group_pos_user"])
        cls.own = user("stb_own", ["sales_team.group_sale_salesman"])
        cls.mixed = user("stb_mixed", ["point_of_sale.group_pos_user", "sales_team.group_sale_salesman"])

    def _setup_approvals(self):
        theirs, mine = self._so({"N": 2}), self._so({"N": 2})
        theirs.user_id, mine.user_id = self.su, self.own
        pick_so = self._so({"C": 2})
        pick_so.user_id = self.mixed
        with freeze_time(T1):
            ex = {}
            for key, so in (("theirs", theirs), ("mine", mine)):
                req = self._request(so, self.su, {"N": 1}, reason="sales reason %s" % key)
                req.action_submit()
                req.with_user(self.sa).action_approve()
                self._receive(req.return_picking_ids, self.iu)          # Inventory executes -> usage
                ex[key] = req.sudo().override_id
            # approval given on a return picking (expired period, Sales Administrator with stock rights)
            ret = self._deliveries(pick_so).with_user(self.sa_ia)._create_return()
            ret.move_ids.with_user(self.sa_ia).write({"product_uom_qty": 1, "quantity": 1, "picked": True})
            action = ret.with_user(self.sa_ia).button_validate()
            wiz = self.env["urp.return.check.wizard"].with_user(self.sa_ia).browse(action["res_id"])
            wiz.reason = "picking override reason"
            wiz.action_override()
            ex["picking"] = ret.urp_override_id
        move = self._deliveries(mine).move_ids
        pos_ov = urp_internal(self.env["urp.return.override"].sudo()).create({
            "channel": "pos", "user_id": self.pa.id, "reason": "pos reason", "company_id": self.company.id,
            "line_ids": [Command.create({"product_id": move.product_id.id, "qty": 1, "origin_move_id": move.id})]})
        urp_internal(self.env["urp.return.exception.usage"].sudo()).create({
            "override_line_id": pos_ov.line_ids.id, "move_id": move.returned_move_ids[:1].id, "qty": 1})
        ex["pos"] = pos_ov
        return ex


@tagged("post_install", "-at_install", "urp", "urp_stabilization")
class TestApprovalVisibility(UrpApprovalVisibilityCommon):

    def test_v01_approvals_per_channel_and_documents(self):
        ex = self._setup_approvals()
        self.assertTrue(all(ex.values()), ex)
        self.assertEqual(ex["picking"].picking_id.sale_id.user_id, self.mixed)
        everything = ex["theirs"] | ex["mine"] | ex["picking"] | ex["pos"]
        sales_all = ex["theirs"] | ex["mine"] | ex["picking"]
        expected = {
            "POS User": (self.pos_user, ex["pos"]),
            # standard pos_stock: POS Administrator implies Inventory User, who executes both channels
            "POS Manager": (self.pa, everything),
            "Sales User (Own Documents)": (self.own, ex["mine"]),
            "Sales User (All Documents)": (self.su, sales_all),
            "Sales Administrator": (self.sa, sales_all),
            "Inventory User": (self.iu, everything),
            "POS User + Sales User (Own)": (self.mixed, ex["picking"] | ex["pos"]),
        }
        self.assertTrue(self.pa.has_group("stock.group_stock_user"), "Odoo standard: POS Administrator is an Inventory User")
        Ov, Line, Usage = (self.env[m] for m in ("urp.return.override", "urp.return.override.line", "urp.return.exception.usage"))
        for label, (u, visible) in expected.items():
            self.assertEqual(Ov.with_user(u).search([]) & everything, visible, label)
            self.assertEqual(Line.with_user(u).search([]) & everything.line_ids, visible.line_ids, "%s: lines" % label)
            self.assertEqual(Usage.with_user(u).search([]) & everything.line_ids.usage_ids, visible.line_ids.usage_ids,
                             "%s: executions" % label)
            for hidden in everything - visible:
                with self.assertRaises(AccessError, msg="%s reads %s" % (label, hidden.reason)):
                    hidden.with_user(u).read(["reason"])
                with self.assertRaises(AccessError, msg="%s reads lines of %s" % (label, hidden.reason)):
                    hidden.sudo().line_ids.with_user(u).read(["qty", "origin_move_id"])
            if visible:
                self.assertTrue(visible.with_user(u).read(["reason", "line_ids"]), label)
        self.assertTrue(all(ex[k].line_ids.usage_ids for k in ("theirs", "mine", "pos")), "every channel has executions")


@tagged("post_install", "-at_install", "urp", "urp_stabilization")
class TestApprovalVisibilityRpc(UrpApprovalVisibilityCommon, HttpCase):

    def _call(self, model, method, args, kwargs=None):
        return self.url_open("/web/dataset/call_kw", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"model": model, "method": method, "args": args, "kwargs": kwargs or {}}}),
            headers={"Content-Type": "application/json"}, timeout=120).json()

    def test_v02_rpc(self):
        ex = self._setup_approvals()
        for login, visible, hidden in (("stb_pos_user", ex["pos"], ex["theirs"] | ex["mine"]),
                                       ("stb_own", ex["mine"], ex["theirs"] | ex["picking"] | ex["pos"]),
                                       ("stb_mixed", ex["picking"] | ex["pos"], ex["theirs"] | ex["mine"])):
            self.authenticate(login, login + "-Pass-1!")
            res = self._call("urp.return.override", "web_search_read", [[]], {"specification": {"reason": {}}})
            self.assertEqual({r["id"] for r in res["result"]["records"]}, set(visible.ids), login)
            res = self._call("urp.return.override.line", "search_read", [[]], {"fields": ["qty"]})
            self.assertEqual({r["id"] for r in res["result"]}, set(visible.line_ids.ids), login)
            for model, ids in (("urp.return.override", hidden.ids), ("urp.return.override.line", hidden.line_ids.ids)):
                res = self._call(model, "read", [ids, ["display_name"]])
                self.assertIn("AccessError", json.dumps(res.get("error")), "%s %s" % (login, model))


@tagged("post_install", "-at_install", "urp", "urp_stabilization")
class TestPinLockAndToken(TestPosApprovalPin):

    def _age(self, when_list):
        """Attempts are dated by the database: give them the scenario times."""
        self.env.flush_all()
        ids = self.env["urp.auth.attempt"].sudo().search([("method", "=", "pin")], order="id").ids
        for i, when in zip(ids, when_list):
            self.env.cr.execute("UPDATE urp_auth_attempt SET create_date = %s WHERE id = %s", (when, i))
        self.env.invalidate_all()

    def test_l01_lock_lasts_fifteen_minutes_from_the_fifth_failure(self):
        with freeze_time(SALE):
            order = self._order({"N": 3})
        ref = self._refund_draft(order, {"N": 1})
        lines = self._lines(ref)
        base = datetime(2026, 10, 5, 12, 0, 0)
        at = lambda m, s=0: base + timedelta(minutes=m, seconds=s)            # noqa: E731
        self._clear_attempts()
        fails = []
        for minute in (0, 2, 5, 8, 10):
            with freeze_time(at(minute)):
                self.assertFalse(self._pin(lines, ref.uuid, pin="999999")["ok"])
            fails.append(at(minute))
            self._age(fails)
        Attempt = self.env["urp.auth.attempt"].sudo()
        for minute, second, locked in ((10, 30, True), (15, 1, True), (20, 0, True), (24, 59, True), (25, 1, False)):
            with freeze_time(at(minute, second)):
                self.assertEqual(Attempt._urp_pin_locked(self.pos_config_usd), locked, "12:%02d:%02d" % (minute, second))
        with freeze_time(at(15, 1)):
            n = Attempt.search_count([])
            self.assertEqual(self._pin(lines, ref.uuid)["error"], "locked", "even the right PIN")
            self.assertEqual(self._pin(lines, ref.uuid, pin="999998")["error"], "locked")
            self.assertEqual(Attempt.search_count([]), n, "nothing is recorded while locked")
        # backend approval of a POS request uses the same lock
        with freeze_time(at(20)):
            self.assertTrue(Attempt._urp_pin_locked(self.pos_config_usd))
        # after 12:25 normal use: the earlier failures never count again
        with freeze_time(at(25, 30)), self.env.cr.savepoint() as sp:
            self.assertTrue(self._pin(lines, ref.uuid)["ok"], "right PIN accepted once the lock ended")
            sp.rollback()
        for minute, second in ((26, 0), (27, 0), (28, 0), (29, 0)):
            with freeze_time(at(minute, second)):
                self.assertFalse(self._pin(lines, ref.uuid, pin="999997")["ok"])
            fails.append(at(minute, second))
            self._age(fails)
        with freeze_time(at(29, 30)):
            self.assertFalse(Attempt._urp_pin_locked(self.pos_config_usd), "4 new failures only")
            self._pin(lines, ref.uuid, pin="999996")
        fails.append(at(29, 30))
        self._age(fails)
        for minute, second, locked in ((29, 31, True), (44, 29, True), (44, 31, False)):
            with freeze_time(at(minute, second)):
                self.assertEqual(Attempt._urp_pin_locked(self.pos_config_usd), locked, "second lock 12:%02d:%02d" % (minute, second))
        self._clear_attempts()

    def test_l02_token_cleared_when_the_order_is_cancelled(self):
        with freeze_time(SALE):
            order = self._order({"N": 6})
        with freeze_time(OCT5):
            for how in ("backend cancel", "removed in the POS", "state written"):
                ref = self._refund_draft(order, {"N": 1})
                tok = self._pin(self._lines(ref), ref.uuid)["token"]
                ref.urp_override_token = tok
                if how == "backend cancel":
                    ref.with_context(active_ids=ref.ids).action_pos_order_cancel()
                elif how == "removed in the POS":
                    self.env["pos.order"].remove_from_ui(ref.ids)
                else:
                    ref.write({"state": "cancel"})
                self.env.flush_all()
                self.env.cr.execute("SELECT state, urp_override_token FROM pos_order WHERE id = %s", ref.ids)
                row = self.env.cr.fetchone()
                self.assertIn(row, (("cancel", None), None), how)          # None: the POS removal deletes the order
            # the binding is unchanged: another order, other quantities, reuse and expiry are refused
            ref = self._refund_draft(order, {"N": 1})
            tok = self._pin(self._lines(ref), ref.uuid)["token"]
            other = self._refund_draft(order, {"N": 1})
            other.urp_override_token = tok
            with self.assertRaisesRegex(UserError, "invalid, expired, already used"), self.env.cr.savepoint():
                self._pay(other, other.amount_total)
            more = self._refund_draft(order, {"N": 2})
            ref_uuid = ref.uuid
            ref.unlink()
            self.env.flush_all()
            more.uuid = ref_uuid
            more.urp_override_token = tok
            with self.assertRaises(UserError), self.env.cr.savepoint():
                self._pay(more, more.amount_total)
            ref = self._refund_draft(order, {"N": 1})
            tok = self._pin(self._lines(ref), ref.uuid)["token"]
            ref.urp_override_token = tok
            self.assertEqual(self._pay(ref, ref.amount_total).state, "paid")
            self.assertFalse(ref.urp_override_token, "cleared once consumed")
            again = self._refund_draft(order, {"N": 1})
            again.urp_override_token = tok
            with self.assertRaisesRegex(UserError, "invalid, expired, already used"), self.env.cr.savepoint():
                self._pay(again, again.amount_total)
            late = self._refund_draft(order, {"N": 1})
            tok_late = self._pin(self._lines(late), late.uuid)["token"]
        with freeze_time("2026-10-05 09:11:00"):
            late.urp_override_token = tok_late
            with self.assertRaisesRegex(UserError, "invalid, expired, already used"), self.env.cr.savepoint():
                self._pay(late, late.amount_total)
