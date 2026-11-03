"""POS Administrator approval with an Odoo account: the answer never tells whether the password was
right.

A right password with a missing code, a right password of an account without the right and a wrong
password all get the same answer, so the approval dialog cannot be used to test the password of an
account. The tests compare the answer of a right password with the answer of a wrong one."""
import base64
import time
import uuid

from odoo.tests import HttpCase, tagged
from odoo.addons.auth_totp.models.totp import hotp

from .test_p5_pos import SECRET, UrpPosCommon

REFUSED = {"ok": False, "error": "invalid"}


@tagged("post_install", "-at_install", "urp", "urp_pos_auth")
class TestPosAuthAnswer(UrpPosCommon):

    def _ask(self, login, password, totp=None, user=None, order_uuid=None):
        return self._session().with_user(user or self.cashier).urp_request_override(
            login, password, totp, "Damaged", [{"product_id": self.p.id, "qty": 1}], order_uuid or str(uuid.uuid4()))

    def _fails(self, user):
        return self.env["urp.auth.attempt"].search_count([("user_id", "=", user.id), ("success", "=", False)])

    def test_a01_right_and_wrong_password_same_answer(self):
        """An account without the POS Administrator right: right or wrong password, same answer."""
        self.assertEqual(self._ask("urp_pos_cash", "Cash-Pass-123!"), REFUSED, "right password")
        self.assertEqual(self._ask("urp_pos_cash", "wrong"), REFUSED, "wrong password")
        self.assertEqual(self._fails(self.cashier), 2, "both still counted as failures")
        self.assertFalse(self.env["urp.return.override"].search([("user_id", "=", self.cashier.id)]))

    def test_a02_administrator_of_another_company(self):
        company_b = self.env["res.company"].create({"name": "Auth answer B"})
        mgr_b = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Auth mgr B", "login": "urp_auth_mgr_b", "password": "AuthB-Pass-123!",
            "group_ids": [(6, 0, [self.env.ref("point_of_sale.group_pos_manager").id])],
            "company_ids": [(6, 0, company_b.ids)], "company_id": company_b.id})
        self.assertEqual(self._ask("urp_auth_mgr_b", "AuthB-Pass-123!"), REFUSED)
        self.assertEqual(self._ask("urp_auth_mgr_b", "wrong"), REFUSED)
        self.assertEqual(self._fails(mgr_b), 2)

    def test_a03_malformed_code_same_answer(self):
        """A code of the wrong type after a right password is refused like a wrong password (no server error)."""
        self.manager.sudo().totp_secret = SECRET
        for code in ("abc", [1], {"a": 1}):
            self.assertEqual(self._ask("urp_pos_mgr", "Mgr-Pass-123!", code), REFUSED, code)
            self.assertEqual(self._ask("urp_pos_mgr", "wrong", code), REFUSED, code)
            self.env["urp.auth.attempt"].sudo().search([("user_id", "=", self.manager.id)]).unlink()   # stay under the lockout

    def test_a04_unchanged_approval_and_lockout(self):
        """A POS Administrator still gets the token; the lockout answer is the same for a right password."""
        res = self._ask("urp_pos_mgr", "Mgr-Pass-123!")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["token"])
        for _ in range(5):
            self.assertEqual(self._ask("urp_pos_mgr", "wrong"), REFUSED)
        self.assertEqual(self._ask("urp_pos_mgr", "Mgr-Pass-123!"), {"ok": False, "error": "locked"})
        self.assertEqual(self._ask("urp_pos_mgr", "wrong"), {"ok": False, "error": "locked"})

    def test_a05_unknown_login(self):
        self.assertEqual(self._ask("urp_nobody", "x"), REFUSED)


@tagged("post_install", "-at_install", "urp", "urp_pos_auth")
class TestPosAuthAnswerTotp(UrpPosCommon, HttpCase):
    """The real JSON-RPC call of the POS, with TOTP enabled on the administrator."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager.sudo().totp_secret = SECRET

    def _rpc(self, password, totp=None):
        return self.make_jsonrpc_request("/web/dataset/call_kw/pos.session/urp_request_override", {
            "model": "pos.session", "method": "urp_request_override",
            "args": [self._session().ids, "urp_pos_mgr", password, totp, "Damaged",
                     [{"product_id": self.p.id, "qty": 1}], str(uuid.uuid4())],
            "kwargs": {}})

    def _code(self):
        return hotp(base64.b32decode(SECRET), int(time.time() / 30))

    def test_a06_totp_answers(self):
        self.assertTrue(self.manager.totp_enabled)
        self.authenticate("urp_pos_cash", "Cash-Pass-123!")
        code = self._code()
        wrong_code = 654321 if code == 123456 else 123456
        answers = {
            "right password, no code": self._rpc("Mgr-Pass-123!"),
            "wrong password, no code": self._rpc("wrong"),
            "right password, wrong code": self._rpc("Mgr-Pass-123!", wrong_code),
            "wrong password, right code": self._rpc("wrong", code),
        }
        for case, res in answers.items():
            self.assertEqual(res, REFUSED, case)
        ok = self._rpc("Mgr-Pass-123!", self._code())
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(self.env["urp.auth.attempt"].search_count([("user_id", "=", self.manager.id), ("success", "=", True)]), 1)
