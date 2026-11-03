from odoo import api, fields, models
from odoo.exceptions import UserError

from .urp_guard import is_urp_internal, urp_internal

CHANNEL_ADMIN_GROUP = {
    "sale": "sales_team.group_sale_manager",
    "pos": "point_of_sale.group_pos_manager",
}


class UrpReturnOverride(models.Model):
    """Reason recorded when an administrator returns goods after the return period.

    Not an approval workflow: it only stores who overrode, why, and for what.
    """
    _name = "urp.return.override"
    _description = "Return Period Override"
    _order = "create_date desc, id desc"

    channel = fields.Selection([("sale", "Sales"), ("pos", "Point of Sale")], required=True, readonly=True)
    user_id = fields.Many2one("res.users", string="Administrator", readonly=True,
                              help="Odoo user of the approver (empty for a POS Manager employee without a user).")
    approver_role = fields.Selection([("sales_admin", "Sales Administrator"), ("pos_admin", "POS Administrator"),
                                      ("pos_manager_employee", "POS Manager (employee)")],
                                     string="Approved As", readonly=True)
    auth_method = fields.Selection([("odoo_session", "Logged-in Odoo user"),
                                    ("password", "Odoo account re-authentication"),
                                    ("pin", "POS Manager approval PIN")],
                                   string="Identity Verified By", readonly=True,
                                   help="How the approver's identity was verified (no secret is ever stored).")
    reason = fields.Text(required=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    picking_id = fields.Many2one("stock.picking", readonly=True, index="btree_not_null")
    line_ids = fields.One2many("urp.return.override.line", "override_id", readonly=True)
    consumed_at = fields.Datetime(readonly=True)
    # display only (not stored): the exception types of the lines (Expired Return Period Override,
    # Non-Returnable Exception, Standalone Refund Exception)
    kind = fields.Selection([("expired", "Expired Return Period Override"), ("non_returnable", "Non-Returnable Exception"),
                             ("standalone", "Standalone Refund Exception"), ("mixed", "Several Types")],
                            string="Override Type", compute="_compute_kind")
    usage = fields.Selection([("used", "Used"), ("partially_used", "Partially Used"), ("unused", "Not used")],
                             string="Authorisation Status", compute="_compute_kind")

    @api.depends("line_ids", "consumed_at")
    def _compute_kind(self):
        for rec in self:
            kinds = {l._urp_exception_type() for l in rec.line_ids}
            rec.kind = kinds.pop() if len(kinds) == 1 else ("mixed" if kinds else False)
            used = sum(rec.line_ids.mapped("used_qty"))
            remaining = sum(rec.line_ids.mapped("remaining_qty"))
            rec.usage = "unused" if not used else ("partially_used" if remaining > 0 else "used")

    @api.constrains("reason")
    def _check_reason(self):
        for rec in self:
            if not (rec.reason or "").strip():
                raise UserError(self.env._("A reason is required to override the return period."))

    def write(self, vals):
        if not is_urp_internal(self.env):
            raise UserError(self.env._("Return overrides cannot be modified."))
        return super().write(vals)

    def _urp_covers(self, violation, pickings):
        """True when this override (set on ``pickings`` by the wizard only: the field is
        write-protected) was issued by an administrator of the violation's channel, is
        still unused, and covers the delivery and the quantity."""
        self.ensure_one()
        ov = self.sudo()
        if ov.consumed_at or ov.channel != violation["channel"] or ov.token_hash or pickings.urp_override_id != self:
            return False
        if not ov._urp_approver_valid():
            return False
        allowed = sum(ov.line_ids.filtered(lambda l: l.origin_move_id == violation["origin"]).mapped("qty"))
        return violation["origin"].uom_id.compare(allowed, violation["requested"]) >= 0

    def _urp_approver_valid(self):
        """Checked at every use, not only when approving: the approving administrator is still
        active, still an administrator of the approval's channel, and still has access to the
        approval's company. An approval of someone who lost one of them can no longer be used."""
        self.ensure_one()
        ov = self.sudo()
        if "approver_employee_id" in ov._fields and ov.approver_employee_id:
            # POS Manager employee: still active, same company, still a manager of that point of sale
            emp, config = ov.approver_employee_id, ov.approver_config_id
            return bool(emp.active and emp.company_id == ov.company_id and config
                        and emp in config._urp_manager_employees())
        user = ov.user_id
        return bool(user and user.active and ov.company_id in user.company_ids and self._urp_user_is_admin(ov.channel, user))

    @api.model
    def _urp_user_is_admin(self, channel, user=None):
        user = user or self.env.user
        return user.has_group(CHANNEL_ADMIN_GROUP[channel])


class UrpReturnOverrideLine(models.Model):
    _name = "urp.return.override.line"
    _description = "Return Period Override Line"

    override_id = fields.Many2one("urp.return.override", required=True, ondelete="cascade", readonly=True)
    company_id = fields.Many2one(related="override_id.company_id", store=True, index=True)
    origin_move_id = fields.Many2one("stock.move", string="Delivery", readonly=True)
    product_id = fields.Many2one("product.product", required=True, readonly=True)
    qty = fields.Float(required=True, readonly=True, digits="Product Unit")

    def _urp_exception_type(self):
        """Stored type; legacy rows without a stored type fall back to the origin (expired / standalone)."""
        self.ensure_one()
        return self.exception_type or ("expired" if self._urp_has_origin() else "standalone")

    def _urp_has_origin(self):
        """Linked to a known sale (delivery move, or original POS line when the POS is installed)."""
        self.ensure_one()
        return bool(self.origin_move_id or ("refunded_line_id" in self._fields and self.refunded_line_id))
