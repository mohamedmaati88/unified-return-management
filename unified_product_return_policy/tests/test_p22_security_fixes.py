"""Security controls: company isolation of override lines, ship-later allocations and
authentication attempts; own-documents visibility of Sales return requests; approver checked again
when a POS authorisation is used. Portal access is covered by test_p24_portal_access.
Attempts made at the same moment need several database connections and are not tested here
(one test transaction cannot race)."""
import json
import uuid

from freezegun import freeze_time

from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import HttpCase, tagged

from ..models.urp_guard import urp_internal
from .test_p15_pos_direct import OCT5, SALE, UrpPosDirectCommon
from .test_p18_sales_exceptions import T1, UrpSalesExceptionCommon
from .test_p20_pos_pin import TestPosApprovalPin

ISOLATED = ("urp.return.override.line", "urp.return.allocation", "urp.auth.attempt")
MANAGER_GROUPS = ("sales_team.group_sale_manager", "stock.group_stock_manager", "point_of_sale.group_pos_manager")


def _user(env, login, groups, company, password=None):
    vals = {"name": login, "login": login, "company_ids": [(6, 0, company.ids)], "company_id": company.id,
            "group_ids": [(6, 0, [env.ref(x).id for x in groups])]}
    if password:
        vals["password"] = password
    return env["res.users"].with_context(no_reset_password=True).create(vals)


@tagged("post_install", "-at_install", "urp", "urp_security_fix")
class TestCompanyIsolation(UrpPosDirectCommon):

    def test_s01_override_lines_allocations_attempts_stay_in_their_company(self):
        A = self.company
        B = self.env["res.company"].create({"name": "Sec B"})
        u_b = _user(self.env, "sec_u_b", MANAGER_GROUPS, B)
        u_a = _user(self.env, "sec_u_a", MANAGER_GROUPS, A)
        pos_user_a = _user(self.env, "sec_pos_a", ["point_of_sale.group_pos_user", "stock.group_stock_user"], A)
        so = self.env["sale.order"].sudo().create({"partner_id": self.partner.id, "order_line": [
            Command.create({"product_id": self.prods["C"].id, "product_uom_qty": 3})]})
        so.action_confirm()
        so.picking_ids.move_ids.write({"quantity": 3, "picked": True})
        so.picking_ids.button_validate()
        move = so.picking_ids.move_ids
        urp_internal(self.env["urp.return.override"].sudo()).create({
            "channel": "sale", "user_id": self.env.uid, "reason": "sec A", "company_id": A.id,
            "line_ids": [Command.create({"origin_move_id": move.id, "product_id": move.product_id.id, "qty": 2})]})
        with freeze_time(SALE):
            order = self._sale(2, ship_later=True, product=self.prods["A"])
            self._deliver(order, 2)
        with freeze_time(OCT5):
            self._pos_refund(order, {"A": 1})                                   # ship-later refund -> allocation
            self._token([{"product_id": self.prods["A"].id, "qty": 1}], str(uuid.uuid4()), password="wrong")
        for model in ISOLATED:
            recs = self.env[model].sudo().search([])
            self.assertTrue(recs and all(r.company_id == A for r in recs), model)
            as_b = self.env[model].with_user(u_b).with_context(allowed_company_ids=B.ids)
            self.assertFalse(as_b.search([]), model)
            self.assertFalse(as_b.search_read([], ["company_id"]), model)
            with self.assertRaises(AccessError, msg=model):
                recs.with_user(u_b).with_context(allowed_company_ids=B.ids).read(["company_id"])
            self.assertEqual(self.env[model].with_user(u_a).search([]), recs, "%s: same-company managers keep it" % model)
        # same-company operational users keep their read rights
        self.assertTrue(self.env["urp.return.allocation"].with_user(pos_user_a).search([]))
        self.assertTrue(self.env["urp.return.override.line"].with_user(pos_user_a).search([]))
        # a user of both companies, working in B only, sees nothing of A; in A and B, everything
        u_ab = _user(self.env, "sec_u_ab", MANAGER_GROUPS, A)
        u_ab.company_ids |= B
        self.assertFalse(self.env["urp.return.allocation"].with_user(u_ab).with_context(allowed_company_ids=B.ids).search([]))
        self.assertTrue(self.env["urp.return.allocation"].with_user(u_ab).with_context(allowed_company_ids=(A | B).ids).search([]))


@tagged("post_install", "-at_install", "urp", "urp_security_fix")
class TestOwnDocuments(UrpSalesExceptionCommon):

    def test_s02_own_documents_salesman(self):
        own = _user(self.env, "sec_own", ["sales_team.group_sale_salesman"], self.company)
        theirs, mine, nobody = self._so({"N": 2}), self._so({"N": 2}), self._so({"N": 2})
        theirs.user_id, mine.user_id, nobody.user_id = self.su, own, False
        with freeze_time(T1):
            reqs = {key: self._request(so, user, {"N": 1}, reason="reason %s" % key)
                    for key, so, user in (("theirs", theirs, self.su), ("mine", mine, own), ("nobody", nobody, self.su))}
            Req, Line = self.env["urp.return.exception"], self.env["urp.return.exception.line"]
            self.assertEqual(Req.with_user(own).search([]), reqs["mine"] | reqs["nobody"])
            self.assertEqual(Line.with_user(own).search([]), (reqs["mine"] | reqs["nobody"]).line_ids)
            for op in (lambda r: r.read(["reason"]), lambda r: r.write({"reason": "changed by a colleague"}),
                       lambda r: r.line_ids.read(["requested_qty"]), lambda r: r.line_ids.write({"requested_qty": 2}),
                       lambda r: r.unlink()):
                with self.assertRaises(AccessError):
                    op(reqs["theirs"].with_user(own))
            # own requests: full workflow unchanged
            reqs["mine"].with_user(own).write({"reason": "updated by its owner"})
            reqs["mine"].with_user(own).action_submit()
            self.assertEqual(reqs["mine"].state, "pending")
            # All Documents salesman and Sales Administrator unchanged
            everything = reqs["theirs"] | reqs["mine"] | reqs["nobody"]
            self.assertEqual(Req.with_user(self.su).search([]), everything)
            self.assertEqual(Req.with_user(self.sa).search([]), everything)
            reqs["theirs"].with_user(self.su).action_submit()
            reqs["theirs"].with_user(self.sa).action_approve()
            self.assertEqual(reqs["theirs"].state, "approved")
            reqs["mine"].with_user(self.sa).action_approve()
            self.assertEqual(reqs["mine"].state, "approved")


@tagged("post_install", "-at_install", "urp", "urp_security_fix")
class TestApproverCheckedAtUse(TestPosApprovalPin):

    def test_s03_authorisation_refused_once_the_approver_lost_the_right(self):
        with freeze_time(SALE):
            order = self._order({"N": 5})
        mgr_group = self.env.ref("point_of_sale.group_pos_manager")
        with freeze_time(OCT5):
            cases = (
                ("employee removed from the POS managers", "pin",
                 lambda: self.pos_config_usd.write({"manager_employee_ids": [(3, self.mgr_emp.id)]}),
                 lambda: self.pos_config_usd.write({"manager_employee_ids": [(4, self.mgr_emp.id)]})),
                ("employee archived", "pin", lambda: self.mgr_emp.sudo().action_archive(),
                 lambda: self.mgr_emp.sudo().action_unarchive()),
                ("POS Administrator group removed", "account", lambda: self.manager.write({"group_ids": [(3, mgr_group.id)]}),
                 lambda: self.manager.write({"group_ids": [(4, mgr_group.id)]})),
                ("account archived", "account", lambda: self.manager.write({"active": False}),
                 lambda: self.manager.write({"active": True})),
            )
            for label, path, lose, restore in cases:
                ref = self._refund_draft(order, {"N": 1})
                tok = (self._pin(self._lines(ref), ref.uuid) if path == "pin" else self._token(self._lines(ref), ref.uuid))["token"]
                lose()
                ref.urp_override_token = tok
                with self.assertRaisesRegex(UserError, "no longer holds that right", msg=label), self.env.cr.savepoint():
                    self._pay(ref, ref.amount_total)
                restore()
                ref.urp_override_token = tok
                self.assertEqual(self._pay(ref, ref.amount_total).state, "paid", "%s: a valid approver still authorises" % label)


@tagged("post_install", "-at_install", "urp", "urp_security_fix")
class TestSecurityRpc(UrpSalesExceptionCommon, HttpCase):
    """The same checks through the web client's JSON-RPC endpoint (list view and direct calls)."""

    def _call(self, model, method, args, kwargs=None):
        return self.url_open("/web/dataset/call_kw", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"model": model, "method": method, "args": args, "kwargs": kwargs or {}}}),
            headers={"Content-Type": "application/json"}, timeout=120).json()

    def test_s04_rpc(self):
        own = _user(self.env, "sec_rpc_own", ["sales_team.group_sale_salesman"], self.company, "Sec-Own-Pass-1!")
        B = self.env["res.company"].create({"name": "Sec RPC B"})
        _user(self.env, "sec_rpc_b", MANAGER_GROUPS, B, "Sec-B-Pass-1!")
        theirs, mine = self._so({"N": 2}), self._so({"N": 2})
        theirs.user_id, mine.user_id = self.su, own
        with freeze_time(T1):
            r_theirs = self._request(theirs, self.su, {"N": 1}, reason="confidential")
            r_mine = self._request(mine, own, {"N": 1})
        move = self._deliveries(theirs).move_ids
        line = urp_internal(self.env["urp.return.override"].sudo()).create({
            "channel": "sale", "user_id": self.env.uid, "reason": "rpc A", "company_id": self.company.id,
            "line_ids": [Command.create({"origin_move_id": move.id, "product_id": move.product_id.id, "qty": 1})]}).line_ids
        self.authenticate("sec_rpc_own", "Sec-Own-Pass-1!")
        res = self._call("urp.return.exception", "web_search_read", [[]], {"specification": {"reason": {}}})
        self.assertEqual({r["id"] for r in res["result"]["records"]}, {r_mine.id})
        for model, method, args in (("urp.return.exception", "read", [r_theirs.ids, ["reason"]]),
                                    ("urp.return.exception", "write", [r_theirs.ids, {"reason": "x"}]),
                                    ("urp.return.exception.line", "read", [r_theirs.line_ids.ids, ["requested_qty"]]),
                                    ("urp.return.exception.line", "write", [r_theirs.line_ids.ids, {"requested_qty": 2}])):
            res = self._call(model, method, args)
            self.assertIn("error", res, "%s %s" % (model, method))
            self.assertIn("AccessError", json.dumps(res["error"]))
        self.assertEqual(r_theirs.reason, "confidential")
        self.authenticate("sec_rpc_b", "Sec-B-Pass-1!")
        res = self._call("urp.return.override.line", "search_read", [[]], {"fields": ["qty", "origin_move_id"]})
        self.assertEqual(res["result"], [])
        res = self._call("urp.return.override.line", "read", [line.ids, ["qty"]])
        self.assertIn("AccessError", json.dumps(res["error"]))
        for model in ("urp.return.allocation", "urp.auth.attempt"):
            self.assertEqual(self._call(model, "search_read", [[]], {"fields": ["company_id"]})["result"], [], model)
