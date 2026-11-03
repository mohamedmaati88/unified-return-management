from odoo.tests import tagged

from odoo.addons.point_of_sale.tests.test_frontend import TestPointOfSaleHttpCommon


@tagged("post_install", "-at_install", "urp", "urp_p5")
class TestP5PosUi(TestPointOfSaleHttpCommon):
    """Browser test of the OWL patches: validateOrder hook, manager dialog, token round trip."""

    def test_u01_standalone_refund_override_in_ui(self):
        company = self.main_pos_config.company_id
        company.write({"urp_tz": "Asia/Riyadh", "urp_default_days": 14, "urp_enabled": True})
        self.env["product.product"].create({
            "name": "URP Gadget", "is_storable": True, "available_in_pos": True, "list_price": 10.0, "taxes_id": False})
        self.main_pos_config.with_user(self.pos_user).open_ui()
        self.start_pos_tour("UrpStandaloneRefundOverride", login="pos_user")
        order = self.env["pos.order"].search([("session_id", "=", self.main_pos_config.current_session_id.id)], limit=1)
        self.assertEqual(order.state, "paid")
        self.assertEqual((order.urp_override_id.user_id, order.urp_override_id.reason),
                         (self.pos_admin, "No receipt, checked by manager"))
        self.assertFalse(order.urp_override_token)
        self.assertEqual(order.urp_override_id.pos_order_uuid, order.uuid, "UI sends the refund order UUID")
        self.assertEqual(order.urp_override_id.pos_session_id, order.session_id)
        self.assertEqual(self.env["urp.auth.attempt"].search_count([("user_id", "=", self.pos_admin.id), ("success", "=", False)]), 1)
