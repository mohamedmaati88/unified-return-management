"""Standalone Refund administrative exception + override audit visibility.

A refund WITHOUT the original receipt has no historical policy to apply: the cashier is always
refused and a POS Administrator may approve it as an administrative exception (existing
re-authentication and one-time token, mandatory reason), even when the product is non-returnable
today. That exception never applies to a refund linked to a receipt, where the policy frozen at
the sale decides (a non-returnable sale stays so, whoever approves)."""
import copy
import uuid

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import tagged

from .test_p15_pos_direct import OCT5, OCT12, SALE, UrpPosDirectCommon


@tagged("post_install", "-at_install", "urp", "urp_standalone_exception")
class TestStandaloneException(UrpPosDirectCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tmpl = cls.env["product.template"].create({"name": "Exc N", "is_storable": True, "available_in_pos": True,
                                                   "list_price": 10, "taxes_id": [(5, 0)]})
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 0})
        categ = cls.env["product.category"].create({"name": "Exc zero categ", "use_custom_return_period": True,
                                                    "custom_return_period_days": 0})
        ktmpl = cls.env["product.template"].create({"name": "Exc K", "is_storable": True, "available_in_pos": True,
                                                    "list_price": 10, "taxes_id": [(5, 0)], "categ_id": categ.id})
        for code, t in (("N", tmpl), ("K", ktmpl)):
            cls.prods[code] = t.product_variant_id
            cls.code_of[t.product_variant_id] = code
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.company_b = cls.env["res.company"].create({"name": "Exc other company"})
        cls.manager_b = Users.create({"name": "Mgr other co", "login": "urp_exc_mgr_b", "password": "Mgr-B-Pass-123!",
                                      "group_ids": [(6, 0, [cls.env.ref("point_of_sale.group_pos_manager").id])],
                                      "company_ids": [(6, 0, cls.company_b.ids)], "company_id": cls.company_b.id})
        cls.plain_user = Users.create({"name": "Plain", "login": "urp_exc_plain", "password": "Plain-Pass-123!",
                                       "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])],
                                       "company_ids": [(6, 0, cls.company.ids)], "company_id": cls.company.id})

    # ------------------------------------------------------------------ helpers
    def _standalone(self, code, qty=1, token=None, order_uuid=None):
        sess = self._session()
        o = self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": sess.id, **({"uuid": order_uuid} if order_uuid else {}),
            "lines": [Command.create({"name": "S", "product_id": self.prods[code].id, "price_unit": 10.0, "qty": -qty,
                                     "price_subtotal": -10.0 * qty, "price_subtotal_incl": -10.0 * qty, "discount": 0})],
            "amount_tax": 0, "amount_total": -10.0 * qty, "amount_paid": 0, "amount_return": 0,
            "urp_override_token": token})
        return self._pay(o, -10.0 * qty)

    def _tok(self, code, qty, order_uuid, reason="Customer lost original receipt", refunded_line=False, **kw):
        return self._token([{"product_id": self.prods[code].id, "qty": qty, "refunded_line_id": refunded_line or False}],
                           order_uuid, reason=reason, **kw)

    def _override(self, order_uuid):
        return self.env["urp.return.override"].search([("pos_order_uuid", "=", order_uuid)])

    # ------------------------------------------------------------------ 1-4: who may refund without receipt
    def test_e01_cashier_refused_manager_exception_accepted_audited(self):
        with self.assertRaisesRegex(UserError, "currently non-returnable"):
            self._standalone("N")                                            # no approval
        uid = str(uuid.uuid4())
        tok = self._tok("N", 1, uid, reason="Defective product")
        self.assertTrue(tok["ok"], tok)
        ref = self._standalone("N", token=tok["token"], order_uuid=uid)    # approved, with reason
        self.assertEqual(ref.state, "paid")
        ov = ref.urp_override_id
        self.assertEqual((ov.user_id, ov.reason, ov.channel, ov.company_id, ov.pos_session_id, ov.pos_order_id),
                         (self.manager, "Defective product", "pos", self.company, ref.session_id, ref))
        self.assertEqual([(l.product_id, l.qty, l.refunded_line_id) for l in ov.line_ids],
                         [(self.prods["N"], 1.0, self.env["pos.order.line"])])
        self.assertTrue(ov.create_date and ov.consumed_at)
        self.assertEqual((ov.kind, ov.usage, ov.pos_amount_total), ("standalone", "used", -10.0))
        self.assertFalse(ref.urp_override_token, "the token is never stored")
        self.assertFalse(ref.lines.urp_has_snapshot, "a refund never creates a sale snapshot")

    def test_e02_category_zero_and_positive_products(self):
        uid = str(uuid.uuid4())
        self.assertEqual(self._standalone("K", token=self._tok("K", 1, uid)["token"], order_uuid=uid).state, "paid")
        with self.assertRaisesRegex(UserError, "requires a POS Administrator"):
            self._standalone("A")

    # ------------------------------------------------------------------ 5-8: authorisation itself
    def test_e03_reason_and_administrator_checks(self):
        uid = str(uuid.uuid4())
        self.assertEqual(self._tok("N", 1, uid, reason=""), {"ok": False, "error": "reason_required"})
        self.assertEqual(self._tok("N", 1, uid, reason="   \n "), {"ok": False, "error": "reason_required"})
        res = self._tok("N", 1, uid, login="urp_exc_mgr_b", password="Mgr-B-Pass-123!")
        self.assertEqual(res, {"ok": False, "error": "invalid"})
        res = self._tok("N", 1, uid, login="urp_pos_cash", password="Cash-Pass-123!")
        self.assertEqual(res, {"ok": False, "error": "invalid"})
        self.assertFalse(self._override(uid), "no authorisation was recorded")
        with self.assertRaises(UserError):
            self._standalone("N", order_uuid=uid)

    # ------------------------------------------------------------------ 9-11: one token, one order, its quantities
    def test_e04_token_bound_to_order_quantity_and_single_use(self):
        uid_a, uid_b = str(uuid.uuid4()), str(uuid.uuid4())
        tok_a = self._tok("N", 1, uid_a)["token"]
        with self.assertRaisesRegex(UserError, "invalid, expired, already used or issued for another order"):
            self._standalone("N", token=tok_a, order_uuid=uid_b)
        uid_c = str(uuid.uuid4())
        tok_c = self._tok("N", 1, uid_c)["token"]
        with self.assertRaisesRegex(UserError, "does not cover"):
            self._standalone("N", qty=2, token=tok_c, order_uuid=uid_c)
        self.assertEqual(self._standalone("N", token=tok_a, order_uuid=uid_a).state, "paid")
        uid_d = str(uuid.uuid4())
        with self.assertRaisesRegex(UserError, "invalid, expired, already used"):
            self._standalone("N", token=tok_a, order_uuid=uid_d)

    # ------------------------------------------------------------------ 12-14: linked refunds keep the history
    def test_e05_standalone_token_never_covers_a_receipt_line(self):
        with freeze_time(SALE):
            sold = self._order({"N": 1, "A": 1})
        with freeze_time(OCT12):                                           # A (7 days) expired, N non-returnable
            n_line = sold.lines.filtered(lambda l: l.product_id == self.prods["N"])
            # a standalone authorisation (no original line) for the very products of the receipt
            ref = self.env["pos.order"].browse(sold.refund()["res_id"])
            ref.lines.filtered(lambda l: l.product_id == self.prods["N"]).unlink()
            ref.lines.qty = -1
            ref.lines._onchange_amount_line_all()
            ref._compute_prices()
            tok = self._tok("A", 1, ref.uuid)
            ref.urp_override_token = tok["token"]
            with self.assertRaisesRegex(UserError, "does not cover"):
                self._pay(ref, ref.amount_total)
            # historical 0 on the receipt: a STANDALONE authorisation is no use; the Non-Returnable
            # Exception must name that very receipt line
            ref2 = self.env["pos.order"].browse(sold.refund()["res_id"])
            ref2.lines.filtered(lambda l: l.product_id == self.prods["A"]).unlink()
            ref2.lines.qty = -1
            ref2.lines._onchange_amount_line_all()
            ref2._compute_prices()
            tok_sa = self._tok("N", 1, ref2.uuid)                                 # standalone token
            ref2.urp_override_token = tok_sa["token"]
            with self.assertRaisesRegex(UserError, "does not cover"):
                self._pay(ref2, ref2.amount_total)
            self.assertFalse(self._override(ref2.uuid).consumed_at, "authorisation not consumed")
            # the same refund order, now with the Non-Returnable Exception for that receipt line
            ref2.urp_override_token = self._tok("N", 1, ref2.uuid, refunded_line=n_line.id)["token"]
            self.assertEqual(self._pay(ref2, ref2.amount_total).state, "paid")
            self.assertEqual(ref2.lines.refunded_orderline_id, n_line, "still a linked refund")
            self.assertEqual(ref2.urp_override_id.line_ids.exception_type, "non_returnable")

    def test_e06_current_policy_changes_do_not_touch_old_receipts(self):
        with freeze_time(SALE):
            sold_a, sold_n = self._order({"A": 2}), self._order({"N": 1})
        self.prods["A"].product_tmpl_id.custom_return_period_days = 0
        self.prods["N"].product_tmpl_id.custom_return_period_days = 14
        with freeze_time(OCT5):
            self.assertEqual(self._pos_refund(sold_a, {"A": 1}).state, "paid", "sold with 7 days")
            with self.assertRaisesRegex(UserError, "non-returnable"):
                self._pos_refund(sold_n, {"N": 1})
            self.assertEqual(self._pos_refund(sold_n, {"N": 1}, token=True).state, "paid", "explicit exception only")
        self.assertEqual(sold_n.lines.urp_return_days, 0, "the snapshot is never rewritten")

    # ------------------------------------------------------------------ 15: repeated synchronisation
    def test_e07_repeated_sync_creates_one_refund(self):
        sess = self._session()
        uid, line_uuid, pay_uuid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        tok = self._tok("N", 1, uid)["token"]
        data = {
            "uuid": uid, "company_id": self.company.id, "session_id": sess.id,
            "date_order": fields.Datetime.to_string(fields.Datetime.now()),
            "lines": [[0, 0, {"uuid": line_uuid, "product_id": self.prods["N"].id, "full_product_name": "N",
                              "price_unit": 10.0, "qty": -1, "price_subtotal": -10.0, "price_subtotal_incl": -10.0,
                              "discount": 0}]],
            "payment_ids": [[0, 0, {"uuid": pay_uuid, "amount": -10.0, "payment_method_id": self.cash_payment_method.id}]],
            "amount_paid": -10.0, "amount_total": -10.0, "amount_tax": 0.0, "amount_return": 0.0,
            "to_invoice": False, "state": "paid", "urp_override_token": tok,
        }
        Order = self.env["pos.order"].with_context(current_order_uuid=uid)
        for _ in range(3):
            Order.sync_from_ui([copy.deepcopy(data)])
        orders = self.env["pos.order"].search([("uuid", "=", uid)])
        self.assertEqual((len(orders), orders.state, len(orders.lines)), (1, "paid", 1))
        self.assertEqual(len(self._override(uid)), 1)
        self.assertEqual(self._override(uid).pos_order_id, orders)
        # a replay that CHANGES the refund (standard Odoo accepts it until the receipt is printed:
        # here -1 -> -5 would refund 5 units approved for 1) is refused; nothing is changed
        changed = copy.deepcopy(data)
        changed["lines"][0][2].update({"qty": -5, "price_subtotal": -50.0, "price_subtotal_incl": -50.0})
        changed["payment_ids"][0][2]["amount"] = -50.0
        changed.update({"amount_paid": -50.0, "amount_total": -50.0})
        extra = copy.deepcopy(data)
        extra["lines"].append([0, 0, dict(extra["lines"][0][2], uuid=str(uuid.uuid4()))])
        extra["payment_ids"][0][2]["amount"] = -20.0
        extra.update({"amount_paid": -20.0, "amount_total": -20.0})
        for payload in (changed, extra):
            with self.assertRaisesRegex(UserError, "already paid"), self.env.cr.savepoint():
                Order.sync_from_ui([payload])
        orders.invalidate_recordset()
        self.assertEqual((orders.lines.mapped("qty"), orders.amount_total, orders.amount_paid), ([-1.0], -10.0, -10.0))
        Order.sync_from_ui([copy.deepcopy(data)])                      # an identical replay is still accepted
        self.assertEqual(orders.lines.mapped("qty"), [-1.0])

    # ------------------------------------------------------------------ 16-18: audit screen and rights
    def test_e08_audit_screen_and_rights(self):
        uid = str(uuid.uuid4())
        ref = self._standalone("N", token=self._tok("N", 1, uid)["token"], order_uuid=uid)
        ov = ref.urp_override_id
        Override = self.env["urp.return.override"]
        form = Override.get_view(self.env.ref("unified_product_return_policy.urp_return_override_view_form").id, "form")["arch"]
        lst = Override.get_view(self.env.ref("unified_product_return_policy.urp_return_override_view_list").id, "list")["arch"]
        for name in ("kind", "user_id", "create_date", "reason", "pos_order_id", "pos_session_id", "company_id",
                     "usage", "consumed_at", "pos_amount_total", "line_ids", "product_id", "qty"):
            self.assertIn(f'name="{name}"', form, name)
        for name in ("create_date", "kind", "user_id", "reason", "pos_order_id", "usage", "consumed_at"):
            self.assertIn(f'name="{name}"', lst, name)
        for user in (self.cashier, self.manager):
            with self.assertRaises(UserError):
                ov.with_user(user).write({"reason": "changed"})
            with self.assertRaises((AccessError, UserError)):
                ov.line_ids.with_user(user).write({"qty": 5})
        with self.assertRaises(UserError):
            ov.write({"reason": "changed by admin"})
        self.assertEqual(ov.reason, "Customer lost original receipt")
        with self.assertRaises(AccessError):
            ov.with_user(self.plain_user).read(["reason"])
        with self.assertRaises(AccessError):
            Override.with_user(self.plain_user).search([])

    def test_e09_period_override_is_typed_apart(self):
        with freeze_time(SALE):
            sold = self._order({"A": 1})
        with freeze_time(OCT12):
            ref = self._pos_refund(sold, {"A": 1}, token=True)
        self.assertEqual((ref.urp_override_id.kind, ref.urp_override_id.line_ids.refunded_line_id), ("expired", sold.lines))
