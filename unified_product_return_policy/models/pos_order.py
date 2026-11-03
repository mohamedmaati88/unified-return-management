import hashlib
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .urp_guard import UrpPayload, is_urp_internal, urp_internal, urp_payload

from .urp_tools import urp_deadline_boundary, urp_lock_rows

POS_ORDER_PROTECTED = ("urp_governed", "urp_override_id")
POS_SNAPSHOT_FIELDS = (
    "urp_has_snapshot", "urp_return_days", "urp_policy_source", "urp_tz", "urp_sale_at", "urp_device_date",
    "urp_received_at", "urp_date_trust", "urp_policy_ambiguous", "urp_ambiguous_history_ids", "urp_deadline",
    "urp_refund_kind", "urp_date_basis",
)


def _urp_extend_fields(base, extra):
    """POS loaders return [] to mean "all fields": only extend an explicit list."""
    return base + [f for f in extra if f not in base] if base else base


class PosOrder(models.Model):
    _inherit = "pos.order"

    urp_governed = fields.Boolean("Return Policy Applies", copy=False, readonly=True)
    urp_override_token = fields.Char(copy=False, help="One-time manager override token sent by the POS with the refund: kept "
                                     "on the order while it is unpaid, cleared when it is paid or cancelled. Valid 10 minutes, "
                                     "for this order, session and quantities only.")
    urp_override_id = fields.Many2one("urp.return.override", string="Return Period Override", copy=False, readonly=True)

    @api.model
    def _load_pos_data_fields(self, config):
        return _urp_extend_fields(super()._load_pos_data_fields(config), ["urp_override_token"])

    @api.model_create_multi
    def create(self, vals_list):
        if not is_urp_internal(self.env):
            for vals in vals_list:
                for f in POS_ORDER_PROTECTED:
                    vals.pop(f, None)
        return super().create(vals_list)

    def write(self, vals):
        if not is_urp_internal(self.env) and set(vals) & set(POS_ORDER_PROTECTED):
            vals = {k: v for k, v in vals.items() if k not in POS_ORDER_PROTECTED}
        if vals.get("state") == "cancel":
            vals = dict(vals, urp_override_token=False)      # a cancelled order keeps no approval token
        return super().write(vals)

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for vals in vals_list:
            for f in POS_ORDER_PROTECTED:
                vals.pop(f, None)
        return vals_list

    # ------------------------------------------------------------------ sync metadata
    @api.model
    def _process_order(self, order, existing_order):
        current = self.env.context.get("current_order_uuid")
        meta = {
            "uuid": order.get("uuid"),
            "orig_session_id": order.get("session_id"),
            "device_date": order.get("date_order"),
            # the standard overwrites date_order with the server time for the order being synced
            "server_time": bool(current) and current == order.get("uuid"),
        }
        return super(PosOrder, self.with_context(urp_sync_meta=UrpPayload(meta)))._process_order(order, existing_order)

    def _urp_sale_time(self):
        """Return (sale_at, device_date, trust, window_start, received_at) for this order."""
        self.ensure_one()
        now = fields.Datetime.now()
        meta = urp_payload(self.env, "urp_sync_meta", {})        # set by _process_order only, never by a client
        if not meta or meta.get("uuid") != self.uuid:
            return now, False, "server", now, now                     # backend payment
        if meta.get("server_time"):
            return now, fields.Datetime.to_datetime(meta.get("device_date")), "server", now, now
        session = self.env["pos.session"].browse(meta.get("orig_session_id")).exists() or self.session_id
        lower = session.start_at or now
        device = fields.Datetime.to_datetime(meta.get("device_date")) or self.date_order
        sale_at = min(max(device, lower), now)
        return sale_at, device, ("bounded" if sale_at == device else "clamped"), lower, now

    def _urp_snapshot_lines(self):
        Hist = self.env["urp.policy.history"]
        for order in self:
            lines = order.lines.filtered(lambda l: l.qty > 0 and not l.urp_has_snapshot and l.product_id.type == "consu")
            if not lines:
                continue
            sale_at, device, trust, start, received = order._urp_sale_time()
            for line in lines:
                at = None if trust == "server" else sale_at
                policy = line.product_id._urp_resolve(order.company_id, at=at)
                if not policy:
                    continue
                ambiguous = Hist
                if trust != "server":
                    tmpl = line.product_id.product_tmpl_id
                    company = order.company_id
                    prod_changes = Hist._urp_changes_between("product", tmpl.id, start, received, company)
                    categ_moves = Hist._urp_categ_changes_between(tmpl, start, received)
                    # every category the product belonged to in the window (historical, not the current one)
                    categ_ids = {Hist._urp_categ_at(tmpl, start)} | set(categ_moves.mapped("categ_id"))
                    ambiguous = Hist._urp_changes_between("company", company.id, start, received) | prod_changes | categ_moves
                    for categ_id in categ_ids - {False, 0}:
                        ambiguous |= Hist._urp_changes_between("category", categ_id, start, received, company)
                vals = {
                    "urp_has_snapshot": True, "urp_return_days": policy["return_days"],
                    "urp_policy_source": policy["policy_source"], "urp_tz": policy["tz"], "urp_sale_at": sale_at,
                    "urp_date_basis": policy["date_basis"],
                    "urp_device_date": device, "urp_received_at": received, "urp_date_trust": trust,
                    "urp_policy_ambiguous": bool(ambiguous), "urp_ambiguous_history_ids": [(6, 0, ambiguous.ids)],
                }
                if not order.shipping_date:
                    # 0 days = non-returnable: no deadline (not "returnable until the end of the sale day")
                    vals["urp_deadline"] = policy["return_days"] and urp_deadline_boundary(
                        sale_at, policy["return_days"], policy["tz"]) or False
                # server-side snapshot: written with superuser rights. The cashier syncing the order has
                # no access to the policy history (administrators only) that the snapshot references,
                # and must not need any (no Access Error on sync)
                urp_internal(line.sudo()).write(vals)
            if order.lines.filtered("urp_has_snapshot"):
                urp_internal(order).urp_governed = True

    # ------------------------------------------------------------------ refund checks
    def _urp_delivered_moves(self, product):
        self.ensure_one()
        return self.picking_ids.move_ids.filtered(
            lambda m: m.product_id == product and m.state == "done" and m.location_dest_usage == "customer"
            and not m.origin_returned_move_id)

    def _urp_check_refunds(self, plans):
        """Server-side checks of every refund line (linked and standalone). Returns the
        list of overridable needs; raises for non-overridable violations. Fills plans
        (refund line id -> [(delivery move, qty)]) for ship-later allocations."""
        now = fields.Datetime.now()
        needs = []
        for order in self:
            negative = order.lines.filtered(lambda l: l.qty < 0)
            # first, before any other check (legacy sales included): the receipt line sent by the
            # device must really be the sale of this product in this company
            order._urp_check_refund_links(negative)
            # D6: quantity cap enforced on the server for the TOTAL of all refund lines of this
            # order that point to the same sale line (standard only checks it in onchange)
            origs = negative.refunded_orderline_id
            if origs:
                # serialise concurrent refunds of the same sale lines (two terminals at the same time)
                urp_lock_rows(self.env.cr, "pos_order_line", origs.ids)
                origs.invalidate_recordset(["refund_orderline_ids"])
            requested_by_orig = {}
            for line in negative.filtered("refunded_orderline_id"):
                requested_by_orig[line.refunded_orderline_id] = requested_by_orig.get(line.refunded_orderline_id, 0.0) - line.qty
            for orig, total in requested_by_orig.items():
                if not orig.urp_has_snapshot:
                    continue
                others = orig.refund_orderline_ids.filtered(
                    lambda l: l.order_id != order and l.order_id.state not in ("draft", "cancel"))
                remaining = orig.qty + sum(others.mapped("qty"))
                if orig.product_uom_id.compare(total, remaining) > 0:
                    raise UserError(self.env._(
                        "Refund of %(q)s x %(p)s exceeds the quantity that can still be refunded (%(r)s).",
                        q=total, p=orig.product_id.display_name, r=remaining))
            order._urp_check_inventory_returns(requested_by_orig)
            inflight = {"returned": {}, "allocated": {}}       # shared by the lines of this refund order
            for line in negative:
                orig = line.refunded_orderline_id
                qty = -line.qty
                if not orig:
                    if line.product_id.type == "consu" and order.company_id.urp_enabled:
                        needs.append({"kind": "standalone", "line": line, "product": line.product_id, "orig": orig, "qty": qty})
                    continue
                if not orig.urp_has_snapshot:
                    continue                                              # legacy sale: standard behaviour
                if orig.order_id.shipping_date:
                    needs += order._urp_check_ship_later(line, orig, qty, now, plans, inflight)
                else:
                    # historical policy of the receipt line: 0 days = Non-Returnable Exception, otherwise
                    # an ended period = Expired Return Period Override (both: POS Administrator, reason)
                    if orig.urp_return_days == 0:
                        needs.append({"kind": "non_returnable", "line": line, "product": line.product_id, "orig": orig, "qty": qty})
                    elif not (orig.urp_deadline and now < orig.urp_deadline):
                        needs.append({"kind": "expired", "line": line, "product": line.product_id, "orig": orig, "qty": qty})
                    urp_internal(line).urp_refund_kind = "return"
        return needs

    def _urp_check_refund_links(self, refund_lines):
        """The receipt line a refund line points to (``refunded_orderline_id``, sent by the device)
        decides its policy, approvals and quantity cap: it must be a sale line (positive quantity)
        of the same product, on another order of the same company that was really sold. Applies
        when the policy is enabled for this company or for the company of that receipt line."""
        self.ensure_one()
        for line in refund_lines.filtered("refunded_orderline_id"):
            orig = line.sudo().refunded_orderline_id
            src = orig.order_id
            if not (self.company_id.urp_enabled or src.company_id.urp_enabled):
                continue
            if (orig.product_id != line.product_id or src.company_id != self.company_id or src == self
                    or orig.product_uom_id.compare(orig.qty, 0) <= 0 or src.state in ("draft", "cancel")):
                raise UserError(self.env._(
                    "Refund of %(p)s refused: it does not match the receipt line it refers to (product, company "
                    "or receipt). Refund it from the original receipt.", p=line.product_id.display_name))

    def _urp_check_inventory_returns(self, requested_by_orig):
        """POS Direct: quantity of the receipt already returned through Inventory (returns made
        before Inventory was closed to governed POS quantity) can no longer be refunded on the
        receipt: the same goods are never returned twice, whatever the channel. The delivery
        moves are locked like an Inventory return locks them (serialises both channels)."""
        self.ensure_one()
        by_src = {}
        for orig, total in requested_by_orig.items():
            if orig.urp_has_snapshot and not orig.order_id.shipping_date:
                key = (orig.order_id, orig.product_id)
                by_src[key] = by_src.get(key, 0.0) + total
        for (src, product), total in by_src.items():
            moves = src.sudo()._urp_delivered_moves(product)
            if not moves:
                continue
            urp_lock_rows(self.env.cr, "stock_move", moves.ids)
            moves.invalidate_recordset(["returned_move_ids"])
            inventory = sum(m._urp_inventory_returned_qty() for m in moves)
            if product.uom_id.is_zero(inventory):
                continue
            sold = src.lines.filtered(lambda l: l.product_id == product and l.qty > 0)
            refunded = -sum(sold.refund_orderline_ids.filtered(
                lambda l: l.order_id != self and l.order_id.state not in ("draft", "cancel")).mapped("qty"))
            remaining = max(sum(sold.mapped("qty")) - refunded - inventory, 0.0)
            if product.uom_id.compare(total, remaining) > 0:
                raise UserError(self.env._(
                    "Refund of %(q)s x %(p)s blocked: %(i)s unit(s) of receipt %(o)s were already returned through "
                    "Inventory. Only %(r)s unit(s) can still be refunded.",
                    q=total, p=product.display_name, i=inventory, o=src.pos_reference or src.name, r=remaining))

    def _urp_check_ship_later(self, line, orig, qty, now, plans, inflight=None):
        """Per-delivery deadlines for ship-later sales: the decision of ``_urp_ship_later_eval``,
        applied to the refund line (refund kind, allocation plan) or raised."""
        res = self._urp_ship_later_eval(orig, qty, now, inflight, exclude_order=line.order_id, lock=True)
        if res["error"]:
            raise UserError(res["error"])
        urp_internal(line).urp_refund_kind = res["kind"]
        if res["kind"] == "cancel":
            return []
        plans[line.id] = res["plan"]
        return [dict(need, line=line) for need in res["needs"]]

    @api.model
    def _urp_ship_later_eval(self, orig, qty, now, inflight=None, exclude_order=None, lock=False):
        """Decision for ``qty`` units of the ship-later receipt line ``orig`` (the rules of
        ``_urp_check_ship_later``). Writes nothing: returns the refund kind, the error that
        refuses it, the allocation plan on the delivered moves and the approvals it needs. ``lock``
        serialises with Inventory returns of the same deliveries (payment only, never the pre-check);
        ``inflight`` carries what the previous lines of the same refund already took."""
        src = orig.order_id
        product = orig.product_id
        res = {"kind": "return", "error": False, "plan": [], "needs": []}
        delivered_moves = src.sudo()._urp_delivered_moves(product)
        if not delivered_moves:
            res["kind"] = "cancel"                          # standard reduces the pending delivery
            return res
        if lock:
            # serialise with Inventory returns of the same deliveries (they lock these moves too)
            urp_lock_rows(self.env.cr, "stock_move", delivered_moves.ids)
            delivered_moves.invalidate_recordset(["returned_move_ids"])
        inventory = {m.id: m._urp_inventory_returned_qty() for m in delivered_moves}
        inventory_total = sum(inventory.values())
        delivered = sum(delivered_moves.mapped("quantity"))
        prev_returns = src.lines.filtered(lambda l: l.product_id == product).refund_orderline_ids.filtered(
            lambda l: l.order_id != exclude_order and l.order_id.state not in ("draft", "cancel") and l.urp_refund_kind == "return")
        inflight = inflight if inflight is not None else {"returned": {}, "allocated": {}}
        key = (src.id, product.id)
        returned_prev = -sum(prev_returns.mapped("qty")) + inflight["returned"].get(key, 0.0)
        returnable = delivered - returned_prev - inventory_total
        if product.uom_id.compare(qty, returnable) > 0 and inventory_total \
                and product.uom_id.compare(qty, delivered - returned_prev) <= 0:
            res["error"] = self.env._(
                "Refund of %(q)s x %(p)s blocked: %(i)s delivered unit(s) were already returned through Inventory. "
                "Only %(r)s unit(s) can still be refunded.",
                q=qty, p=product.display_name, i=inventory_total, r=max(returnable, 0.0))
            return res
        if product.uom_id.compare(qty, returnable) > 0:
            not_delivered = sum(src.lines.filtered(lambda l: l.product_id == product).mapped("qty")) - delivered
            pending = sum(src.picking_ids.move_ids.filtered(
                lambda m: m.product_id == product and m.state not in ("done", "cancel") and m.location_dest_usage == "customer"
            ).mapped("product_uom_qty"))
            res["error"] = self.env._(
                "Refund blocked for %(p)s: requested %(q)s, but only %(m)s delivered unit(s) can still be returned.\n"
                "%(n)s unit(s) were never delivered (%(pend)s still pending). After a partial delivery, Odoo would record "
                "a stock return for goods that never left the warehouse - cancelling the pending delivery does NOT "
                "prevent this.\nRefund only the delivered quantity here, and handle the undelivered quantity with "
                "procedure URP-OPS-01 (cancel pending delivery + customer credit note).",
                p=product.display_name, q=qty, m=returnable, n=not_delivered, pend=pending)
            return res
        inflight["returned"][key] = inflight["returned"].get(key, 0.0) + qty
        # allocate on delivered moves, nearest deadline first, eligible ones first
        Alloc = self.env["urp.return.allocation"].sudo()
        allocated = {m.id: sum(Alloc.search([("delivery_move_id", "=", m.id)]).mapped("qty"))
                     + inflight["allocated"].get(m.id, 0.0) for m in delivered_moves}
        ordered = delivered_moves.sorted(lambda m: (not (m.urp_deadline and now < m.urp_deadline), m.urp_deadline or now))
        left, expired_qty, plan = qty, 0.0, []
        for move in ordered:
            free = move.quantity - allocated[move.id] - inventory[move.id]
            take = min(free, left)
            if take <= 0:
                continue
            plan.append((move, take))
            inflight["allocated"][move.id] = inflight["allocated"].get(move.id, 0.0) + take
            if not (move.urp_deadline and now < move.urp_deadline):
                expired_qty += take
            left -= take
        res["plan"] = plan
        if orig.urp_return_days == 0:
            # non-returnable when sold: every delivered unit needs the Non-Returnable Exception
            res["needs"] = [{"kind": "non_returnable", "product": product, "orig": orig, "qty": qty}]
        elif expired_qty > 0:
            res["needs"] = [{"kind": "expired", "product": product, "orig": orig, "qty": expired_qty}]
        return res

    def _urp_consume_override(self, needs):
        self.ensure_one()
        token = self.urp_override_token
        if not token:
            raise UserError(self._urp_needs_message(needs))
        Override = self.env["urp.return.override"].sudo()
        ov = Override.search([("token_hash", "=", hashlib.sha256(token.encode()).hexdigest())], limit=1)
        now = fields.Datetime.now()
        if not ov or ov.channel != "pos" or ov.consumed_at or not ov.expires_at or ov.expires_at <= now \
                or ov.company_id != self.company_id or ov.pos_session_id != self.session_id \
                or not ov.pos_order_uuid or ov.pos_order_uuid != self.uuid:
            raise UserError(self.env._("The manager override is invalid, expired, already used or issued for another order.\n%s",
                                       self._urp_needs_message(needs)))
        if not ov._urp_approver_valid():
            # checked again at use: an approver who lost the role (removed from the managers,
            # archived, administrator group or company access withdrawn) no longer authorises
            raise UserError(self.env._("The manager who approved this refund no longer holds that right: ask an authorised "
                                       "POS Manager to approve it again.\n%s", self._urp_needs_message(needs)))
        allowed = {}
        for l in ov.line_ids:
            k = (l.product_id.id, l.refunded_line_id.id)
            allowed[k] = allowed.get(k, 0.0) + l.qty
        required = {}
        for n in needs:
            k = (n["product"].id, n["orig"].id)
            required[k] = required.get(k, 0.0) + n["qty"]
        for k, qty in required.items():
            if self.env["product.product"].browse(k[0]).uom_id.compare(allowed.get(k, 0.0), qty) < 0:
                raise UserError(self.env._("The manager override does not cover these lines or quantities."))
        urp_internal(ov).write({"consumed_at": now, "pos_order_id": self.id})
        urp_internal(self).urp_override_id = ov

    def _urp_needs_message(self, needs):
        parts = []
        for n in needs:
            if n["kind"] == "non_returnable":
                parts.append(self.env._(
                    "%(p)s: this product is non-returnable. POS Administrator approval is required for an exceptional "
                    "refund of %(q)s unit(s), with a reason.", p=n["product"].display_name, q=n["qty"]))
            elif n["kind"] == "standalone":
                policy = n["product"]._urp_resolve(self.company_id)
                if policy and policy["non_returnable"]:
                    parts.append(self.env._(
                        "%(p)s: refund without the original receipt of a product that is currently non-returnable: "
                        "administrative exception of a POS Administrator, with a reason.", p=n["product"].display_name))
                else:
                    parts.append(self.env._("%(p)s: refund without the original receipt requires a POS Administrator.",
                                            p=n["product"].display_name))
            else:
                parts.append(self.env._("%(p)s: return period ended for %(q)s unit(s); a POS Administrator override with a reason is required.",
                                        p=n["product"].display_name, q=n["qty"]))
        return "\n".join(parts)

    def action_pos_order_paid(self):
        for order in self:
            plans = {}
            needs = order._urp_check_refunds(plans)
            if needs:
                order._urp_consume_override(needs)
            order._urp_write_allocations(plans)
            order._urp_snapshot_lines()
        self.filtered("urp_override_token").write({"urp_override_token": False})
        return super().action_pos_order_paid()

    def process_saved_payments(self, order, existing_order):
        """Re-synchronisation of an order that is already paid (standard: allowed until the receipt
        is printed, it rewrites lines and payments without paying the order again). The refund
        lines checked at payment - quantities, receipt lines, period, administrator authorisation -
        must stay exactly as checked: replaying the same data is fine, changing them is refused."""
        before = existing_order._urp_refund_signature()
        res = super().process_saved_payments(order, existing_order)
        if existing_order.company_id.urp_enabled and existing_order._urp_refund_signature() != before:
            raise UserError(self.env._(
                "Order %s is already paid: its refund lines cannot be changed by a new synchronisation. "
                "Create a new refund instead.", existing_order.pos_reference or existing_order.name))
        return res

    def _urp_refund_signature(self):
        self.ensure_one()
        return sorted((l.product_id.id, l.qty, l.refunded_orderline_id.id, l.price_unit)
                      for l in self.lines if l.qty < 0)

    def _urp_write_allocations(self, plans):
        Alloc = self.env["urp.return.allocation"].sudo()
        Alloc.create([{"refund_line_id": line_id, "delivery_move_id": m.id, "qty": q}
                      for line_id, plan in plans.items() for m, q in plan])

class PosOrderLine(models.Model):
    _inherit = "pos.order.line"

    urp_has_snapshot = fields.Boolean(readonly=True, copy=False)
    urp_return_days = fields.Integer("Return Days", readonly=True, copy=False)
    urp_policy_source = fields.Selection([("product", "Product"), ("category", "Product Category"), ("company", "Company Default")],
                                         readonly=True, copy=False)
    urp_tz = fields.Char(readonly=True, copy=False)
    urp_sale_at = fields.Datetime("Policy Sale Time", readonly=True, copy=False)
    urp_device_date = fields.Datetime("Device Date", readonly=True, copy=False)
    urp_received_at = fields.Datetime("Received by Server", readonly=True, copy=False)
    urp_date_trust = fields.Selection([("server", "Server time"), ("bounded", "Device time within session"),
                                       ("clamped", "Device time corrected")], readonly=True, copy=False)
    urp_policy_ambiguous = fields.Boolean("Policy Uncertain", readonly=True, copy=False, index=True)
    urp_ambiguous_history_ids = fields.Many2many("urp.policy.history", string="Policy Changes in Window", readonly=True, copy=False)
    urp_deadline = fields.Datetime("Return Deadline", readonly=True, copy=False)
    urp_date_basis = fields.Selection([("validation", "Actual Validation Date"), ("effective", "Effective Date")],
                                      string="Return Period Based On", readonly=True, copy=False,
                                      help="Frozen at the sale; used for ship-later deliveries only.")
    urp_refund_kind = fields.Selection([("cancel", "Cancellation"), ("return", "Return")], readonly=True, copy=False)
    urp_allocation_ids = fields.One2many("urp.return.allocation", "refund_line_id", readonly=True)

    @api.model
    def _load_pos_data_fields(self, config):
        return _urp_extend_fields(super()._load_pos_data_fields(config), ["urp_has_snapshot", "urp_deadline", "urp_return_days"])

    @api.model_create_multi
    def create(self, vals_list):
        if not is_urp_internal(self.env):
            for vals in vals_list:
                for f in POS_SNAPSHOT_FIELDS:
                    vals.pop(f, None)                    # never trust snapshot values sent by a client
        return super().create(vals_list)

    def write(self, vals):
        if not is_urp_internal(self.env):
            vals = {k: v for k, v in vals.items() if k not in POS_SNAPSHOT_FIELDS}
        return super().write(vals)

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for vals in vals_list:
            for f in POS_SNAPSHOT_FIELDS:
                vals.pop(f, None)
        return vals_list


class UrpReturnAllocation(models.Model):
    _name = "urp.return.allocation"
    _description = "POS Ship-Later Refund Allocation"

    refund_line_id = fields.Many2one("pos.order.line", required=True, ondelete="cascade", readonly=True, index=True)
    delivery_move_id = fields.Many2one("stock.move", required=True, readonly=True, index=True)
    qty = fields.Float(required=True, readonly=True, digits="Product Unit")
    company_id = fields.Many2one(related="refund_line_id.order_id.company_id", store=True, index=True)

    _refund_move_uniq = models.Constraint("UNIQUE(refund_line_id, delivery_move_id)", "Duplicate allocation.")


class ResCompanyPos(models.Model):
    _inherit = "res.company"

    @api.model
    def _load_pos_data_fields(self, config):
        return _urp_extend_fields(super()._load_pos_data_fields(config), ["urp_enabled"])


class StockMovePos(models.Model):
    _inherit = "stock.move"

    # The POS data below is always read with superuser rights: the return policy is evaluated on
    # the data, never on what the current user may read. A warehouse user without Point of Sale
    # rights cannot read POS order lines and would otherwise see a governed POS delivery as a
    # delivery unrelated to any receipt (fail-open).

    def _urp_pos_source_lines(self):
        """POS sale lines whose quantity this delivery move carries (superuser records).

        * picking of one order (ship later, or per-order picking): that order's lines;
        * aggregated session picking (D10): the lines of every order of the session that was
          delivered through it, i.e. not ship-later orders, which have their own picking.
        Only lines of the same product with a positive quantity are relevant."""
        self.ensure_one()
        picking = self.sudo().picking_id
        if picking.pos_order_id:
            orders = picking.pos_order_id
        elif picking.pos_session_id:
            # orders delivered through the session picking: no picking of their own, not ship-later,
            # same customer location as this picking
            dest = picking.location_dest_id
            orders = picking.pos_session_id.order_ids.filtered(
                lambda o: o.state != "cancel" and not o.shipping_date and not o.picking_ids
                and (o.partner_id.property_stock_customer or dest) == dest)
        else:
            return self.env["pos.order.line"].sudo()
        return orders.lines.filtered(lambda l: l.product_id == self.product_id and l.qty > 0)

    def _urp_is_attributable(self):
        """True when the quantity of this delivery move can be traced to ONE sale (the picking
        of a single POS order): its lines then carry the historical policy of that sale."""
        self.ensure_one()
        return bool(self.sudo().picking_id.pos_order_id)

    def _urp_origin_channel(self):
        """POS policy applies when the move carries at least one governed POS sale line.

        * single-order picking: the policy of that order (deadline of its lines);
        * aggregated session picking: the returned quantity cannot be linked to a receipt, so
          no deadline is applied here at all (see ``_urp_unattributable_limit``)."""
        channel = super()._urp_origin_channel()
        if channel:
            return channel
        lines = self._urp_pos_source_lines()
        if lines.filtered(lambda l: l.urp_has_snapshot and l.order_id.urp_governed):
            return "pos"
        return False

    def _urp_delivery_deadline(self):
        deadline = super()._urp_delivery_deadline()
        if deadline or not self._urp_is_attributable():
            return deadline                 # aggregated move: no common deadline is invented
        lines = self._urp_pos_source_lines().filtered("urp_has_snapshot")
        deadlines = set(lines.mapped("urp_deadline"))
        # one order = one sale instant and one policy per product: a single deadline
        return deadlines.pop() if len(deadlines) == 1 else False

    def _urp_unattributable_limit(self):
        """Quantity of a POS delivery that may still be returned from Inventory, or ``None`` when
        the normal deadline check applies (sales, POS ship later).

        * POS Direct (picking of one order paid and delivered at once, no ship later) and
          aggregated session pickings: governed quantity must go through POS Refund on the
          original receipt, where the historical policy of each sale line is checked, the
          manager override is tied to the receipt and the money refund and the stock return
          stay one operation. Inventory may only return the share of sales made before the
          policy (legacy lines, standard behaviour): legacy quantity - legacy POS refunds -
          manual returns already done.
        * POS ship later: ``None`` (own delivery deadline, Inventory return allowed in time)."""
        limit = super()._urp_unattributable_limit()
        if limit is not None:
            return limit
        picking = self.sudo().picking_id
        if picking.pos_order_id:
            if picking.pos_order_id.shipping_date:
                return None
        elif not picking.pos_session_id:
            return None
        lines = self._urp_pos_source_lines()
        legacy = lines.filtered(lambda l: not (l.urp_has_snapshot and l.order_id.urp_governed))
        legacy_refunded = -sum(legacy.refund_orderline_ids.filtered(
            lambda r: r.order_id.state not in ("draft", "cancel")).mapped("qty"))
        manual = sum(m.uom_id._compute_quantity(m.quantity, self.uom_id)
                     for m in self.sudo().returned_move_ids.filtered(lambda m: m.state == "done"))
        return max(sum(legacy.mapped("qty")) - legacy_refunded - manual, 0.0)

    def _urp_non_returnable(self):
        """POS Direct: the policy frozen on the receipt lines of this product is 0 days."""
        if super()._urp_non_returnable():
            return True                                    # ship later (and sales)
        order = self.sudo().picking_id.pos_order_id
        if not order or order.shipping_date or not order.urp_governed:
            return False
        lines = self._urp_pos_source_lines().filtered("urp_has_snapshot")
        return bool(lines) and all(l.urp_return_days == 0 for l in lines)

    def _urp_inventory_kind(self):
        kind = super()._urp_inventory_kind()
        return "pos_receipt" if self._urp_is_attributable() else kind

    def _urp_receipt_name(self):
        order = self.sudo().picking_id.pos_order_id
        return order.pos_reference or order.name or ""

    def _urp_inventory_returned_qty(self):
        """Quantity of this delivery already returned through Inventory (done return moves)."""
        self.ensure_one()
        done = self.sudo().returned_move_ids.filtered(lambda m: m.state == "done")
        return sum(m.uom_id._compute_quantity(m.quantity, self.uom_id) for m in done)

    def _urp_returned_qty(self, origin, exclude=None):
        qty = super()._urp_returned_qty(origin, exclude=exclude)
        allocs = self.env["urp.return.allocation"].sudo().search([("delivery_move_id", "=", origin.id)])
        return qty + sum(allocs.mapped("qty"))

    def _urp_policy_params(self):
        """Ship-later POS delivery: policy frozen on the POS line at the sale; the deadline starts
        at the delivery (validation or effective date, per the frozen basis). Without ship later the
        deadline stays the one of the sale itself (POS line) and is never stamped on the move."""
        params = super()._urp_policy_params()
        if params:
            return params
        order = self.sudo().picking_id.pos_order_id
        if not order or not order.shipping_date or not order.urp_governed:
            return False
        line = order.lines.filtered(lambda l: l.product_id == self.product_id and l.urp_has_snapshot)[:1]
        if not line:
            return False
        return line.urp_return_days, None, line.urp_tz, line.urp_date_basis or "validation"
