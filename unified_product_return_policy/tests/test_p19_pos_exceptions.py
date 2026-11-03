"""POS return exceptions: Non-Returnable Exception on the receipt, Expired Return
Period Override, Standalone Refund Exception - all by the POS Administrator (re-authentication,
reason, one-time token bound to company, session, order, lines and quantities) - and POS
ship-later deliveries returned from Inventory with a request approved by the POS Administrator."""
import uuid

from freezegun import freeze_time

from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import Form, tagged
from odoo.addons.point_of_sale.tests.test_frontend import TestPointOfSaleHttpCommon

from .test_p15_pos_direct import OCT5, OCT12, SALE, UrpPosDirectCommon


@tagged("post_install", "-at_install", "urp", "urp_pos_exceptions")
class TestPosExceptions(UrpPosDirectCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tmpl = cls.env["product.template"].create({"name": "PX N", "is_storable": True, "available_in_pos": True,
                                                   "list_price": 10, "taxes_id": [(5, 0)]})
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 0})
        cls.prods["N"] = tmpl.product_variant_id
        cls.code_of[tmpl.product_variant_id] = "N"
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        g = lambda xid: cls.env.ref(xid).id                       # noqa: E731
        cls.pos_admin_only = Users.create({"name": "PA only", "login": "urp_px_pa", "password": "Px-Pa-Pass-123!",
                                           "group_ids": [(6, 0, [g("point_of_sale.group_pos_manager")])],
                                           "company_ids": [(6, 0, cls.company.ids)], "company_id": cls.company.id})
        cls.stock_admin = Users.create({"name": "IA", "login": "urp_px_ia", "group_ids": [(6, 0, [g("stock.group_stock_manager")])],
                                        "company_ids": [(6, 0, cls.company.ids)], "company_id": cls.company.id})
        cls.sales_admin = Users.create({"name": "SA", "login": "urp_px_sa", "group_ids": [(6, 0, [g("sales_team.group_sale_manager")])],
                                        "company_ids": [(6, 0, cls.company.ids)], "company_id": cls.company.id})

    def _refund_draft(self, order, qtys):
        ref = self.env["pos.order"].browse(order.refund()["res_id"])
        for line in ref.lines:
            code = self.code_of.get(line.product_id)
            if code in qtys:
                line.qty = -qtys[code]
            else:
                line.unlink()
        ref.lines._onchange_amount_line_all()
        ref._compute_prices()
        return ref

    def _tok_lines(self, ref, codes):
        return [l for l in self._lines(ref) if self.code_of[self.env["product.product"].browse(l["product_id"])] in codes]


    def test_p01_wrong_password_and_other_session(self):
        with freeze_time(SALE):
            order = self._order({"N": 1})
        with freeze_time(OCT12):
            ref = self._refund_draft(order, {"N": 1})
            self.assertEqual(self._token(self._lines(ref), ref.uuid, password="wrong")["ok"], False)
            journal = self.env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", self.company.id)], limit=1)
            pm = self.env["pos.payment.method"].create({"name": "PX bank", "type": "bank", "journal_id": journal.id})
            other = self.env["pos.config"].create({"name": "PX other", "payment_method_ids": [(6, 0, pm.ids)]})
            other.open_ui()
            other_sess = other.current_session_id
            other_sess.set_opening_control(0, "")
            tok = self._token(self._lines(ref), ref.uuid, session=other_sess)
            self.assertTrue(tok["ok"], tok)
            ref.urp_override_token = tok["token"]
            with self.assertRaisesRegex(UserError, "issued for another order"):
                self._pay(ref, ref.amount_total)


    def test_p02_linked_token_never_covers_a_standalone_refund(self):
        with freeze_time(SALE):
            order = self._order({"N": 1})
        with freeze_time(OCT12):
            uid = str(uuid.uuid4())
            n_line = order.lines
            tok = self._token([{"product_id": self.prods["N"].id, "refunded_line_id": n_line.id, "qty": 1}], uid)
            sess = self._session()
            o = self.env["pos.order"].create({
                "company_id": self.company.id, "session_id": sess.id, "uuid": uid,
                "lines": [Command.create({"name": "S", "product_id": self.prods["N"].id, "price_unit": 10.0, "qty": -1,
                                         "price_subtotal": -10.0, "price_subtotal_incl": -10.0, "discount": 0})],
                "amount_tax": 0, "amount_total": -10.0, "amount_paid": 0, "amount_return": 0,
                "urp_override_token": tok["token"]})
            with self.assertRaisesRegex(UserError, "does not cover"):
                self._pay(o, -10.0)


    def test_p03_mixed_refund_typed_per_line(self):
        with freeze_time(SALE):
            order = self._order({"A": 2, "N": 2, "B": 2})                 # A 7 days, B 15 days, N 0 days
        with freeze_time(OCT12):                                           # A expired, B within, N non-returnable
            ref = self._refund_draft(order, {"A": 1, "N": 1, "B": 1})
            with self.assertRaises(UserError) as err:
                self._pay(ref, ref.amount_total)
            self.assertIn("non-returnable", str(err.exception))
            self.assertIn("return period ended", str(err.exception))
            ref.urp_override_token = self._token(self._tok_lines(ref, {"N"}), ref.uuid)["token"]
            with self.assertRaisesRegex(UserError, "does not cover"):
                self._pay(ref, ref.amount_total)                           # A is not covered by N's approval
            ref.urp_override_token = self._token(self._tok_lines(ref, {"N", "A"}), ref.uuid)["token"]
            self.assertEqual(self._pay(ref, ref.amount_total).state, "paid")
            ov = ref.urp_override_id
            self.assertEqual({l.product_id: l.exception_type for l in ov.line_ids},
                             {self.prods["A"]: "expired", self.prods["N"]: "non_returnable"})
            self.assertEqual(ov.kind, "mixed")
            self.assertTrue(all(l.refunded_orderline_id for l in ref.lines), "the refund stays linked to the receipt")
            form = ov.get_view(self.env.ref("unified_product_return_policy.urp_return_override_view_form").id, "form")["arch"]
            self.assertIn('name="exception_type"', form)

    # ship later executed from Inventory: the POS Administrator approves, Inventory executes
    def test_p04_ship_later_inventory_request_approved_by_pos_admin(self):
        with freeze_time(SALE):
            order = self._sale(3, ship_later=True, product=self.prods["A"])
            self._deliver(order, 3)
            dpick = order.picking_ids.filtered(lambda p: p.state == "done")
        with freeze_time(OCT12):
            with self.assertRaisesRegex(UserError, "approved return exception is required"):
                dpick.with_user(self.wh_user)._create_return()
            action = order.with_user(self.cashier).action_urp_request_exception()
            req = self.env["urp.return.exception"].with_user(self.cashier).browse(action["res_id"])
            self.assertEqual((req.source, req.line_ids.exception_type), ("pos", "expired"))
            req.line_ids.requested_qty = 2
            req.reason = "Late delivery complaint"
            req.action_submit()
            for user in (self.cashier, self.stock_admin, self.sales_admin):
                with self.assertRaises(AccessError, msg=user.login):
                    self.env["urp.return.exception"].with_user(user).browse(req.id).action_approve()
            req.with_user(self.pos_admin_only).action_approve()         # POS admin without stock rights
            self.assertEqual((req.state, req.sudo().override_id.channel), ("approved", "pos"))
            ret = dpick.with_user(self.wh_user)._create_return()
            ret.move_ids.write({"product_uom_qty": 2, "quantity": 2, "picked": True})
            self.assertIs(ret.with_user(self.wh_user).button_validate(), True)
            self.assertEqual(req.state, "used")
            # double return prevented: the POS cannot refund those 2 delivered units again
            with self.assertRaisesRegex(UserError, "already returned through Inventory"):
                self._pos_refund(order, {"A": 2}, token=True)

    def test_p06_cashier_sync_of_a_governed_sale(self):
        """A cashier (POS User, no access to the policy history) synchronises a sale under the policy:
        the policy snapshot is written without an Access Error."""
        sess = self._session()
        uid = str(uuid.uuid4())
        data = {"uuid": uid, "company_id": self.company.id, "session_id": sess.id,
                "date_order": "2026-10-27 08:00:00",
                "lines": [[0, 0, {"product_id": self.prods["A"].id, "full_product_name": "A", "price_unit": 10.0, "qty": 1,
                                  "price_subtotal": 10.0, "price_subtotal_incl": 10.0, "discount": 0}]],
                "payment_ids": [[0, 0, {"amount": 10.0, "payment_method_id": self.cash_payment_method.id}]],
                "amount_paid": 10.0, "amount_total": 10.0, "amount_tax": 0.0, "amount_return": 0.0,
                "to_invoice": False, "state": "paid"}
        self.env["pos.order"].with_user(self.cashier).with_context(current_order_uuid=uid).sync_from_ui([data])
        order = self.env["pos.order"].search([("uuid", "=", uid)])
        self.assertEqual((order.state, order.lines.urp_has_snapshot, order.lines.urp_return_days), ("paid", True, 7))

    # on a refund linked to the receipt (non-returnable when sold)
    def test_p07_linked_exception_authorisation_checks(self):
        company_b = self.env["res.company"].create({"name": "PX other co"})
        self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "PX mgr B", "login": "urp_px_mgr_b", "password": "Px-B-Pass-123!",
            "group_ids": [(6, 0, [self.env.ref("point_of_sale.group_pos_manager").id])],
            "company_ids": [(6, 0, company_b.ids)], "company_id": company_b.id})
        with freeze_time(SALE):
            order1, order2 = self._order({"N": 2}), self._order({"N": 1})
        with freeze_time(OCT12):
            ref = self._refund_draft(order1, {"N": 2})
            lines = self._lines(ref)
            refused = {"reason_required": [dict(reason=""), dict(reason=" \n  ")],
                       "invalid": [dict(login="urp_pos_cash", password="Cash-Pass-123!"),
                                                 dict(login="urp_px_mgr_b", password="Px-B-Pass-123!")]}
            for error, attempts in refused.items():
                for kw in attempts:
                    self.assertEqual(self._token(lines, ref.uuid, **kw), {"ok": False, "error": error}, kw)
            self.assertFalse(self.env["urp.return.override"].search([("pos_order_uuid", "=", ref.uuid)]))
            ref2 = self._refund_draft(order2, {"N": 1})
            ref.urp_override_token = self._token(self._lines(ref2), ref2.uuid)["token"]
            with self.assertRaisesRegex(UserError, "issued for another order"), self.env.cr.savepoint():
                self._pay(ref, ref.amount_total)
            ref.urp_override_token = self._token([dict(l, qty=1) for l in lines], ref.uuid)["token"]
            with self.assertRaisesRegex(UserError, "does not cover"), self.env.cr.savepoint():
                self._pay(ref, ref.amount_total)
            tok = self._token(lines, ref.uuid)["token"]
            ref.urp_override_token = tok
            self.assertEqual(self._pay(ref, ref.amount_total).state, "paid")
            self.assertEqual(ref.lines.refunded_orderline_id, order1.lines, "still linked to the receipt")
            ref2.urp_override_token = tok
            with self.assertRaisesRegex(UserError, "already used"), self.env.cr.savepoint():
                self._pay(ref2, ref2.amount_total)

    # lot-tracked product, non-returnable when sold
    def test_p08_lot_tracked_non_returnable_linked_refund(self):
        tmpl = self.env["product.template"].create({"name": "PX Lot", "is_storable": True, "tracking": "lot",
                                                   "available_in_pos": True, "list_price": 10, "taxes_id": [(5, 0)]})
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 0})
        lot_p = tmpl.product_variant_id
        self.code_of[lot_p] = "L"
        with freeze_time(SALE):
            order = self._order({"L": 1}, {"L": lot_p})
            pick = order.picking_ids
            lot = self.env["stock.lot"].create({"name": "PX-L-1", "product_id": lot_p.id})
            pick.move_ids.quantity = 1
            pick.move_ids.move_line_ids.lot_id = lot
            pick.move_ids.picked = True
            res = pick.button_validate()
            if isinstance(res, dict):
                Form.from_action(self.env, res).save().process()
            self.assertEqual((pick.state, pick.move_line_ids.lot_id), ("done", lot))
        with freeze_time(OCT5):
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._pos_refund(order, {"L": 1})
            with self.assertRaisesRegex(UserError, "POS Refund"):
                pick.with_user(self.wh_user)._create_return()
            ref = self._pos_refund(order, {"L": 1}, token=True)
            self.assertEqual((ref.state, ref.lines.refunded_orderline_id), ("paid", order.lines))
            self.assertEqual(ref.urp_override_id.line_ids.exception_type, "non_returnable")
        self.assertEqual(order.lines.urp_return_days, 0)

    # ship later executed from Inventory: an Inventory Administrator alone never passes the POS policy
    def test_p09_ship_later_inventory_administrator_cannot_bypass(self):
        with freeze_time(SALE):
            order = self._sale(2, ship_later=True, product=self.prods["N"])
            self._deliver(order, 2)
        with freeze_time(OCT5):
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._pick(order).with_user(self.stock_admin)._create_return()
            forged = self._forged_return(order, self.stock_admin, {"N": 1})
            wiz = self._wizard(forged.with_user(self.stock_admin).button_validate(), self.stock_admin)
            self.assertEqual((wiz.line_ids.kind, wiz.can_override), ("non_returnable", False))
            wiz.reason = "try"
            with self.assertRaises(AccessError):
                wiz.action_override()
            with self.assertRaises(UserError):
                forged.move_ids.with_user(self.stock_admin).with_context(
                    urp_internal=True, urp_skip_guard=True, urp_checked=True)._action_done()
            self.assertNotEqual(forged.state, "done")
            forged.action_cancel()
            req = self.env["urp.return.exception"].browse(order.with_user(self.cashier).action_urp_request_exception()["res_id"])
            req.line_ids.requested_qty = 1
            req.reason = "Damaged in delivery"
            req.action_submit()
            with self.assertRaises(AccessError):
                req.with_user(self.stock_admin).action_approve()
            req.with_user(self.pos_admin_only).action_approve()
            ret = self._pick(order).with_user(self.stock_admin)._create_return()
            ret.move_ids.write({"product_uom_qty": 1, "quantity": 1, "picked": True})
            self.assertIs(ret.with_user(self.stock_admin).button_validate(), True)
            self.assertEqual(req.state, "used")
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._pick(order).with_user(self.stock_admin)._create_return()

    def _sync_as_cashier(self, uid, lines, token):
        total = sum(l["qty"] * 10.0 for l in lines)
        data = {"uuid": uid, "company_id": self.company.id, "session_id": self._session().id,
                "date_order": "2026-10-12 09:00:00",
                "lines": [[0, 0, {"product_id": l["product"].id, "full_product_name": l["product"].name, "price_unit": 10.0,
                                  "qty": l["qty"], "refunded_orderline_id": l.get("refunded", False),
                                  "price_subtotal": 10.0 * l["qty"], "price_subtotal_incl": 10.0 * l["qty"], "discount": 0}]
                          for l in lines],
                "payment_ids": [[0, 0, {"amount": total, "payment_method_id": self.cash_payment_method.id}]],
                "amount_paid": total, "amount_total": total, "amount_tax": 0.0, "amount_return": 0.0,
                "to_invoice": False, "state": "paid", "urp_override_token": token}
        self.env["pos.order"].with_user(self.cashier).with_context(current_order_uuid=uid).sync_from_ui([data])
        return self.env["pos.order"].search([("uuid", "=", uid)])

    def test_p10_pos_refunds_need_no_inventory_right_for_the_cashier(self):
        self.assertFalse(self.cashier.has_group("stock.group_stock_user"))
        with freeze_time(SALE):
            order = self._order({"N": 1, "A": 1})
        with freeze_time(OCT12):                                           # N non-returnable, A expired
            uid = str(uuid.uuid4())
            tok = self._token([{"product_id": l.product_id.id, "refunded_line_id": l.id, "qty": 1} for l in order.lines], uid)
            ref = self._sync_as_cashier(uid, [{"product": l.product_id, "qty": -1, "refunded": l.id} for l in order.lines],
                                        tok["token"])
            self.assertEqual((ref.state, ref.lines.refunded_orderline_id), ("paid", order.lines), "linked refund")
            self.assertEqual({l.product_id: l.exception_type for l in ref.urp_override_id.line_ids},
                             {self.prods["N"]: "non_returnable", self.prods["A"]: "expired"})
            self.assertEqual(ref.picking_ids.mapped("state"), ["done"], "stock moved back by the POS")
            uid2 = str(uuid.uuid4())
            tok2 = self._token([{"product_id": self.prods["N"].id, "qty": 1}], uid2)
            st = self._sync_as_cashier(uid2, [{"product": self.prods["N"], "qty": -1}], tok2["token"])
            self.assertEqual((st.state, st.urp_override_id.kind, st.picking_ids.mapped("state")), ("paid", "standalone", ["done"]))

    def _assert_forged_refund_refused(self, lines, product):
        """A refund sent by the device with a wrong receipt line is refused at payment: nothing is
        recorded (no refund order, no payment, no stock move back)."""
        uid = str(uuid.uuid4())
        stock_before = product.qty_available
        with self.assertRaisesRegex(UserError, "does not match the receipt line"), self.env.cr.savepoint():
            self._sync_as_cashier(uid, lines, False)
        product.invalidate_recordset(["qty_available"])
        self.assertFalse(self.env["pos.order"].search([("uuid", "=", uid)]), "no refund order recorded")
        self.assertFalse(self.env["pos.payment"].search([("pos_order_id.uuid", "=", uid)]), "no refund paid")
        self.assertEqual(product.qty_available, stock_before, "no stock moved back")

    def test_p12_refund_linked_to_the_line_of_another_product_refused(self):
        with freeze_time(SALE):
            order = self._order({"A": 1, "N": 1})
        a_line = order.lines.filtered(lambda l: l.product_id == self.prods["A"])
        n_line = order.lines - a_line
        with freeze_time(OCT5):                                            # A within its period, N non-returnable
            # N sent as a refund of the A line (in time, no approval needed for A): refused
            self._assert_forged_refund_refused([{"product": self.prods["N"], "qty": -1, "refunded": a_line.id}],
                                               self.prods["N"])
            # mixed with a genuine line in the same refund: the whole refund is refused
            self._assert_forged_refund_refused([{"product": self.prods["A"], "qty": -1, "refunded": a_line.id},
                                                {"product": self.prods["N"], "qty": -1, "refunded": a_line.id}],
                                               self.prods["N"])
            # the genuine refund of A still goes through, and N on its own line still needs the approval
            ref = self._sync_as_cashier(str(uuid.uuid4()), [{"product": self.prods["A"], "qty": -1, "refunded": a_line.id}],
                                        False)
            self.assertEqual((ref.state, ref.lines.refunded_orderline_id), ("paid", a_line))
            with self.assertRaisesRegex(UserError, "non-returnable"), self.env.cr.savepoint():
                self._sync_as_cashier(str(uuid.uuid4()), [{"product": self.prods["N"], "qty": -1, "refunded": n_line.id}],
                                      False)

    def test_p13_refund_linked_to_a_refund_line_refused(self):
        with freeze_time(SALE):
            order = self._order({"A": 2})
        with freeze_time(OCT5):
            ref = self._sync_as_cashier(str(uuid.uuid4()), [{"product": self.prods["A"], "qty": -1, "refunded": order.lines.id}],
                                        False)
            self.assertEqual(ref.state, "paid")
            # a refund line is not a sale: it cannot be the receipt line of another refund
            self._assert_forged_refund_refused([{"product": self.prods["A"], "qty": -1, "refunded": ref.lines.id}],
                                               self.prods["A"])

    def test_p14_legacy_sale_link_checked_too(self):
        """A sale made before the policy was enabled keeps the standard refund, but only for its own
        product: the link is checked before the legacy exemption."""
        with freeze_time(SALE):
            self.company.urp_enabled = False
            legacy = self._order({"A": 1, "N": 1})
            self.company.urp_enabled = True
        self.assertFalse(any(legacy.lines.mapped("urp_has_snapshot")))
        a_line = legacy.lines.filtered(lambda l: l.product_id == self.prods["A"])
        n_line = legacy.lines - a_line
        with freeze_time(OCT12):
            self._assert_forged_refund_refused([{"product": self.prods["N"], "qty": -1, "refunded": a_line.id}],
                                               self.prods["N"])
            ref = self._sync_as_cashier(str(uuid.uuid4()), [{"product": self.prods["N"], "qty": -1, "refunded": n_line.id}],
                                        False)
            self.assertEqual((ref.state, ref.lines.refunded_orderline_id), ("paid", n_line), "legacy sale: standard refund")

    def test_p11_ship_later_exception_executed_by_inventory_only(self):
        with freeze_time(SALE):
            order = self._sale(2, ship_later=True, product=self.prods["N"])
            self._deliver(order, 2)
        with freeze_time(OCT5):
            req = self.env["urp.return.exception"].browse(order.with_user(self.cashier).action_urp_request_exception()["res_id"])
            req.line_ids.requested_qty = 1
            req.reason = "Damaged in delivery"
            req.action_submit()
            req.with_user(self.pos_admin_only).action_approve()
            ret = self._forged_return(order, self.wh_user, {"N": 1})
            # a POS User may write pickings in standard Odoo: not enough to execute the exception
            with self.assertRaisesRegex(AccessError, "requires Inventory rights"), self.env.cr.savepoint():
                ret.with_user(self.cashier).button_validate()
            with self.assertRaisesRegex(AccessError, "requires Inventory rights"), self.env.cr.savepoint():
                ret.move_ids.with_user(self.cashier).sudo().with_context(urp_internal=True, urp_checked=True)._action_done()
            self.assertEqual((ret.state != "done", req.state), (True, "approved"))
            self.assertIs(ret.with_user(self.wh_user).button_validate(), True)
            self.assertEqual(req.state, "used")

    def test_p05_sales_request_impossible_for_pos_delivery(self):
        with freeze_time(SALE):
            order = self._order({"N": 1})
        with self.assertRaisesRegex(UserError, "POS Refund on the receipt"):
            order.with_user(self.cashier).action_urp_request_exception()


class UrpNonReturnableUiCommon(TestPointOfSaleHttpCommon):

    def _run(self):
        company = self.main_pos_config.company_id
        company.write({"urp_tz": "Asia/Riyadh", "urp_default_days": 14, "urp_enabled": True})
        p = self.env["product.product"].create({"name": "URP NoReturn", "is_storable": True, "available_in_pos": True,
                                                "list_price": 10.0, "taxes_id": False})
        p.product_tmpl_id.write({"use_custom_return_period": True, "custom_return_period_days": 0})
        self.main_pos_config.with_user(self.pos_user).open_ui()
        self.start_pos_tour("UrpNonReturnableLinkedRefund", login="pos_user")
        orders = self.env["pos.order"].search([("session_id", "=", self.main_pos_config.current_session_id.id)], order="id")
        sale, refund = orders.filtered(lambda o: o.amount_total > 0), orders.filtered(lambda o: o.amount_total < 0)
        self.assertEqual((sale.lines.urp_return_days, refund.state), (0, "paid"))
        self.assertEqual(refund.lines.refunded_orderline_id, sale.lines, "linked refund, never standalone")
        ov = refund.urp_override_id
        self.assertEqual((ov.user_id, ov.reason, ov.line_ids.exception_type), (self.pos_admin, "Defective, refund approved",
                                                                              "non_returnable"))


@tagged("post_install", "-at_install", "urp", "urp_pos_exceptions")
class TestPosExceptionUi(UrpNonReturnableUiCommon):

    def test_u02_non_returnable_linked_refund_in_ui(self):
        self._run()


@tagged("post_install", "-at_install", "urp", "urp_pos_exceptions")
class TestPosExceptionUiMobile(UrpNonReturnableUiCommon):
    browser_size = "375x667"
    touch_enabled = True

    def test_u03_non_returnable_linked_refund_in_mobile_ui(self):
        self._run()
