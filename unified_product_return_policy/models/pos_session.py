import hashlib
import secrets
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessDenied, UserError

from .urp_tools import urp_lock_rows

MAX_FAILS = 5
WINDOW_MIN = 15
TOKEN_MINUTES = 10


class UrpAuthAttempt(models.Model):
    _name = "urp.auth.attempt"
    _description = "POS Manager Re-authentication Attempt"

    user_id = fields.Many2one("res.users", index=True, readonly=True)
    employee_id = fields.Many2one("hr.employee", readonly=True)
    method = fields.Selection([("password", "Odoo account"), ("pin", "Approval PIN")], default="password", readonly=True)
    session_id = fields.Many2one("pos.session", readonly=True)
    config_id = fields.Many2one("pos.config", readonly=True, index="btree_not_null")
    company_id = fields.Many2one(related="config_id.company_id", store=True, index=True)
    success = fields.Boolean(readonly=True)

    @api.model
    def _urp_serialise(self, config):
        """Attempts made at the same moment on one point of sale are serialised before the failures
        are counted (no-op update of its row: a concurrent attempt fails with a serialization error
        and Odoo retries it with fresh data). Without it each attempt would count only the failures
        committed before it started, and a burst could try far more than the limit."""
        urp_lock_rows(self.env.cr, "pos_config", config.ids)

    @api.model
    def _urp_pin_locked(self, config):
        """Approval PINs are short: repeated failures on a point of sale lock its PIN approvals
        for a while (any session, any device), the Odoo account path stays available.

        The lock lasts WINDOW_MIN full minutes from the failure that reached MAX_FAILS within
        WINDOW_MIN minutes. Nothing is recorded while locked, so the failures before a lock are
        all older than WINDOW_MIN when it ends: they never count again."""
        now = fields.Datetime.now()
        window = timedelta(minutes=WINDOW_MIN)
        self.sudo().invalidate_model(["create_date"])            # dates as stored, never a stale cache
        fails = self.sudo().search([("config_id", "=", config.id), ("method", "=", "pin"), ("success", "=", False),
                                    ("create_date", ">", now - 2 * window)], order="create_date, id").mapped("create_date")
        return any(t > now - window and sum(1 for s in fails if t - window <= s <= t) >= MAX_FAILS for t in fails)


class UrpReturnOverridePos(models.Model):
    _inherit = "urp.return.override"

    pos_session_id = fields.Many2one("pos.session", readonly=True)
    pos_order_id = fields.Many2one("pos.order", readonly=True)
    approver_employee_id = fields.Many2one("hr.employee", string="Approving POS Manager (employee)", readonly=True)
    approver_config_id = fields.Many2one("pos.config", string="Manager of Point of Sale", readonly=True)
    # who completed the refund on the POS (display only, from the refund order)
    executed_by_user_id = fields.Many2one(related="pos_order_id.user_id", string="Refund Done By (user)")
    executed_by_employee_id = fields.Many2one(related="pos_order_id.employee_id", string="Refund Done By (employee)")
    same_person = fields.Boolean("Approver = Executor", compute="_compute_same_person")

    def _compute_same_person(self):
        for rec in self:
            ov = rec.sudo()
            order = ov.pos_order_id
            if ov.approver_employee_id and order.employee_id:
                rec.same_person = ov.approver_employee_id == order.employee_id
            elif ov.user_id and order and not order.employee_id:
                rec.same_person = ov.user_id == order.user_id
            elif ov.exception_id:
                rec.same_person = bool(ov.user_id) and ov.user_id in ov.usage_ids.user_id
            else:
                rec.same_person = False
    pos_order_uuid = fields.Char("POS Order UUID", readonly=True, index="btree_not_null", copy=False,
                                 help="The refund order this authorisation was issued for; it cannot be used for any other order.")
    token_hash = fields.Char(readonly=True, index=True, copy=False)
    expires_at = fields.Datetime(readonly=True)
    # display only (not stored): what the existing data already tells
    pos_currency_id = fields.Many2one(related="pos_order_id.currency_id")
    pos_amount_total = fields.Monetary(related="pos_order_id.amount_total", string="Refund Amount",
                                       currency_field="pos_currency_id")


class UrpReturnOverrideLinePos(models.Model):
    _inherit = "urp.return.override.line"

    refunded_line_id = fields.Many2one("pos.order.line", string="Original POS Line", readonly=True)


class PosSession(models.Model):
    _inherit = "pos.session"

    @api.model
    def _urp_exception_type_of(self, line, refunded):
        """Audit type of an authorised line, from the receipt line (server data only): a linked
        line sold non-returnable is a Non-Returnable Exception, any other linked line an Expired
        Return Period Override, a line without receipt the Standalone Refund Exception. The
        authorisation stays bound to that very line: a standalone one never covers a receipt."""
        if not line["refunded_line_id"]:
            return "standalone"
        orig = refunded.filtered(lambda r: r.id == line["refunded_line_id"])
        return "non_returnable" if (orig.urp_has_snapshot and orig.urp_return_days == 0) else "expired"

    def _urp_override_precheck(self, reason, lines, order_uuid):
        """Checks shared by both approval paths: (error, normalised lines, original lines)."""
        self.ensure_one()
        self.check_access("read")
        if self.state != "opened":
            return "session_closed", None, None
        if not (reason or "").strip():
            return "reason_required", None, None
        if not order_uuid or not isinstance(order_uuid, str):
            return "no_order", None, None
        if self.env["pos.order"].sudo().search_count([("uuid", "=", order_uuid), ("state", "not in", ("draft", "cancel"))]):
            return "no_order", None, None
        try:
            lines = [{"product_id": int(l["product_id"]), "qty": float(l.get("qty") or 0),
                      "refunded_line_id": int(l.get("refunded_line_id") or 0)}
                     for l in (lines or []) if isinstance(l, dict) and l.get("product_id")]
        except (TypeError, ValueError):
            return "no_lines", None, None
        lines = [l for l in lines if l["qty"] > 0]
        if not lines:
            return "no_lines", None, None
        refunded = self.env["pos.order.line"].sudo().browse([l["refunded_line_id"] for l in lines if l["refunded_line_id"]]).exists()
        for l in lines:
            if l["refunded_line_id"]:
                orig = refunded.filtered(lambda r: r.id == l["refunded_line_id"])
                if not orig or orig.product_id.id != l["product_id"] or orig.order_id.company_id != self.company_id:
                    return "no_lines", None, None
        return None, lines, refunded

    def _urp_issue_token(self, approver_vals, reason, lines, refunded, order_uuid):
        token = secrets.token_urlsafe(32)
        self.env["urp.return.override"].sudo().create(dict(approver_vals, **{
            "channel": "pos", "reason": reason.strip(), "company_id": self.company_id.id,
            "pos_session_id": self.id, "pos_order_uuid": order_uuid,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": fields.Datetime.now() + timedelta(minutes=TOKEN_MINUTES),
            "line_ids": [(0, 0, {"product_id": l["product_id"], "qty": l["qty"],
                                 "refunded_line_id": l["refunded_line_id"] or False,
                                 "exception_type": self._urp_exception_type_of(l, refunded)}) for l in lines],
        }))
        return token

    def urp_refund_needs(self, lines=None):
        """Pre-check of ship-later refund lines before payment, read only.

        The deadlines of a ship-later line are per delivery, unknown to the device, while the
        payment requires a manager when the period ended or the product is non-returnable. Same decision as the payment (``_urp_ship_later_eval``),
        without the row locks: nothing is written, no token is issued, no attempt is recorded.
        The answer is advisory only: the payment checks everything again and stays the only
        authority. ``lines``: [{product_id, refunded_line_id, qty}] as the approval dialog sends them.
        Returns {"needs": [{product_id, refunded_line_id, kind, qty}], "blocked": message or False}."""
        self.ensure_one()
        self.check_access("read")                            # the caller's own right on this session
        res = {"needs": [], "blocked": False}
        if self.state != "opened" or not self.company_id.urp_enabled:
            return res
        try:
            wanted = [(int(l["refunded_line_id"]), int(l["product_id"]), float(l.get("qty") or 0))
                      for l in (lines or []) if isinstance(l, dict) and l.get("refunded_line_id") and l.get("product_id")]
        except (TypeError, ValueError):
            return res
        Order = self.env["pos.order"].sudo()
        origs = self.env["pos.order.line"].sudo().browse({w[0] for w in wanted}).exists()
        now = fields.Datetime.now()
        inflight = {"returned": {}, "allocated": {}}           # lines of one refund, as at payment
        for orig_id, product_id, qty in wanted:
            orig = origs.filtered(lambda r: r.id == orig_id)
            if (qty <= 0 or not orig or orig.product_id.id != product_id or orig.order_id.company_id != self.company_id
                    or not orig.urp_has_snapshot or not orig.order_id.shipping_date):
                continue                                     # not a ship-later line of this company
            ev = Order._urp_ship_later_eval(orig, qty, now, inflight)
            if ev["error"]:
                return {"needs": [], "blocked": ev["error"]}
            res["needs"] += [{"product_id": n["product"].id, "refunded_line_id": orig.id, "kind": n["kind"], "qty": n["qty"]}
                             for n in ev["needs"]]
        return res

    def urp_request_override_pin(self, pin=None, reason=None, lines=None, order_uuid=None):
        """POS Manager employee path: the approval PIN alone identifies the manager,
        verified on the server among the POS Manager employees of this point of sale (the browser
        sends no employee, no role). Same one-time, 10-minute token bound to this session, its
        company, the refund order and its lines. The PIN is never stored or logged."""
        error, lines, refunded = self._urp_override_precheck(reason, lines, order_uuid)
        if error:
            return {"ok": False, "error": error}
        config = self.config_id
        Attempt = self.env["urp.auth.attempt"].sudo()
        Attempt._urp_serialise(config)
        if Attempt._urp_pin_locked(config):
            return {"ok": False, "error": "locked"}
        employee, error = config._urp_identify_manager(pin)
        if error:
            if error != "pin_unavailable":
                Attempt.create({"method": "pin", "session_id": self.id, "config_id": config.id, "success": False})
            return {"ok": False, "error": error}
        Attempt.create({"method": "pin", "employee_id": employee.id, "user_id": employee.user_id.id,
                        "session_id": self.id, "config_id": config.id, "success": True})
        token = self._urp_issue_token({
            "user_id": employee.user_id.id or False, "approver_employee_id": employee.id,
            "approver_config_id": config.id, "approver_role": "pos_manager_employee", "auth_method": "pin",
        }, reason, lines, refunded, order_uuid)
        return {"ok": True, "token": token, "manager": employee.name}

    def urp_request_override(self, login, password, totp=None, reason=None, lines=None, order_uuid=None):
        """Re-authenticate a POS Administrator on the server and issue a one-time,
        10-minute override token bound to this session, its company, one refund order
        (``order_uuid``) and the (product, original line, qty) lines.
        Never raises on bad credentials so that failed attempts are committed and counted."""
        error, lines, refunded = self._urp_override_precheck(reason, lines, order_uuid)
        if error:
            return {"ok": False, "error": error}
        Attempt = self.env["urp.auth.attempt"].sudo()
        Attempt._urp_serialise(self.config_id)
        user = self.env["res.users"].sudo().search([("login", "=", login)], limit=1)
        if not user:
            return {"ok": False, "error": "invalid"}
        urp_lock_rows(self.env.cr, "res_users", user.ids)     # the limit is per account, on every POS
        since = fields.Datetime.now() - timedelta(minutes=WINDOW_MIN)
        if Attempt.search_count([("user_id", "=", user.id), ("success", "=", False), ("create_date", ">=", since)]) >= MAX_FAILS:
            return {"ok": False, "error": "locked"}
        try:
            as_user = user.with_user(user)
            as_user._check_credentials({"type": "password", "password": password}, {"interactive": True})
            if "totp_enabled" in user._fields and user.totp_enabled:
                if not totp:
                    raise AccessDenied("totp_required")
                as_user._check_credentials({"type": "totp", "token": int(totp)}, {"interactive": True})
            if not user.has_group("point_of_sale.group_pos_manager") or self.company_id not in user.company_ids:
                raise AccessDenied("not_pos_administrator")
        except (AccessDenied, TypeError, ValueError):
            Attempt.create({"user_id": user.id, "session_id": self.id, "config_id": self.config_id.id, "success": False})
            # one answer for every refusal, so that the answer never tells whether the
            # password was right (missing or wrong code, not a POS Administrator of this company)
            return {"ok": False, "error": "invalid"}
        Attempt.create({"user_id": user.id, "session_id": self.id, "config_id": self.config_id.id, "success": True})
        token = self._urp_issue_token({"user_id": user.id, "approver_role": "pos_admin", "auth_method": "password"},
                                      reason, lines, refunded, order_uuid)
        return {"ok": True, "token": token, "manager": user.name}
