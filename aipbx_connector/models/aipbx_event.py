import json
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

CALL_EVENTS = ("call.ringing", "call.offered", "call.answered", "call.completed")


class AipbxEvent(models.Model):
    """Every signed delivery, kept once per (connection, event id): a repeated delivery is
    answered OK and not processed again; a failed one is retried by the cron."""

    _name = "aipbx.event"
    _description = "AiPBX event"
    _order = "id desc"

    connection_id = fields.Many2one("aipbx.connection", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", related="connection_id.company_id", store=True, index=True)
    event_id = fields.Char("Event id", required=True, index=True)
    event = fields.Char(required=True)
    payload = fields.Text()
    state = fields.Selection([("new", "New"), ("done", "Done"), ("failed", "Failed")], default="new", index=True)
    attempts = fields.Integer()
    error = fields.Char()

    _event_unique = models.Constraint("unique(connection_id, event_id)", "This event was received already.")

    def _process(self):
        for rec in self:
            try:
                with self.env.cr.savepoint():
                    rec._handle(json.loads(rec.payload or "{}").get("data") or {})
                rec.write({"state": "done", "error": False})
            except Exception as e:  # kept for the retry; the PBX got its 200 already
                _logger.warning("AiPBX event %s (%s) failed, kept for the retry: %s", rec.event_id, rec.event, e)
                rec.write({"state": "failed", "attempts": rec.attempts + 1, "error": str(e)[:250]})

    def _handle(self, data):
        conn = self.connection_id
        Call = self.env["aipbx.call"].sudo()
        if self.event == "webhook.test":
            conn.sudo().last_test_at = fields.Datetime.now()
        elif self.event in CALL_EVENTS:
            call = Call._upsert(conn, data)
            call._on_event(self.event, data)
        elif self.event == "recording.ready":
            linkedid = str(data.get("linkedid") or "").strip()
            if linkedid and data.get("recording_id"):
                rec_id = int(data["recording_id"])
                call = Call._get_or_create_for_event(conn, linkedid)
                if not call.recording_id:
                    call.recording_id = rec_id
                    call._log_recording()
        elif self.event == "call.ai_completed":
            linkedid = str(data.get("linkedid") or "").strip()
            if linkedid:
                call = Call._get_or_create_for_event(conn, linkedid)
                call._log_ai(data)
        conn.sudo().last_event_at = fields.Datetime.now()

    @api.model
    def _cron_retry(self):
        self.search([("state", "=", "failed"), ("attempts", "<", 5)], limit=200)._process()
        # Kept for a month: enough to look into a problem.
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=30)
        self.search([
            ("create_date", "<", cutoff),
            "|", ("state", "=", "done"),
            "&", ("state", "=", "failed"), ("attempts", ">=", 5),
        ]).unlink()
