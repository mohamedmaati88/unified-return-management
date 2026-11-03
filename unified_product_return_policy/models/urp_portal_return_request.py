from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .urp_guard import is_urp_internal, is_urp_split, urp_internal, urp_split
from .urp_tools import urp_lock_rows


class UrpPortalReturnRequest(models.Model):
    """Proof that a customer asked for a return through the portal, at a server time.

    Not a workflow: staff still create the standard Return from the delivery. The record
    only reserves quantities (no double request) and keeps the in-time right.
    """
    _name = "urp.portal.return.request"
    _description = "Portal Return Request"
    _order = "submitted_at desc, id desc"

    name = fields.Char(compute="_compute_name")
    order_id = fields.Many2one("sale.order", required=True, readonly=True, index=True, ondelete="cascade")
    partner_id = fields.Many2one(related="order_id.partner_id", store=True)
    company_id = fields.Many2one(related="order_id.company_id", store=True)
    submitted_at = fields.Datetime(required=True, readonly=True)
    idempotency_key = fields.Char(required=True, readonly=True)
    return_reason_id = fields.Many2one("return.reason", readonly=True)
    state = fields.Selection([("active", "Open"), ("done", "Processed"), ("cancelled", "Cancelled")],
                             default="active", required=True, readonly=True)
    line_ids = fields.One2many("urp.portal.return.request.line", "request_id", readonly=True)

    _order_key_uniq = models.Constraint("UNIQUE(order_id, idempotency_key)", "Duplicate return request.")

    def _compute_name(self):
        for rec in self:
            rec.name = "%s / %s" % (rec.order_id.name, rec.id)

    def _urp_refresh_state(self):
        for req in self.filtered(lambda r: r.state == "active"):
            if all(l.open_qty <= 0 for l in req.line_ids):
                req.state = "done" if any(l.processed_qty for l in req.line_ids) else "cancelled"

    def action_cancel_remaining(self):
        """Staff action: release the quantities not yet processed (no new right is granted).

        The rights of the calling user are checked on every request *before* ``sudo()``:
        sales group, read access to the request (company record rule) and write access to
        its sale order (company + salesman rules), and the order company must be one of the
        companies the user is currently working in."""
        if not self.env.user.has_group("sales_team.group_sale_salesman"):
            raise AccessError(self.env._("Only sales users can cancel a return request."))
        requests = self.exists()
        if not requests:
            return True
        requests.check_access("read")
        requests.order_id.check_access("write")
        allowed = self.env.companies
        if any(r.company_id not in allowed for r in requests):
            raise AccessError(self.env._("You cannot cancel a return request of another company."))
        active = requests.sudo().filtered(lambda r: r.state == "active")
        # the quantity an open return linked to the request is executing is never released
        # (same lock as the link and quantity checks of the returns: a link made at the same moment is
        # serialised with this cancellation)
        origins = active.line_ids.move_id
        if origins:
            urp_lock_rows(self.env.cr, "stock_move", origins.ids)
            origins.invalidate_recordset(["returned_move_ids"])
            active.line_ids.invalidate_recordset()
        plan, blocked = [], []
        for req in active:
            free_lines, held_moves = [], self.env["stock.move"]
            for line in req.line_ids.filtered(lambda l: l.open_qty > 0):
                held, moves = line._urp_held_by_returns()
                held_moves |= moves
                free = max(line.open_qty - held, 0.0)
                if line.move_id.uom_id.compare(free, 0.0) > 0:
                    free_lines.append((line, free))
            if free_lines:
                plan += free_lines
            elif held_moves:
                blocked.append((req, held_moves))
        if blocked:
            raise UserError(self.env._(
                "The remaining quantity cannot be cancelled: it is being returned by an open Inventory return.\n%s\n"
                "Validate or cancel that return first.",
                "\n".join("- %s: %s" % (req.name, ", ".join(moves.picking_id.mapped("name"))) for req, moves in blocked)))
        for line, free in plan:
            line.cancelled_qty = line.cancelled_qty + free
        active._urp_refresh_state()
        return True


class UrpPortalReturnRequestLine(models.Model):
    _name = "urp.portal.return.request.line"
    _description = "Portal Return Request Line"
    _rec_names_search = ["order_id.name"]

    request_id = fields.Many2one("urp.portal.return.request", required=True, ondelete="cascade", readonly=True, index=True)
    order_id = fields.Many2one(related="request_id.order_id", store=True)
    move_id = fields.Many2one("stock.move", string="Delivery", required=True, readonly=True, index=True)
    product_id = fields.Many2one(related="move_id.product_id", store=True)
    submitted_at = fields.Datetime(related="request_id.submitted_at", store=True)
    requested_qty = fields.Float(required=True, readonly=True, digits="Product Unit")
    processed_qty = fields.Float(readonly=True, digits="Product Unit")
    cancelled_qty = fields.Float(readonly=True, digits="Product Unit")
    open_qty = fields.Float(compute="_compute_open_qty", store=True, digits="Product Unit")
    deadline_at_submit = fields.Datetime(
        "Portal Deadline at Submission", readonly=True,
        help="Portal deadline in force when the customer submitted the request. The request keeps the "
             "right it had then, even if the Effective Date of the delivery is corrected afterwards.")
    in_time = fields.Boolean("Submitted in Time", readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        for line in lines:
            deadline = line.move_id.urp_portal_deadline
            line.write({"deadline_at_submit": deadline,
                        "in_time": bool(deadline and line.submitted_at and line.submitted_at < deadline)})
        return lines

    @api.depends("requested_qty", "processed_qty", "cancelled_qty")
    def _compute_open_qty(self):
        for line in self:
            line.open_qty = max(line.requested_qty - line.processed_qty - line.cancelled_qty, 0.0)

    def _urp_held_by_returns(self):
        """Open quantity of this request line held by open return moves linked to it (backorders
        included), in the unit of its delivery move, and those moves. Cancelled and done returns
        hold nothing."""
        self.ensure_one()
        origin = self.sudo().move_id
        moves = self.env["stock.move"].sudo().search([("urp_portal_line_id", "=", self.id),
                                                       ("state", "not in", ("done", "cancel"))])
        held = sum(m.uom_id._compute_quantity(max(m.product_uom_qty, m.quantity), origin.uom_id) for m in moves)
        return min(held, self.open_qty), moves

    @api.depends("request_id", "submitted_at", "open_qty", "in_time")
    def _compute_display_name(self):
        """What the Inventory user picks in the Portal Request column of a return line."""
        for line in self:
            req = line.sudo().request_id
            day = fields.Date.to_string(fields.Datetime.context_timestamp(line, line.submitted_at)) if line.submitted_at else ""
            text = "%s / %s · %s · %s %s" % (req.order_id.name or "", req.id or "", day, line.open_qty, self.env._("open"))
            if line.in_time:
                text += " · %s" % self.env._("in time")
            line.display_name = text


class StockMovePortal(models.Model):
    _inherit = "stock.move"

    urp_request_line_ids = fields.One2many("urp.portal.return.request.line", "move_id")
    # return line -> the customer's portal request it executes, chosen by the Inventory user
    urp_portal_line_id = fields.Many2one(
        "urp.portal.return.request.line", string="Portal Request", copy=False, index="btree_not_null",
        ondelete="restrict",
        domain="[('move_id', '=', origin_returned_move_id), ('request_id.state', '=', 'active'), ('open_qty', '>', 0)]",
        help="Select it only when this return executes that customer's portal request. Leave it empty for any "
             "other return: an empty line never uses a portal request, its quantity or its in-time right.")

    # ------------------------------------------------------------------ quantities of a delivery move
    def _urp_portal_breakdown(self, exclude=()):
        """Unified rule on a delivery move (``self``), in its unit: ``(R, U, {line: (O_p, L_p)})``.

        R: returns done; U: open returns not executing an open request of this delivery (independent);
        O_p: open quantity of each active portal request line; L_p: open returns linked to it.
        Committed = R + U + sum(max(O_p, L_p)): a linked return IS its request being executed, so the
        larger of the two counts once. ``exclude``: ids of return moves left out (the one being edited)."""
        self.ensure_one()
        origin = self.sudo()
        lines = origin.urp_request_line_ids.filtered(lambda l: l.request_id.state == "active")
        linked = dict.fromkeys(lines.ids, 0.0)
        independent = 0.0
        for move in origin.returned_move_ids:
            if move.state in ("done", "cancel") or move.id in exclude:
                continue
            demand = move.uom_id._compute_quantity(max(move.product_uom_qty, move.quantity), origin.uom_id)
            if move.urp_portal_line_id.id in linked:
                linked[move.urp_portal_line_id.id] += demand
            else:
                independent += demand
        return origin._urp_returned_qty(origin), independent, {l: (l.open_qty, linked[l.id]) for l in lines}

    def _urp_portal_uncovered_qty(self, exclude=(), skip_line=None):
        """Open portal quantity of this delivery move not yet held by an open return linked to it
        (``skip_line``: request lines left out, those the return line being checked executes)."""
        _r, _u, lines = self._urp_portal_breakdown(exclude)
        skip = skip_line if skip_line is not None else self.env["urp.portal.return.request.line"]
        return sum(max(o - l, 0.0) for line, (o, l) in lines.items() if line not in skip)

    def _urp_portal_available(self):
        """Portal: delivered - committed (returns done, open returns, open portal requests)."""
        self.ensure_one()
        r, u, lines = self._urp_portal_breakdown()
        return self.quantity - (r + u + sum(max(o, l) for o, l in lines.values()))

    def _urp_in_time_request_open(self):
        """A request submitted within the period is still open: the delivery line stays on a new
        Return after the deadline, so that the Inventory user can select it (never selected for him)."""
        self.ensure_one()
        return any(l.request_id.state == "active" and l.in_time and l.open_qty > 0
                   for l in self.sudo().urp_request_line_ids)

    def _urp_portal_hint(self, moves=None, reserved_only=False):
        """Text naming the open portal requests of this delivery move not executed by ``moves``."""
        self.ensure_one()
        _r, _u, lines = self._urp_portal_breakdown()
        mine = (moves or self.browse()).sudo().urp_portal_line_id
        names = ["%s (%s %s%s)" % (line.request_id.name, max(o - l, 0.0) if reserved_only else o,
                                   self.env._("open"), ", %s" % self.env._("submitted in time") if line.in_time else "")
                 for line, (o, l) in lines.items()
                 if line not in mine and (max(o - l, 0.0) > 0 if reserved_only else o > 0)]
        if not names:
            return ""
        return self.env._("Open portal request(s) of this delivery: %(r)s. If this return executes one of them, "
                          "select it in the Portal Request column of the return line.", r=", ".join(names))

    # ------------------------------------------------------------------ link checks (server side)
    def _urp_check_portal_link(self, new_ids=(), vals=None):
        """The link is checked on the server for every caller (form, import, RPC, module code): the
        request must be one of the delivery line this return line returns (so the same order, product
        and company), it must still be open and readable by the user, and an open request already held
        by another open return cannot be executed twice. Origins are locked as for the quantities.

        ``vals``: the values about to be written (checked before the write, so that a wrong link is
        reported as such, before any quantity check); ``new_ids``: lines whose link is new."""
        vals = vals or {}
        Line, Move = self.env["urp.portal.return.request.line"], self.env["stock.move"]
        todo = []
        for move in self:
            origin = Move.browse(vals["origin_returned_move_id"]) if "origin_returned_move_id" in vals else move.origin_returned_move_id
            if "urp_portal_line_id" in vals:
                line = Line.browse(vals["urp_portal_line_id"] or [])
            elif origin != move.origin_returned_move_id:
                line = Line                                  # moved to another delivery line: link dropped
            else:
                line = move.urp_portal_line_id
            if line:
                product = self.env["product.product"].browse(vals["product_id"]) if "product_id" in vals else move.product_id
                company = self.env["res.company"].browse(vals["company_id"]) if "company_id" in vals else move.company_id
                todo.append((move, line, origin, product, company))
        if not todo:
            return
        origins = Move.union(*(t[2] for t in todo))
        if origins:
            urp_lock_rows(self.env.cr, "stock_move", origins.ids)
            origins.invalidate_recordset(["returned_move_ids"])
        for move, line, origin, product, company in todo:
            new = move.id in new_ids and not is_urp_split(self.env)
            if new:
                line.check_access("read")                    # the user's own rights (record rules)
            line, origin = line.sudo(), origin.sudo()
            if (not origin or line.move_id != origin or origin.state != "done" or not origin._urp_is_customer_delivery()
                    or line.product_id != product or line.request_id.company_id != company):
                raise UserError(self.env._(
                    "%(p)s: the Portal Request must be a request for the very delivery line this return line returns.",
                    p=product.display_name))
            if not new:
                continue                                    # unchanged link, or carried to its backorder
            if move.state in ("done", "cancel"):
                raise UserError(self.env._("A Portal Request cannot be selected on a done or cancelled return line."))
            if line.request_id.state != "active" or origin.uom_id.compare(line.open_qty, 0.0) <= 0:
                raise UserError(self.env._("%(r)s: this portal request has no open quantity left.", r=line.display_name))
            others = origin.returned_move_ids.filtered(lambda m: m.state not in ("done", "cancel") and m != move
                                                       and m.urp_portal_line_id == line)
            held = sum(m.uom_id._compute_quantity(max(m.product_uom_qty, m.quantity), origin.uom_id) for m in others)
            if origin.uom_id.compare(held, line.open_qty) >= 0:
                raise UserError(self.env._("%(r)s is already being executed by %(t)s.",
                                           r=line.display_name, t=", ".join(others.picking_id.mapped("name"))))

    @api.model_create_multi
    def create(self, vals_list):
        moves = super().create(vals_list)
        moves._urp_check_portal_link(new_ids=set(moves.filtered("urp_portal_line_id").ids))
        return moves

    def write(self, vals):
        if "urp_portal_line_id" in vals and not is_urp_internal(self.env):
            new = vals["urp_portal_line_id"] or False
            if any(m.state in ("done", "cancel") and m.urp_portal_line_id.id != new for m in self):
                raise UserError(self.env._("The Portal Request of a done or cancelled return line cannot be changed."))
        relinked = self.browse()
        if "origin_returned_move_id" in vals and "urp_portal_line_id" not in vals:
            # a return line moved to another delivery line no longer executes the request of the first one
            relinked = self.filtered(lambda m: m.urp_portal_line_id and m.origin_returned_move_id.id != vals["origin_returned_move_id"])
        if "urp_portal_line_id" in vals or set(vals) & {"origin_returned_move_id", "product_id", "company_id"}:
            changed = self.filtered(lambda m: m.urp_portal_line_id.id != (vals.get("urp_portal_line_id") or False)) \
                if "urp_portal_line_id" in vals else self.browse()
            self._urp_check_portal_link(new_ids=set(changed.ids), vals=vals)       # before the quantity checks
        res = super().write(vals)
        if relinked:
            urp_internal(relinked).write({"urp_portal_line_id": False})
        return res

    def _prepare_move_split_vals(self, qty):
        vals = super()._prepare_move_split_vals(qty)
        vals["urp_portal_line_id"] = self.urp_portal_line_id.id     # the backorder still executes the request
        return vals

    def _prepare_merge_moves_distinct_fields(self):
        return super()._prepare_merge_moves_distinct_fields() + ["urp_portal_line_id"]

    def _create_backorder(self):
        return super(StockMovePortal, urp_split(self))._create_backorder().with_env(self.env)

    def _urp_portal_open(self, now=None):
        self.ensure_one()
        now = now or fields.Datetime.now()
        if self._urp_non_returnable():
            return False
        return bool(self.urp_batch_id.portal_eligible and self.urp_portal_deadline and now < self.urp_portal_deadline)

    def _urp_grace_qty(self, origin):
        """Open quantity requested through the portal before the portal deadline, for
        the return lines (``self``) that execute that request only: an independent return
        of the same delivery never uses the right of a request it is not linked to."""
        # the right is the one of the submission time (frozen on the line), not the current deadline;
        # only ACTIVE requests count: a cancelled or processed request is never revived
        executed = {}
        for move in self.filtered(lambda m: m.origin_returned_move_id == origin and m.state != "cancel"
                                  and m.urp_portal_line_id):
            line = move.sudo().urp_portal_line_id
            if line.move_id == origin and line.request_id.state == "active" and line.in_time:
                executed[line] = executed.get(line, 0.0) + move.uom_id._compute_quantity(move.quantity, origin.uom_id)
        portal = sum(min(qty, line.open_qty) for line, qty in executed.items())
        # a return entered within the period documents the same goods: the larger of the two, never their sum
        return max(super()._urp_grace_qty(origin), portal)

    def _action_done(self, cancel_backorder=False):
        res = super()._action_done(cancel_backorder=cancel_backorder)
        # only the request a return line is linked to is consumed, with the quantity really
        # returned (never the oldest request of the delivery: an independent return consumes nothing)
        lines = self.env["urp.portal.return.request.line"].sudo()
        for move in res.filtered(lambda m: m.state == "done" and m.origin_returned_move_id and m.urp_portal_line_id):
            origin = move.origin_returned_move_id
            line = move.sudo().urp_portal_line_id
            if line.move_id != origin or line.request_id.state != "active":
                continue
            take = min(move.uom_id._compute_quantity(move.quantity, origin.uom_id), line.open_qty)
            if take > 0:
                line.processed_qty = line.processed_qty + take
                lines |= line
        lines.request_id._urp_refresh_state()
        return res


class SaleOrderPortal(models.Model):
    _inherit = "sale.order"

    urp_request_ids = fields.One2many("urp.portal.return.request", "order_id", string="Portal Return Requests", readonly=True)

    def _urp_governed_deliveries(self):
        self.ensure_one()
        return self.order_line.move_ids.filtered(
            lambda m: m.state == "done" and m.urp_batch_id and m._urp_is_customer_delivery())

    def _urp_portal_open_deliveries(self):
        return self._urp_governed_deliveries().filtered(lambda m: m._urp_portal_open() and m._urp_portal_available() > 0)

    def _is_portal_return_allowed(self):
        self.ensure_one()
        governed = self._urp_governed_deliveries()
        if not governed:
            return super()._is_portal_return_allowed()
        # current switch decides whether NEW requests may be submitted; history decides the rights
        return bool(self.company_id.allow_spontaneous_returns and self.state == "sale"
                    and self._urp_portal_open_deliveries())
