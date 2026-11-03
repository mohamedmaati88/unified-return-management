import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from .urp_guard import UrpPayload, urp_internal, urp_payload

_logger = logging.getLogger(__name__)


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    urp_batch_ids = fields.One2many("urp.sale.batch", "sale_line_id", string="Return Policy Batches", copy=False, readonly=True)

    def _urp_is_governed_product(self):
        self.ensure_one()
        return not self.display_type and self.product_id.type == "consu" and not self.is_expense

    def _urp_create_batch(self, qty):
        """Snapshot the policy in force now for ``qty`` approved units. Returns an empty
        recordset when the policy is not enabled for the order's company (legacy behaviour)."""
        self.ensure_one()
        policy = self.product_id._urp_resolve(self.order_id.company_id)
        if not policy or self.product_uom_id.is_zero(qty):
            return self.env["urp.sale.batch"]
        return self.env["urp.sale.batch"].sudo().create({
            "sale_line_id": self.id, "qty": qty, "approved_at": fields.Datetime.now(),
            "company_id": policy["company_id"], "return_days": policy["return_days"],
            "policy_source": policy["policy_source"], "portal_eligible": policy["portal_eligible"],
            "portal_days": policy["portal_days"], "tz": policy["tz"], "date_basis": policy["date_basis"],
        })

    def _urp_reduce_lifo(self, reduce_qty):
        """Remove undelivered quantity (in the line's unit) from the most recently approved batches first."""
        self.ensure_one()
        uom = self.product_uom_id
        for batch in self.urp_batch_ids.sorted(lambda b: (b.approved_at, b.id), reverse=True):
            if uom.compare(reduce_qty, 0) <= 0:
                break
            for move in batch._urp_customer_moves().filtered(lambda m: m.state not in ("done", "cancel")).sorted("id", reverse=True):
                # the move may use another unit than the line (sold by the dozen, stocked by the unit)
                open_qty = move.uom_id._compute_quantity(move.product_uom_qty, uom, rounding_method="HALF-UP")
                take = min(open_qty, reduce_qty)
                if uom.compare(take, 0) <= 0:
                    continue
                if uom.compare(open_qty, take) <= 0:
                    move._action_cancel()
                else:
                    move.with_context(do_not_unreserve=False).product_uom_qty = move.product_uom_qty - uom._compute_quantity(
                        take, move.uom_id, rounding_method="HALF-UP")
                # batches are read-only for every group: the module's own bookkeeping only
                urp_internal(batch.sudo()).qty_cancelled = batch.qty_cancelled + take
                reduce_qty -= take
        return reduce_qty

    def write(self, vals):
        if "product_uom_qty" not in vals:
            return super().write(vals)
        batch_map = dict(urp_payload(self.env, "urp_batch_map", {}))
        governed = self.filtered(lambda l: l.state == "sale" and l.urp_batch_ids and l._urp_is_governed_product())
        # the batches below are updated as superuser: only for a user allowed to change these lines
        governed.check_access("write")
        for line in governed:
            new_qty = vals["product_uom_qty"]
            delta = new_qty - line.product_uom_qty
            uom = line.product_uom_id
            if uom.compare(delta, 0) > 0:
                batch = line._urp_create_batch(delta)
                if batch:
                    batch_map[line.id] = batch.id
            elif uom.compare(delta, 0) < 0 and \
                    uom.compare(new_qty, line.qty_delivered) >= 0:
                # shrink the newest batch ourselves so that Odoo creates no negative procurement
                left = line._urp_reduce_lifo(-delta)
                if uom.compare(left, 0) > 0:
                    _logger.warning("URP: %s unit(s) of %s could not be removed from open batches", left, line)
        return super(SaleOrderLine, self.with_context(urp_batch_map=UrpPayload(batch_map))).write(vals)

    def _prepare_procurement_values(self):
        values = super()._prepare_procurement_values()
        if not self._urp_is_governed_product():
            return values
        bmap = urp_payload(self.env, "urp_batch_map", {})
        batch_id = bmap.get(self.id)
        if not batch_id:
            unlinked = self.urp_batch_ids.filtered(lambda b: not b.move_ids and not b.qty_cancelled)
            if len(unlinked) == 1:
                batch_id = unlinked.id
            elif len(unlinked) > 1:
                raise UserError(self.env._(
                    "Return policy: cannot decide which approved quantity this delivery belongs to (%s).", self.display_name))
            elif not self.urp_batch_ids and self.state == "sale":
                # line added to an already confirmed order: this IS the commercial approval
                batch_id = self._urp_create_batch(self.product_uom_qty).id
        values["urp_batch_id"] = batch_id or False
        return values


class SaleOrder(models.Model):
    _inherit = "sale.order"

    urp_batch_ids = fields.One2many("urp.sale.batch", "order_id", string="Return Policy Batches", readonly=True)

    def _action_cancel(self):
        if self.order_line.urp_batch_ids:
            self.check_access("write")      # batches are updated as superuser below
        for batch in self.order_line.urp_batch_ids:
            open_qty = batch._urp_open_qty()
            if open_qty:
                urp_internal(batch.sudo()).qty_cancelled = batch.qty_cancelled + open_qty
        return super()._action_cancel()

    def _action_confirm(self):
        """Create (first confirmation) or restore (re-confirmation) the batches,
        then let Odoo launch procurements with an explicit line -> batch map."""
        bmap, plan = {}, {}
        if self.order_line.urp_batch_ids.filtered("qty_cancelled"):
            self.check_access("write")      # cancelled batch quantities are restored as superuser below
        for line in self.order_line.filtered(lambda l: l._urp_is_governed_product()):
            moves = line.move_ids.filtered(lambda m: m.state != "cancel" and m.location_dest_usage == "customer")
            uom = line.product_uom_id
            covered = sum(m.uom_id._compute_quantity(m.quantity if m.state == "done" else m.product_uom_qty, uom,
                                                     rounding_method="HALF-UP") for m in moves)
            needed = line.product_uom_qty - covered
            if uom.compare(needed, 0) <= 0:
                continue
            parts = []
            for batch in line.urp_batch_ids.filtered(lambda b: b.qty_cancelled > 0).sorted(lambda b: (b.approved_at, b.id)):
                take = min(needed, batch.qty_cancelled)
                if uom.compare(take, 0) <= 0:
                    break
                urp_internal(batch.sudo()).qty_cancelled = batch.qty_cancelled - take
                parts.append((batch, take))
                needed -= take
            if uom.compare(needed, 0) > 0:
                new = line._urp_create_batch(needed)
                if new:
                    parts.append((new, needed))
            if parts:
                bmap[line.id] = parts[0][0].id
                plan[line.id] = parts
        res = super(SaleOrder, self.with_context(urp_batch_map=UrpPayload(bmap)))._action_confirm()
        for parts in plan.values():
            first = parts[0][0]
            move = first.move_ids.filtered(lambda m: m.state not in ("done", "cancel") and m.location_dest_usage == "customer").sorted("id")[-1:]
            for batch, qty in parts[1:]:
                if not move:
                    break
                # ``qty`` is in the line's unit, ``_split`` takes the move's
                vals = move._split(first.sale_line_id.product_uom_id._compute_quantity(qty, move.uom_id, rounding_method="HALF-UP"))
                if vals:
                    vals[0]["urp_batch_id"] = batch.id
                    urp_internal(self.env["stock.move"]).create(vals).with_env(self.env)._action_confirm(merge=False)
        return res
