from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError

from .urp_guard import is_urp_internal, urp_internal

from .urp_tools import urp_deadline_boundary, urp_lock_rows
from odoo.addons.base.models.ir_model import MODULE_UNINSTALL_FLAG

URP_MOVE_FIELDS = ("urp_batch_id", "urp_deadline", "urp_portal_deadline", "urp_validated_at", "urp_date_basis",
                   "urp_claim_qty", "urp_claimed_at", "urp_request_line_id")
URP_STAMP_FIELDS = ("urp_deadline", "urp_portal_deadline", "urp_validated_at", "urp_date_basis",
                    "urp_claim_qty", "urp_claimed_at")
URP_CLAIM_TRIGGERS = ("product_uom_qty", "quantity")


class StockMove(models.Model):
    _inherit = "stock.move"

    urp_batch_id = fields.Many2one("urp.sale.batch", string="Return Policy Batch", copy=False, readonly=True,
                                   index="btree_not_null")
    # return move created for a return request line (kept by backorder splits, reset on a return of the return)
    urp_request_line_id = fields.Many2one("urp.return.exception.line", string="Return Request Line", readonly=True,
                                          index="btree_not_null", ondelete="set null")
    urp_deadline = fields.Datetime("Return Deadline", copy=False, readonly=True,
                                   help="Exclusive boundary (UTC): returns are allowed strictly before this instant.")
    urp_portal_deadline = fields.Datetime("Portal Return Deadline", copy=False, readonly=True)
    urp_validated_at = fields.Datetime(
        "Actual Validation Date", copy=False, readonly=True,
        help="Server time at which this delivery was validated. Set once by the module, never editable "
             "(unlike the Effective Date of the transfer).")
    urp_date_basis = fields.Selection(
        [("validation", "Actual Validation Date"), ("effective", "Effective Date")],
        string="Return Period Based On", copy=False, readonly=True,
        help="Frozen from the sale. Effective Date: the deadline follows corrections of the effective date.")
    # return moves: documented in-time return request (server time), never editable by users
    urp_claim_qty = fields.Float(
        "Quantity Requested in Time", copy=False, readonly=True, digits="Product Unit",
        help="Highest quantity of this return entered before the return deadline of the delivery "
             "(server time). It keeps its right if the return is validated after the deadline.")
    urp_claimed_at = fields.Datetime("Return Requested On", copy=False, readonly=True,
                                     help="Server time at which the quantity was first entered within the period.")

    @api.model_create_multi
    def create(self, vals_list):
        if not is_urp_internal(self.env):
            for vals in vals_list:
                # deadlines and validation data are only ever stamped by the module at delivery
                for f in URP_STAMP_FIELDS:
                    vals.pop(f, None)
        moves = super().create(vals_list)
        if not is_urp_internal(self.env):
            # a batch may only be linked to a move of its own sale order line (procurement, split)
            wrong = moves.filtered(lambda m: m.urp_batch_id and m.urp_batch_id.sale_line_id != m.sale_line_id)
            # a request line link is only ever carried over by a split of its own return move
            wrong |= moves.filtered(lambda m: m.urp_request_line_id
                                    and m.urp_request_line_id.sudo().origin_move_id != m.origin_returned_move_id)
            if wrong:
                raise UserError(self.env._("Return policy data on stock moves cannot be set manually."))
        moves.filtered("origin_returned_move_id")._urp_record_claims()
        if not is_urp_internal(self.env):
            moves._urp_check_return_demand()
        return moves

    def write(self, vals):
        if not is_urp_internal(self.env) and set(vals) & set(URP_MOVE_FIELDS):
            raise UserError(self.env._("Return policy data on stock moves cannot be modified."))
        follow = self.browse()
        if "date" in vals:
            # Effective Date corrected on a validated delivery (picking.date_done -> move.date)
            follow = self.filtered(lambda m: m.state == "done" and m.urp_date_basis == "effective" and m.urp_deadline)
            if follow:
                # serialise with return validation (which locks and writes the origin moves, see urp_lock_rows)
                self.env.cr.execute("SELECT id FROM stock_move WHERE id IN %s FOR UPDATE", [tuple(follow.ids)])
        before = {m.id: (m.date, m.urp_deadline, m.urp_portal_deadline) for m in follow}
        check_demand = not is_urp_internal(self.env) and set(vals) & {"product_uom_qty", "quantity", "origin_returned_move_id",
                                                                     "state", "urp_portal_line_id"}
        demand_before = {m.id: (m.state, m.origin_returned_move_id.id, max(m.product_uom_qty, m.quantity),
                                m.urp_portal_line_id.id) for m in self} if check_demand else {}
        relinked = self.browse()
        if "origin_returned_move_id" in vals and not is_urp_internal(self.env):
            # a documented request belongs to the delivery it was made for: re-linking a return
            # line to another delivery drops it (it never carries a right over to that delivery)
            relinked = self.filtered(lambda m: m.urp_claim_qty and m.origin_returned_move_id.id != vals["origin_returned_move_id"])
        res = super().write(vals)
        if relinked:
            urp_internal(relinked).write({"urp_claim_qty": 0.0, "urp_claimed_at": False})
        if follow:
            follow._urp_follow_effective_date(before)
        if set(vals) & set(URP_CLAIM_TRIGGERS):
            self.filtered("origin_returned_move_id")._urp_record_claims()
        if check_demand:
            # only a return whose own demand grows (or that is re-opened / re-linked) is checked:
            # validation writes and other returns already open are never blocked by it
            grown = self.filtered(lambda m: m.id in demand_before and (
                demand_before[m.id][0] in ("done", "cancel")
                or demand_before[m.id][1] != m.origin_returned_move_id.id
                or demand_before[m.id][3] != m.urp_portal_line_id.id          # a portal request (un)selected
                or m.uom_id.compare(max(m.product_uom_qty, m.quantity), demand_before[m.id][2]) > 0))
            grown._urp_check_return_demand()
        return res

    # ------------------------------------------------------------------ open return quantities
    def _urp_governed_returns(self):
        """Open return moves of a validated customer delivery under the return policy."""
        return self.filtered(lambda m: m.state not in ("done", "cancel") and m.origin_returned_move_id
                             and m.origin_returned_move_id.state == "done"
                             and m.origin_returned_move_id.location_dest_usage == "customer"
                             and m.origin_returned_move_id._urp_origin_channel())

    def _urp_return_demand(self, origin):
        """Quantity asked by these return moves, in the unit of ``origin`` (demand or quantity entered)."""
        return sum(m.uom_id._compute_quantity(max(m.product_uom_qty, m.quantity), origin.uom_id) for m in self)

    def _urp_check_return_demand(self):
        """Refuse, when it is entered, a return quantity above what is still returnable: delivered,
        minus returns already done, minus every other open return of that delivery line (whoever
        created it). Locks the original moves (same serialisation as validation). Purchase returns
        and other transfers are never concerned. Validation still re-checks everything."""
        moves = self._urp_governed_returns()
        if not moves:
            return
        origins = moves.origin_returned_move_id
        urp_lock_rows(self.env.cr, "stock_move", origins.ids)
        origins.invalidate_recordset(["returned_move_ids"])
        problems = []
        for origin in origins:
            open_moves = origin.returned_move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
            done = origin._urp_returned_qty(origin)
            demanded = open_moves._urp_return_demand(origin)
            # open portal quantity not yet held by a return linked to its request
            reserved = origin._urp_portal_uncovered_qty()
            if origin.uom_id.compare(demanded + reserved, origin.quantity - done) > 0:
                own = moves.filtered(lambda m: m.origin_returned_move_id == origin)
                own_lines = own.sudo().urp_portal_line_id               # requests these lines execute
                most = max(origin.quantity - done - (demanded - own._urp_return_demand(origin))
                           - origin._urp_portal_uncovered_qty(exclude=set(own.ids), skip_line=own_lines), 0.0)
                if origin.uom_id.is_zero(reserved):
                    problems.append(self.env._(
                        "- %(p)s (%(d)s): delivered %(q)s, already returned %(r)s, in open returns %(o)s - "
                        "at most %(a)s can still be returned.",
                        p=origin.product_id.display_name, d=origin.picking_id.name or origin.reference, q=origin.quantity,
                        r=done, o=demanded, a=most))
                elif not own_lines:
                    problems.append(self.env._(
                        "- %(p)s (%(d)s): delivered %(q)s, already returned %(r)s, in open returns %(o)s, reserved by "
                        "open portal requests %(s)s - at most %(a)s can still be returned without a portal request.\n  %(h)s",
                        p=origin.product_id.display_name, d=origin.picking_id.name or origin.reference, q=origin.quantity,
                        r=done, o=demanded, s=reserved, a=most, h=origin._urp_portal_hint(own, reserved_only=True)))
                else:
                    problems.append(self.env._(
                        "- %(p)s (%(d)s): delivered %(q)s, already returned %(r)s, in open returns %(o)s, reserved by "
                        "other open portal requests %(s)s - at most %(a)s can still be returned on this return line.",
                        p=origin.product_id.display_name, d=origin.picking_id.name or origin.reference, q=origin.quantity,
                        r=done, o=demanded, s=reserved, a=most))
        if problems:
            raise UserError(self.env._("This return asks for more than can still be returned:\n%s", "\n".join(problems)))

    urp_returnable_qty = fields.Float("Still Returnable", compute="_compute_urp_returnable_qty", digits="Product Unit",
                                      help="Delivered, minus returns done, minus the other open returns of that delivery.")

    @api.depends("origin_returned_move_id", "urp_portal_line_id")
    def _compute_urp_returnable_qty(self):
        for move in self:
            origin = move.origin_returned_move_id
            if not origin or origin.state != "done" or origin.location_dest_usage != "customer":
                move.urp_returnable_qty = 0.0
                continue
            origin = origin.sudo()
            others = origin.returned_move_ids.filtered(lambda m: m.state not in ("done", "cancel") and m.id != move.id
                                                       and m.id != move._origin.id)
            free = origin.quantity - origin._urp_returned_qty(origin) - others._urp_return_demand(origin)
            # open portal requests not held by another return; the request this line executes is its own
            link = move.sudo().urp_portal_line_id
            free -= origin._urp_portal_uncovered_qty(exclude={move.id, move._origin.id}, skip_line=link)
            move.urp_returnable_qty = origin.uom_id._compute_quantity(max(free, 0.0), move.uom_id)

    def _action_cancel(self):
        res = super()._action_cancel()
        # a cancelled return gives no right any more, even if it were ever reset to draft
        claimed = self.filtered(lambda m: m.state == "cancel" and m.urp_claim_qty)
        if claimed:
            urp_internal(claimed).write({"urp_claim_qty": 0.0})
        return res

    def _prepare_merge_moves_distinct_fields(self):
        return super()._prepare_merge_moves_distinct_fields() + ["urp_batch_id"]

    @api.model
    def _prepare_merge_negative_moves_excluded_distinct_fields(self):
        return super()._prepare_merge_negative_moves_excluded_distinct_fields() + ["urp_batch_id"]

    def _prepare_move_split_vals(self, qty):
        vals = super()._prepare_move_split_vals(qty)
        vals["urp_batch_id"] = self.urp_batch_id.id
        return vals

    def _urp_is_customer_delivery(self):
        self.ensure_one()
        return self.location_dest_usage == "customer" and not self.origin_returned_move_id

    # ------------------------------------------------------------------ deadlines
    def _urp_policy_params(self):
        """(return_days, portal_days or None, tz, date_basis) frozen at the sale; False if not governed."""
        self.ensure_one()
        b = self.sudo().urp_batch_id
        if not b:
            return False
        return b.return_days, (b.portal_days if b.portal_eligible else None), b.tz, b.date_basis or "validation"

    def _urp_deadline_vals(self, reference):
        days, portal_days, tz, _basis = self._urp_policy_params()
        if days == 0:
            # non-returnable: no deadline at all (never "returnable until the end of day 0")
            return {"urp_deadline": False, "urp_portal_deadline": False}
        vals = {"urp_deadline": urp_deadline_boundary(reference, days, tz)}
        if portal_days is not None:
            vals["urp_portal_deadline"] = urp_deadline_boundary(reference, portal_days, tz)
        return vals

    def _urp_stamp_deadlines(self):
        """At validation: record the server validation time and the frozen calculation basis, and
        store the deadlines from the reference date of that basis (day 0 = that local day)."""
        now = fields.Datetime.now()
        for move in self.filtered(lambda m: m.state == "done" and not m.urp_deadline and m._urp_is_customer_delivery()):
            params = move._urp_policy_params()
            if not params:
                continue
            basis = params[3]
            reference = move.date if basis == "effective" else now
            vals = move._urp_deadline_vals(reference)
            vals.update({"urp_validated_at": now, "urp_date_basis": basis})
            urp_internal(move).write(vals)

    def _urp_follow_effective_date(self, before):
        """Effective Date basis: recompute the deadlines from the corrected effective date, with
        the return days frozen at the sale, and keep an audit row for every change."""
        Log = self.env["urp.deadline.log"].sudo()
        for move in self:
            old_date, old_deadline, old_portal = before[move.id]
            vals = move._urp_deadline_vals(move.date)
            if vals.get("urp_deadline") == old_deadline and vals.get("urp_portal_deadline", old_portal) == old_portal:
                continue
            urp_internal(move).write(vals)
            Log.create({
                "move_id": move.id, "picking_id": move.picking_id.id, "company_id": move.company_id.id,
                "old_date": old_date, "new_date": move.date,
                "old_deadline": old_deadline, "new_deadline": move.urp_deadline,
                "old_portal_deadline": old_portal, "new_portal_deadline": move.urp_portal_deadline,
            })
            if move.picking_id:
                move.picking_id.sudo().message_post(body=self.env._(
                    "Return deadline of %(p)s recalculated from the corrected Effective Date: %(o)s -> %(n)s (UTC).",
                    p=move.product_id.display_name, o=old_deadline, n=move.urp_deadline))

    def _action_done(self, cancel_backorder=False):
        moves = super()._action_done(cancel_backorder=cancel_backorder)
        moves._urp_stamp_deadlines()
        return moves


class UrpDeadlineLog(models.Model):
    """Append-only audit of return deadlines recalculated after an Effective Date correction."""
    _name = "urp.deadline.log"
    _description = "Return Deadline Change"
    _order = "create_date desc, id desc"

    move_id = fields.Many2one("stock.move", string="Delivery", required=True, readonly=True, index=True, ondelete="cascade")
    picking_id = fields.Many2one("stock.picking", readonly=True)
    company_id = fields.Many2one("res.company", readonly=True)
    old_date = fields.Datetime("Previous Effective Date", readonly=True)
    new_date = fields.Datetime("New Effective Date", readonly=True)
    old_deadline = fields.Datetime("Previous Deadline", readonly=True)
    new_deadline = fields.Datetime("New Deadline", readonly=True)
    old_portal_deadline = fields.Datetime("Previous Portal Deadline", readonly=True)
    new_portal_deadline = fields.Datetime("New Portal Deadline", readonly=True)
    user_id = fields.Many2one("res.users", readonly=True, default=lambda self: self.env.uid)

    def write(self, vals):
        raise UserError(self.env._("The return deadline log is append-only."))

    def unlink(self):
        if not self.env.context.get(MODULE_UNINSTALL_FLAG):
            raise UserError(self.env._("The return deadline log is append-only."))
        return super().unlink()


class StockRule(models.Model):
    _inherit = "stock.rule"

    def _get_custom_move_fields(self):
        return super()._get_custom_move_fields() + ["urp_batch_id"]


class StockMoveReturnPolicy(models.Model):
    _inherit = "stock.move"

    urp_return_deadline = fields.Datetime(related="origin_returned_move_id.urp_deadline", string="Return Deadline (origin)")

    # ------------------------------------------------------------------ scope
    def _urp_origin_channel(self):
        """Channel whose policy governs the delivery ``self`` (an origin move), or False."""
        self.ensure_one()
        if self.sudo().urp_batch_id:
            return "sale"
        return False

    def _urp_delivery_deadline(self):
        self.ensure_one()
        return self.urp_deadline

    def _urp_non_returnable(self):
        """True when the policy frozen at the sale is 0 days: no return through the channels of
        the module, and no period override can change that (it is not an expired period)."""
        self.ensure_one()
        params = self._urp_policy_params()
        return bool(params) and params[0] == 0

    def _urp_grace_qty(self, origin):
        """Quantity of ``origin`` covered by an in-time, documented request: the quantity these
        return moves recorded before the deadline (``urp_claim_qty``). Extended by the portal."""
        claims = self.filtered(lambda m: m.origin_returned_move_id == origin and m.state != "cancel")
        return sum(m.uom_id._compute_quantity(m.urp_claim_qty, origin.uom_id) for m in claims)

    def _urp_record_claims(self, now=None):
        """Document the return request: while the delivery is still within its return period,
        freeze (server time) the highest quantity entered on each draft return move. Quantities
        entered after the deadline are never added, and a claim never exceeds what was still
        returnable at that moment."""
        now = now or fields.Datetime.now()
        for move in self:
            origin = move.origin_returned_move_id
            if move.state in ("done", "cancel") or origin.state != "done" or origin.location_dest_usage != "customer":
                continue
            if not origin._urp_origin_channel() or origin._urp_unattributable_limit() is not None:
                continue
            deadline = origin._urp_delivery_deadline()
            if not (deadline and now < deadline):
                continue
            requested = move.uom_id._compute_quantity(max(move.product_uom_qty, move.quantity), origin.uom_id)
            remaining = max(origin.quantity - move._urp_returned_qty(origin, exclude=move), 0.0)
            claim = min(requested, remaining)
            if origin.uom_id.compare(claim, move.urp_claim_qty) > 0:
                urp_internal(move).write({"urp_claim_qty": claim, "urp_claimed_at": move.urp_claimed_at or now})

    def _urp_exception_type(self, now=None):
        """Exception an administrator must approve before this delivery move can be returned from
        Inventory: 'non_returnable' (0 days frozen at the sale), 'expired' (period ended), or
        False (within the period, not governed, or returned through the POS receipt only)."""
        self.ensure_one()
        if not self._urp_origin_channel() or self._urp_unattributable_limit() is not None:
            return False
        if self._urp_non_returnable():
            return "non_returnable"
        deadline = self._urp_delivery_deadline()
        now = now or fields.Datetime.now()
        return False if (deadline and now < deadline) else "expired"

    def _urp_approval_lines(self):
        """Approved, still usable exception lines (requests approved by the administrator of the
        sales channel) for this original delivery move. Read-only lookup, never used to grant
        stock rights: the return is executed with the user's own rights."""
        self.ensure_one()
        lines = self.env["urp.return.override.line"].sudo().search([
            ("origin_move_id", "=", self.id), ("override_id.company_id", "=", self.company_id.id),
            ("override_id.exception_id.state", "in", ("approved", "partially_used")),
        ], order="id")
        return lines.filtered(lambda l: l.override_id.channel == self._urp_origin_channel()
                              and l.override_id._urp_approver_valid())

    def _urp_approval_remaining(self, exclude=None):
        """Approved quantity still available for this delivery move, minus what is reserved by
        open Return Transfers created for those approvals (other than ``exclude``, the moves
        being validated): a reserved quantity cannot be taken by another return."""
        self.ensure_one()
        lines = self._urp_approval_lines()
        remaining = sum(lines.mapped("remaining_qty"))
        if not lines:
            return 0.0
        reserved = self.env["stock.move"].sudo().search([
            ("urp_request_line_id", "in", lines.exception_line_id.ids), ("state", "not in", ("done", "cancel")),
            ("id", "not in", (exclude or self.browse()).ids)])
        held = sum(m.uom_id._compute_quantity(m.product_uom_qty, self.uom_id) for m in reserved)
        return max(remaining - held, 0.0)

    def _urp_open_return_qty(self):
        """Quantity of this delivery move in open (not done, not cancelled) returns."""
        self.ensure_one()
        moves = self.sudo().returned_move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        return sum(m.uom_id._compute_quantity(m.product_uom_qty, self.uom_id) for m in moves)

    def _urp_free_return_qty(self):
        self.ensure_one()
        return max(self.quantity - self._urp_returned_qty(self) - self._urp_open_return_qty(), 0.0)

    def _urp_expired_origins(self, now=None):
        """Delivery moves (``self``) that need an administrator's exception (period ended with no
        documented right left, or non-returnable), with the approved quantity still available:
        list of dicts for the early check at return creation."""
        now = now or fields.Datetime.now()
        res = []
        for origin in self.filtered(lambda m: m.state == "done" and m._urp_is_customer_delivery()):
            kind = origin._urp_exception_type(now)
            if not kind:
                continue
            # an open request submitted in time: the line stays on the Return, so that the Inventory
            # user can select it in its Portal Request column (the right is checked at validation)
            if kind == "expired" and origin._urp_in_time_request_open():
                continue
            res.append({"origin": origin, "channel": origin._urp_origin_channel(), "kind": kind,
                        "deadline": origin._urp_delivery_deadline() if kind == "expired" else False,
                        "product": origin.product_id, "approved": origin._urp_approval_remaining()})
        return res

    def _urp_unattributable_limit(self):
        """For deliveries whose governed quantity must be returned through another channel (POS
        Direct and aggregated POS pickings): the quantity that may still be returned manually
        from Inventory; ``None`` when the normal deadline check applies."""
        self.ensure_one()
        return None

    def _urp_inventory_kind(self):
        """Violation kind when Inventory may only return a limited share of this delivery."""
        return "unattributable"

    def _urp_receipt_name(self):
        return ""

    def _urp_receipt_only_origins(self):
        """Delivery moves (``self``) of which nothing may be returned from Inventory: the governed
        quantity must go through the original receipt (POS Refund). Early check at return
        creation, for every user (no override here: it is given on the receipt)."""
        res = []
        for origin in self.filtered(lambda m: m.state == "done" and m._urp_is_customer_delivery()):
            if not origin._urp_origin_channel():
                continue
            limit = origin._urp_unattributable_limit()
            if limit is not None and origin.uom_id.compare(limit, 0.0) <= 0:
                res.append({"origin": origin, "product": origin.product_id, "kind": origin._urp_inventory_kind(),
                            "receipt": origin._urp_receipt_name()})
        return res

    def _urp_returned_qty(self, origin, exclude=None):
        done = origin.returned_move_ids.filtered(lambda m: m.state == "done" and m not in (exclude or self.browse()))
        return sum(m.uom_id._compute_quantity(m.quantity, origin.uom_id) for m in done)

    # ------------------------------------------------------------------ check
    def _urp_return_violations(self, now=None, plan=None):
        """Return a list of dicts describing policy violations of the return moves in ``self``.

        Only returns of customer deliveries that carry a policy snapshot are checked;
        purchase returns, internal transfers, POS refund pickings (no origin link) and
        legacy deliveries are out of scope.

        Quantities approved by an administrator's exception (non-returnable or expired period)
        are eligible up to the approved quantity still available; ``plan`` (a list) receives
        the quantity each origin takes from its approvals when nothing is violated.
        """
        now = now or fields.Datetime.now()
        res = []
        returns = self.filtered(lambda m: m.state not in ("done", "cancel") and m.origin_returned_move_id
                                and m.origin_returned_move_id.state == "done"
                                and m.origin_returned_move_id.location_dest_usage == "customer")
        by_origin = {}
        for move in returns:
            by_origin.setdefault(move.origin_returned_move_id, self.browse())
            by_origin[move.origin_returned_move_id] |= move
        for origin, moves in by_origin.items():
            channel = origin._urp_origin_channel()
            if not channel:
                continue
            requested = sum(m.uom_id._compute_quantity(m.quantity, origin.uom_id) for m in moves)
            if origin.uom_id.is_zero(requested):
                continue
            remaining = origin.quantity - moves._urp_returned_qty(origin, exclude=moves)
            limit = origin._urp_unattributable_limit()
            non_returnable = origin._urp_non_returnable()
            if limit is not None:
                # aggregated delivery: no receipt, no deadline; only the legacy share is returnable here
                eligible = max(min(remaining, limit), 0.0)
                base = {"moves": moves, "origin": origin, "channel": channel, "product": origin.product_id,
                        "requested": requested, "remaining": remaining, "eligible": eligible, "deadline": False}
                if origin.uom_id.compare(requested, remaining) > 0:
                    res.append(dict(base, kind="over_qty"))
                elif origin.uom_id.compare(requested, eligible) > 0:
                    res.append(dict(base, kind="non_returnable" if non_returnable else origin._urp_inventory_kind()))
                continue
            deadline = False if non_returnable else origin._urp_delivery_deadline()
            in_time = not non_returnable and bool(deadline) and now < deadline
            if non_returnable:
                base_eligible = 0.0          # policy 0 days frozen at the sale: no grace, no period
            else:
                grace = moves._urp_grace_qty(origin)
                base_eligible = remaining if in_time else min(remaining, grace)
            approved = 0.0 if in_time else origin._urp_approval_remaining(exclude=moves)
            # open portal quantity not held by a return linked to its request (these lines included)
            reserved = origin._urp_portal_uncovered_qty()
            eligible = max(min(remaining, base_eligible + approved, remaining - reserved), 0.0)
            base = {"moves": moves, "origin": origin, "channel": channel, "product": origin.product_id,
                    "requested": requested, "remaining": remaining, "eligible": eligible, "deadline": deadline,
                    "approved": approved, "reserved": reserved}
            if origin.uom_id.compare(requested, remaining) > 0:
                res.append(dict(base, kind="over_qty"))
            elif origin.uom_id.compare(requested, remaining - reserved) > 0:
                res.append(dict(base, kind="portal_reserved"))
            elif origin.uom_id.compare(requested, eligible) > 0:
                res.append(dict(base, kind="non_returnable" if non_returnable else "expired"))
            elif plan is not None and origin.uom_id.compare(requested, base_eligible) > 0:
                plan.append({"origin": origin, "moves": moves, "need": requested - max(base_eligible, 0.0)})
        return res

    def _urp_lock_origins(self):
        origins = self.origin_returned_move_id
        if origins:
            urp_lock_rows(self.env.cr, "stock_move", origins.ids)
            origins.invalidate_recordset(["returned_move_ids"])

    def _action_done(self, cancel_backorder=False):
        """Final server-side guard (covers barcode, RPC and any other caller).

        It cannot be switched off from the context. The only ways past an expired period or a
        non-returnable policy are an administrator's approval: an approved exception request
        (consumed here, quantity by quantity) or the override created by the wizard for that
        very picking."""
        self._urp_lock_origins()
        plan = []
        violations = self._urp_return_violations(plan=plan)
        blocking = []
        exceptional = bool(plan)                  # quantity taken from an approved exception request
        for v in violations:
            pickings = v["moves"].picking_id
            override = pickings.urp_override_id
            if v["kind"] in ("expired", "non_returnable") and len(override) == 1 and override._urp_covers(v, pickings):
                exceptional = True                # quantity covered by the administrator's override
                continue
            blocking.append(v)
        if blocking:
            raise UserError(self.env["urp.return.check.wizard"]._urp_message(blocking))
        if exceptional:
            self._urp_check_exception_executor()
        if plan:
            self._urp_consume_approvals(plan)
        overrides = self.picking_id.urp_override_id.filtered(lambda o: not o.consumed_at)
        if overrides:
            urp_internal(overrides.sudo()).write({"consumed_at": fields.Datetime.now()})
        res = super()._action_done(cancel_backorder=cancel_backorder)
        requests = self.sudo().urp_request_line_id.exception_id
        if requests:
            requests._urp_refresh_usage_state()
        return res

    def _urp_check_exception_executor(self):
        """Executing, from Inventory, a customer return that only an administrator's exception
        allows is a stock operation: the executing user needs Inventory rights of his own
        (Inventory User). Standard Odoo lets Sales Administrators and POS users write pickings;
        that is not enough here. Only these exceptional returns are concerned: returns within
        the period and POS refunds (which never link their moves to the original delivery) are
        untouched. The real user is checked, so sudo() or a context flag change nothing; only
        the system user (cron, shell, migration) is exempt."""
        if self.env.uid == SUPERUSER_ID or self.env.user.has_group("stock.group_stock_user"):
            return
        raise AccessError(self.env._(
            "Executing a return approved as an exception requires Inventory rights (Inventory User). "
            "The approval stays available for an authorised Inventory user."))

    def _urp_consume_approvals(self, plan):
        """Take the quantity each origin needs from its approved exception lines, oldest first,
        after locking them (and their requests: a cancellation at the same moment is serialised).
        Recorded per return move; the same approved quantity can never be used twice."""
        Usage = urp_internal(self.env["urp.return.exception.usage"].sudo())
        for entry in plan:
            origin, need = entry["origin"], entry["need"]
            lines = origin._urp_approval_lines()
            if lines:
                urp_lock_rows(self.env.cr, "urp_return_override_line", lines.ids)
                exceptions = lines.sudo().override_id.exception_id
                urp_lock_rows(self.env.cr, "urp_return_exception", exceptions.ids)
                lines.invalidate_recordset()
                exceptions.invalidate_recordset()
                lines = origin._urp_approval_lines()
            if origin.uom_id.compare(sum(lines.mapped("remaining_qty")), need) < 0:
                raise UserError(self.env._("%s: the approved exception no longer covers this quantity.",
                                           origin.product_id.display_name))
            moves = [[m, m.uom_id._compute_quantity(m.quantity, origin.uom_id)] for m in entry["moves"].sorted("id")]
            linked = entry["moves"].sudo().urp_request_line_id
            lines = lines.sorted(lambda l: (l.sudo().exception_line_id not in linked, l.id))
            for line in lines:
                avail = line.remaining_qty
                while need > 0 and avail > 0 and moves:
                    move, qty = moves[0]
                    take = min(need, avail, qty)
                    Usage.create({"override_line_id": line.id, "move_id": move.id, "qty": take})
                    need -= take
                    avail -= take
                    moves[0][1] -= take
                    if moves[0][1] <= 0:
                        moves.pop(0)
            lines.invalidate_recordset(["usage_ids", "used_qty", "remaining_qty", "usable"])
            lines.sudo().override_id.exception_id._urp_refresh_usage_state()
