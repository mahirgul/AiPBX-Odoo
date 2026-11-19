import json
import uuid
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAipbxCrm(TransactionCase):
    def setUp(self):
        super().setUp()
        self.conn = self.env["aipbx.connection"].create({"name": "PBX"})
        self.conn._apply_pack(json.dumps({"aipbx_connection": 1, "pbx_url": "https://pbx.example.com", "connection_id": str(uuid.uuid4()),
                                          "api_token": "apbx_t", "webhook_secret": "whsec_t"}))
        self.partner = self.env["res.partner"].create({"name": "ACME", "phone": "+90 212 555 00 11"})
        self.lead = self.env["crm.lead"].create({"name": "ACME deal", "partner_id": self.partner.id, "type": "opportunity"})

    def _event(self, name, data):
        rec = self.env["aipbx.event"].create({"connection_id": self.conn.id, "event_id": str(uuid.uuid4()), "event": name,
                                              "payload": json.dumps({"data": data})})
        rec._process()
        self.assertEqual(rec.state, "done", rec.error)

    def test_a_call_of_the_contact_lands_on_its_opportunity(self):
        base = {"linkedid": "c1", "direction": "inbound", "caller": {"number": "02125550011"}}
        with patch.object(type(self.env["aipbx.call"]), "_pop_up", lambda *a: None):
            self._event("call.ringing", base)
        self._event("call.completed", dict(base, outcome="answered", talk_seconds=30))
        self._event("call.ai_completed", {"linkedid": "c1", "summary": "Wants a quote.", "qa_score": 70})
        call = self.env["aipbx.call"].search([("linkedid", "=", "c1")])
        self.assertEqual(call.lead_id, self.lead)
        self.assertEqual(call._pop_up_payload()["lead_id"], self.lead.id)
        bodies = " ".join(self.lead.message_ids.mapped(lambda m: str(m.body)))
        self.assertIn("📞", bodies)
        self.assertIn("Wants a quote.", bodies)
        self.assertEqual(self.lead.aipbx_call_count, 1)

    def test_an_opportunity_found_by_its_own_number(self):
        lead = self.env["crm.lead"].create({"name": "Walk-in", "phone": "0532 999 88 77", "type": "lead"})
        self._event("call.ringing", {"linkedid": "c2", "direction": "inbound", "caller": {"number": "+905329998877"}})
        self.assertEqual(self.env["aipbx.call"].search([("linkedid", "=", "c2")]).lead_id, lead)

    def test_two_open_opportunities_link_none(self):
        self.env["crm.lead"].create({"name": "Second deal", "partner_id": self.partner.id, "type": "opportunity"})
        self._event("call.ringing", {"linkedid": "c3", "direction": "inbound", "caller": {"number": "02125550011"}})
        call = self.env["aipbx.call"].search([("linkedid", "=", "c3")])
        self.assertEqual(call.partner_id, self.partner)
        self.assertFalse(call.lead_id, "not guessed")

    def test_won_leads_do_not_exhaust_limit_50_and_popup_checks_crm_access(self):
        won_stage = self.env["crm.stage"].create({"name": "WonTest", "is_won": True})
        for i in range(55):
            self.env["crm.lead"].create({"name": f"Old Won {i}", "phone": "0532 777 66 55", "stage_id": won_stage.id, "type": "opportunity"})
        open_lead = self.env["crm.lead"].create({"name": "Open Deal", "phone": "0532 777 66 55", "type": "opportunity"})
        self._event("call.ringing", {"linkedid": "c4", "direction": "inbound", "caller": {"number": "+905327776655"}})
        call = self.env["aipbx.call"].search([("linkedid", "=", "c4")])
        self.assertEqual(call.lead_id, open_lead)

        # User without CRM read rights must not receive lead_id/lead name in pop-up payload.
        grp_field = "group_ids" if "group_ids" in self.env["res.users"]._fields else "groups_id"
        base_user = self.env.ref("base.group_user")
        no_crm = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "No CRM", "login": "no_crm_aipbx@example.com",
            grp_field: [(6, 0, [base_user.id])],
        })
        payload = call._pop_up_payload(user=no_crm)
        self.assertNotIn("lead_id", payload)
        self.assertNotEqual(payload["name"], open_lead.name)
