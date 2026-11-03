from odoo import api, fields, models
from odoo.addons.base.models.res_partner import _tz_get

from .urp_policy_history import DATE_BASIS

URP_SETTINGS_FIELDS = ("urp_enabled", "urp_default_days", "urp_tz", "urp_date_basis")


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # Not ``related`` fields on purpose: a related field writes its target through its own
    # inverse, one field at a time, while the settings record is created. Enabling the policy
    # would then reach res.company as a write of ``urp_enabled`` alone, before ``urp_tz`` is
    # written, and the company constraint (timezone required) fails although the form holds
    # a timezone. The three values are written to the company together in ``set_values``.
    urp_enabled = fields.Boolean("Enable Unified Return Policy", compute="_compute_urp_enabled",
                                 store=True, readonly=False)
    urp_default_days = fields.Integer("Default Return Period (Days)", compute="_compute_urp_default_days",
                                      store=True, readonly=False)
    urp_tz = fields.Selection(_tz_get, string="Return Policy Timezone", compute="_compute_urp_tz",
                              store=True, readonly=False)
    urp_date_basis = fields.Selection(DATE_BASIS, string="Return Period Calculation Based On",
                                      compute="_compute_urp_date_basis", store=True, readonly=False)

    # one compute per field: a value given for one field must not reset the others
    @api.depends("company_id")
    def _compute_urp_enabled(self):
        for settings in self:
            settings.urp_enabled = settings.company_id.urp_enabled

    @api.depends("company_id")
    def _compute_urp_default_days(self):
        for settings in self:
            settings.urp_default_days = settings.company_id.urp_default_days

    @api.depends("company_id")
    def _compute_urp_tz(self):
        for settings in self:
            settings.urp_tz = settings.company_id.urp_tz

    @api.depends("company_id")
    def _compute_urp_date_basis(self):
        for settings in self:
            settings.urp_date_basis = settings.company_id.urp_date_basis or "validation"

    def set_values(self):
        super().set_values()
        for settings in self:
            company = settings.company_id
            vals = {f: settings[f] for f in URP_SETTINGS_FIELDS if settings[f] != company[f]}
            if vals:
                # one write: the constraint and the policy history see the final, consistent values
                company.write(vals)
