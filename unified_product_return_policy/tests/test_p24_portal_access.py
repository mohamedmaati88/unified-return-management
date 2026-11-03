"""Portal request access: request lines stay in their company, and Own Documents
salesmen see only the portal requests of their own or unassigned sales orders. Access rules only:
the customer's portal flow is unchanged (covered by the existing portal tests)."""
import json

from odoo.exceptions import AccessError
from odoo.fields import Command
from odoo.tests import HttpCase, tagged

from .test_p18_sales_exceptions import UrpSalesExceptionCommon

MODELS = ("urp.portal.return.request", "urp.portal.return.request.line")


class UrpPortalAccessCommon(UrpSalesExceptionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        g = lambda xid: cls.env.ref(xid).id                       # noqa: E731

        def user(login, groups, company=None):
            company = company or cls.company
            return Users.create({"name": login, "login": login, "password": login + "-Pass-1!",
                                 "group_ids": [(6, 0, [g(x) for x in groups])],
                                 "company_ids": [(6, 0, company.ids)], "company_id": company.id})
        cls.own = user("pa_own", ["sales_team.group_sale_salesman"])
        cls.mixed = user("pa_mixed", ["sales_team.group_sale_salesman", "point_of_sale.group_pos_user"])
        cls.company_b = cls.env["res.company"].create({"name": "Portal Access B"})
        cls.b_mgr = user("pa_b_mgr", ["sales_team.group_sale_manager", "stock.group_stock_manager",
                                      "point_of_sale.group_pos_manager"], cls.company_b)
        cls.portal_user = Users.create({"name": "pa portal", "login": "pa_portal", "password": "pa_portal-Pass-1!",
                                        "group_ids": [(6, 0, [g("base.group_portal")])]})

    def _portal_request(self, so, key):
        move = self._deliveries(so).move_ids
        return self.env["urp.portal.return.request"].sudo().create({
            "order_id": so.id, "submitted_at": "2026-10-28 00:00:00", "idempotency_key": key,
            "line_ids": [Command.create({"move_id": move.id, "requested_qty": 1})]})

    def _setup(self):
        orders = {"theirs": self._so({"A": 2}), "mine": self._so({"A": 2}), "nobody": self._so({"A": 2}),
                  "mixed": self._so({"A": 2})}
        orders["theirs"].user_id, orders["mine"].user_id = self.su, self.own
        orders["nobody"].user_id, orders["mixed"].user_id = False, self.mixed
        return {k: self._portal_request(so, k) for k, so in orders.items()}


@tagged("post_install", "-at_install", "urp", "urp_portal_access")
class TestPortalAccess(UrpPortalAccessCommon):

    def test_a01_own_documents_and_roles(self):
        req = self._setup()
        everything = req["theirs"] | req["mine"] | req["nobody"] | req["mixed"]
        expected = {
            "Sales User (Own Documents)": (self.own, req["mine"] | req["nobody"]),
            "Sales + POS (Own Documents)": (self.mixed, req["mixed"] | req["nobody"]),
            "Sales User (All Documents)": (self.su, everything),
            "Sales Administrator": (self.sa, everything),
            "Inventory User": (self.iu, everything),
            "Inventory Administrator": (self.ia, everything),
        }
        for label, (u, visible) in expected.items():
            self.assertEqual(self.env["urp.portal.return.request"].with_user(u).search([]) & everything, visible, label)
            self.assertEqual(self.env["urp.portal.return.request.line"].with_user(u).search([]) & everything.line_ids,
                             visible.line_ids, "%s: lines" % label)
            for hidden in everything - visible:
                with self.assertRaises(AccessError, msg=label):
                    hidden.with_user(u).read(["order_id"])
                with self.assertRaises(AccessError, msg="%s: lines" % label):
                    hidden.line_ids.with_user(u).read(["requested_qty", "order_id"])
        # Cancel Remaining: own documents only for the Own Documents salesman, unchanged for the others
        with self.assertRaises(AccessError):
            req["theirs"].with_user(self.own).action_cancel_remaining()
        self.assertEqual(req["theirs"].state, "active")
        req["mine"].with_user(self.own).action_cancel_remaining()
        self.assertEqual(req["mine"].state, "cancelled")
        req["theirs"].with_user(self.su).action_cancel_remaining()
        self.assertEqual(req["theirs"].state, "cancelled")
        # no direct write or delete for anybody (unchanged)
        for u in (self.own, self.su, self.sa, self.iu):
            with self.assertRaises(AccessError, msg=u.login):
                req["nobody"].with_user(u).write({"state": "done"})
            with self.assertRaises(AccessError, msg=u.login):
                req["nobody"].line_ids.with_user(u).unlink()

    def test_a02_company_isolation(self):
        req = self._setup()
        everything = req["theirs"] | req["mine"] | req["nobody"] | req["mixed"]
        for model, recs in (("urp.portal.return.request", everything), ("urp.portal.return.request.line", everything.line_ids)):
            as_b = self.env[model].with_user(self.b_mgr).with_context(allowed_company_ids=self.company_b.ids)
            self.assertFalse(as_b.search([]), model)
            self.assertFalse(as_b.search_read([], ["id"]), model)
            with self.assertRaises(AccessError, msg=model):
                recs.with_user(self.b_mgr).with_context(allowed_company_ids=self.company_b.ids).read(["id"])
        # a user of both companies working in B only sees nothing of A; in A, everything
        self.b_mgr.company_ids |= self.company
        line = self.env["urp.portal.return.request.line"].with_user(self.b_mgr)
        self.assertFalse(line.with_context(allowed_company_ids=self.company_b.ids).search([]) & everything.line_ids)
        self.assertEqual(line.with_context(allowed_company_ids=(self.company | self.company_b).ids).search([]) & everything.line_ids,
                         everything.line_ids)


@tagged("post_install", "-at_install", "urp", "urp_portal_access")
class TestPortalAccessRpc(UrpPortalAccessCommon, HttpCase):

    def _call(self, model, method, args, kwargs=None):
        return self.url_open("/web/dataset/call_kw", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"model": model, "method": method, "args": args, "kwargs": kwargs or {}}}),
            headers={"Content-Type": "application/json"}, timeout=120).json()

    def test_a03_rpc(self):
        req = self._setup()
        everything = req["theirs"] | req["mine"] | req["nobody"] | req["mixed"]
        for login, visible in (("pa_own", req["mine"] | req["nobody"]), ("pa_b_mgr", req.__class__())):
            self.authenticate(login, login + "-Pass-1!")
            for model in MODELS:
                recs = visible if model == MODELS[0] else (visible.line_ids if visible else visible)
                res = self._call(model, "search_read", [[]], {"fields": ["id"]})
                ids = {r["id"] for r in res["result"]}
                all_ids = set((everything if model == MODELS[0] else everything.line_ids).ids)
                self.assertEqual(ids & all_ids, set(recs.ids) if recs else set(), "%s %s" % (login, model))
            res = self._call("urp.portal.return.request.line", "read", [req["theirs"].line_ids.ids, ["requested_qty"]])
            self.assertIn("AccessError", json.dumps(res.get("error")), login)
        # a portal customer has no direct access to these models (unchanged): the portal pages use the order token
        self.authenticate("pa_portal", "pa_portal-Pass-1!")
        for model in MODELS:
            res = self._call(model, "search_read", [[]], {"fields": ["id"]})
            self.assertIn("AccessError", json.dumps(res.get("error")), model)
