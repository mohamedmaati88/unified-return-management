import uuid
from datetime import timedelta

from freezegun import freeze_time

from odoo import fields
from odoo.tests import Form, HttpCase, tagged


class UrpPortalCommon(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.write({"urp_tz": "Asia/Riyadh", "urp_default_days": 14, "urp_enabled": True,
                           "allow_spontaneous_returns": True, "return_validity_days": 10})
        cls.product = cls.env["product.product"].create({"name": "URP Portal Widget", "type": "consu", "is_storable": True, "list_price": 10,
                                                        "use_custom_return_period": True, "custom_return_period_days": 7})
        wh = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.env["stock.quant"]._update_available_quantity(cls.product, wh.lot_stock_id, 100)
        cls.partner = cls.env["res.partner"].create({"name": "Portal Cust", "email": "pc@example.com"})

    def _delivered(self, qty=5):
        so = self.env["sale.order"].create({"partner_id": self.partner.id, "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": qty})]})
        so.action_confirm()
        pick = so.picking_ids
        pick.move_ids.quantity = qty
        pick.move_ids.picked = True
        pick.button_validate()
        so._portal_ensure_token()
        return so, so.order_line.move_ids.filtered(lambda m: m.state == "done")

    def _data(self, so, token=None):
        return self.make_jsonrpc_request("/my/order/return_data", {"order_id": so.id, "access_token": token or so.access_token})

    def _request(self, so, details, key=None, token=None):
        return self.make_jsonrpc_request(f"/my/orders/{so.id}/urp_return_request", {
            "access_token": token or so.access_token, "idempotency_key": key or str(uuid.uuid4()),
            "return_details": {str(k.id): v for k, v in details.items()}})



@tagged("post_install", "-at_install", "urp", "urp_p4")
class TestP4Portal(UrpPortalCommon):

    def test_h01_return_data_portal_period_is_min(self):
        so, move = self._delivered()
        self.assertEqual(move.urp_batch_id.portal_days, 7, "MIN(7 product, 10 portal)")
        data = self._data(so)
        self.assertEqual([l["remaining_delivered_qty"] for l in data["returnable_lines"]], [5])
        with freeze_time(move.urp_portal_deadline + timedelta(seconds=1)):
            self.assertIn("error", self._data(so))

    def test_h02_request_idempotent_and_reserves(self):
        so, move = self._delivered()
        key = str(uuid.uuid4())
        r1 = self._request(so, {move: 2}, key)
        r2 = self._request(so, {move: 2}, key)
        self.assertEqual(r1["request_id"], r2["request_id"])
        req = self.env["urp.portal.return.request"].browse(r1["request_id"])
        self.assertTrue(abs((req.submitted_at - fields.Datetime.now()).total_seconds()) < 120)
        self.assertEqual(len(so.message_ids.filtered(lambda m: "return label" in (m.body or ""))), 1, "chatter once")
        self.assertEqual(self._data(so)["returnable_lines"][0]["remaining_delivered_qty"], 3)
        self.assertIn("error", self._request(so, {move: 4}))
        self.assertIn("request_id", self._request(so, {move: 3}))
        self.assertIn("error", self._data(so), "nothing left to request")

    def test_h03_label_is_read_only_print(self):
        so, move = self._delivered()
        r = self._request(so, {move: 1})
        n_msgs = len(so.message_ids)
        ok = self.url_open(r["label_url"])
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(so.message_ids), n_msgs, "reprint creates no message")
        bad = self.url_open(f"/my/orders/{so.id}/download_return_label?access_token={so.access_token}"
                            f"&return_details=%7B%22{move.id}%22%3A%201%7D&return_reason_id=1")
        self.assertEqual(bad.status_code, 400, "legacy GET path cannot create requests on governed orders")
        self.assertEqual(self.env["urp.portal.return.request"].search_count([("order_id", "=", so.id)]), 1)

    def test_h04_after_deadline_reprint_only(self):
        so, move = self._delivered()
        r = self._request(so, {move: 1})
        with freeze_time(move.urp_portal_deadline + timedelta(hours=1)):
            self.assertIn("error", self._request(so, {move: 1}))
            self.assertEqual(self.url_open(r["label_url"]).status_code, 200)

    def test_h05_portal_switch_history(self):
        self.company.allow_spontaneous_returns = False
        so, move = self._delivered()
        self.assertFalse(move.urp_batch_id.portal_eligible)
        self.company.allow_spontaneous_returns = True
        self.assertIn("error", self._data(so), "sold while disabled: no portal right gained later")
        so2, move2 = self._delivered()
        self.company.allow_spontaneous_returns = False
        self.assertIn("error", self._request(so2, {move2: 1}), "service off: no new request")
        self.company.allow_spontaneous_returns = True
        self.company.return_validity_days = 30
        self.assertEqual(move2.urp_batch_id.portal_days, 7, "history kept, no extension")

    def test_h06_in_time_request_processed_after_deadline(self):
        so, move = self._delivered()
        r = self._request(so, {move: 2})
        req = self.env["urp.portal.return.request"].browse(r["request_id"])
        with freeze_time(move.urp_deadline + timedelta(days=5)):
            ret = move.picking_id._create_return()
            # the right belongs to the return line that executes the request, selected by the
            # Inventory user; test_p25_portal_link proves that an independent return neither uses nor
            # consumes it
            ret.move_ids.urp_portal_line_id = req.line_ids
            ret.move_ids.product_uom_qty = 2
            ret.move_ids.quantity = 2
            ret.move_ids.picked = True
            self.assertTrue(ret.button_validate() is True, "in-time request keeps the right")
            self.assertEqual(req.state, "done")
            ret2 = move.picking_id._create_return()
            ret2.move_ids.product_uom_qty = 1
            ret2.move_ids.quantity = 1
            ret2.move_ids.picked = True
            action = ret2.button_validate()
            self.assertEqual(action["res_model"], "urp.return.check.wizard", "request does not extend other quantities")

    def test_h07_cancel_remaining_gives_no_new_right(self):
        so, move = self._delivered()
        r = self._request(so, {move: 2})
        req = self.env["urp.portal.return.request"].browse(r["request_id"])
        req.action_cancel_remaining()
        self.assertEqual(req.state, "cancelled")
        with freeze_time(move.urp_portal_deadline + timedelta(hours=1)):
            self.assertIn("error", self._request(so, {move: 2}))
            self.assertEqual(self.url_open(r["label_url"]).status_code, 400)

    def test_h08_foreign_token_rejected(self):
        so, move = self._delivered()
        self.assertIn("error", self._request(so, {move: 1}, token="not-the-token"))

    def test_h09_legacy_order_standard_flow(self):
        self.company.urp_enabled = False
        so, move = self._delivered()
        self.company.urp_enabled = True
        data = self._data(so)
        self.assertNotIn("urp_request_url", data)
        res = self.url_open(f"/my/orders/{so.id}/download_return_label?access_token={so.access_token}"
                            f"&return_details=%7B%22{move.id}%22%3A%201%7D&return_reason_id={self.env['return.reason'].search([], limit=1).id or 0}")
        self.assertIn(res.status_code, (200, 303))


@tagged("post_install", "-at_install", "urp", "urp_p4")
class TestP4PortalUi(UrpPortalCommon):
    """Browser test of the ReturnOrderDialog patch (POST request instead of the GET side effect)."""

    def test_u01_portal_dialog_creates_request(self):
        so, move = self._delivered()
        self.env["return.reason"].search([], limit=1) or self.env["return.reason"].create({"name": "Damaged"})
        self.start_tour(f"/my/orders/{so.id}?access_token={so.access_token}", "UrpPortalReturnRequest")
        req = self.env["urp.portal.return.request"].search([("order_id", "=", so.id)])
        self.assertEqual(len(req), 1)
        self.assertEqual(req.line_ids.requested_qty, 1)
        self.assertEqual(self._data(so)["returnable_lines"][0]["remaining_delivered_qty"], 4)
