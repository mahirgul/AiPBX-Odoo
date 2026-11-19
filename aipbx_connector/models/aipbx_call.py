import logging
import math

from markupsafe import Markup, escape

from odoo import _, api, fields, models

from . import tools

_logger = logging.getLogger(__name__)


class AipbxCall(models.Model):
    """One call, however many legs it had on the PBX (the PBX's linkedid)."""

    _name = "aipbx.call"
    _description = "AiPBX call"
    _order = "started_at desc, id desc"
    _rec_name = "display_number"

    connection_id = fields.Many2one("aipbx.connection", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="connection_id.company_id", store=True, index=True)
    linkedid = fields.Char(required=True, index=True)
    direction = fields.Selection([("inbound", "Inbound"), ("outbound", "Outbound"), ("internal", "Internal")])
    number = fields.Char("Outside number", help="The caller of an inbound call, the dialled number of an outbound one.")
    display_number = fields.Char(compute="_compute_display_number")
    caller_name = fields.Char("Caller ID name")
    did = fields.Char("Dialled company number")
    queue = fields.Char()
    agent_extension = fields.Char("Extension")
    user_id = fields.Many2one("res.users", "Answered by", index=True)
    offered_user_ids = fields.Many2many("res.users", string="Rang for")
    partner_id = fields.Many2one("res.partner", "Contact", index=True)
    state = fields.Selection([("ringing", "Ringing"), ("answered", "Answered"), ("done", "Done")], default="ringing")
    outcome = fields.Selection([("answered", "Answered"), ("missed", "Missed"), ("abandoned", "Gave up in the queue")])
    started_at = fields.Datetime()
    answered_at = fields.Datetime()
    ended_at = fields.Datetime()
    wait_seconds = fields.Integer("Wait (s)")
    talk_seconds = fields.Integer("Talk (s)")
    recording_id = fields.Integer("Recording", help="The PBX's id of the recording; played through Odoo.")
    logged = fields.Boolean(help="The chatter line and the activity were written.")
    ai_summary = fields.Text("AI summary")
    ai_transcript = fields.Text("Transcript")
    ai_qa_score = fields.Integer("Quality score")
    ai_sentiment = fields.Char("Mood")

    _sql_constraints = [("linkedid_unique", "unique(connection_id, linkedid)", "One record per call.")]

    def _compute_display_number(self):
        for rec in self:
            rec.display_number = rec.number or rec.agent_extension or rec.linkedid

    # ------------------------------------------------------------------ partner matching

    @api.model
    def _match_partners(self, number, company):
        """Contacts whose phone (or mobile) ends with the same digits: any spelling of a number."""
        key = tools.match_key(number)
        if len(key) < 5:
            return self.env["res.partner"]
        Partner = self.env["res.partner"]
        cols = [f for f in ("phone", "mobile") if f in Partner._fields and Partner._fields[f].store]
        where = " OR ".join(
            f"right(regexp_replace(coalesce({c}, ''), '\\D', '', 'g'), %s) = %s" for c in cols)
        params = []
        for _c in cols:
            params += [len(key), key]
        self.env.cr.execute(
            f"SELECT id FROM res_partner WHERE active AND ({where}) "
            f"AND (company_id IS NULL OR company_id = %s) ORDER BY is_company, id LIMIT 20",
            params + [company.id])
        return Partner.browse([r[0] for r in self.env.cr.fetchall()])

    # ------------------------------------------------------------------ events

    @api.model
    def _get_or_create_for_event(self, connection, linkedid):
        call = self.sudo().search([("connection_id", "=", connection.id), ("linkedid", "=", linkedid)], limit=1)
        if call:
            return call
        try:
            with self.env.cr.savepoint():
                return self.sudo().create({"connection_id": connection.id, "linkedid": linkedid})
        except tools.IntegrityError as e:
            if getattr(e, "pgcode", None) != "23505":
                raise
            return self.sudo().search([("connection_id", "=", connection.id), ("linkedid", "=", linkedid)], limit=1)

    @api.model
    def _upsert(self, connection, data):
        linkedid = str(data.get("linkedid") or "").strip()
        if not linkedid:
            raise ValueError("Missing linkedid")
        call = self.sudo().search([("connection_id", "=", connection.id), ("linkedid", "=", linkedid)], limit=1)
        direction = data.get("direction") or "internal"
        party = data.get("callee") if direction == "outbound" else data.get("caller")
        party = party or {}
        vals = {
            "direction": direction,
            "number": party.get("number") or False,
            "did": data.get("did") or False,
            "queue": data.get("queue") or False,
        }
        if (data.get("caller") or {}).get("name"):
            vals["caller_name"] = data["caller"]["name"]
        if not call:
            vals.update(connection_id=connection.id, linkedid=linkedid,
                        started_at=self._time(data.get("started_at")) or fields.Datetime.now())
            if direction != "internal":
                partners = self._match_partners(vals["number"], connection.company_id)
                if len(partners) == 1:
                    vals["partner_id"] = partners.id
            try:
                with self.env.cr.savepoint():
                    call = self.sudo().create(vals)
            except tools.IntegrityError as e:
                if getattr(e, "pgcode", None) != "23505":
                    raise
                call = self.sudo().search([("connection_id", "=", connection.id), ("linkedid", "=", linkedid)], limit=1)
                call.write({k: v for k, v in vals.items() if v})
        else:
            write_vals = {k: v for k, v in vals.items() if v}
            if not call.started_at:
                write_vals["started_at"] = self._time(data.get("started_at")) or fields.Datetime.now()
            if not call.partner_id and direction != "internal" and vals["number"]:
                partners = self._match_partners(vals["number"], connection.company_id)
                if len(partners) == 1:
                    write_vals["partner_id"] = partners.id
            if write_vals:
                call.write(write_vals)
        from_ext = str(data.get("from_extension") or "").strip()
        if from_ext and call.direction == "outbound":
            if not call.agent_extension:
                call.agent_extension = from_ext
            from_user = call._user_of(from_ext)
            if from_user and from_user not in call.offered_user_ids:
                call.offered_user_ids = [(4, from_user.id)]
        return call

    @api.model
    def _time(self, value):
        if not value:
            return False
        try:
            from datetime import datetime, timezone
            return datetime.fromisoformat(str(value)).astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            return False

    def _user_of(self, extension):
        if not extension:
            return self.env["res.users"]
        user = self.env["aipbx.extension"].sudo().search(
            [("connection_id", "=", self.connection_id.id), ("extension", "=", str(extension))], limit=1).user_id
        if user and user.active and not user.share and (not self.company_id or self.company_id in user.company_ids):
            return user
        return self.env["res.users"]

    def _pop_up_payload(self, user=None):
        user = user or self.env.user
        partner = self.partner_id if self.partner_id and tools.user_can_read(self.partner_id, user) else self.env["res.partner"]
        return {
            "call_id": self.id,
            "number": self.number or "",
            "name": partner.display_name if partner else (self.caller_name or ""),
            "partner_id": partner.id or False,
            "queue": self.queue or "",
            "direction": self.direction,
        }

    def _pop_up(self, user):
        """Tell that user's browsers who is calling (only that user: their own bus channel)."""
        if not user or not user.active or user.share:
            return
        if not user.has_group("aipbx_connector.group_aipbx_user"):
            return
        if self.company_id and self.company_id not in user.company_ids:
            return
        payload = self._pop_up_payload(user=user)
        target = user.partner_id
        if hasattr(target, "_bus_send"):  # Odoo 18+
            target._bus_send("aipbx/call", payload)
        else:  # Odoo 17
            self.env["bus.bus"]._sendone(target, "aipbx/call", payload)

    def _on_event(self, event, data):
        self.ensure_one()
        if event == "call.offered":
            user = self._user_of(data.get("agent_extension"))
            if user and user not in self.offered_user_ids:
                self.offered_user_ids = [(4, user.id)]
                if self.direction == "inbound" and self.state != "done" and not self.ended_at:
                    self._pop_up(user)
        elif event == "call.answered":
            user = self._user_of(data.get("agent_extension"))
            vals = {}
            if self.state != "done":
                vals["state"] = "answered"
                if data.get("agent_extension"):
                    vals["agent_extension"] = data["agent_extension"]
                if user:
                    vals["user_id"] = user.id
            else:
                if not self.agent_extension and data.get("agent_extension"):
                    vals["agent_extension"] = data["agent_extension"]
                if user and not self.user_id:
                    vals["user_id"] = user.id
            if not self.answered_at:
                vals["answered_at"] = self._time(data.get("answered_at")) or fields.Datetime.now()
            if vals:
                self.write(vals)
        elif event == "call.completed":
            agent_ext = data.get("agent_extension") or self.agent_extension
            if not agent_ext and self.direction == "outbound":
                agent_ext = data.get("from_extension") or False
            self.write({
                "state": "done",
                "outcome": data.get("outcome") or "missed",
                "answered_at": self._time(data.get("answered_at")) or self.answered_at,
                "ended_at": self._time(data.get("ended_at")) or fields.Datetime.now(),
                "wait_seconds": int(data.get("wait_seconds") or 0),
                "talk_seconds": int(data.get("talk_seconds") or 0),
                "agent_extension": agent_ext,
                "user_id": (self._user_of(data.get("agent_extension")) or self.user_id).id or False,
            })
            self._log_once()

    def _log_targets(self):
        """The records a call is written on: the contact (the CRM add-on adds the opportunity)."""
        return [self.partner_id] if self.partner_id else []

    def _post(self, body):
        for rec in self._log_targets():
            rec.sudo().message_post(body=body, message_type="comment", subtype_xmlid="mail.mt_note")

    def _post_ai_summary(self, show_zero=False):
        if not self.ai_summary:
            return
        show_score = bool(self.ai_qa_score) or show_zero
        extra = Markup(" · {} {}/100").format(escape(_("quality")), self.ai_qa_score) if show_score else ""
        self._post(Markup("<p>🤖 {}{}</p>").format(escape(self.ai_summary), extra))

    def _log_once(self):
        """One chatter line per call on the contact, and a call-back activity for a missed one."""
        if self.logged or not self._log_targets():
            return
        self.logged = True
        minutes, seconds = divmod(self.talk_seconds or 0, 60)
        direction = dict(self._fields["direction"].selection).get(self.direction, "")
        outcome = dict(self._fields["outcome"].selection).get(self.outcome, "")
        body = Markup("<p>📞 {} · {}{}{}</p>").format(
            escape(direction), escape(outcome),
            Markup(" · {} {}").format(escape(_("by")), escape(self.user_id.name)) if self.user_id else "",
            Markup(" · {}:{:02d}").format(minutes, seconds) if self.talk_seconds else "",
        )
        if self.recording_id:
            body += Markup('<p><a href="/aipbx/recording/{}">{}</a></p>').format(self.id, escape(_("Listen to the recording")))
        self._post(body)
        if self.ai_summary:
            self._post_ai_summary()
        if self.partner_id and self.direction == "inbound" and self.outcome in ("missed", "abandoned"):
            user = self.offered_user_ids[:1] or self.connection_id.missed_call_user_id
            if user:
                self.partner_id.sudo().activity_schedule(
                    "mail.mail_activity_data_call", user_id=user.id,
                    summary=_("Call back %s", self.number or ""),
                    note=_("Missed call at %s", fields.Datetime.to_string(self.started_at)))

    def _log_recording(self):
        """The recording is ready after the call is over: one more line on the contact."""
        if self.logged:
            self._post(Markup('<p>🎧 <a href="/aipbx/recording/{}">{}</a></p>').format(self.id, escape(_("Listen to the recording"))))

    def _log_ai(self, data):
        """The PBX's call analysis: kept on the call, the summary also on the contact."""
        score = data.get("qa_score")
        has_score = isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)
        had_summary = bool(self.ai_summary)
        self.write({
            "ai_summary": data.get("summary") or False,
            "ai_transcript": data.get("transcript") or False,
            "ai_qa_score": int(score) if has_score else 0,
            "ai_sentiment": data.get("qa_sentiment") or False,
        })
        if data.get("summary") and self.logged and not had_summary:
            self._post_ai_summary(show_zero=has_score)

    def action_play_recording(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": f"/aipbx/recording/{self.id}", "target": "new"}
