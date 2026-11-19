from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.aipbx_connector.models import tools


class CrmLead(models.Model):
    _inherit = "crm.lead"

    aipbx_call_ids = fields.One2many("aipbx.call", "lead_id", "Calls")
    aipbx_call_count = fields.Integer(compute="_compute_aipbx_call_count")

    def _compute_aipbx_call_count(self):
        data = self.env["aipbx.call"]._read_group([("lead_id", "in", self.ids)], ["lead_id"], ["__count"])
        counts = {lead.id: n for lead, n in data}
        for rec in self:
            rec.aipbx_call_count = counts.get(rec.id, 0)

    def action_aipbx_call(self):
        """Click-to-call the opportunity (its phone, or its contact's) through your extension."""
        self.ensure_one()
        if not self.env.su:
            if not self.env.user.has_group("aipbx_connector.group_aipbx_user"):
                raise AccessError(_("You are not allowed to place AiPBX calls."))
            tools.check_record_access(self, "read")
        number = (
            self.phone
            or ("mobile" in self._fields and self.mobile)
            or self.partner_id.phone
            or ("mobile" in self.partner_id._fields and self.partner_id.mobile)
        )
        if not number:
            raise UserError(_("%s has no phone number.", self.display_name))
        conn = self.env["aipbx.connection"]._for_user(self.env.user)
        if not conn:
            raise UserError(_("You have no extension on a connected PBX. Ask an administrator to assign one."))
        ext = conn._originate(self.env.user, number)
        return conn._notify(_("Your phone (%s) rings now; pick it up to call %s.", ext.extension, number))

    def action_aipbx_calls(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("aipbx_connector.action_aipbx_call")
        action["domain"] = [("lead_id", "=", self.id)]
        action["context"] = {}
        return action
