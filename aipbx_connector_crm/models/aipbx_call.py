from odoo import api, fields, models

from odoo.addons.aipbx_connector.models import tools


class AipbxCall(models.Model):
    _inherit = "aipbx.call"

    lead_id = fields.Many2one("crm.lead", "Opportunity", index=True)

    @api.model
    def _match_lead(self, partner, number, company):
        """The contact's only open opportunity; without a contact, the only open one with the number."""
        Lead = self.env["crm.lead"].sudo()
        open_domain = [("active", "=", True), "|", ("stage_id", "=", False), ("stage_id.is_won", "=", False),
                       "|", ("company_id", "=", False), ("company_id", "=", company.id)]
        if partner:
            leads = Lead.search([("partner_id", "child_of", partner.commercial_partner_id.id)] + open_domain, limit=2)
            return leads if len(leads) == 1 else Lead
        key = tools.match_key(number)
        if len(key) < 5:
            return Lead
        cols = [f for f in ("phone", "mobile") if f in Lead._fields and Lead._fields[f].store]
        if not cols:
            return Lead
        where = " OR ".join(f"right(regexp_replace(coalesce(l.{c}, ''), '\\D', '', 'g'), %s) = %s" for c in cols)
        params = [company.id]
        for _c in cols:
            params += [len(key), key]
        self.env.cr.execute(
            f"SELECT l.id FROM crm_lead l "
            f"LEFT JOIN crm_stage s ON s.id = l.stage_id "
            f"WHERE l.active AND (l.company_id IS NULL OR l.company_id = %s) "
            f"AND (l.stage_id IS NULL OR COALESCE(s.is_won, FALSE) = FALSE) "
            f"AND ({where}) LIMIT 50",
            params,
        )
        leads = Lead.browse([r[0] for r in self.env.cr.fetchall()]).filtered_domain(open_domain)
        return leads if len(leads) == 1 else Lead

    @api.model
    def _upsert(self, connection, data):
        call = super()._upsert(connection, data)
        if not call.lead_id and call.direction != "internal":
            lead = self._match_lead(call.partner_id, call.number, connection.company_id)
            if lead:
                call.lead_id = lead
                if not call.partner_id and lead.partner_id:
                    call.partner_id = lead.partner_id
        return call

    def _log_targets(self):
        targets = super()._log_targets()
        return targets + [self.lead_id] if self.lead_id else targets

    def _pop_up_payload(self, user=None):
        user = user or self.env.user
        payload = super()._pop_up_payload(user=user)
        if self.lead_id and tools.user_can_read(self.lead_id, user):
            if not self.lead_id.company_id or self.lead_id.company_id in user.company_ids:
                payload["lead_id"] = self.lead_id.id
                payload["name"] = payload["name"] or self.lead_id.name
        return payload
