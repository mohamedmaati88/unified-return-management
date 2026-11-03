from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.addons.base.models.ir_model import MODULE_UNINSTALL_FLAG

SHARED_POLICY_COMPANY_PARAM = "unified_product_return_policy.shared_policy_max_company_id"
COMPANY_FIELDS = ("urp_enabled", "urp_default_days", "urp_tz", "allow_spontaneous_returns", "return_validity_days",
                  "urp_date_basis")
DATE_BASIS = [("validation", "Actual Validation Date"), ("effective", "Effective Date")]
POLICY_FIELDS = ("use_custom_return_period", "custom_return_period_days")


class UrpPolicyHistory(models.Model):
    """Append-only log of every return-policy value change.

    Used to resolve the policy that was in force at a past instant (POS offline sales)
    and to detect sales whose policy cannot be proven.
    """
    _name = "urp.policy.history"
    _description = "Return Policy History"
    _order = "valid_from desc, id desc"
    _rec_name = "scope"

    scope = fields.Selection([("company", "Company"), ("category", "Product Category"), ("product", "Product")],
                             required=True, readonly=True, index=True)
    res_id = fields.Integer("Record ID", required=True, readonly=True, index=True)
    company_id = fields.Many2one("res.company", readonly=True, index=True)
    valid_from = fields.Datetime(required=True, readonly=True, index=True, default=fields.Datetime.now)
    origin = fields.Selection([("baseline", "Baseline"), ("change", "Change"), ("detected", "Detected mismatch")],
                              required=True, readonly=True, default="change")
    # company scope
    enabled = fields.Boolean(readonly=True)
    default_days = fields.Integer(readonly=True)
    tz = fields.Char(readonly=True)
    portal_allowed = fields.Boolean(readonly=True)
    portal_days = fields.Integer(readonly=True)
    date_basis = fields.Selection(DATE_BASIS, readonly=True)
    # product / category scope
    use_custom = fields.Boolean(readonly=True)
    custom_days = fields.Integer(readonly=True)
    categ_id = fields.Integer("Category ID", readonly=True, help="Product category at that time (product scope)")
    policy_company_id = fields.Many2one(
        "res.company", string="Policy Company", readonly=True, index=True,
        help="Company whose product/category return policy this row describes. Empty on legacy rows "
             "that describe one value shared by all companies.")
    user_id = fields.Many2one("res.users", readonly=True, default=lambda self: self.env.uid)

    _scope_res_valid = models.Index("(scope, res_id, valid_from)")

    def write(self, vals):
        raise UserError(self.env._("The return policy history is append-only."))

    def unlink(self):
        if not self.env.context.get(MODULE_UNINSTALL_FLAG):
            raise UserError(self.env._("The return policy history is append-only."))
        return super().unlink()

    # ------------------------------------------------------------------ writers
    @api.model
    def _urp_company_vals(self, company):
        return {
            "scope": "company", "res_id": company.id, "company_id": company.id,
            "enabled": company.urp_enabled, "default_days": company.urp_default_days, "tz": company.urp_tz or False,
            "portal_allowed": company.allow_spontaneous_returns, "portal_days": company.return_validity_days,
            "date_basis": company.urp_date_basis or "validation",
        }

    @api.model
    def _urp_record_vals(self, record, company):
        """Product/category values IN ``company`` (company-dependent fields)."""
        scope = "product" if record._name == "product.template" else "category"
        rec = record.with_company(company)
        vals = {
            "scope": scope, "res_id": record.id, "company_id": company.id, "policy_company_id": company.id,
            "use_custom": rec.use_custom_return_period, "custom_days": rec.custom_return_period_days,
        }
        if scope == "product":
            vals["categ_id"] = record.categ_id.id
        return vals

    @api.model
    def _urp_log(self, records, origin="change", company=None):
        """Log companies, or products/categories for ``company`` (default: the current company,
        the only one whose values a write can change)."""
        Hist = self.sudo()
        company = company or self.env.company
        vals_list = []
        for rec in records:
            vals = self._urp_company_vals(rec) if rec._name == "res.company" else self._urp_record_vals(rec, company)
            vals["origin"] = origin
            vals_list.append(vals)
        return Hist.create(vals_list) if vals_list else Hist

    @api.model
    def _urp_scope(self, record):
        return {"res.company": "company", "product.template": "product", "product.category": "category"}[record._name]

    @api.model
    def _urp_has_rows(self, record, company):
        """True when the history already describes ``record`` for ``company`` (own rows, or legacy
        shared rows that apply to that company)."""
        return bool(self.sudo().search_count(self._urp_scope_domain(self._urp_scope(record), record.id, company), limit=1))

    @api.model
    def _urp_shared_rows_apply(self, company):
        """Legacy rows without a policy company describe ONE value shared by all the companies that
        existed then. They only apply to a company that already existed when the policy became
        company-dependent: the highest company id at that time is kept in a system parameter (ids
        are sequential, unlike timestamps shared by records of one transaction). A company created
        afterwards never received that value and must not inherit it, today or for a past instant.
        Without that parameter (new database) the legacy rows apply to no company."""
        max_id = self.env["ir.config_parameter"].sudo().get_int(SHARED_POLICY_COMPANY_PARAM)
        return bool(max_id) and company.id <= max_id   # no parameter = fresh install: no shared rows

    @api.model
    def _urp_log_previous(self, record, before, company):
        """Baseline row with the values ``record`` had in ``company`` before its first logged
        change, valid since the record was created."""
        vals = self._urp_record_vals(record, company)
        vals.update({
            "origin": "baseline",
            # whole seconds, like every other row: a baseline must never sort after a change made in
            # the same second (create_date carries microseconds)
            "valid_from": (record.create_date or fields.Datetime.to_datetime("1970-01-01")).replace(microsecond=0),
            "use_custom": before.get("use_custom_return_period", vals["use_custom"]),
            "custom_days": before.get("custom_return_period_days", vals["custom_days"]),
        })
        if "categ_id" in vals and "categ_id" in before:
            vals["categ_id"] = before["categ_id"].id
        return self.sudo().create(vals)

    @api.model
    def _urp_write_baseline(self):
        return self._urp_log(self.env["res.company"].sudo().search([]), origin="baseline")

    # ------------------------------------------------------------------ readers
    @api.model
    def _urp_scope_domain(self, scope, res_id, company=None):
        domain = [("scope", "=", scope), ("res_id", "=", res_id)]
        if scope != "company" and company:
            # rows of that company, plus the legacy shared rows (no policy company) only
            # for companies that existed when that value was shared
            ids = [company.id, False] if self._urp_shared_rows_apply(company) else [company.id]
            domain.append(("policy_company_id", "in", ids))
        return domain

    @api.model
    def _urp_latest(self, rows):
        """Most recent row, comparing timestamps to the second, then the creation order (id).
        Legacy baseline rows may carry microseconds (create_date); without this
        they could hide a change logged in the same second."""
        return max(rows, key=lambda r: (r.valid_from.replace(microsecond=0), r.id)) if rows else rows

    @api.model
    def _urp_row_at(self, scope, res_id, at, company=None):
        rows = self.sudo().search(self._urp_scope_domain(scope, res_id, company) + [("valid_from", "<=", at)], limit=50)
        return self._urp_latest(rows)

    @api.model
    def _urp_categ_at(self, template, at):
        """Category of ``template`` at instant ``at``. The category is shared by all companies, so
        product rows of any company tell it; the current category only when the product has no
        row at all (never changed)."""
        row = self._urp_row_at("product", template.id, at)
        if row:
            return row.categ_id or False
        if self.sudo().search_count([("scope", "=", "product"), ("res_id", "=", template.id)], limit=1):
            # rows exist but all after ``at``: the earliest one is a baseline of the previous state
            rows = self.sudo().search([("scope", "=", "product"), ("res_id", "=", template.id)], order="valid_from, id", limit=50)
            first = min(rows, key=lambda r: (r.valid_from.replace(microsecond=0), r.id))
            return first.categ_id or False
        return template.categ_id.id or False

    @api.model
    def _urp_changes_between(self, scope, res_id, start, stop, company=None):
        """Rows that changed the policy of one record (for ``company``) inside ]start, stop]."""
        return self.sudo().search(self._urp_scope_domain(scope, res_id, company) + [
            ("valid_from", ">", start), ("valid_from", "<=", stop), ("origin", "!=", "baseline")])

    @api.model
    def _urp_categ_changes_between(self, template, start, stop):
        """Product rows of ANY company inside ]start, stop] that moved the product to another
        category (the category is shared: a move logged in company A also concerns company B)."""
        categ_start = self._urp_categ_at(template, start)
        rows = self.sudo().search([("scope", "=", "product"), ("res_id", "=", template.id),
                                   ("valid_from", ">", start), ("valid_from", "<=", stop)])
        return rows.filtered(lambda r: r.categ_id and r.categ_id != categ_start)

    # ------------------------------------------------------------------ consistency
    @api.model
    def _cron_urp_check_consistency(self):
        """Detect values changed outside the ORM (e.g. direct SQL) and log them as 'detected',
        company by company (product and category values are company-dependent)."""
        companies = self.env["res.company"].sudo().search([])
        mismatched = self.env["res.company"]
        for company in companies:
            last = self._urp_row_at("company", company.id, fields.Datetime.now())
            cur = self._urp_company_vals(company)
            if not last or any(last[k] != cur[k] for k in ("enabled", "default_days", "tz", "portal_allowed", "portal_days", "date_basis")):
                mismatched |= company
        rows = self._urp_log(mismatched, origin="detected") if mismatched else self.browse()
        for model in ("product.category", "product.template"):
            scope = "category" if model == "product.category" else "product"
            Model = self.env[model].sudo().with_context(active_test=False)
            logged = Model.browse(set(self.sudo().search([("scope", "=", scope)]).mapped("res_id"))).exists()
            for company in companies:
                recs = Model.with_company(company).search([("use_custom_return_period", "=", True)]) | logged
                bad = self.env[model]
                for rec in recs:
                    last = self._urp_row_at(scope, rec.id, fields.Datetime.now(), company)
                    cur = self._urp_record_vals(rec, company)
                    # no row = never customised in that company = default values
                    last_custom, last_days = (last.use_custom, last.custom_days) if last else (False, 0)
                    if last_custom != cur["use_custom"] or last_days != cur["custom_days"] or \
                            (last and scope == "product" and last.categ_id != cur["categ_id"]):
                        bad |= rec
                if bad:
                    rows |= self._urp_log(bad, origin="detected", company=company)
        for row in rows:
            self.env["ir.logging"].sudo().create({
                "name": "unified_product_return_policy", "type": "server", "level": "WARNING",
                "dbname": self.env.cr.dbname, "path": "urp.policy.history", "func": "_cron_urp_check_consistency",
                "line": "0", "message": "Return policy value changed outside the ORM: %s #%s" % (row.scope, row.res_id),
            })
        return rows
