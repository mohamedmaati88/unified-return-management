from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .urp_policy_history import POLICY_FIELDS


class UrpPolicyMixin(models.AbstractModel):
    _name = "urp.policy.mixin"
    _description = "Return period customisation"

    # company-dependent: each company sets its own policy on the shared product / category
    use_custom_return_period = fields.Boolean(
        "Use Custom Return Period", company_dependent=True,
        help="Specific to the current company: other companies keep their own setting.")
    custom_return_period_days = fields.Integer(
        "Return Period (Days)", company_dependent=True,
        help="Set 0 to make it non-returnable. Specific to the current company: other companies keep "
             "their own setting.")
    urp_policy_visible = fields.Boolean(compute="_compute_urp_policy_visible")
    urp_non_returnable = fields.Boolean(compute="_compute_urp_non_returnable")

    # 0 days with the custom period enabled = NON-RETURNABLE (a final policy, never "not set"):
    # (True, 0) -> non-returnable, (True, n > 0) -> n days, (False, any) -> next level
    URP_FIRST_ENABLE_DAYS = 1

    @api.depends("use_custom_return_period", "custom_return_period_days")
    @api.depends_context("company")
    def _compute_urp_non_returnable(self):
        for rec in self:
            rec.urp_non_returnable = bool(rec.use_custom_return_period) and rec.custom_return_period_days == 0

    @api.depends_context("company")
    def _compute_urp_policy_visible(self):
        visible = self.env.company.urp_enabled
        for rec in self:
            rec.urp_policy_visible = visible

    @api.constrains("use_custom_return_period", "custom_return_period_days")
    def _check_custom_return_period(self):
        # values of the company being written (env.company)
        for rec in self.filtered("use_custom_return_period"):
            if rec.custom_return_period_days < 0:
                raise ValidationError(self.env._(
                    "%(name)s: the return period cannot be negative (0 = non-returnable).",
                    name=rec.display_name))

    def _urp_was_custom(self, company=None):
        """True when the custom period was already enabled once for ``company`` (policy history):
        a stored 0 is then a deliberate non-returnable choice, kept when re-enabled."""
        self.ensure_one()
        real = self._origin                    # the saved record behind a form (onchange), or itself
        if not real.id:
            return False                       # new record: never enabled
        Hist = self.env["urp.policy.history"].sudo()
        domain = Hist._urp_scope_domain(Hist._urp_scope(real), real.id, company or self.env.company)
        return bool(Hist.search_count(domain + [("use_custom", "=", True)], limit=1))

    @api.onchange("use_custom_return_period")
    def _onchange_urp_first_enable(self):
        """First activation only: suggest 1 day. A 0 chosen earlier (non-returnable) is kept."""
        for rec in self:
            if rec.use_custom_return_period and not rec.custom_return_period_days and not rec._urp_was_custom():
                rec.custom_return_period_days = self.URP_FIRST_ENABLE_DAYS

    def _urp_tracked(self):
        return POLICY_FIELDS + (("categ_id",) if self._name == "product.template" else ())

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # ORM / import / API: custom period enabled without any number of days -> 1 day;
            # an explicit 0 (non-returnable) is kept as sent
            if vals.get("use_custom_return_period") and "custom_return_period_days" not in vals:
                vals["custom_return_period_days"] = self.URP_FIRST_ENABLE_DAYS
        records = super().create(vals_list)
        # company-dependent values are set for the current company only
        self.env["urp.policy.history"]._urp_log(records.filtered("use_custom_return_period"), company=self.env.company)
        return records

    def write(self, vals):
        if vals.get("use_custom_return_period") and "custom_return_period_days" not in vals:
            # first activation without a number of days: 1 day (records never enabled for this
            # company and holding no value); a deliberate 0 set earlier is kept on re-activation
            first = self.filtered(lambda r: not r.use_custom_return_period and not r.custom_return_period_days
                                  and not r._urp_was_custom())
            if first:
                if self - first:
                    (self - first).write(vals)
                first.write(dict(vals, custom_return_period_days=self.URP_FIRST_ENABLE_DAYS))
                return True
        tracked = [f for f in self._urp_tracked() if f in vals]
        before = {r.id: {f: r[f] for f in tracked} for r in self} if tracked else {}
        res = super().write(vals)
        if tracked:
            changed = self.filtered(lambda r: any(r[f] != before[r.id][f] for f in tracked))
            Hist = self.env["urp.policy.history"]
            company = self.env.company           # the only company whose policy values changed
            # first change of a record never logged for this company (e.g. created before the
            # module was installed, or a product category change): record the previous state
            # first, so that a past instant resolves to the old values and not to the current ones
            for rec in changed:
                if not Hist._urp_has_rows(rec, company):
                    Hist._urp_log_previous(rec, before[rec.id], company)
            Hist._urp_log(changed, company=company)
        return res


class ProductTemplate(models.Model):
    _name = "product.template"
    _inherit = ["product.template", "urp.policy.mixin"]


class ProductCategory(models.Model):
    _name = "product.category"
    _inherit = ["product.category", "urp.policy.mixin"]


class ProductProduct(models.Model):
    _inherit = "product.product"

    def _urp_resolve(self, company, at=None):
        """Return the effective policy of this product for ``company``.

        Priority (no min/max between levels): Product -> direct Category -> Company default.
        ``at`` (UTC datetime) resolves from the policy history instead of live values.
        Returns ``False`` when the policy is not enabled for the company at that instant.
        """
        self.ensure_one()
        company = company.sudo()
        tmpl = self.product_tmpl_id.sudo()
        Hist = self.env["urp.policy.history"]
        if at:
            crow = Hist._urp_row_at("company", company.id, at)
            if not crow:
                return False
            comp = {"enabled": crow.enabled, "default_days": crow.default_days, "tz": crow.tz,
                    "portal_allowed": crow.portal_allowed, "portal_days": crow.portal_days,
                    "date_basis": crow.date_basis or "validation"}
            prow = Hist._urp_row_at("product", tmpl.id, at, company)
            p_custom, p_days = (prow.use_custom, prow.custom_days) if prow else (False, 0)
            categ_id = Hist._urp_categ_at(tmpl, at)              # historical category, not the current one
            cat_row = Hist._urp_row_at("category", categ_id, at, company) if categ_id else Hist
            c_custom, c_days = (cat_row.use_custom, cat_row.custom_days) if cat_row else (False, 0)
        else:
            comp = {"enabled": company.urp_enabled, "default_days": company.urp_default_days, "tz": company.urp_tz,
                    "portal_allowed": company.allow_spontaneous_returns, "portal_days": company.return_validity_days,
                    "date_basis": company.urp_date_basis or "validation"}
            # policy of the DOCUMENT company, whatever the user's active company
            ptmpl, pcateg = tmpl.with_company(company), tmpl.categ_id.with_company(company)
            p_custom, p_days = ptmpl.use_custom_return_period, ptmpl.custom_return_period_days
            c_custom, c_days = pcateg.use_custom_return_period, pcateg.custom_return_period_days
        if not comp["enabled"]:
            return False
        # an enabled custom period is final, 0 included (non-returnable): never "or fallback"
        if p_custom:
            days, source = p_days or 0, "product"
        elif c_custom:
            days, source = c_days or 0, "category"
        else:
            days, source = comp["default_days"], "company"
        portal_eligible = bool(comp["portal_allowed"])
        return {
            "return_days": days,
            "non_returnable": days == 0,
            "policy_source": source,
            "portal_eligible": portal_eligible,
            "portal_days": min(days, comp["portal_days"]) if portal_eligible else 0,
            "tz": comp["tz"],
            "date_basis": comp["date_basis"],
            "company_id": company.id,
        }
