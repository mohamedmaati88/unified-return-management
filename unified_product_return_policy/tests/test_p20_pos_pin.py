"""POS approval by a POS Manager employee with a separate approval PIN.

The standard POS login PIN cannot prove anything to the server (clear text in the database,
unsalted SHA-1 of every employee's PIN sent to each POS device), so the approval PIN is a
separate secret: salted hash, server-only, never loaded in the POS, never equal to a login PIN.
POS Administrator only -> Odoo account; POS Manager employee (with or without an administrator
user) -> approval PIN + reason; cashier -> refused."""
import re
import uuid
from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import tagged
from odoo.addons.point_of_sale.tests.test_frontend import TestPointOfSaleHttpCommon

from ..models import urp_approval_pin
from ..models.urp_approval_pin import URP_PIN_CTX
from .test_p15_pos_direct import OCT5, OCT12, SALE, UrpPosDirectCommon


def _set_pin(employee, pin):
    employee.sudo().urp_approval_pin_hash = URP_PIN_CTX.hash(pin)


@tagged("post_install", "-at_install", "urp", "urp_pos_pin")
class TestPosApprovalPin(UrpPosDirectCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tmpl = cls.env["product.template"].create({"name": "PIN N", "is_storable": True, "available_in_pos": True,
                                                   "list_price": 10, "taxes_id": [(5, 0)]})
        tmpl.write({"use_custom_return_period": True, "custom_return_period_days": 0})
        cls.prods["N"] = tmpl.product_variant_id
        cls.code_of[tmpl.product_variant_id] = "N"
        Emp = cls.env["hr.employee"].sudo()                # test data only (HR rights)
        cls.mgr_emp = Emp.create({"name": "PIN Manager", "company_id": cls.company.id, "pin": "135724"})
        cls.admin_emp = cls.manager.sudo().employee_id or Emp.create({"name": "PIN Admin", "user_id": cls.manager.id,
                                                               "company_id": cls.company.id})
        cls.admin_emp.write({"pin": "246813", "company_id": cls.company.id})
        cls.cash_emp = Emp.create({"name": "PIN Cashier", "company_id": cls.company.id, "pin": "258036"})
        cls.pos_config_usd.write({
            "module_pos_hr": True,
            "manager_employee_ids": [(6, 0, cls.mgr_emp.ids)],
            "cashier_employee_ids": [(6, 0, (cls.cash_emp | cls.admin_emp).ids)],
        })
        _set_pin(cls.mgr_emp, "482613")
        _set_pin(cls.admin_emp, "593724")
        _set_pin(cls.cash_emp, "604835")                    # a cashier with an approval PIN: still no manager
        cls.company_b = cls.env["res.company"].create({"name": "PIN other co"})
        cls.emp_b = Emp.create({"name": "PIN Manager B", "company_id": cls.company_b.id})
        _set_pin(cls.emp_b, "715946")
        cls.emp_other_pos = Emp.create({"name": "PIN Manager other POS", "company_id": cls.company.id})
        _set_pin(cls.emp_other_pos, "826457")
        journal = cls.env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", cls.company.id)], limit=1)
        pm = cls.env["pos.payment.method"].create({"name": "PIN bank", "type": "bank", "journal_id": journal.id})
        cls.other_config = cls.env["pos.config"].create({"name": "PIN other POS", "payment_method_ids": [(6, 0, pm.ids)],
                                                         "module_pos_hr": True,
                                                         "manager_employee_ids": [(6, 0, cls.emp_other_pos.ids)]})
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.stock_admin = Users.create({"name": "PIN IA", "login": "urp_pin_ia",
                                        "group_ids": [(6, 0, [cls.env.ref("stock.group_stock_manager").id])],
                                        "company_ids": [(6, 0, cls.company.ids)], "company_id": cls.company.id})

    # ------------------------------------------------------------------ helpers
    def _pin(self, lines, order_uuid, pin="482613", reason="Damaged on arrival", session=None, **ctx):
        sess = (session or self._session()).with_user(self.cashier).with_context(**ctx)
        return sess.urp_request_override_pin(pin, reason, lines, order_uuid)

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

    def _clear_attempts(self):
        self.env["urp.auth.attempt"].sudo().search([]).unlink()

    def _standalone(self, code, token, order_uuid):
        o = self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": self._session().id, "uuid": order_uuid,
            "lines": [Command.create({"name": "S", "product_id": self.prods[code].id, "price_unit": 10.0, "qty": -1,
                                     "price_subtotal": -10.0, "price_subtotal_incl": -10.0, "discount": 0})],
            "amount_tax": 0, "amount_total": -10.0, "amount_paid": 0, "amount_return": 0, "urp_override_token": token})
        return self._pay(o, -10.0)

    # ------------------------------------------------------------------ 1. 2. 3. 20.
    def test_k01_three_ways_of_approving(self):
        with freeze_time(SALE):
            order = self._order({"N": 3})
        with freeze_time(OCT5):
            # POS Administrator only (Odoo account + reason)
            ref1 = self._refund_draft(order, {"N": 1})
            ref1.urp_override_token = self._token(self._lines(ref1), ref1.uuid)["token"]
            self._pay(ref1, ref1.amount_total)
            # POS Manager employee only (approval PIN + reason)
            ref2 = self._refund_draft(order, {"N": 1})
            res = self._pin(self._lines(ref2), ref2.uuid, pin="482613", reason="Broken seal")
            self.assertEqual((res["ok"], res["manager"]), (True, "PIN Manager"))
            ref2.urp_override_token = res["token"]
            self._pay(ref2, ref2.amount_total)
            # POS Administrator who is also a POS employee: approval PIN + reason only
            ref3 = self._refund_draft(order, {"N": 1})
            ref3.urp_override_token = self._pin(self._lines(ref3), ref3.uuid, pin="593724")["token"]
            self._pay(ref3, ref3.amount_total)
        audit = [(r.urp_override_id.approver_role, r.urp_override_id.auth_method, r.urp_override_id.user_id,
                  r.urp_override_id.approver_employee_id) for r in (ref1, ref2, ref3)]
        self.assertEqual(audit, [
            ("pos_admin", "password", self.manager, self.env["hr.employee"]),
            ("pos_manager_employee", "pin", self.env["res.users"], self.mgr_emp),
            ("pos_manager_employee", "pin", self.manager, self.admin_emp),
        ])
        for ref in (ref1, ref2, ref3):                                                   # stays linked
            ov = ref.urp_override_id
            self.assertEqual((ref.state, ref.lines.refunded_orderline_id, ov.line_ids.exception_type, ov.pos_session_id),
                             ("paid", order.lines, "non_returnable", ref.session_id))
        self.assertEqual(ref2.urp_override_id.reason, "Broken seal")
        self.assertEqual(ref2.urp_override_id.approver_config_id, self.pos_config_usd)
        self.assertEqual(order.lines.urp_return_days, 0, "16. snapshot unchanged")

    # ------------------------------------------------------------------ 5. 6. 7. 8. 9. 10.
    def test_k02_refused(self):
        with freeze_time(SALE):
            order = self._order({"N": 1})
        with freeze_time(OCT5):
            ref = self._refund_draft(order, {"N": 1})
            lines = self._lines(ref)
            for label, pin in (("wrong PIN", "000000"), ("not digits", "12ab56"), ("too short (5 digits)", "48261"),
                               ("too long", "482613001"),
                               ("cashier's login PIN", "258036"), ("cashier's approval PIN", "604835"),
                               ("manager's login PIN", "135724"), ("other company", "715946"),
                               ("manager of another POS", "826457")):
                self._clear_attempts()
                self.assertEqual(self._pin(lines, ref.uuid, pin=pin), {"ok": False, "error": "invalid_pin"}, label)
            self._clear_attempts()
            for reason in ("", "   \n "):
                self.assertEqual(self._pin(lines, ref.uuid, reason=reason), {"ok": False, "error": "reason_required"})
            # the browser cannot name the employee or the role
            forged = self._pin(lines, ref.uuid, pin="604835", employee_id=self.mgr_emp.id, is_manager=True,
                               current_cashier_id=self.mgr_emp.id)
            self.assertEqual(forged, {"ok": False, "error": "invalid_pin"})
            with self.assertRaises(TypeError):
                self._session().with_user(self.cashier).urp_request_override_pin(
                    "604835", "x", lines, ref.uuid, employee_id=self.mgr_emp.id)
            self.assertFalse(self.env["urp.return.override"].search([("pos_order_uuid", "=", ref.uuid)]))
            with self.assertRaisesRegex(UserError, "non-returnable"):                    # cashier alone
                self._pay(ref, ref.amount_total)
            self.pos_config_usd.module_pos_hr = False
            self.assertEqual(self._pin(lines, ref.uuid), {"ok": False, "error": "pin_unavailable"})

    def test_k03_lockout_and_no_secret_kept(self):
        with freeze_time(SALE):
            order = self._order({"N": 1})
        # real clock here: attempts are dated by the database (non-returnable: any date needs approval)
        ref = self._refund_draft(order, {"N": 1})
        lines = self._lines(ref)
        for _ in range(5):
            self.assertEqual(self._pin(lines, ref.uuid, pin="111111")["error"], "invalid_pin")
        self.assertEqual(self._pin(lines, ref.uuid, pin="482613")["error"], "locked", "even the right PIN")
        self.other_config.open_ui()
        other_sess = self.other_config.current_session_id
        other_sess.set_opening_control(0, "")
        self.assertTrue(self._pin(lines, ref.uuid, pin="826457", session=other_sess)["ok"], "the lock is per POS")
        acc = self._token(lines, ref.uuid)
        self.assertTrue(acc["ok"], acc)                                                  # Odoo account path still open
        self.env.cr.execute("UPDATE urp_auth_attempt SET create_date = create_date - interval '16 minutes'")
        self.assertTrue(self._pin(lines, ref.uuid, pin="482613")["ok"], "lock lifted after 15 minutes")
        stored = self.mgr_emp.sudo().urp_approval_pin_hash
        self.assertTrue(stored.startswith("$pbkdf2-sha512$") and "482613" not in stored)
        self.assertNotIn("pin", self.env["urp.auth.attempt"]._fields)
        for model in ("urp.auth.attempt", "urp.return.override", "urp.return.override.line"):
            for rec in self.env[model].sudo().search([]):
                self.assertNotIn("482613", str(rec.read()[0]), model)
        with self.assertRaises(AccessError):
            self.mgr_emp.with_user(self.manager).read(["urp_approval_pin_hash"])
        self.assertNotIn("urp_approval_pin_hash", self.env["hr.employee"]._load_pos_data_fields(self.pos_config_usd))

    # ------------------------------------------------------------------ 11. 12. 13. 14.
    def test_k04_token_binding(self):
        with freeze_time(SALE):
            order = self._order({"N": 2})
        with freeze_time(OCT5):
            ref = self._refund_draft(order, {"N": 2})
            lines = self._lines(ref)
            self.other_config.open_ui()
            other_sess = self.other_config.current_session_id
            other_sess.set_opening_control(0, "")
            res = self._pin(lines, ref.uuid, pin="826457", session=other_sess)            # manager of the other POS
            self.assertTrue(res["ok"], res)
            ref.urp_override_token = res["token"]
            with self.assertRaisesRegex(UserError, "issued for another order"), self.env.cr.savepoint():
                self._pay(ref, ref.amount_total)                                         # other session
            ref.urp_override_token = self._pin([dict(l, qty=1) for l in lines], ref.uuid)["token"]
            with self.assertRaisesRegex(UserError, "does not cover"), self.env.cr.savepoint():
                self._pay(ref, ref.amount_total)                                         # larger quantity
            tok = self._pin(lines, ref.uuid)["token"]
            ref.urp_override_token = tok
            self._pay(ref, ref.amount_total)
            uid = str(uuid.uuid4())
            with self.assertRaisesRegex(UserError, "already used"), self.env.cr.savepoint():
                self._standalone("N", tok, uid)                                          # reuse
            # a linked PIN approval never covers a standalone refund, and the other way round
            uid2 = str(uuid.uuid4())
            linked_tok = self._pin([{"product_id": self.prods["N"].id, "refunded_line_id": order.lines.id, "qty": 1}],
                                   uid2)["token"]
            with self.assertRaisesRegex(UserError, "does not cover"), self.env.cr.savepoint():
                self._standalone("N", linked_tok, uid2)

    # ------------------------------------------------------------------ 15. 16. 17. 18.
    def test_k05_expired_standalone_and_pos_direct(self):
        with freeze_time(SALE):
            order = self._order({"A": 1})
        with freeze_time(OCT12):                                                        # A: 7 days, ended
            ref = self._refund_draft(order, {"A": 1})
            ref.urp_override_token = self._pin(self._lines(ref), ref.uuid)["token"]
            self.assertEqual(self._pay(ref, ref.amount_total).urp_override_id.line_ids.exception_type, "expired")
            uid = str(uuid.uuid4())
            tok = self._pin([{"product_id": self.prods["N"].id, "qty": 1}], uid)["token"]
            st = self._standalone("N", tok, uid)
            self.assertEqual((st.state, st.urp_override_id.kind, st.urp_override_id.auth_method),
                             ("paid", "standalone", "pin"))
            with self.assertRaisesRegex(UserError, "POS Refund"):
                self._pick(order).with_user(self.wh_user)._create_return()

    # ------------------------------------------------------------------ 19.
    def test_k06_ship_later_request_approved_with_pin(self):
        with freeze_time(SALE):
            order = self._sale(2, ship_later=True, product=self.prods["N"])
            self._deliver(order, 2)
        with freeze_time(OCT5):
            Req = self.env["urp.return.exception"].with_user(self.cashier)
            req = Req.browse(order.with_user(self.cashier).action_urp_request_exception()["res_id"])
            req.line_ids.requested_qty = 1
            req.reason = "Damaged in delivery"
            req.action_submit()
            self.assertTrue(req.can_approve_with_pin)
            Wiz = self.env["urp.exception.pin.wizard"].with_user(self.cashier)
            wrong = Wiz.create({"exception_id": req.id, "pin": "604835"})                  # the cashier's own PIN
            self.assertEqual((wrong.verified, req.state), (False, "pending"))
            self.assertIn("refused", wrong.error)
            self.assertEqual(wrong.action_approve()["res_model"], "urp.exception.pin.wizard", "dialog stays open")
            ok = Wiz.create({"exception_id": req.id, "pin": "482613"})
            self.assertEqual(ok.action_approve(), {"type": "ir.actions.act_window_close"})
            ov = req.sudo().override_id
            self.assertEqual((req.state, req.approved_by_employee_id, ov.approver_role, ov.auth_method, ov.channel),
                             ("approved", self.mgr_emp, "pos_manager_employee", "pin", "pos"))
            ret = self._pick(order).with_user(self.wh_user)._create_return()
            ret.move_ids.write({"product_uom_qty": 1, "quantity": 1, "picked": True})
            with self.assertRaisesRegex(AccessError, "requires Inventory rights"), self.env.cr.savepoint():
                ret.with_user(self.cashier).button_validate()                            # approval gives no stock right
            self.assertIs(ret.with_user(self.wh_user).button_validate(), True)
            self.assertEqual(req.state, "used")

    # ------------------------------------------------------------------ issuing the approval PIN (generated by the server)
    def _issue(self, employee, user=None):
        """Issue a PIN as ``user`` (POS Administrator); returns (the PIN shown once, the action)."""
        wiz = self.env["urp.approval.pin.wizard"].with_user(user or self.manager).create({"employee_id": employee.id})
        action = wiz.action_set()
        self.assertEqual((action["type"], action["tag"], action["params"]["sticky"]),
                         ("ir.actions.client", "display_notification", True))
        pins = re.findall(r"\b\d{8}\b", action["params"]["message"])
        self.assertEqual(len(pins), 1, action["params"]["message"])
        return pins[0], action

    def test_k07_approval_pin_rules(self):
        """The server generates the PIN; the rights and the company checks are unchanged."""
        pin, _action = self._issue(self.mgr_emp)
        self.assertTrue(pin.isdigit() and len(pin) == 8)
        self.assertTrue(self.mgr_emp._urp_check_approval_pin(pin))
        self.assertFalse(self.mgr_emp._urp_check_approval_pin("482613"), "previous PIN revoked")
        self.assertNotIn(pin, self.mgr_emp.sudo().urp_approval_pin_hash, "hash only")
        self.env.cr.execute("SELECT * FROM urp_approval_pin_wizard")
        self.assertNotIn(pin, str(self.env.cr.fetchall()), "the PIN is never kept in the wizard")
        bodies = " ".join(self.mgr_emp.sudo().message_ids.mapped("body"))
        self.assertIn("re-issued", bodies)
        self.assertNotIn(pin, bodies, "the log names who issued it, never the PIN")
        with self.assertRaises(AccessError):
            self.env["urp.approval.pin.wizard"].with_user(self.cashier).create({"employee_id": self.mgr_emp.id})
        with self.assertRaises(AccessError):
            self.env["urp.approval.pin.wizard"].with_user(self.manager).create({"employee_id": self.emp_b.id})

    def test_k09_issued_pin_never_a_login_pin_nor_another_managers(self):
        self.cash_emp.sudo().pin = "11112222"                                # an 8-digit POS login PIN
        _set_pin(self.emp_other_pos, "33334444")                            # another employee's approval PIN
        with patch.object(urp_approval_pin.secrets, "randbelow", side_effect=[11112222, 33334444, 55556666]):
            pin, action = self._issue(self.mgr_emp)
        self.assertEqual(pin, "55556666", "both conflicting candidates skipped, silently")
        self.assertNotIn("11112222", str(action))
        self.assertNotIn("33334444", str(action))
        self.assertTrue(self.emp_other_pos._urp_check_approval_pin("33334444"), "the other PIN is untouched")
        before = self.mgr_emp.sudo().urp_approval_pin_hash
        with patch.object(urp_approval_pin.secrets, "randbelow", return_value=33334444):
            with self.assertRaisesRegex(UserError, "No approval PIN could be issued"), self.env.cr.savepoint():
                self._issue(self.mgr_emp)
        self.assertEqual(self.mgr_emp.sudo().urp_approval_pin_hash, before, "nothing changed when no PIN is issued")

    def test_k10_setup_screen_cannot_test_a_pin(self):
        """A PIN sent to the setup wizard through RPC is refused the same way, used or not: no oracle."""
        before = self.mgr_emp.sudo().urp_approval_pin_hash
        outcomes = []
        for pin in ("593724", "999999"):                       # another manager's PIN / a free number
            outcome = []
            for call in ("create", "write"):
                try:
                    with self.env.cr.savepoint():
                        Wiz = self.env["urp.approval.pin.wizard"].with_user(self.manager)
                        if call == "create":
                            Wiz.create({"employee_id": self.mgr_emp.id, "pin": pin, "pin_confirm": pin})
                        else:
                            Wiz.create({"employee_id": self.mgr_emp.id}).write({"pin": pin, "pin_confirm": pin})
                    outcome.append((call, "accepted"))
                except Exception as exc:                                                       # noqa: BLE001
                    outcome.append((call, type(exc).__name__, str(exc).replace(pin, "<pin>")))
            outcomes.append(outcome)
        self.assertEqual(outcomes[0], outcomes[1], "same answer for a used and a free number")
        self.assertEqual(self.mgr_emp.sudo().urp_approval_pin_hash, before, "a typed PIN is never stored")
        self.assertTrue(self.mgr_emp._urp_check_approval_pin("482613"))

    def test_k11_approval_with_an_issued_pin(self):
        pin, _action = self._issue(self.mgr_emp)
        with freeze_time(SALE):
            order = self._order({"N": 2})
        with freeze_time(OCT5):
            ref = self._refund_draft(order, {"N": 1})
            self.assertEqual(self._pin(self._lines(ref), ref.uuid, pin="482613")["error"], "invalid_pin", "revoked")
            res = self._pin(self._lines(ref), ref.uuid, pin=pin, reason="Broken seal")
            self.assertEqual((res["ok"], res["manager"]), (True, "PIN Manager"))
            ref.urp_override_token = res["token"]
            self._pay(ref, ref.amount_total)
        ov = ref.urp_override_id
        self.assertEqual((ov.approver_role, ov.auth_method, ov.approver_employee_id, ov.approver_config_id),
                         ("pos_manager_employee", "pin", self.mgr_emp, self.pos_config_usd))
        self.assertTrue(self.admin_emp._urp_check_approval_pin("593724"), "other managers' PINs unchanged")

    def test_k12_two_issues_and_clear(self):
        with patch.object(urp_approval_pin.secrets, "randbelow", side_effect=[77778888, 77778888, 99990000]):
            first, _a = self._issue(self.mgr_emp)
            second, _b = self._issue(self.emp_other_pos)
        self.assertEqual((first, second), ("77778888", "99990000"), "never the same PIN for two employees")
        wiz = self.env["urp.approval.pin.wizard"].with_user(self.manager).create({"employee_id": self.mgr_emp.id})
        wiz.action_clear()
        self.assertFalse(self.mgr_emp.urp_has_approval_pin)
        self.assertFalse(self.mgr_emp._urp_check_approval_pin(first))
        self.assertIn("removed", " ".join(self.mgr_emp.sudo().message_ids.mapped("body")))

    def test_k08_employee_approval_unusable_once_no_longer_manager(self):
        with freeze_time(SALE):
            order = self._sale(2, ship_later=True, product=self.prods["N"])
            self._deliver(order, 2)
        with freeze_time(OCT5):
            req = self.env["urp.return.exception"].browse(order.with_user(self.cashier).action_urp_request_exception()["res_id"])
            req.line_ids.requested_qty = 1
            req.reason = "Damaged"
            req.action_submit()
            self.env["urp.exception.pin.wizard"].with_user(self.cashier).create({"exception_id": req.id, "pin": "482613"})
            origin = self._pick(order).move_ids
            self.assertEqual(origin._urp_approval_remaining(), 1.0)
            self.pos_config_usd.manager_employee_ids = [(3, self.mgr_emp.id)]
            self.assertEqual(origin._urp_approval_remaining(), 0.0, "removed from the managers")
            self.pos_config_usd.manager_employee_ids = [(4, self.mgr_emp.id)]
            self.mgr_emp.sudo().action_archive()
            self.assertEqual(origin._urp_approval_remaining(), 0.0, "archived")
            self.mgr_emp.sudo().action_unarchive()
            self.assertEqual(origin._urp_approval_remaining(), 1.0)


class UrpPinUiCommon(TestPointOfSaleHttpCommon):

    def _run(self):
        config = self.main_pos_config
        company = config.company_id
        company.write({"urp_tz": "Asia/Riyadh", "urp_default_days": 14, "urp_enabled": True})
        p = self.env["product.product"].create({"name": "URP NoReturn", "is_storable": True, "available_in_pos": True,
                                                "list_price": 10.0, "taxes_id": False})
        p.product_tmpl_id.write({"use_custom_return_period": True, "custom_return_period_days": 0})
        cashier = self.env["hr.employee"].sudo().create({"name": "URP Cashier", "company_id": company.id, "pin": "2580"})
        manager = self.env["hr.employee"].sudo().create({"name": "URP Manager", "company_id": company.id, "pin": "1357"})
        _set_pin(manager, "482613")
        config.write({"module_pos_hr": True, "cashier_employee_ids": [(6, 0, cashier.ids)],
                      "manager_employee_ids": [(6, 0, manager.ids)]})
        config.with_user(self.pos_user).open_ui()
        self.start_pos_tour("UrpPinApprovalLinkedRefund", login="pos_user")
        orders = self.env["pos.order"].search([("session_id", "=", config.current_session_id.id)], order="id")
        sale, refund = orders.filtered(lambda o: o.amount_total > 0), orders.filtered(lambda o: o.amount_total < 0)
        self.assertEqual((refund.state, refund.lines.refunded_orderline_id), ("paid", sale.lines))
        ov = refund.urp_override_id
        self.assertEqual((ov.approver_employee_id, ov.auth_method, ov.approver_role, ov.reason, ov.line_ids.exception_type),
                         (manager, "pin", "pos_manager_employee", "Defective, approved by the manager", "non_returnable"))
        self.assertEqual((ov.executed_by_employee_id, ov.same_person), (cashier, False))
        attempts = self.env["urp.auth.attempt"].sudo().search([("config_id", "=", config.id), ("method", "=", "pin")])
        self.assertEqual(sorted(attempts.mapped("success")), [False, True], "the wrong PIN was recorded")


@tagged("post_install", "-at_install", "urp", "urp_pos_pin")
class TestPosPinUi(UrpPinUiCommon):

    def test_u04_pin_approval_in_ui(self):
        self._run()


@tagged("post_install", "-at_install", "urp", "urp_pos_pin")
class TestPosPinUiMobile(UrpPinUiCommon):
    browser_size = "375x667"
    touch_enabled = True

    def test_u05_pin_approval_in_mobile_ui(self):
        self._run()
