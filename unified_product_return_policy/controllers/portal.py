import json

from markupsafe import Markup
from werkzeug.exceptions import BadRequest

from odoo import fields
from odoo.exceptions import AccessError, MissingError
from odoo.http import request, route
from odoo.http.stream import content_disposition

from odoo.addons.sale_stock.controllers.return_order import CustomerPortal as SaleStockPortal

from ..models.urp_tools import urp_lock_rows


class CustomerPortal(SaleStockPortal):

    def _urp_order(self, order_id, access_token):
        try:
            return self._document_check_access("sale.order", order_id, access_token=access_token)
        except (AccessError, MissingError):
            return None

    @route()
    def my_order_return_data(self, order_id, access_token):
        data = super().my_order_return_data(order_id, access_token)
        order_sudo = self._urp_order(order_id, access_token)
        if not order_sudo or data.get("error") or not order_sudo._urp_governed_deliveries():
            return data
        lines = []
        for vals in data["returnable_lines"]:
            move = request.env["stock.move"].sudo().browse(vals["move_id"])
            if move.urp_batch_id:
                if not move._urp_portal_open():
                    continue
                available = move._urp_portal_available()
                if available <= 0:
                    continue
                vals = dict(vals, remaining_delivered_qty=available)
            lines.append(vals)
        data["returnable_lines"] = lines
        data["urp_request_url"] = f"/my/orders/{order_sudo.id}/urp_return_request"
        return data

    @route("/my/orders/<int:order_id>/urp_return_request", type="jsonrpc", auth="public", methods=["POST"])
    def urp_return_request(self, order_id, access_token, idempotency_key, return_details, return_reason_id=None):
        """The customer's actual confirmation: records the request (server time) and reserves quantities."""
        order_sudo = self._urp_order(order_id, access_token)
        if not order_sudo:
            return {"error": "Invalid order."}
        if not idempotency_key or not isinstance(return_details, dict) or not return_details:
            return {"error": "Missing required parameters."}
        Request = request.env["urp.portal.return.request"].sudo()
        existing = Request.search([("order_id", "=", order_sudo.id), ("idempotency_key", "=", str(idempotency_key))], limit=1)
        if existing:
            return {"request_id": existing.id, "label_url": self._urp_label_url(order_sudo, existing, access_token)}
        if not order_sudo._is_portal_return_allowed():
            return {"error": "Returns are not allowed for this order."}
        moves = order_sudo.order_line.move_ids.filtered(
            lambda m: str(m.id) in return_details and m.state == "done" and m._urp_is_customer_delivery())
        if len(moves) != len(return_details):
            return {"error": "Invalid return lines."}
        # same lock as the Inventory return checks (a plain FOR UPDATE keeps the old snapshot under
        # REPEATABLE READ): a request and a return of the same delivery line are serialised
        urp_lock_rows(request.env.cr, "stock_move", moves.ids)
        moves.invalidate_recordset()
        now = fields.Datetime.now()
        line_vals = []
        for move in moves:
            try:
                qty = float(return_details[str(move.id)])
            except (TypeError, ValueError):
                return {"error": "Invalid quantity."}
            if qty <= 0:
                return {"error": "Invalid quantity."}
            if move.urp_batch_id:
                if move._urp_non_returnable():
                    return {"error": "%s is not returnable." % move.product_id.display_name}
                if not move._urp_portal_open(now):
                    return {"error": "The return period has ended for %s." % move.product_id.display_name}
                available = move._urp_portal_available()
            else:
                available = move._prepare_return_data()["remaining_delivered_qty"]
            if move.uom_id.compare(qty, available) > 0:
                return {"error": "Only %s unit(s) of %s can still be returned." % (available, move.product_id.display_name)}
            line_vals.append((0, 0, {"move_id": move.id, "requested_qty": qty}))
        reason = request.env["return.reason"].sudo().browse(int(return_reason_id)).exists() if return_reason_id else False
        req = Request.create({
            "order_id": order_sudo.id, "submitted_at": now, "idempotency_key": str(idempotency_key),
            "return_reason_id": reason.id if reason else False, "line_ids": line_vals,
        })
        move_qty_by_picking = {}
        for line in req.line_ids:
            move_qty_by_picking.setdefault(line.move_id.picking_id, {})[line.move_id] = line.requested_qty
        order_sudo.message_post(body=self._build_return_log_message(move_qty_by_picking, reason or request.env["return.reason"]))
        return {"request_id": req.id, "label_url": self._urp_label_url(order_sudo, req, access_token)}

    def _urp_label_url(self, order_sudo, req, access_token):
        return f"/my/orders/{order_sudo.id}/download_return_label?access_token={access_token}&urp_request_id={req.id}"

    @route()
    def order_return_label(self, order_id, access_token=None, return_details=None, return_reason_id=None, urp_request_id=None):
        order_sudo = self._urp_order(order_id, access_token)
        if not order_sudo or not order_sudo._urp_governed_deliveries():
            return super().order_return_label(order_id, access_token=access_token, return_details=return_details,
                                              return_reason_id=return_reason_id)
        # governed order: the label is a read-only print of an existing request (no side effect)
        req = request.env["urp.portal.return.request"].sudo().browse(int(urp_request_id or 0)).exists()
        if not req or req.order_id != order_sudo or req.state == "cancelled":
            raise BadRequest("Unknown return request.")
        move_qty_by_picking = {}
        for line in req.line_ids:
            move_qty_by_picking.setdefault(line.move_id.picking_id, {})[line.move_id] = line.requested_qty
        pdf = request.env["ir.actions.report"].sudo()._render_qweb_pdf(
            "sale_stock.action_report_return_label", [p.id for p in move_qty_by_picking],
            data={"wh_address_id": order_sudo.warehouse_id.partner_id, "move_qty_by_picking": move_qty_by_picking,
                  "return_reason": req.return_reason_id})[0]
        headers = [("Content-Type", "application/pdf"),
                   ("Content-Disposition", content_disposition(f"Return-{order_sudo.name}.pdf", "inline")),
                   ("Content-Length", len(pdf))]
        return request.make_response(pdf, headers=headers)
