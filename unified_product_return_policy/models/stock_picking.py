from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from odoo import api, fields, models
from odoo.exceptions import UserError

from .urp_guard import is_urp_checked, is_urp_internal, urp_internal
from .urp_tools import urp_lock_rows


class StockPicking(models.Model):
    _inherit = "stock.picking"

    urp_override_id = fields.Many2one("urp.return.override", string="Return Period Override", copy=False, readonly=True)

    def write(self, vals):
        if "urp_override_id" in vals and not is_urp_internal(self.env):
            raise UserError(self.env._("The return period override of a transfer cannot be set manually."))
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        if not is_urp_internal(self.env):
            for vals in vals_list:
                vals.pop("urp_override_id", None)
        return super().create(vals_list)

    def _create_return(self):
        """Early server-side check when a return is created (Return button, RPC, exchange).

        * POS Direct / aggregated POS deliveries: the governed quantity is never added to an
          Inventory return, whoever the user is; it is returned through POS Refund on the original
          receipt (its own policy, manager approval on the receipt, refund and stock in one step).
        * Deliveries that need an administrator's exception (period ended with no documented
          in-time request, or non-returnable) are added only when an approved exception request
          still covers a quantity, or for an administrator of their sales channel (who approves
          with a mandatory reason at validation). Otherwise they are left out.
        The return is refused when nothing else is returnable, otherwise it is created for the
        eligible products only and the excluded ones are listed on it."""
        self.ensure_one()
        receipt_only = self.move_ids._urp_receipt_only_origins()
        needs = self.move_ids._urp_expired_origins()
        Override = self.env["urp.return.override"]
        blocked = [e for e in needs if e["approved"] <= 0 and not Override._urp_user_is_admin(e["channel"])]
        # nothing left: everything delivered is already returned or held by another open return
        exhausted = [m for m in self.move_ids.filtered(lambda m: m.state == "done" and m._urp_is_customer_delivery()
                                                       and m._urp_origin_channel())
                     if m.uom_id.compare(m._urp_free_return_qty(), 0.0) <= 0]
        excluded = {e["origin"] for e in receipt_only} | {e["origin"] for e in blocked} | set(exhausted)
        if excluded and len(excluded) == len(self.move_ids.filtered(lambda m: m.state != "cancel")):
            parts = []
            if receipt_only:
                parts.append(self._urp_receipt_text(receipt_only))
            if blocked:
                parts.append(self.env._("An approved return exception is required:\n%(l)s\nRequest it from the original "
                                        "order; the administrator of its sales channel approves it with a reason.",
                                        l=self._urp_expired_text(blocked)))
            if exhausted:
                parts.append(self._urp_exhausted_text(exhausted))
            raise UserError(self.env._("No return can be created for %(p)s.\n%(l)s", p=self.name, l="\n".join(parts)))
        new_picking = super()._create_return()
        if excluded:
            origins = self.env["stock.move"].browse([o.id for o in excluded])
            # draft return lines created a moment ago by this very call: removed whatever the user's
            # delete rights on stock moves (the user never had them as returnable lines)
            new_picking.move_ids.filtered(lambda m: m.origin_returned_move_id in origins).sudo().unlink()
        if receipt_only:
            new_picking.message_post(body=self.env._("Not added to this return:\n%s", self._urp_receipt_text(receipt_only)))
        if blocked:
            new_picking.message_post(body=self.env._(
                "Not added to this return, an approved return exception is required:\n%s", self._urp_expired_text(blocked)))
        if exhausted:
            new_picking.message_post(body=self.env._("Not added to this return:\n%s", self._urp_exhausted_text(exhausted)))
        approved = [e for e in needs if e not in blocked and e["approved"] > 0]
        if approved:
            new_picking.message_post(body=self.env._(
                "Approved return exception, within the approved quantity:\n%s", self._urp_expired_text(approved)))
        admin_needs = [e for e in needs if e not in blocked and e not in approved]
        if admin_needs:
            new_picking.message_post(body=self.env._(
                "Administrator approval (override with a reason required at validation):\n%s", self._urp_expired_text(admin_needs)))
        return new_picking

    def _prepare_return_move_default_values(self, move_id):
        vals = super()._prepare_return_move_default_values(move_id)
        vals["urp_request_line_id"] = False          # a return of a return is not the request's return
        vals["urp_portal_line_id"] = False           # nor the execution of a portal request
        return vals

    def _urp_exhausted_text(self, moves):
        lines = []
        for m in moves:
            pending = m._urp_open_return_qty()
            lines.append(self.env._(
                "- %(p)s: nothing left to return (delivered %(q)s, returned %(r)s, in open returns %(o)s).",
                p=m.product_id.display_name, q=m.quantity, r=m._urp_returned_qty(m), o=pending))
        return "\n".join(lines)

    def action_return_all(self):
        """Standard "Return All", capped for the policy's customer returns at what is still
        returnable (returns done and the other open returns deducted)."""
        res = super(StockPicking, urp_internal(self)).action_return_all()     # capped just below
        moves = self.move_ids._urp_governed_returns()
        if moves:
            urp_lock_rows(self.env.cr, "stock_move", moves.origin_returned_move_id.ids)
            for move in moves:
                cap = move.urp_returnable_qty
                line = move.sudo().urp_portal_line_id
                origin = move.origin_returned_move_id
                if line and line.move_id == origin and line.request_id.state == "active":
                    # a line executing a portal request: its open quantity not held by another linked return
                    held = sum(m.uom_id._compute_quantity(max(m.product_uom_qty, m.quantity), origin.uom_id)
                               for m in origin.sudo().returned_move_ids
                               if m != move and m.state not in ("done", "cancel") and m.urp_portal_line_id == line)
                    cap = min(cap, origin.uom_id._compute_quantity(max(line.open_qty - held, 0.0), move.uom_id,
                                                                   rounding_method="DOWN"))
                if move.uom_id.compare(move.product_uom_qty, cap) > 0:
                    urp_internal(move).write({"product_uom_qty": cap})
        return res

    def _urp_receipt_text(self, receipt_only):
        lines = []
        for e in receipt_only:
            if e["kind"] == "non_returnable":
                lines.append(self.env._("- %(p)s: non-returnable (return period of 0 days when it was sold).",
                                        p=e["product"].display_name))
            elif e["kind"] == "pos_receipt":
                lines.append(self.env._("- %(p)s: sold by POS receipt %(r)s under the return policy. Return it with "
                                        "POS Refund on that receipt.", p=e["product"].display_name, r=e["receipt"]))
            else:
                lines.append(self.env._("- %(p)s: grouped POS delivery, the quantity cannot be linked to its receipt. "
                                        "Return it with POS Refund on the original receipt.", p=e["product"].display_name))
        return "\n".join(lines)

    def _urp_expired_text(self, expired):
        lines = []
        for e in expired:
            # the stored deadline is an exclusive boundary (start of the next local day, UTC): show its
            # last minute in the timezone frozen with the policy, not in the user's timezone
            dl = "-"
            if e["deadline"]:
                params = e["origin"]._urp_policy_params()
                tz = (params and params[2]) or e["origin"].company_id.urp_tz or "UTC"
                local = e["deadline"].replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)) - timedelta(minutes=1)
                dl = "%s (%s)" % (local.strftime("%Y-%m-%d %H:%M"), tz)
            if e.get("kind") == "non_returnable":
                text = self.env._("- %(p)s: non-returnable (return period of 0 days when it was sold)", p=e["product"].display_name)
            else:
                text = self.env._("- %(p)s: return period has ended, return allowed until %(d)s", p=e["product"].display_name, d=dl)
            if e.get("approved"):
                text += self.env._(" - approved exception: %s unit(s) left", e["approved"])
            lines.append(text)
        return "\n".join(lines)

    def button_validate(self):
        if not is_urp_checked(self.env):
            violations = self.move_ids._urp_return_violations()
            if violations:
                return self.env["urp.return.check.wizard"]._urp_open(self, violations)
        return super().button_validate()
