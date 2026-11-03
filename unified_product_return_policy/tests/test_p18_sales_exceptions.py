"""Sales return exceptions: Sales Order -> Return Exception Request -> Sales
Administrator approval -> Inventory return, executed with the executing user's own stock rights.

Delivery validated on 25 Oct 2026 12:00 (Asia/Riyadh, day 0); checked on 30 Oct:
A = 7 days (still within the period), C = 3 days (period ended), N = 0 days (non-returnable)."""
from datetime import datetime

from freezegun import freeze_time

from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import Form, tagged

from .common import UrpCommon
from ..models.urp_guard import urp_checked, urp_internal

VAL = datetime(2026, 10, 25, 9, 0)
T1 = datetime(2026, 10, 30, 9, 0)
FORGED = {"urp_internal": True, "urp_skip_guard": True, "urp_checked": True}


class UrpSalesExceptionCommon(UrpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        g = lambda xid: cls.env.ref(xid).id                       # noqa: E731
        Users = cls.env["res.users"].with_context(no_reset_password=True)

        def user(login, groups, company=None):
            company = company or cls.company
            return Users.create({"name": login, "login": login, "group_ids": [(6, 0, [g(x) for x in groups])],
                                 "company_ids": [(6, 0, company.ids)], "company_id": company.id})
        cls.su = user("urp_x_su", ["sales_team.group_sale_salesman_all_leads"])
        cls.sa = user("urp_x_sa", ["sales_team.group_sale_manager"])
        cls.iu = user("urp_x_iu", ["stock.group_stock_user"])
        cls.ia = user("urp_x_ia", ["stock.group_stock_manager"])
        cls.sa_iu = user("urp_x_sa_iu", ["sales_team.group_sale_manager", "stock.group_stock_user"])
        cls.sa_ia = user("urp_x_sa_ia", ["sales_team.group_sale_manager", "stock.group_stock_manager"])
        cls.pa = user("urp_x_pa", ["point_of_sale.group_pos_manager"])
        cls.prods = {}
        for code, days in (("A", 7), ("C", 3), ("N", 0)):
            p = cls.env["product.product"].create({"name": "X " + code, "is_storable": True, "list_price": 10,
                                                   "use_custom_return_period": True, "custom_return_period_days": days})
            cls.env["stock.quant"]._update_available_quantity(p, cls.warehouse.lot_stock_id, 100)
            cls.prods[code] = p

    # ------------------------------------------------------------------ helpers
    def _so(self, qtys, deliver=None):
        so = self.env["sale.order"].create({"partner_id": self.partner.id, "order_line": [
            Command.create({"product_id": self.prods[c].id, "product_uom_qty": q}) for c, q in qtys.items()]})
        so.action_confirm()
        for at, part in (deliver or [(VAL, qtys)]):
            with freeze_time(at):
                pick = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
                for move in pick.move_ids:
                    move.quantity = part.get(self._code(move.product_id), 0)
                    move.picked = True
                res = pick.button_validate()
                if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
                    Form.from_action(self.env, res).save().process()
        return so

    def _code(self, product):
        return next(c for c, p in self.prods.items() if p == product)

    def _deliveries(self, so):
        return so.picking_ids.filtered(lambda p: p.state == "done" and p.picking_type_code == "outgoing").sorted("id")

    def _request(self, so, user, qtys, reason="Customer complaint accepted", delivery=None):
        action = so.with_user(user).action_urp_request_exception()
        req = self.env["urp.return.exception"].with_user(user).browse(action["res_id"])
        for line in req.line_ids:
            if delivery and line.origin_move_id.picking_id != delivery:
                line.unlink()
                continue
            q = qtys.get(self._code(line.product_id), 0)
            if q:
                line.requested_qty = q
            else:
                line.unlink()
        req.reason = reason
        return req

    def _return(self, pick, user, qtys):
        ret = pick.with_user(user)._create_return()
        for move in ret.move_ids:
            q = qtys.get(self._code(move.product_id), 0)
            move.write({"product_uom_qty": q, "quantity": q, "picked": True})
        return ret

    def _validate(self, ret, user):
        return ret.with_user(user).button_validate()

    def _wizard(self, action, user):
        self.assertIsInstance(action, dict)
        self.assertEqual(action["res_model"], "urp.return.check.wizard")
        return self.env["urp.return.check.wizard"].with_user(user).browse(action["res_id"])

    def _auto(self, req):
        """The Return Transfer(s) created automatically for the request and still open."""
        return req.return_picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))

    def _receive(self, pick, user, qtys=None, backorder=True):
        """Inventory validates the physical receipt (quantities received per product code)."""
        for move in pick.move_ids:
            q = move.product_uom_qty if qtys is None else qtys.get(self._code(move.product_id), 0)
            move.with_user(user).write({"quantity": q, "picked": True})
        res = pick.with_user(user).button_validate()
        if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
            wiz = Form.from_action(self.env, res).save()
            res = (wiz.process() if backorder else wiz.process_cancel_backorder()) or True
        return res

    def _approved(self, qtys, approver=None, so=None):
        so = so or self._so({c: 5 for c in qtys})
        with freeze_time(T1):
            req = self._request(so, self.su, qtys)
            req.action_submit()
            req.with_user(approver or self.sa).action_approve()
        return so, req


@tagged("post_install", "-at_install", "urp", "urp_sales_exceptions")
class TestSalesExceptionRights(UrpSalesExceptionCommon):

    def test_x01_request_by_sales_user_approval_by_sales_admin_only(self):
        so = self._so({"N": 3})
        with freeze_time(T1):
            req = self._request(so, self.su, {"N": 2})
            self.assertEqual((req.state, req.requested_by, req.line_ids.exception_type), ("draft", self.su, "non_returnable"))
            req.action_submit()
            self.assertEqual(req.state, "pending")
            for user in (self.su, self.ia, self.iu, self.pa):
                with self.assertRaises(AccessError, msg=user.login):
                    req.with_user(user).action_approve()
            self.assertFalse(req.with_user(self.su).can_approve)
            req.with_user(self.sa).action_approve()                                      # (no stock right)
            self.assertEqual((req.state, req.approved_by), ("approved", self.sa))
            ov = req.sudo().override_id
            self.assertEqual((ov.user_id, ov.channel, ov.reason, ov.line_ids.qty, ov.line_ids.exception_type),
                             (self.sa, "sale", "Customer complaint accepted", 2.0, "non_returnable"))
            self.assertEqual((ov.approver_role, ov.auth_method), ("sales_admin", "odoo_session"))
            self.assertFalse(self.sa.has_group("stock.group_stock_user"), "42. no stock right given")
            with self.assertRaises(AccessError):
                self._deliveries(so).with_user(self.sa)._create_return()
            # new 1-5: the standard Return Transfer was created at approval, not validated
            delivery = self._deliveries(so)
            ret = self._auto(req)
            self.assertEqual(len(ret), 1)
            self.assertEqual((ret.state, ret.return_id, ret.picking_type_id), ("assigned", delivery, self.warehouse.in_type_id))
            self.assertEqual((ret.location_id, ret.location_dest_id),
                             (delivery.location_dest_id, self.warehouse.lot_stock_id))
            self.assertEqual([(m.product_id, m.product_uom_qty, m.uom_id, m.origin_returned_move_id, m.state)
                              for m in ret.move_ids],
                             [(self.prods["N"], 2.0, delivery.move_ids.uom_id, delivery.move_ids, "assigned")])
            self.assertEqual(so.urp_return_count, 1)
            # new 11: the Sales Administrator without Inventory rights cannot validate it
            with self.assertRaisesRegex(AccessError, "requires Inventory rights"), self.env.cr.savepoint():
                self._receive(ret, self.sa)
            # new 3: visible in Inventory's usual receipts for the Inventory User, who validates it (6. new 13)
            receipts = self.env["stock.picking"].with_user(self.iu).search([("picking_type_code", "=", "incoming"),
                                                                            ("return_id", "=", delivery.id)])
            self.assertEqual(receipts, ret)
            self.assertIs(self._receive(ret, self.iu), True)
            usage = ov.line_ids.usage_ids
            self.assertEqual((usage.user_id, usage.qty, usage.move_id.picking_id), (self.iu, 2.0, ret))
            self.assertEqual(req.state, "used")

    def test_x02_sales_admin_with_stock_rights_approves_and_executes_alone(self):
        for admin in (self.sa_iu, self.sa_ia):
            so = self._so({"C": 2})
            with freeze_time(T1):
                req = self._request(so, admin, {"C": 2}, reason="Self-approved, damaged on arrival")
                req.action_approve()                                                     # own request, no 2nd person
                self.assertEqual((req.requested_by, req.approved_by, req.state), (admin, admin, "approved"))
                ret = self._auto(req)
                self.assertIs(self._receive(ret, admin), True)
                self.assertEqual(req.sudo().override_id.line_ids.usage_ids.user_id, admin)
                self.assertEqual(req.state, "used")

    def test_x03_inventory_cannot_approve_or_execute_without_approval(self):
        so = self._so({"N": 2, "C": 2, "A": 2})
        with freeze_time(T1):
            for user in (self.iu, self.ia):                                               # scenario 3
                ret = self._return(self._deliveries(so), user, {"A": 1})
                self.assertEqual(ret.move_ids.product_id, self.prods["A"], "N and C left out without approval")
                self.assertIs(self._validate(ret, user), True, "15. within the period: normal return")
            forged = self.env["stock.picking"].with_user(self.ia).create({
                "picking_type_id": self.warehouse.in_type_id.id, "location_id": self.partner.property_stock_customer.id,
                "location_dest_id": self.warehouse.lot_stock_id.id,
                "move_ids": [Command.create({"product_id": self.prods["N"].id, "product_uom_qty": 1,
                                             "origin_returned_move_id": self._deliveries(so).move_ids.filtered(
                                                 lambda m: m.product_id == self.prods["N"]).id,
                                             "location_id": self.partner.property_stock_customer.id,
                                             "location_dest_id": self.warehouse.lot_stock_id.id})]})
            forged.action_confirm()
            forged.move_ids.write({"quantity": 1, "picked": True})
            wiz = self._wizard(self._validate(forged, self.ia), self.ia)
            self.assertEqual(wiz.line_ids.kind, "non_returnable")
            self.assertFalse(wiz.can_override, "Inventory Administrator is no Sales Administrator")
            with self.assertRaises(UserError):
                forged.move_ids.with_user(self.ia)._action_done()
            # context flags are ignored (privilege markers are server objects, not context values)
            self._wizard(forged.with_user(self.ia).with_context(**FORGED).button_validate(), self.ia)
            with self.assertRaises(UserError):
                forged.move_ids.with_user(self.ia).with_context(**FORGED)._action_done()
            self.assertNotEqual(forged.state, "done")

    def test_x04_rights_and_visibility(self):
        so, req = self._approved({"N": 1})
        ov = req.sudo().override_id
        with self.assertRaises(AccessError):
            req.with_user(self.iu).read(["reason"])
        with self.assertRaises(AccessError):
            ov.with_user(self.iu).read(["exception_id"])
        self.assertEqual(ov.with_user(self.iu).read(["reason"])[0]["reason"], "Customer complaint accepted")
        approvals = self._deliveries(so).with_user(self.iu).urp_approval_line_ids
        self.assertEqual((approvals.product_id, approvals.qty, approvals.remaining_qty), (self.prods["N"], 1.0, 1.0))
        with self.assertRaises(AccessError):                                              # RPC approval
            self.env["urp.return.exception"].with_user(self.su).browse(req.id).action_reject()
        for user in (self.su, self.sa, self.iu):                                          # approval records
            with self.assertRaises((AccessError, UserError)):
                ov.with_user(user).write({"reason": "changed"})
            with self.assertRaises((AccessError, UserError)):
                ov.line_ids.with_user(user).write({"qty": 9})
        with self.assertRaises(UserError):
            req.with_user(self.sa).write({"reason": "changed"})
        with self.assertRaises(UserError):
            req.line_ids.with_user(self.sa).write({"requested_qty": 5})
        with self.assertRaises((AccessError, UserError)):
            req.with_user(self.sa).write({"state": "approved"})

    def test_x05_other_company_approver(self):
        company_b = self.env["res.company"].create({"name": "X other co"})
        sa_b = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "sa_b", "login": "urp_x_sa_b", "group_ids": [(6, 0, [self.env.ref("sales_team.group_sale_manager").id])],
            "company_ids": [(6, 0, company_b.ids)], "company_id": company_b.id})
        so = self._so({"N": 1})
        with freeze_time(T1):
            req = self._request(so, self.su, {"N": 1})
            req.action_submit()
            with self.assertRaises(AccessError):
                req.with_user(sa_b).with_context(allowed_company_ids=company_b.ids).action_approve()
            self.assertEqual(req.state, "pending")

    def test_x06_no_inventory_right_given_to_the_sales_administrator(self):
        """The module adds no Inventory right: no access row, record rule or group on stock models,
        no group change when approving, the standard Return button stays refused. (Odoo itself,
        through sale_stock, already lets a Sales Administrator create and validate pickings by
        ORM/RPC: that standard right is neither added nor removed by this module.)"""
        # Odoo 20: access rights and record rules are both rows of ir.access
        data = self.env["ir.model.data"].search([("module", "=", "unified_product_return_policy"),
                                                 ("model", "in", ("ir.access", "res.groups"))])
        access = self.env["ir.access"].browse(data.filtered(lambda d: d.model == "ir.access").mapped("res_id"))
        self.assertTrue(access)
        self.assertFalse(access.filtered(lambda a: a.model_id.model.startswith("stock.")), "no stock access row or rule")
        self.assertFalse(data.filtered(lambda d: d.model == "res.groups"), "no group created or changed")
        groups_before = self.sa.group_ids
        so = self._so({"N": 2})
        delivery = self._deliveries(so)
        with freeze_time(T1):
            req = self._request(so, self.sa, {"N": 1})
            req.action_submit()
            req.action_approve()
            self.assertEqual(self.sa.group_ids, groups_before, "approving changes no group")
            self.assertFalse(self.sa.has_group("stock.group_stock_user") or self.sa.has_group("stock.group_stock_manager"))
            with self.assertRaises(AccessError):                                          # standard Return button
                delivery.with_user(self.sa)._create_return()
            self.assertIs(self._receive(self._auto(req), self.iu), True)
            self.assertEqual((req.state, req.sudo().override_id.line_ids.usage_ids.user_id), ("used", self.iu))

    def _built_return(self, so, user, code, qty):
        """Return picking built through the ORM/RPC with the user's standard rights (sale_stock
        lets a Sales Administrator create and validate pickings), without the Return button."""
        origin = self._deliveries(so).move_ids.filtered(lambda m: m.product_id == self.prods[code])
        ret = self.env["stock.picking"].with_user(user).create({
            "picking_type_id": self.warehouse.in_type_id.id, "location_id": self.partner.property_stock_customer.id,
            "location_dest_id": self.warehouse.lot_stock_id.id,
            "move_ids": [Command.create({"product_id": self.prods[code].id, "product_uom_qty": qty,
                                         "origin_returned_move_id": origin.id,
                                         "location_id": self.partner.property_stock_customer.id,
                                         "location_dest_id": self.warehouse.lot_stock_id.id})]})
        ret.with_user(user).action_confirm()
        ret.move_ids.with_user(user).write({"quantity": qty, "picked": True})
        return ret

    def test_x08_exceptional_execution_needs_inventory_rights(self):
        so = self._so({"N": 2, "A": 2})
        with freeze_time(T1):
            # within the period: standard Odoo behaviour is kept, no extra condition
            ret_a = self._built_return(so, self.sa, "A", 1)
            self.assertIs(ret_a.with_user(self.sa).button_validate(), True)
            # the Sales Administrator without Inventory rights approves...
            req = self._request(so, self.sa, {"N": 1})
            req.action_submit()
            req.action_approve()
            self.assertEqual(req.state, "approved")
            # ... but cannot execute the exceptional return himself: not by RPC, sudo() or context flags
            ret = self._auto(req)
            ret.move_ids.with_user(self.sa).write({"quantity": 1, "picked": True})     # standard sale_stock right
            for ctx in ({}, FORGED):
                with self.assertRaisesRegex(AccessError, "requires Inventory rights"), self.env.cr.savepoint():
                    ret.with_user(self.sa).with_context(**ctx).button_validate()
                with self.assertRaisesRegex(AccessError, "requires Inventory rights"), self.env.cr.savepoint():
                    ret.move_ids.with_user(self.sa).sudo().with_context(**ctx)._action_done()
            self.assertNotEqual(ret.state, "done")
            line = req.sudo().override_id.line_ids
            self.assertEqual((req.state, line.used_qty, line.remaining_qty), ("approved", 0.0, 1.0), "nothing consumed")
            # an Inventory User executes it, within the approved quantity
            self.assertIs(ret.with_user(self.iu).button_validate(), True)
            self.assertEqual((req.state, line.usage_ids.user_id), ("used", self.iu))

    def test_x09_override_at_validation_needs_inventory_rights(self):
        so = self._so({"N": 2})
        n_move = self._deliveries(so).move_ids
        with freeze_time(T1):
            ret = self._built_return(so, self.iu, "N", 1)
            ov = urp_internal(self.env["urp.return.override"].sudo()).create({
                "channel": "sale", "user_id": self.sa_iu.id, "reason": "Override at validation",
                "company_id": self.company.id, "picking_id": ret.id,
                "line_ids": [Command.create({"origin_move_id": n_move.id, "product_id": n_move.product_id.id, "qty": 1,
                                             "exception_type": "non_returnable"})]})
            urp_internal(ret.sudo()).write({"urp_override_id": ov.id})
            with self.assertRaises(AccessError), self.env.cr.savepoint():      # first layer: check wizard (Inventory only)
                ret.with_user(self.sa).button_validate()
            with self.assertRaisesRegex(AccessError, "requires Inventory rights"), self.env.cr.savepoint():
                urp_checked(ret.with_user(self.sa)).button_validate()          # final guard, wizard already passed
            with self.assertRaisesRegex(AccessError, "requires Inventory rights"), self.env.cr.savepoint():
                ret.move_ids.with_user(self.sa).sudo()._action_done()
            self.assertFalse(ov.consumed_at)
            self.assertIs(urp_checked(ret.with_user(self.iu)).button_validate(), True)
            self.assertTrue(ov.consumed_at)

    def test_x10_approval_unusable_once_the_approver_lost_his_rights(self):
        company_b = self.env["res.company"].create({"name": "X10 other co"})
        self.sa.write({"company_ids": [(4, company_b.id)]})
        so = self._so({"N": 3})
        delivery = self._deliveries(so)
        n_move = delivery.move_ids
        with freeze_time(T1):
            req = self._request(so, self.su, {"N": 2})
            req.action_submit()
            req.with_user(self.sa).action_approve()
            ret = self._auto(req)
            ret.move_ids.write({"quantity": 2, "picked": True})
            self.assertEqual(n_move._urp_approval_remaining(exclude=ret.move_ids), 2.0)
            lost = {
                "company access": ({"company_id": company_b.id, "company_ids": [(3, self.company.id)]},
                                   {"company_ids": [(4, self.company.id)], "company_id": self.company.id}),
                "archived": ({"active": False}, {"active": True}),
                "administrator group": (
                    {"group_ids": [(6, 0, [self.env.ref("sales_team.group_sale_salesman_all_leads").id])]},
                    {"group_ids": [(6, 0, [self.env.ref("sales_team.group_sale_manager").id])]}),
            }
            for label, (lose, restore) in lost.items():
                self.sa.write(lose)
                self.assertEqual(n_move._urp_approval_remaining(exclude=ret.move_ids), 0.0, label)
                wiz = self._wizard(self._validate(ret, self.iu), self.iu)
                self.assertEqual((wiz.line_ids.kind, wiz.can_override), ("non_returnable", False), label)
                self.sa.write(restore)
                self.assertEqual(n_move._urp_approval_remaining(exclude=ret.move_ids), 2.0, label)
            self.assertIs(self._validate(ret, self.iu), True)
            self.assertEqual(req.state, "used")

    def test_x07_forged_requests_and_approvals(self):
        so = self._so({"N": 2})
        delivery = self._deliveries(so)
        n_move = delivery.move_ids
        with freeze_time(T1):
            forged = self.env["urp.return.exception"].with_user(self.su).create({
                "source": "sale", "sale_order_id": so.id, "company_id": self.company.id, "reason": "forged",
                "state": "approved", "approved_by": self.sa.id, "requested_by": self.sa.id,
                "line_ids": [Command.create({"origin_move_id": n_move.id, "requested_qty": 2, "approved_qty": 2,
                                             "exception_type": "expired"})]})
            self.assertEqual((forged.state, forged.approved_by, forged.requested_by), ("draft", self.env["res.users"], self.su))
            self.assertEqual((forged.line_ids.approved_qty, forged.line_ids.exception_type), (0.0, "non_returnable"))
            with self.assertRaises(UserError):
                forged.write({"state": "approved"})
            with self.assertRaises(UserError):
                forged.line_ids.write({"approved_qty": 2})
            with self.assertRaises(AccessError):
                forged.with_context(**FORGED).action_approve()
            with self.assertRaises((AccessError, UserError)):
                self.env["urp.return.override"].with_user(self.su).create({
                    "channel": "sale", "reason": "forged", "company_id": self.company.id, "user_id": self.sa.id})
            with self.assertRaises((AccessError, UserError)):
                self.env["urp.return.exception.usage"].with_user(self.iu).with_context(**FORGED).create(
                    {"override_line_id": 1, "move_id": n_move.id, "qty": 1})
            self.assertFalse(n_move._urp_approval_lines(), "nothing usable was created")
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._return(delivery, self.iu, {"N": 1})
            # a user of another company cannot open a request on this order
            company_b = self.env["res.company"].create({"name": "X7 other co"})
            su_b = self.env["res.users"].with_context(no_reset_password=True).create({
                "name": "su_b", "login": "urp_x7_su_b",
                "group_ids": [(6, 0, [self.env.ref("sales_team.group_sale_salesman_all_leads").id])],
                "company_ids": [(6, 0, company_b.ids)], "company_id": company_b.id})
            with self.assertRaises(AccessError):
                so.with_user(su_b).action_urp_request_exception()


@tagged("post_install", "-at_install", "urp", "urp_sales_exceptions")
class TestSalesExceptionPolicyAndQuantities(UrpSalesExceptionCommon):

    def test_y07_expired_needs_approval_in_time_does_not(self):
        so = self._so({"A": 2, "C": 2})
        delivery = self._deliveries(so)
        a_move, c_move = (delivery.move_ids.filtered(lambda m, c=c: m.product_id == self.prods[c]) for c in ("A", "C"))
        with freeze_time(T1):
            self.assertEqual((a_move._urp_exception_type(), c_move._urp_exception_type()), (False, "expired"))
            # within the period: the usual return (Inventory's Return button), no approval involved
            ret_a = self._return(delivery, self.iu, {"A": 1})
            self.assertEqual(ret_a.move_ids.product_id, self.prods["A"], "C (period ended) is left out")
            self.assertIs(self._validate(ret_a, self.iu), True)
            self.assertFalse(self.env["urp.return.override.line"].search([("origin_move_id", "=", a_move.id)]))
            # period ended: refused without approval, Inventory Administrator included
            forged = self.env["stock.picking"].with_user(self.ia).create({
                "picking_type_id": self.warehouse.in_type_id.id, "location_id": self.partner.property_stock_customer.id,
                "location_dest_id": self.warehouse.lot_stock_id.id,
                "move_ids": [Command.create({"product_id": self.prods["C"].id, "product_uom_qty": 1,
                                             "origin_returned_move_id": c_move.id,
                                             "location_id": self.partner.property_stock_customer.id,
                                             "location_dest_id": self.warehouse.lot_stock_id.id})]})
            forged.action_confirm()
            forged.move_ids.write({"quantity": 1, "picked": True})
            wiz = self._wizard(self._validate(forged, self.ia), self.ia)
            self.assertEqual((wiz.line_ids.kind, wiz.can_override), ("expired", False))
            with self.assertRaises(UserError):
                forged.move_ids.with_user(self.ia)._action_done()
            forged.action_cancel()
            # ... and allowed once a Sales Administrator approved it, for the approved quantity only
            req = self._request(so, self.su, {"C": 1})
            self.assertEqual(req.line_ids.exception_type, "expired")
            req.action_submit()
            self.assertFalse(req.return_picking_ids, "nothing created before the approval")
            req.with_user(self.sa).action_approve()
            ret_c = self._auto(req)
            self.assertEqual(ret_c.move_ids.product_uom_qty, 1.0)
            ret_c.move_ids.write({"quantity": 2, "picked": True})
            self.assertEqual(self._wizard(self._validate(ret_c, self.iu), self.iu).line_ids.eligible, 1.0)
            self.assertIs(self._receive(ret_c, self.iu), True)
            ov_line = req.sudo().override_id.line_ids
            self.assertEqual((ov_line.exception_type, ov_line.used_qty, req.state), ("expired", 1.0, "used"))

    def test_y01_history_is_frozen(self):
        so = self._so({"N": 2, "C": 2})
        self.prods["N"].product_tmpl_id.custom_return_period_days = 30
        self.prods["C"].categ_id = self.env["product.category"].create({"name": "Y cat", "use_custom_return_period": True,
                                                                         "custom_return_period_days": 0})
        with freeze_time(T1):
            req = self._request(so, self.su, {"N": 1, "C": 1})
            self.assertEqual(sorted(req.line_ids.mapped("exception_type")), ["expired", "non_returnable"], "14.")
            self.assertEqual(req.line_ids.filtered(lambda l: l.product_id == self.prods["N"]).policy_label,
                             "0 days (non-returnable)")
            req.action_submit()
            req.with_user(self.sa).action_approve()
            self.assertIs(self._receive(self._auto(req), self.iu), True)
        moves = self._deliveries(so).move_ids
        self.assertEqual(sorted(moves.urp_batch_id.mapped("return_days")), [0, 3], "snapshots unchanged")

    def test_y02_within_period_request_creates_the_return_at_once(self):
        so = self._so({"A": 2})
        with freeze_time(T1):
            req = self._request(so, self.su, {"A": 1}, reason="")
            self.assertEqual((req.line_ids.exception_type, req.needs_approval), ("within_period", False))
            req.action_submit()
            self.assertEqual((req.state, req.approved_by, req.override_id), ("approved", self.env["res.users"],
                                                                             self.env["urp.return.override"]))
            ret = self._auto(req)
            self.assertEqual((ret.state, ret.move_ids.product_uom_qty, ret.return_id), ("assigned", 1.0, self._deliveries(so)))
            self.assertTrue(ret.move_ids.urp_claim_qty, "in-time right documented at creation")
        with freeze_time(datetime(2026, 11, 10, 9, 0)):                                  # validated after the deadline
            self.assertIs(self._receive(ret, self.iu), True)
        self.assertEqual((req.state, req.line_ids.used_qty), ("used", 1.0))

    def test_y03_partial_consumption(self):
        so, req = self._approved({"N": 5})
        delivery = self._deliveries(so)
        line = req.sudo().override_id.line_ids
        with freeze_time(T1):
            ret = self._auto(req)
            self.assertIs(self._receive(ret, self.iu, {"N": 2}), True)                   # 2 received, backorder of 3
            self.assertEqual((req.state, line.used_qty, line.remaining_qty), ("partially_used", 2.0, 3.0))
            backorder = self._auto(req)
            self.assertEqual((len(backorder), backorder.move_ids.product_uom_qty), (1, 3.0), "the backorder keeps the link")
            self.assertIs(self._receive(backorder, self.iu), True)
            self.assertEqual((req.state, line.used_qty, line.remaining_qty), ("used", 5.0, 0.0))
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._return(delivery, self.iu, {"N": 1})

    def test_y04_quantity_caps(self):
        so, req = self._approved({"N": 2})
        delivery = self._deliveries(so)
        with freeze_time(T1):
            ret = self._auto(req)
            ret.move_ids.write({"product_uom_qty": 3, "quantity": 3, "picked": True})    # more than approved
            wiz = self._wizard(self._validate(ret, self.iu), self.iu)
            self.assertEqual((wiz.line_ids.kind, wiz.line_ids.eligible), ("non_returnable", 2.0))
            with self.assertRaises(UserError):
                ret.move_ids.with_user(self.iu)._action_done()
            wiz.action_return_eligible_only()
            self.assertEqual(ret.move_ids.filtered(lambda m: m.state == "done").quantity, 2.0)
            req2 = self._request(so, self.su, {"N": 4})
            with self.assertRaisesRegex(UserError, "can still be returned"):
                req2.action_submit()                                                     # 5 delivered, 2 returned
        # an open (not yet received) return already holds its quantity
        so3, req3 = self._approved({"N": 4}, so=self._so({"N": 5}))
        with freeze_time(T1):
            self.assertEqual(self._auto(req3).move_ids.product_uom_qty, 4.0)
            req4 = self._request(so3, self.su, {"N": 2})
            self.assertEqual((req4.line_ids.pending_qty, req4.line_ids.available_qty), (4.0, 1.0))
            with self.assertRaisesRegex(UserError, "can still be returned"):
                req4.action_submit()

    def test_y05_deliveries_kept_apart(self):
        so = self._so({"N": 4, "C": 2}, deliver=[(VAL, {"N": 2, "C": 2}), (datetime(2026, 10, 26, 9, 0), {"N": 2})])
        first, second = self._deliveries(so)
        with freeze_time(T1):
            req = self._request(so, self.su, {"N": 1}, delivery=first)
            req.action_submit()
            req.with_user(self.sa).action_approve()
            self.assertEqual((self._auto(req).return_id, self._auto(req).move_ids.product_id), (first, self.prods["N"]))
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._return(second, self.iu, {"N": 1})                                  # other delivery
            self.assertIs(self._receive(self._auto(req), self.iu), True)
            # one request over both deliveries: one Return Transfer per delivery, each quantity from its own
            both = self._request(so, self.su, {"N": 1})
            self.assertEqual(sorted(both.line_ids.origin_move_id.picking_id.ids), sorted((first | second).ids))
            both.action_submit()
            both.with_user(self.sa).action_approve()
            rets = self._auto(both)
            self.assertEqual(sorted((r.return_id.id, r.move_ids.product_uom_qty) for r in rets),
                             sorted([(first.id, 1.0), (second.id, 1.0)]))

    def test_y06_mixed_returns(self):
        so = self._so({"A": 2, "N": 2, "C": 2})
        with freeze_time(T1):
            req = self._request(so, self.su, {"A": 1, "N": 1, "C": 2})
            self.assertEqual({l.product_id: l.exception_type for l in req.line_ids},
                             {self.prods["A"]: "within_period", self.prods["N"]: "non_returnable",
                              self.prods["C"]: "expired"})
            req.action_submit()
            normal = self._auto(req)
            self.assertEqual((req.state, normal.move_ids.product_id), ("pending", self.prods["A"]),
                             "within the period: created at once; the blocked lines wait for the approval")
            req.with_user(self.sa).action_approve()
            exceptional = self._auto(req) - normal
            self.assertEqual(sorted(exceptional.move_ids.product_id.ids), sorted((self.prods["N"] | self.prods["C"]).ids))
            for ret in normal | exceptional:
                self.assertIs(self._receive(ret, self.iu), True)
            types = {l.product_id: (l.exception_type, l.used_qty) for l in req.sudo().override_id.line_ids}
            self.assertEqual(types, {self.prods["N"]: ("non_returnable", 1.0), self.prods["C"]: ("expired", 2.0)})
            self.assertEqual(req.state, "used")

    def test_y08_no_duplicate_return(self):
        so, req = self._approved({"N": 2})
        with freeze_time(T1):
            self.assertEqual(len(req.return_picking_ids), 1)
            self.assertFalse(req.can_create_returns)
            with self.assertRaisesRegex(UserError, "Nothing left to return"):
                req.with_user(self.su).action_urp_create_returns()
            self.assertFalse(req._urp_create_returns(req.line_ids), "repeated creation: nothing more")
            self.assertEqual(len(req.return_picking_ids), 1)

    def test_y09_cancelled_return_transfer(self):                                        # new 10. 35.
        so, req = self._approved({"N": 2})
        line = req.sudo().override_id.line_ids
        with freeze_time(T1):
            first = self._auto(req)
            first.with_user(self.iu).action_cancel()                                     # before the goods arrived
            self.assertEqual((req.state, line.used_qty, line.remaining_qty, req.can_create_returns),
                             ("approved", 0.0, 2.0, True), "nothing consumed, nothing more granted")
            req.with_user(self.su).action_urp_create_returns()
            second = self._auto(req)
            self.assertEqual((second.move_ids.product_uom_qty, len(req.return_picking_ids)), (2.0, 2))
            self.assertIs(self._receive(second, self.iu), True)
            self.assertEqual((line.used_qty, req.state, req.can_create_returns), (2.0, "used", False))
            with self.assertRaises(UserError):
                second.with_user(self.iu).action_cancel()                                # standard: done stays done
            self.assertEqual((line.used_qty, line.remaining_qty), (2.0, 0.0))


@tagged("post_install", "-at_install", "urp", "urp_sales_exceptions")
class TestSalesExceptionStates(UrpSalesExceptionCommon):

    def test_z01_pending_rejected_cancelled_block(self):
        so = self._so({"N": 3})
        delivery = self._deliveries(so)
        with freeze_time(T1):
            pending = self._request(so, self.su, {"N": 1})
            pending.action_submit()
            self.assertFalse(pending.return_picking_ids)
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._return(delivery, self.iu, {"N": 1})
            pending.with_user(self.sa).action_reject()
            self.assertEqual((pending.state, pending.rejected_by, pending.return_picking_ids),
                             ("rejected", self.sa, self.env["stock.picking"]))
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._return(delivery, self.iu, {"N": 1})
            approved = self._request(so, self.su, {"N": 1})
            approved.action_submit()
            approved.with_user(self.sa).action_approve()
            ret = self._auto(approved)
            approved.with_user(self.su).action_cancel()                                  # its open return goes with it
            self.assertEqual((approved.state, ret.state), ("cancelled", "cancel"))
            forged = self._return_after_cancel(so)
            wiz = self._wizard(self._validate(forged, self.iu), self.iu)
            self.assertEqual(wiz.line_ids.kind, "non_returnable", "a cancelled approval is unusable")
            with self.assertRaises(UserError):
                forged.move_ids.with_user(self.iu)._action_done()

    def _return_after_cancel(self, so):
        origin = self._deliveries(so).move_ids
        ret = self.env["stock.picking"].with_user(self.ia).create({
            "picking_type_id": self.warehouse.in_type_id.id, "location_id": self.partner.property_stock_customer.id,
            "location_dest_id": self.warehouse.lot_stock_id.id,
            "move_ids": [Command.create({"product_id": origin.product_id.id, "product_uom_qty": 1,
                                         "origin_returned_move_id": origin.id,
                                         "location_id": self.partner.property_stock_customer.id,
                                         "location_dest_id": self.warehouse.lot_stock_id.id})]})
        ret.action_confirm()
        ret.move_ids.write({"quantity": 1, "picked": True})
        return ret
