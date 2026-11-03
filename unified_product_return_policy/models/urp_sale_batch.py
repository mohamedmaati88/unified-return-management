from odoo import fields, models
from odoo.exceptions import UserError

from .urp_guard import is_urp_internal, urp_internal

SNAPSHOT_FIELDS = ("return_days", "policy_source", "portal_eligible", "portal_days", "tz", "approved_at", "company_id", "qty",
                   "date_basis")


class UrpSaleBatch(models.Model):
    """Quantity of a sale order line approved at one commercial event, with the return
    policy in force at that moment (immutable snapshot)."""
    _name = "urp.sale.batch"
    _description = "Approved Sale Quantity (Return Policy Snapshot)"
    _order = "approved_at, id"

    sale_line_id = fields.Many2one("sale.order.line", required=True, index=True, ondelete="cascade", readonly=True)
    order_id = fields.Many2one(related="sale_line_id.order_id", store=True, index=True)
    product_id = fields.Many2one(related="sale_line_id.product_id", store=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    qty = fields.Float("Approved Quantity", required=True, readonly=True, digits="Product Unit")
    qty_cancelled = fields.Float("Cancelled Quantity", readonly=True, digits="Product Unit")
    approved_at = fields.Datetime(required=True, readonly=True)
    return_days = fields.Integer("Return Days", readonly=True)
    policy_source = fields.Selection([("product", "Product"), ("category", "Product Category"), ("company", "Company Default")], readonly=True)
    portal_eligible = fields.Boolean("Portal Return Allowed", readonly=True)
    portal_days = fields.Integer("Portal Return Days", readonly=True)
    tz = fields.Char("Timezone", readonly=True)
    date_basis = fields.Selection([("validation", "Actual Validation Date"), ("effective", "Effective Date")],
                                  string="Return Period Based On", readonly=True,
                                  help="Frozen at the sale. Empty on batches created before this option: validation date.")
    move_ids = fields.One2many("stock.move", "urp_batch_id", readonly=True)

    def write(self, vals):
        if not is_urp_internal(self.env) and set(vals) & set(SNAPSHOT_FIELDS + ("qty_cancelled", "sale_line_id")):
            raise UserError(self.env._("Return policy snapshots cannot be modified."))
        return super().write(vals)

    def _urp_customer_moves(self):
        return self.move_ids.filtered(lambda m: m.location_dest_usage == "customer")

    def _urp_open_qty(self):
        """Undelivered quantity, in the sale line's unit (as ``qty`` and ``qty_cancelled``)."""
        self.ensure_one()
        uom = self.sale_line_id.product_uom_id
        return sum(m.uom_id._compute_quantity(m.product_uom_qty, uom, rounding_method="HALF-UP")
                   for m in self._urp_customer_moves().filtered(lambda m: m.state not in ("done", "cancel")))
