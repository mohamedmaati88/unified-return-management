"""Cancel remaining of a portal request with linked Inventory returns.

*Cancel remaining* never releases the quantity an open return linked to the request (Portal Request
column) is executing: only the rest is cancelled, and when nothing else is left the cancellation is
refused while the return waits for the goods."""
import json
from datetime import datetime

from freezegun import freeze_time

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, tagged

from .test_p18_sales_exceptions import T1
from .test_p25_portal_link import UrpPortalLinkCommon


@tagged("post_install", "-at_install", "urp", "urp_portal_cancel")
class TestPortalCancelRemaining(UrpPortalLinkCommon):

    def test_c01_fully_held_by_an_open_return_is_refused(self):
        """The reported scenario: 10 delivered, request of 3, linked open return of 3."""
        so = self._so({"A": 10})
        req = self._preq(so, 3)
        with freeze_time(T1):
            ret = self._ret(so, 3, req.line_ids)
            with self.assertRaisesRegex(UserError, "(?s)cannot be cancelled.*%s" % ret.name):
                req.with_user(self.su).action_cancel_remaining()
            self.assertEqual(self._state(req), ("active", 3.0, 0.0), "nothing released")
            self.assertIs(self._done(ret), True, "the linked return still executes the request")
        self.assertEqual(self._state(req), ("done", 0.0, 3.0))

    def test_c02_partly_held_only_the_rest_is_cancelled(self):
        so = self._so({"A": 10})
        req = self._preq(so, 5)
        with freeze_time(T1):
            ret = self._ret(so, 2, req.line_ids)
            req.with_user(self.su).action_cancel_remaining()
            self.assertEqual(self._state(req), ("active", 2.0, 0.0), "3 cancelled, the 2 being returned kept")
            self.assertEqual(req.line_ids.cancelled_qty, 3.0)
            self.assertEqual(self._move(so)._urp_portal_available(), 8.0, "10 - 2 held by the linked return")
            # nothing else left: a second cancellation is refused
            with self.assertRaisesRegex(UserError, ret.name):
                req.with_user(self.su).action_cancel_remaining()
            self.assertIs(self._done(ret), True)
        self.assertEqual(self._state(req), ("done", 0.0, 2.0))

    def test_c03_cancelled_linked_return_releases_the_request(self):
        so = self._so({"A": 10})
        req = self._preq(so, 3)
        with freeze_time(T1):
            ret = self._ret(so, 3, req.line_ids)
            ret.with_user(self.iu).action_cancel()
            req.with_user(self.su).action_cancel_remaining()
        self.assertEqual(self._state(req), ("cancelled", 0.0, 0.0), "unchanged when no return is linked")

    def test_c04_without_linked_return_unchanged(self):
        so = self._so({"A": 10})
        req = self._preq(so, 3)
        with freeze_time(T1):
            self._ret(so, 2)                                                  # independent return: holds nothing
            req.with_user(self.su).action_cancel_remaining()
        self.assertEqual(self._state(req), ("cancelled", 0.0, 0.0))

    def test_c05_several_returns_and_backorder(self):
        with freeze_time(T1):
            so = self._so({"A": 10})
            req = self._preq(so, 6)
            first = self._ret(so, 2, req.line_ids)
            second = self._ret(so, 3, req.line_ids)
            req.with_user(self.su).action_cancel_remaining()
            self.assertEqual(self._state(req), ("active", 5.0, 0.0), "1 cancelled, 2 + 3 held by two returns")
            # partial receipt with a backorder: the backorder keeps holding the rest
            self.assertIs(self._done(second, 1), True)
            self.assertEqual(self._state(req), ("active", 4.0, 1.0))
            with self.assertRaisesRegex(UserError, "(?s)%s.*%s" % (first.name, second.backorder_ids.name)):
                req.with_user(self.su).action_cancel_remaining()
            self.assertEqual(self._state(req), ("active", 4.0, 1.0))
            # backorder cancelled: only the first return still holds
            second.backorder_ids.with_user(self.iu).action_cancel()
            req.with_user(self.su).action_cancel_remaining()
            self.assertEqual(self._state(req), ("active", 2.0, 1.0))
            self.assertIs(self._done(first), True)
        self.assertEqual(self._state(req), ("done", 0.0, 3.0))

    def test_c06_partial_receipt_without_backorder(self):
        with freeze_time(T1):
            so = self._so({"A": 10})
            req = self._preq(so, 5)
            self.assertIs(self._done(self._ret(so, 5, req.line_ids), 3, backorder=False), True)
            req.with_user(self.su).action_cancel_remaining()               # nothing open holds the 2 left
        self.assertEqual(self._state(req), ("done", 0.0, 3.0))

    def test_c07_after_execution(self):
        so = self._so({"A": 10})
        req = self._preq(so, 3)
        with freeze_time(T1):
            self.assertIs(self._done(self._ret(so, 3, req.line_ids)), True)
            req.with_user(self.su).action_cancel_remaining()               # done request: nothing to do
        self.assertEqual(self._state(req), ("done", 0.0, 3.0))

    def test_c08_rights_and_companies_unchanged(self):
        so = self._so({"A": 10})
        so.user_id = self.su
        req = self._preq(so, 3)
        with freeze_time(T1):
            self._ret(so, 3, req.line_ids)
            with self.assertRaises(AccessError):
                req.with_user(self.own).action_cancel_remaining()          # not his order: checked first
            with self.assertRaises(AccessError):
                req.with_user(self.iu).action_cancel_remaining()           # no sales group
            with self.assertRaises(AccessError):
                req.line_ids.with_user(self.sa).write({"cancelled_qty": 3})  # no direct write for anybody
            with self.assertRaises(UserError):
                req.with_user(self.sa).with_context(urp_internal="internal").action_cancel_remaining()
            other = self.env["res.company"].create({"name": "Portal Cancel B"})
            self.sa.company_ids |= other
            with self.assertRaises(AccessError):
                req.with_user(self.sa).with_context(allowed_company_ids=other.ids).action_cancel_remaining()
        self.assertEqual(self._state(req), ("active", 3.0, 0.0))


@tagged("post_install", "-at_install", "urp", "urp_portal_cancel")
class TestPortalCancelRemainingRpc(UrpPortalLinkCommon, HttpCase):

    def test_c09_rpc(self):
        so = self._so({"A": 10})
        req = self._preq(so, 3)
        self.su.password = "pc_su-Pass-1!"
        with freeze_time(T1):
            ret = self._ret(so, 3, req.line_ids)
        self.authenticate(self.su.login, "pc_su-Pass-1!")
        res = self.url_open("/web/dataset/call_kw", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"model": "urp.portal.return.request", "method": "action_cancel_remaining",
                       "args": [req.ids], "kwargs": {"context": {"urp_internal": "internal"}}}}),
            headers={"Content-Type": "application/json"}, timeout=120).json()
        self.assertIn(ret.name, json.dumps(res.get("error")))
        self.assertEqual(self._state(req), ("active", 3.0, 0.0))
