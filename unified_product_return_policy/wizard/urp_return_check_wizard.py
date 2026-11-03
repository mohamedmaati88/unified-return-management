from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from ..models.urp_guard import urp_checked, urp_internal


class UrpReturnCheckWizard(models.TransientModel):
    _name = "urp.return.check.wizard"
    _description = "Return Policy Check"

    picking_ids = fields.Many2many("stock.picking", required=True)
    line_ids = fields.One2many("urp.return.check.wizard.line", "wizard_id")
    message = fields.Text(readonly=True)
    reason = fields.Text("Override Reason")
    can_return_eligible = fields.Boolean(compute="_compute_flags")
    can_override = fields.Boolean(compute="_compute_flags")

    @api.depends("line_ids")
    def _compute_flags(self):
        for wiz in self:
            others = wiz.picking_ids.move_ids.filtered(lambda m: m.quantity) - wiz.line_ids.move_ids
            wiz.can_return_eligible = any(l.eligible > 0 for l in wiz.line_ids) or bool(others)
            channels = set(wiz.line_ids.mapped("channel"))
            wiz.can_override = bool(wiz.line_ids) and all(l.kind in ("expired", "non_returnable") for l in wiz.line_ids) and \
                all(self.env["urp.return.override"]._urp_user_is_admin(c) for c in channels)

    # ------------------------------------------------------------------ helpers
    @api.model
    def _urp_message(self, violations):
        lines = []
        for v in violations:
            dl = fields.Datetime.context_timestamp(self, v["deadline"]).strftime("%Y-%m-%d %H:%M") if v["deadline"] else "-"
            if v["kind"] == "non_returnable":
                lines.append(self.env._(
                    "%(p)s: non-returnable - the return policy in force when it was sold is 0 days. Only an "
                    "approved Non-Returnable Exception allows it (approved and still available: %(a)s of %(r)s "
                    "requested).", p=v["product"].display_name, a=v.get("approved", 0.0), r=v["requested"]))
            elif v["kind"] == "pos_receipt":
                lines.append(self.env._(
                    "%(p)s: sold by POS receipt %(o)s under the return policy. Only %(e)s of %(r)s unit(s) (sales made "
                    "before the return policy) can be returned here. Use POS Refund on that receipt for the rest: "
                    "its return period, the manager override and the refund are handled there.",
                    p=v["product"].display_name, o=v["origin"]._urp_receipt_name(), e=v["eligible"], r=v["requested"]))
            elif v["kind"] == "unattributable":
                lines.append(self.env._(
                    "%(p)s: this delivery groups several POS orders, so the returned quantity cannot be linked to "
                    "its receipt and its own return period. Only %(e)s of %(r)s unit(s) (sales made before the "
                    "return policy) can be returned here. Use POS Refund on the original receipt for the rest.",
                    p=v["product"].display_name, e=v["eligible"], r=v["requested"]))
            elif v["kind"] == "over_qty":
                lines.append(self.env._("%(p)s: %(r)s requested but only %(m)s delivered unit(s) remain returnable.",
                                        p=v["product"].display_name, r=v["requested"], m=v["remaining"]))
            elif v["kind"] == "portal_reserved":
                lines.append(self.env._("%(p)s: %(r)s requested but %(s)s of the %(m)s returnable unit(s) are reserved by open "
                                        "portal requests not executed by this return. Eligible: %(e)s.\n  %(h)s",
                                        p=v["product"].display_name, r=v["requested"], s=v["reserved"], m=v["remaining"],
                                        e=v["eligible"], h=v["origin"]._urp_portal_hint(v["moves"], reserved_only=True)))
            else:
                lines.append(self.env._("%(p)s: return period ended (deadline %(d)s). Eligible: %(e)s of %(r)s requested "
                                        "(of which %(a)s from an approved exception).",
                                        p=v["product"].display_name, d=dl, e=v["eligible"], r=v["requested"],
                                        a=v.get("approved", 0.0)))
                if v["origin"]._urp_in_time_request_open():
                    lines.append("  " + v["origin"]._urp_portal_hint(v["moves"]))
        return self.env._("Return policy check failed:\n%s", "\n".join(lines))

    @api.model
    def _urp_open(self, picking, violations):
        wiz = self.create({
            "picking_ids": [(6, 0, picking.ids)],
            "message": self._urp_message(violations),
            "line_ids": [(0, 0, {
                "product_id": v["product"].id, "origin_move_id": v["origin"].id, "kind": v["kind"], "channel": v["channel"],
                "requested": v["requested"], "eligible": v["eligible"], "deadline": v["deadline"],
                "move_ids": [(6, 0, v["moves"].ids)],
            }) for v in violations],
        })
        return {
            "type": "ir.actions.act_window", "res_model": self._name, "res_id": wiz.id, "view_mode": "form",
            "target": "new", "name": self.env._("Return Policy"),
        }

    # ------------------------------------------------------------------ actions
    def action_return_eligible_only(self):
        self.ensure_one()
        for line in self.line_ids:
            remaining_to_keep = line.eligible
            for move in line.move_ids.sorted("id"):
                qty_origin = move.uom_id._compute_quantity(move.quantity, line.origin_move_id.uom_id)
                keep = min(qty_origin, remaining_to_keep)
                remaining_to_keep -= keep
                keep_move_uom = line.origin_move_id.uom_id._compute_quantity(keep, move.uom_id)
                if move.uom_id.is_zero(keep_move_uom):
                    move._action_cancel()
                else:
                    move.write({"product_uom_qty": keep_move_uom, "quantity": keep_move_uom})
        return urp_checked(self.picking_ids).button_validate()

    def action_override(self):
        self.ensure_one()
        if not self.can_override:
            raise AccessError(self.env._("Only the administrator of the original sales channel can override the return period."))
        if not (self.reason or "").strip():
            raise UserError(self.env._("Enter the reason for the override."))
        channel = self.line_ids[:1].channel
        override = self.env["urp.return.override"].sudo().create({
            "channel": channel, "user_id": self.env.uid, "reason": self.reason, "company_id": self.picking_ids[:1].company_id.id,
            "approver_role": "sales_admin" if channel == "sale" else "pos_admin", "auth_method": "odoo_session",
            "picking_id": self.picking_ids[:1].id,
            "line_ids": [(0, 0, {"origin_move_id": l.origin_move_id.id, "product_id": l.product_id.id, "qty": l.requested,
                                 "exception_type": l.kind}) for l in self.line_ids],
        })
        urp_internal(self.picking_ids.sudo()).write({"urp_override_id": override.id})
        return urp_checked(self.picking_ids).button_validate()


class UrpReturnCheckWizardLine(models.TransientModel):
    _name = "urp.return.check.wizard.line"
    _description = "Return Policy Check Line"

    wizard_id = fields.Many2one("urp.return.check.wizard", required=True, ondelete="cascade")
    product_id = fields.Many2one("product.product", readonly=True)
    origin_move_id = fields.Many2one("stock.move", readonly=True)
    move_ids = fields.Many2many("stock.move", readonly=True)
    kind = fields.Selection([("expired", "Return period ended"), ("over_qty", "Quantity above returnable"),
                             ("unattributable", "Use POS Refund (grouped delivery)"),
                             ("pos_receipt", "Use POS Refund (POS receipt)"),
                             ("portal_reserved", "Reserved by an open portal request"),
                             ("non_returnable", "Non-returnable (approval required)")], readonly=True)
    channel = fields.Selection([("sale", "Sales"), ("pos", "Point of Sale")], readonly=True)
    requested = fields.Float(readonly=True, digits="Product Unit")
    eligible = fields.Float(readonly=True, digits="Product Unit")
    deadline = fields.Datetime(readonly=True)
