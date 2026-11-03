from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.addons.base.models.res_partner import _tz_get

from .urp_policy_history import COMPANY_FIELDS, DATE_BASIS


class ResCompany(models.Model):
    _inherit = "res.company"

    urp_enabled = fields.Boolean("Enable Unified Return Policy")
    urp_default_days = fields.Integer("Default Return Period (Days)", default=14)
    urp_tz = fields.Selection(_tz_get, string="Return Policy Timezone",
                              help="Timezone used to compute return deadlines (end of the local day).")
    urp_date_basis = fields.Selection(
        DATE_BASIS, string="Return Period Calculation Based On", required=True, default="validation",
        help="Actual Validation Date: server time at which the delivery was validated (never changes).\n"
             "Effective Date: the effective date of the delivery; the deadline follows later corrections "
             "of that date. Frozen per sale: changing this setting does not affect past sales.")

    @api.constrains("urp_enabled", "urp_default_days", "urp_tz")
    def _check_urp_settings(self):
        for company in self.filtered("urp_enabled"):
            if company.urp_default_days <= 0:
                raise ValidationError(self.env._("The default return period must be greater than zero (company %s).", company.name))
            if not company.urp_tz:
                raise ValidationError(self.env._("Set the return policy timezone before enabling the policy (company %s).", company.name))

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create(vals_list)
        self.env["urp.policy.history"]._urp_log(companies, origin="baseline")
        return companies

    def write(self, vals):
        if vals.get("urp_enabled") and "urp_tz" not in vals:
            for company in self.filtered(lambda c: not c.urp_tz and c.partner_id.tz):
                super(ResCompany, company).write({"urp_tz": company.partner_id.tz})
        tracked = [f for f in COMPANY_FIELDS if f in vals]
        before = {c.id: {f: c[f] for f in tracked} for c in self} if tracked else {}
        res = super().write(vals)
        if tracked:
            changed = self.filtered(lambda c: any(c[f] != before[c.id][f] for f in tracked))
            self.env["urp.policy.history"]._urp_log(changed)
        return res
