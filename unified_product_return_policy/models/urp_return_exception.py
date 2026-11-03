"""Return exception requests: request -> administrator approval -> stock execution.

* The REQUEST (``urp.return.exception``) is long-lived and belongs to the sales channel that
  made the sale: a sales order (approved by a Sales Administrator) or a POS ship-later order
  executed from Inventory (approved by a POS Administrator).
* The APPROVAL is written in the existing audit trail ``urp.return.override`` (one line per
  original delivery, with its exception type and approved quantity).
* The EXECUTION is a standard Inventory return, done with the executing user's own stock
  rights; the guard consumes the approved quantity (``urp.return.exception.usage``) only for
  return moves that are actually validated, in the same transaction.

An approval never changes the product policy or the historical snapshot, never grants stock
rights, and is limited to the approved deliveries, products and quantities.
"""
from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .urp_guard import is_urp_internal, urp_internal
from .urp_return_override import CHANNEL_ADMIN_GROUP
from .urp_tools import urp_lock_rows

REQUEST_LINE_TYPES = [
    ("within_period", "Within Return Period (no approval)"),
    ("expired", "Expired Return Period Override"),
    ("non_returnable", "Non-Returnable Exception"),
]
EXCEPTION_TYPES = [
    ("expired", "Expired Return Period Override"),
    ("non_returnable", "Non-Returnable Exception"),
    ("standalone", "Standalone Refund Exception"),
]
STATES = [
    ("draft", "Draft"), ("pending", "Pending Approval"), ("approved", "Approved"),
    ("partially_used", "Partially Used"), ("used", "Used"), ("rejected", "Rejected"), ("cancelled", "Cancelled"),
]
USABLE_STATES = ("approved", "partially_used")
EDITABLE_FIELDS = ("reason", "line_ids")


class UrpReturnException(models.Model):
    _name = "urp.return.exception"
    _description = "Return Request"
    _order = "id desc"

    name = fields.Char(compute="_compute_name")
    source = fields.Selection([("sale", "Sales"), ("pos", "Point of Sale")], required=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    sale_order_id = fields.Many2one("sale.order", string="Sales Order", readonly=True, index="btree_not_null")
    pos_order_id = fields.Many2one("pos.order", string="POS Order", readonly=True, index="btree_not_null")
    state = fields.Selection(STATES, default="draft", required=True, readonly=True, index=True)
    reason = fields.Text("Reason for Exception")
    line_ids = fields.One2many("urp.return.exception.line", "exception_id", string="Products")
    requested_by = fields.Many2one("res.users", string="Requested By", readonly=True, default=lambda self: self.env.uid)
    request_date = fields.Datetime("Requested On", readonly=True)
    approved_by = fields.Many2one("res.users", string="Approved By", readonly=True)
    approved_by_employee_id = fields.Many2one("hr.employee", string="Approved By (POS Manager)", readonly=True)
    approved_at = fields.Datetime("Approved On", readonly=True)
    rejected_by = fields.Many2one("res.users", string="Rejected By", readonly=True)
    rejected_at = fields.Datetime("Rejected On", readonly=True)
    rejection_note = fields.Text("Rejection Note", readonly=True)
    cancelled_by = fields.Many2one("res.users", string="Cancelled By", readonly=True)
    cancelled_at = fields.Datetime("Cancelled On", readonly=True)
    override_id = fields.Many2one("urp.return.override", string="Approval Record", readonly=True)
    can_approve = fields.Boolean(compute="_compute_can_approve")
    needs_approval = fields.Boolean(compute="_compute_needs_approval",
                                    help="Some lines are out of the return policy and need the administrator's approval.")
    return_picking_ids = fields.Many2many("stock.picking", string="Return Transfers", compute="_compute_returns")
    return_picking_count = fields.Integer(compute="_compute_returns")
    can_create_returns = fields.Boolean(compute="_compute_returns")

    @api.depends("line_ids.exception_type")
    def _compute_needs_approval(self):
        for rec in self:
            rec.needs_approval = any(l.exception_type != "within_period" for l in rec.line_ids)

    def _compute_returns(self):
        for rec in self:
            moves = rec.line_ids.sudo().return_move_ids
            pickings = self.env["stock.picking"].browse(moves.picking_id.ids)
            rec.return_picking_ids = pickings
            rec.return_picking_count = len(pickings)
            rec.can_create_returns = rec.source == "sale" and any(
                l._urp_returnable_now() and l._urp_to_create() > 0 for l in rec.line_ids)

    def _compute_name(self):
        for rec in self:
            rec.name = "RE/%05d" % rec.id if rec.id else self.env._("New")

    can_approve_with_pin = fields.Boolean(compute="_compute_can_approve")

    def _compute_can_approve(self):
        for rec in self:
            rec.can_approve = rec._urp_user_may_approve()
            rec.can_approve_with_pin = rec.source == "pos" and rec.state in ("draft", "pending") and bool(
                rec.pos_order_id.sudo().config_id.module_pos_hr)

    # ------------------------------------------------------------------ protections
    @api.model_create_multi
    def create(self, vals_list):
        if not is_urp_internal(self.env):
            for vals in vals_list:
                for f in ("state", "approved_by", "approved_at", "rejected_by", "rejected_at", "rejection_note",
                          "cancelled_by", "cancelled_at", "override_id", "request_date"):
                    vals.pop(f, None)
                vals["requested_by"] = self.env.uid
        recs = super().create(vals_list)
        for rec in recs:
            rec._urp_check_document_access()
        return recs

    def write(self, vals):
        if not is_urp_internal(self.env):
            forbidden = set(vals) - set(EDITABLE_FIELDS)
            if forbidden:
                raise UserError(self.env._("Return exception requests change only through their actions."))
            if any(rec.state != "draft" for rec in self):
                raise UserError(self.env._("A submitted return exception request cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if not is_urp_internal(self.env) and any(rec.state != "draft" for rec in self):
            raise UserError(self.env._("Only draft return exception requests can be deleted; cancel it instead."))
        return super().unlink()

    def _urp_check_document_access(self):
        """The requester must be able to read the original document (standard rules)."""
        self.ensure_one()
        doc = self.sale_order_id if self.source == "sale" else self.pos_order_id
        if not doc or len(doc) != 1:
            raise UserError(self.env._("A return exception request needs its original order."))
        doc.check_access("read")
        if doc.company_id != self.company_id:
            raise UserError(self.env._("The request company must be the company of the original order."))

    # ------------------------------------------------------------------ rights
    def _urp_user_may_approve(self, user=None):
        self.ensure_one()
        user = user or self.env.user
        return (self.state in ("draft", "pending") and user.has_group(CHANNEL_ADMIN_GROUP[self.source])
                and self.company_id in user.company_ids)

    def _urp_check_approver(self):
        for rec in self:
            if not rec.env.user.has_group(CHANNEL_ADMIN_GROUP[rec.source]):
                raise AccessError(self.env._(
                    "Only a %s Administrator can approve or reject this return exception.",
                    "Sales" if rec.source == "sale" else "POS"))
            if rec.company_id not in rec.env.user.company_ids or rec.company_id not in rec.env.companies:
                raise AccessError(self.env._("You cannot approve a return exception of another company."))
            rec.check_access("write")

    # ------------------------------------------------------------------ validation of the lines
    def _urp_check_lines(self, lines=None):
        """Every line: an original delivery of THIS order and channel, for at most the quantity
        still returnable (returns already done AND open return transfers deducted). Sales lines
        within the return period need no approval; every other line needs an exception now."""
        for rec in self:
            lines = (lines & rec.line_ids) if lines is not None else rec.line_ids
            if not lines:
                raise UserError(self.env._("Add at least one product with a quantity to return."))
            if any(l.exception_type != "within_period" for l in lines) and not (rec.reason or "").strip():
                raise UserError(self.env._("Enter the reason for the exception."))
            seen = set()
            for line in lines:
                origin = line.origin_move_id.sudo()
                if origin.id in seen:
                    raise UserError(self.env._("%s appears twice in the request.", origin.product_id.display_name))
                seen.add(origin.id)
                if not rec._urp_is_own_delivery(origin):
                    raise UserError(self.env._("%s: this delivery does not belong to the original order.",
                                               origin.product_id.display_name))
                kind = origin._urp_exception_type() or ("within_period" if rec.source == "sale" else False)
                if not kind:
                    raise UserError(self.env._(
                        "%s: no exception is needed (still within its return period, or not under the return "
                        "policy); return it the usual way.", origin.product_id.display_name))
                if origin.uom_id.compare(line.requested_qty, 0.0) <= 0:
                    raise UserError(self.env._("%s: enter the quantity to return.", origin.product_id.display_name))
                available = line._urp_available_qty()
                if origin.uom_id.compare(line.requested_qty, available) > 0:
                    raise UserError(self.env._("%(p)s: %(q)s requested but only %(a)s delivered unit(s) can still be returned.",
                                               p=origin.product_id.display_name, q=line.requested_qty, a=available))
                if line.exception_type != kind:
                    urp_internal(line).exception_type = kind

    def _urp_is_own_delivery(self, origin):
        self.ensure_one()
        if origin.state != "done" or not origin._urp_is_customer_delivery() or origin.company_id != self.company_id:
            return False
        if origin._urp_origin_channel() != self.source:
            return False
        if self.source == "sale":
            return origin.sale_line_id.order_id == self.sale_order_id
        return origin.picking_id.pos_order_id == self.pos_order_id and bool(self.pos_order_id.shipping_date)

    # ------------------------------------------------------------------ actions
    def action_submit(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError(self.env._("Only a draft request can be submitted."))
            urp_internal(rec.line_ids.filtered(lambda l: not l.requested_qty)).unlink()
            rec._urp_lock_for_quantities()
            rec._urp_check_lines()
            exceptional = rec.line_ids.filtered(lambda l: l.exception_type != "within_period")
            urp_internal(rec).write({"state": "pending" if exceptional else "approved",
                                     "request_date": fields.Datetime.now()})
            rec._urp_start_normal_lines()
        return True

    def _urp_lock_for_quantities(self):
        """Serialise every decision on the quantities of these deliveries (submission, approval,
        automatic return creation, return validation): the request and its original moves."""
        self.ensure_one()
        urp_lock_rows(self.env.cr, "urp_return_exception", self.ids)
        origins = self.line_ids.sudo().origin_move_id
        if origins:
            urp_lock_rows(self.env.cr, "stock_move", origins.ids)
            origins.invalidate_recordset(["returned_move_ids"])
        self.invalidate_recordset()

    def _urp_start_normal_lines(self):
        """Lines within the return period: no approval, their Return Transfer is created now."""
        self.ensure_one()
        normal = self.line_ids.filtered(lambda l: l.exception_type == "within_period" and not l.approved_qty)
        for line in normal:
            urp_internal(line).approved_qty = line.requested_qty
        if normal:
            self._urp_create_returns(normal)

    def action_approve(self):
        """Approve exactly the requested quantities (self-approval allowed). The approval is
        recorded in the override audit trail; nothing is moved in stock here."""
        self._urp_check_approver()
        return self._urp_do_approve()

    def action_urp_approve_with_pin(self):
        """POS requests: approval by a POS Manager employee with the approval PIN (wizard)."""
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "urp.exception.pin.wizard", "view_mode": "form",
                "target": "new", "name": self.env._("POS Manager approval"),
                "context": {"default_exception_id": self.id}}

    def _urp_approve_with_pin(self, pin):
        """Identity proven by the approval PIN of a POS Manager employee of the original order's
        point of sale (server check, lockout); the Odoo user doing it needs no administrator group."""
        self.ensure_one()
        if self.source != "pos":
            raise AccessError(self.env._("Sales return exceptions are approved by a Sales Administrator."))
        if self.state not in ("draft", "pending"):
            raise UserError(self.env._("Only a draft or pending request can be approved."))
        self.check_access("write")
        if self.company_id not in self.env.companies:
            raise AccessError(self.env._("You cannot approve a return exception of another company."))
        config = self.pos_order_id.sudo().config_id
        if not config.module_pos_hr:
            raise UserError(self.env._("%s does not use employee login: approve with a POS Administrator account.", config.name))
        Attempt = self.env["urp.auth.attempt"].sudo()
        Attempt._urp_serialise(config)
        if Attempt._urp_pin_locked(config):
            return self.env._("Too many wrong approval PINs on %s: try again in 15 minutes.", config.name)
        employee, error = config._urp_identify_manager(pin)
        if error:
            # returned, not raised: the failed attempt must be committed to count for the lockout
            Attempt.create({"method": "pin", "config_id": config.id, "success": False})
            return self.env._("Approval PIN refused: it is not the approval PIN of a POS Manager of %s.", config.name)
        Attempt.create({"method": "pin", "employee_id": employee.id, "user_id": employee.user_id.id,
                        "config_id": config.id, "success": True})
        self._urp_do_approve(employee=employee, config=config)
        return None

    def _urp_do_approve(self, employee=None, config=None):
        self = self.with_context(urp_approver_employee=employee, urp_approver_config=config)
        now = fields.Datetime.now()
        Override = self.env["urp.return.override"].sudo()
        for rec in self:
            if rec.state not in ("draft", "pending"):
                raise UserError(self.env._("Only a draft or pending request can be approved."))
            rec._urp_lock_for_quantities()
            draft = rec.state == "draft"
            if draft:
                urp_internal(rec.line_ids.filtered(lambda l: not l.requested_qty)).unlink()
                rec._urp_check_lines()
            exceptional = rec.line_ids.filtered(lambda l: l.exception_type != "within_period")
            if not draft:
                rec._urp_check_lines(exceptional)
            override = Override.browse()
            if exceptional:
                override = Override.create(dict(rec._urp_approval_vals(), **{
                    "channel": rec.source, "reason": rec.reason.strip(), "company_id": rec.company_id.id,
                    "exception_id": rec.id,
                    "line_ids": [(0, 0, {"origin_move_id": l.origin_move_id.id, "product_id": l.product_id.id,
                                         "qty": l.requested_qty, "exception_type": l.exception_type,
                                         "exception_line_id": l.id}) for l in exceptional],
                }))
                for line in exceptional:
                    urp_internal(line).approved_qty = line.requested_qty
            urp_internal(rec).write(dict(rec._urp_approved_by_vals(), **{
                "state": "approved", "approved_at": now, "request_date": rec.request_date or now,
                "override_id": override.id}))
            if draft:
                rec._urp_start_normal_lines()
            if rec.source == "sale" and exceptional:
                rec._urp_create_returns(exceptional)
            rec._urp_refresh_usage_state()
        return True

    def _urp_approval_vals(self):
        """Identity of the approver on the audit record: the Odoo administrator logged in here,
        or the POS Manager employee identified by the approval PIN."""
        employee = self.env.context.get("urp_approver_employee")
        if employee:
            return {"user_id": employee.user_id.id or False, "approver_employee_id": employee.id,
                    "approver_config_id": self.env.context["urp_approver_config"].id,
                    "approver_role": "pos_manager_employee", "auth_method": "pin"}
        return {"user_id": self.env.uid, "approver_role": "%s_admin" % ("sales" if self.source == "sale" else "pos"),
                "auth_method": "odoo_session"}

    def _urp_approved_by_vals(self):
        employee = self.env.context.get("urp_approver_employee")
        if employee:
            return {"approved_by": employee.user_id.id or False, "approved_by_employee_id": employee.id}
        return {"approved_by": self.env.uid}

    def action_reject(self, note=None):
        self._urp_check_approver()
        for rec in self:
            if rec.state not in ("draft", "pending"):
                raise UserError(self.env._("Only a draft or pending request can be rejected."))
            urp_internal(rec).write({"state": "rejected", "rejected_by": self.env.uid,
                                     "rejected_at": fields.Datetime.now(), "rejection_note": note or False})
        return True

    def action_cancel(self):
        for rec in self:
            if rec.state in ("used", "rejected", "cancelled"):
                raise UserError(self.env._("This request can no longer be cancelled."))
            if rec.requested_by != self.env.user and not rec._urp_user_may_approve() and not self.env.user.has_group(
                    CHANNEL_ADMIN_GROUP[rec.source]):
                raise AccessError(self.env._("Only the requester or an administrator can cancel this request."))
            rec.check_access("write")
            # serialise with a return consuming this approval at the same moment
            urp_lock_rows(self.env.cr, "urp_return_exception", rec.ids)
            urp_internal(rec).write({"state": "cancelled", "cancelled_by": self.env.uid,
                                     "cancelled_at": fields.Datetime.now()})
            # its open Return Transfers are withdrawn with it (the user's own rights: no sudo)
            moves = rec.line_ids.sudo().return_move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
            pickings = self.env["stock.picking"].browse(moves.picking_id.ids)
            if pickings:
                pickings.action_cancel()
        return True

    # ------------------------------------------------------------------ automatic Return Transfers (sales)
    def action_urp_create_returns(self):
        """Create again what is approved but has no open Return Transfer (e.g. after one was
        cancelled before the goods arrived). Never more than approved and still returnable."""
        for rec in self:
            rec.check_access("write")
            if rec.source != "sale":
                raise UserError(self.env._("Return Transfers are created automatically for sales orders only."))
            lines = rec.line_ids.filtered(lambda l: l._urp_returnable_now())
            pickings = rec._urp_create_returns(lines)
            if not pickings:
                raise UserError(self.env._("Nothing left to return: every approved quantity already has a Return Transfer."))
        return self.action_urp_view_returns()

    def action_urp_view_returns(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "stock.picking", "name": self.env._("Return Transfers"),
                "view_mode": "list,form", "domain": [("id", "in", self.return_picking_ids.ids)],
                "context": {"create": False}}

    def _urp_create_returns(self, lines):
        """Same as Delivery Order > Return (standard ``stock.picking._create_return``), done for
        the user once the quantity is allowed: within the return period at submission, after the
        Sales Administrator's approval otherwise. One Return Transfer per original delivery, with
        exactly the quantities still approved and not already in an open return; left waiting for
        the physical receipt - an Inventory user validates it (never validated here).

        The standard creation runs with sudo() because Sales users have no right on the Return
        action; it is limited to this approved return: the request and the original moves are
        locked, the quantities re-checked, the policy guard of ``_create_return`` still applies
        to the real user, and validating keeps requiring the user's own Inventory rights."""
        self.ensure_one()
        cr = self.env.cr
        urp_lock_rows(cr, "urp_return_exception", self.ids)
        origins = lines.origin_move_id.sudo()
        if origins:
            urp_lock_rows(cr, "stock_move", origins.ids)
        self.invalidate_recordset()
        lines.invalidate_recordset()
        origins.invalidate_recordset(["returned_move_ids"])
        todo = {}
        for line in lines.filtered(lambda l: l._urp_returnable_now()):
            qty = line._urp_to_create()
            origin = line.origin_move_id.sudo()
            if origin.uom_id.compare(qty, 0.0) > 0:
                todo.setdefault(origin.picking_id, {})[origin] = (line, qty)
        created = self.env["stock.picking"]
        for delivery, entries in todo.items():
            ret = delivery.sudo()._create_return()
            keep = ret.move_ids.browse()
            for move in ret.move_ids:
                entry = entries.get(move.origin_returned_move_id)
                if entry:
                    urp_internal(move).write({"product_uom_qty": entry[1], "urp_request_line_id": entry[0].id})
                    keep |= move
            (ret.move_ids - keep).unlink()
            if not keep:
                ret.unlink()
                continue
            ret.action_confirm()
            ret.message_post(body=self.env._(
                "Created automatically from return request %(r)s (%(o)s). Validate it when the goods are received.",
                r=self.name, o=self.sale_order_id.name))
            created |= ret
        return self.env["stock.picking"].browse(created.ids)

    def _urp_refresh_usage_state(self):
        for rec in self.filtered(lambda r: r.state in USABLE_STATES):
            lines = rec.line_ids.filtered(lambda l: l.approved_qty > 0)
            lines.invalidate_recordset(["used_qty", "remaining_qty"])
            used = sum(lines.mapped("used_qty"))
            remaining = sum(lines.mapped("remaining_qty"))
            state = "used" if remaining <= 0 else ("partially_used" if used > 0 else "approved")
            if state != rec.state:
                urp_internal(rec).state = state

    # ------------------------------------------------------------------ creation from the orders
    @api.model
    def _urp_candidate_origins(self, source, order):
        if source == "sale":
            moves = order.order_line.move_ids
        else:
            moves = order.picking_ids.move_ids
        moves = moves.sudo().filtered(lambda m: m.state == "done" and m._urp_is_customer_delivery())
        return moves.filtered(lambda m: m._urp_origin_channel() == source
                              and (m._urp_exception_type() or source == "sale")
                              and m.uom_id.compare(m._urp_free_return_qty(), 0.0) > 0)

    @api.model
    def _urp_open_new(self, source, order):
        order.check_access("read")
        origins = self._urp_candidate_origins(source, order)
        if not origins:
            raise UserError(self.env._(
                "No delivered product of %s can be returned (nothing delivered, or everything already returned "
                "or in an open return).", order.display_name) if source == "sale" else self.env._(
                "No delivered product of %s needs a return exception: products still within their return period "
                "are returned the usual way.", order.display_name))
        req = self.create({
            "source": source, "company_id": order.company_id.id,
            ("sale_order_id" if source == "sale" else "pos_order_id"): order.id,
            "line_ids": [(0, 0, {"origin_move_id": m.id}) for m in origins],
        })
        return req._urp_action_open()

    def _urp_action_open(self):
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id, "view_mode": "form",
                "name": self.env._("Return Exception"), "target": "current"}


class UrpReturnExceptionLine(models.Model):
    _name = "urp.return.exception.line"
    _description = "Return Exception Request Line"

    exception_id = fields.Many2one("urp.return.exception", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="exception_id.company_id", store=True)
    origin_move_id = fields.Many2one("stock.move", string="Original Delivery Move", required=True, readonly=True, index=True)
    product_id = fields.Many2one("product.product", compute="_compute_origin_info", store=True)
    delivery_name = fields.Char("Original Delivery", compute="_compute_origin_info", store=True)
    exception_type = fields.Selection(REQUEST_LINE_TYPES, string="Return Type", readonly=True)
    policy_label = fields.Char("Historical Return Policy", compute="_compute_quantities")
    delivered_qty = fields.Float("Delivered", compute="_compute_quantities", digits="Product Unit")
    returned_qty = fields.Float("Already Returned", compute="_compute_quantities", digits="Product Unit")
    pending_qty = fields.Float("In Open Returns", compute="_compute_quantities", digits="Product Unit")
    available_qty = fields.Float("Available to Return", compute="_compute_quantities", digits="Product Unit")
    requested_qty = fields.Float("Requested", digits="Product Unit")
    approved_qty = fields.Float("Approved", readonly=True, digits="Product Unit")
    used_qty = fields.Float("Used", compute="_compute_usage", digits="Product Unit")
    remaining_qty = fields.Float("Remaining Approved", compute="_compute_usage", digits="Product Unit")
    return_move_ids = fields.One2many("stock.move", "urp_request_line_id", string="Return Moves", readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not is_urp_internal(self.env):
            for vals in vals_list:
                vals.pop("approved_qty", None)
                vals.pop("exception_type", None)
        lines = super().create(vals_list)
        for line in lines:
            if not is_urp_internal(self.env) and line.exception_id.state != "draft":
                raise UserError(self.env._("A submitted return exception request cannot be modified."))
            urp_internal(line).exception_type = line.origin_move_id.sudo()._urp_exception_type() or (
                "within_period" if line.exception_id.source == "sale" else False)
        return lines

    def write(self, vals):
        if not is_urp_internal(self.env):
            if set(vals) - {"requested_qty"} or any(l.exception_id.state != "draft" for l in self):
                raise UserError(self.env._("A submitted return exception request cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if not is_urp_internal(self.env) and any(l.exception_id.state != "draft" for l in self):
            raise UserError(self.env._("A submitted return exception request cannot be modified."))
        return super().unlink()

    @api.depends("origin_move_id")
    def _compute_origin_info(self):
        for line in self:
            origin = line.origin_move_id.sudo()
            line.product_id = origin.product_id
            line.delivery_name = origin.picking_id.name or origin.reference

    def _urp_available_qty(self):
        """Delivered minus returned minus already in an open return (whatever created it), minus the
        open portal quantity not held by a return linked to its request."""
        self.ensure_one()
        origin = self.origin_move_id.sudo()
        return max(origin._urp_free_return_qty() - origin._urp_portal_uncovered_qty(), 0.0)

    def _urp_open_qty(self):
        """Quantity of this line already in an open (not done, not cancelled) Return Transfer."""
        self.ensure_one()
        origin = self.origin_move_id.sudo()
        moves = self.sudo().return_move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        return sum(m.uom_id._compute_quantity(m.product_uom_qty, origin.uom_id) for m in moves)

    def _urp_returnable_now(self):
        """Approved (or within the period, submitted) and the request still open for execution."""
        self.ensure_one()
        req = self.exception_id
        if self.exception_type == "within_period":
            return req.state not in ("draft", "cancelled") and self.approved_qty > 0
        return req.state in USABLE_STATES and self.approved_qty > 0

    def _urp_to_create(self):
        """Approved, not used, not already in an open return - and still returnable at all."""
        self.ensure_one()
        own_open = self._urp_open_qty()
        return max(min(self.remaining_qty - own_open, self._urp_available_qty()), 0.0)

    def _compute_quantities(self):
        for line in self:
            origin = line.origin_move_id.sudo()
            params = origin._urp_policy_params() if origin else False
            days = params[0] if params else None
            if days == 0:
                label = self.env._("0 days (non-returnable)")
            elif days:
                deadline = origin._urp_delivery_deadline()
                label = self.env._("%(d)s days, until %(t)s (UTC)", d=days, t=deadline) if deadline else self.env._("%s days", days)
            else:
                label = ""
            line.policy_label = label
            line.delivered_qty = origin.quantity if origin else 0.0
            line.returned_qty = origin._urp_returned_qty(origin) if origin else 0.0
            line.pending_qty = origin._urp_open_return_qty() if origin else 0.0
            reserved = origin._urp_portal_uncovered_qty() if origin else 0.0
            line.available_qty = max(line.delivered_qty - line.returned_qty - line.pending_qty - reserved, 0.0)

    def _compute_usage(self):
        OvLine = self.env["urp.return.override.line"].sudo()
        for line in self:
            if line.exception_type == "within_period":
                # no approval to consume: what its own Return Transfers really received
                origin = line.origin_move_id.sudo()
                done = line.sudo().return_move_ids.filtered(lambda m: m.state == "done")
                used = sum(m.uom_id._compute_quantity(m.quantity, origin.uom_id) for m in done)
                line.used_qty = used
                line.remaining_qty = max(line.approved_qty - used, 0.0)
                continue
            ov_lines = OvLine.search([("exception_line_id", "=", line.id)]) if line.id else OvLine
            line.used_qty = sum(ov_lines.mapped("used_qty"))
            line.remaining_qty = sum(ov_lines.mapped("remaining_qty"))


class UrpReturnExceptionUsage(models.Model):
    """Quantity of an approval consumed by a validated return move (append-only audit)."""
    _name = "urp.return.exception.usage"
    _description = "Return Exception Usage"
    _order = "id desc"

    override_line_id = fields.Many2one("urp.return.override.line", required=True, readonly=True, index=True, ondelete="cascade")
    move_id = fields.Many2one("stock.move", string="Return Move", required=True, readonly=True, index=True)
    picking_id = fields.Many2one(related="move_id.picking_id", string="Return Document")
    qty = fields.Float(required=True, readonly=True, digits="Product Unit")
    user_id = fields.Many2one("res.users", string="Executed By", readonly=True, default=lambda self: self.env.uid)
    date = fields.Datetime("Executed On", readonly=True, default=fields.Datetime.now)
    company_id = fields.Many2one(related="override_line_id.override_id.company_id", store=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not is_urp_internal(self.env):
            raise UserError(self.env._("Approval usage is recorded by the return validation only."))
        return super().create(vals_list)

    def write(self, vals):
        raise UserError(self.env._("Approval usage records cannot be modified."))

    def unlink(self):
        if not is_urp_internal(self.env):
            raise UserError(self.env._("Approval usage records cannot be deleted."))
        return super().unlink()


class UrpReturnOverrideApproval(models.Model):
    _inherit = "urp.return.override"

    exception_id = fields.Many2one("urp.return.exception", string="Exception Request", readonly=True,
                                   index="btree_not_null",
                                   groups="sales_team.group_sale_salesman,point_of_sale.group_pos_user")
    usage_ids = fields.Many2many("urp.return.exception.usage", string="Executions", compute="_compute_usage_ids")

    def _compute_usage_ids(self):
        for rec in self:
            rec.usage_ids = rec.line_ids.usage_ids


class UrpReturnOverrideLineApproval(models.Model):
    _inherit = "urp.return.override.line"

    exception_type = fields.Selection(EXCEPTION_TYPES, string="Exception Type", readonly=True)
    exception_line_id = fields.Many2one("urp.return.exception.line", readonly=True, index="btree_not_null",
                                        groups="sales_team.group_sale_salesman,point_of_sale.group_pos_user")
    usage_ids = fields.One2many("urp.return.exception.usage", "override_line_id", string="Executions", readonly=True)
    used_qty = fields.Float("Used", compute="_compute_used", digits="Product Unit")
    remaining_qty = fields.Float("Remaining", compute="_compute_used", digits="Product Unit")
    approver_id = fields.Many2one(related="override_id.user_id", string="Approved By")
    approved_at = fields.Datetime(related="override_id.create_date", string="Approved On")
    usable = fields.Boolean(compute="_compute_used", help="Approval of a request still open for execution.")

    def write(self, vals):
        if not is_urp_internal(self.env):
            raise UserError(self.env._("Return overrides cannot be modified."))
        return super().write(vals)

    def _compute_used(self):
        for line in self:
            usages = line.sudo().usage_ids
            ov = line.sudo().override_id
            if usages:
                used = sum(usages.mapped("qty"))
            else:
                used = line.qty if ov.consumed_at else 0.0      # POS tokens and execution-time overrides
            line.used_qty = used
            line.remaining_qty = max(line.qty - used, 0.0)
            line.usable = bool(ov.exception_id) and ov.exception_id.state in USABLE_STATES and line.remaining_qty > 0


class SaleOrderReturnException(models.Model):
    _inherit = "sale.order"

    urp_exception_ids = fields.One2many("urp.return.exception", "sale_order_id", string="Return Exceptions")
    urp_exception_count = fields.Integer(compute="_compute_urp_exception_count")

    def _compute_urp_exception_count(self):
        for order in self:
            order.urp_exception_count = len(order.urp_exception_ids)

    urp_return_count = fields.Integer(compute="_compute_urp_return_count")

    def _compute_urp_return_count(self):
        for order in self:
            order.urp_return_count = len(order.urp_exception_ids.return_picking_ids)

    def action_urp_request_exception(self):
        self.ensure_one()
        return self.env["urp.return.exception"]._urp_open_new("sale", self)

    def action_urp_view_returns(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "stock.picking", "name": self.env._("Return Transfers"),
                "view_mode": "list,form", "domain": [("id", "in", self.urp_exception_ids.return_picking_ids.ids)],
                "context": {"create": False}}

    def action_urp_view_exceptions(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "urp.return.exception", "view_mode": "list,form",
                "name": self.env._("Return Exceptions"), "domain": [("sale_order_id", "=", self.id)],
                "context": {"create": False}}


class PosOrderReturnException(models.Model):
    _inherit = "pos.order"

    urp_exception_ids = fields.One2many("urp.return.exception", "pos_order_id", string="Return Exceptions")
    urp_exception_count = fields.Integer(compute="_compute_urp_exception_count")

    def _compute_urp_exception_count(self):
        for order in self:
            order.urp_exception_count = len(order.urp_exception_ids)

    def action_urp_request_exception(self):
        """Ship-later deliveries returned from Inventory: the POS Administrator approves."""
        self.ensure_one()
        if not self.shipping_date:
            raise UserError(self.env._("Delivered POS orders (no ship later) are returned with POS Refund on the receipt."))
        return self.env["urp.return.exception"]._urp_open_new("pos", self)

    def action_urp_view_exceptions(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "urp.return.exception", "view_mode": "list,form",
                "name": self.env._("Return Exceptions"), "domain": [("pos_order_id", "=", self.id)],
                "context": {"create": False}}


class StockPickingReturnException(models.Model):
    _inherit = "stock.picking"

    # what Inventory needs to execute an approved return, without access to the sales documents
    urp_approval_line_ids = fields.Many2many("urp.return.override.line", string="Approved Return Exceptions",
                                             compute="_compute_urp_approval_line_ids")

    def _compute_urp_approval_line_ids(self):
        OvLine = self.env["urp.return.override.line"]
        for picking in self:
            origins = picking.move_ids.filtered(lambda m: m.state == "done" and m.location_dest_usage == "customer")
            if not origins or not OvLine.has_access("read"):
                picking.urp_approval_line_ids = OvLine
                continue
            lines = OvLine.sudo().search([("origin_move_id", "in", origins.ids), ("override_id.exception_id", "!=", False)])
            picking.urp_approval_line_ids = OvLine.browse(lines.ids)
