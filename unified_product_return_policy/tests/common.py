from odoo.tests.common import TransactionCase


class UrpCommon(TransactionCase):
    """Shared fixtures: one company with the policy enabled (Asia/Riyadh, 14 days)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.write({"urp_tz": "Asia/Riyadh", "urp_default_days": 14, "urp_enabled": True})
        cls.categ = cls.env["product.category"].create({"name": "URP Cat"})
        cls.product = cls.env["product.product"].create({
            "name": "URP Widget", "type": "consu", "is_storable": True, "categ_id": cls.categ.id, "list_price": 10.0})
        cls.partner = cls.env["res.partner"].create({"name": "URP Customer", "email": "urp@example.com"})
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.env["stock.quant"]._update_available_quantity(cls.product, cls.warehouse.lot_stock_id, 1000)
