"""Sequential counterparts of concurrency checks made with two database connections. When two
transactions collide, the losing one is retried by
Odoo with fresh data; these tests pin down what that retry must then find:

* a request already approved is never approved a second time (one approval record, one
  Return Transfer);
* a request whose approval is fully used can no longer be cancelled (its usage stays).

The collisions themselves (two database connections) cannot run inside one test transaction:
they stay external probes."""
from freezegun import freeze_time

from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_p18_sales_exceptions import T1, UrpSalesExceptionCommon


@tagged("post_install", "-at_install", "urp", "urp_sales_exceptions")
class TestQaRetryOutcomes(UrpSalesExceptionCommon):

    def _overrides(self, req):
        return self.env["urp.return.override"].sudo().search([("exception_id", "=", req.id)])

    def test_w01_second_approval_refused(self):
        so, req = self._approved({"N": 2})
        sa2 = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "urp_x_sa2", "login": "urp_x_sa2", "group_ids": [(6, 0, self.sa.group_ids.ids)],
            "company_ids": [(6, 0, self.company.ids)], "company_id": self.company.id})
        with freeze_time(T1):
            self.assertEqual((req.state, len(self._overrides(req)), len(req.return_picking_ids)), ("approved", 1, 1))
            for approver in (sa2, self.sa):
                with self.assertRaisesRegex(UserError, "Only a draft or pending request can be approved"):
                    req.with_user(approver).action_approve()
            self.assertEqual((req.state, len(self._overrides(req)), len(req.return_picking_ids)), ("approved", 1, 1),
                             "one approval record and one Return Transfer, whoever tries again")

    def test_w02_used_request_cannot_be_cancelled(self):
        so, req = self._approved({"N": 2})
        line = req.sudo().override_id.line_ids
        with freeze_time(T1):
            ret = self._auto(req)
            self.assertIs(self._receive(ret, self.iu), True)
            self.assertEqual((req.state, line.used_qty, ret.state), ("used", 2.0, "done"))
            for user in (self.sa, self.su):
                with self.assertRaisesRegex(UserError, "can no longer be cancelled"):
                    req.with_user(user).action_cancel()
            self.assertEqual((req.state, line.used_qty, line.remaining_qty, ret.state), ("used", 2.0, 0.0, "done"),
                             "the executed return and its usage are kept")
