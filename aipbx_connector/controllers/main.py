import json
import logging
import re

from werkzeug.exceptions import NotFound

from odoo import SUPERUSER_ID, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models import tools

_logger = logging.getLogger(__name__)
MAX_BODY = 256 * 1024
_AUDIO_CT_RE = re.compile(r"^audio/[a-z0-9.+-]+$")


class _ClosingStream:
    def __init__(self, resp):
        self._resp = resp

    def __iter__(self):
        try:
            yield from self._resp.iter_content(64 * 1024)
        finally:
            self._resp.close()

    def close(self):
        self._resp.close()


class AipbxController(http.Controller):

    @http.route("/aipbx/webhook/<string:connection_uuid>", type="http", auth="none", methods=["POST"], csrf=False, save_session=False, readonly=False)
    def webhook(self, connection_uuid, **_kw):
        """The PBX's signed deliveries. 2xx only once the event is stored; a repeat is 2xx too."""
        if not request.db:
            return request.make_response("not found", status=404)
        cl = request.httprequest.content_length
        if cl is not None and cl > MAX_BODY:
            return request.make_response("too large", status=413)
        # Odoo's request object has no stream; the length was checked above (and is again).
        body = request.httprequest.get_data()
        if len(body) > MAX_BODY:
            return request.make_response("too large", status=413)
        # No session here: the work is done as the system user (chatter, activities need a user).
        env = request.env(user=SUPERUSER_ID, su=True)
        conn = env["aipbx.connection"].search([("connection_uuid", "=", connection_uuid), ("state", "=", "ready")], limit=1)
        headers = request.httprequest.headers
        if not conn or not tools.check_signature(conn._secret(), headers.get("X-AiPBX-Timestamp"), body,
                                                 headers.get("X-AiPBX-Signature-256", "")):
            # The same answer for an unknown connection and a bad signature.
            return request.make_response("unauthorized", status=401)
        try:
            payload_text = body.decode("utf-8")
            envelope = json.loads(payload_text)
        except (UnicodeDecodeError, ValueError):
            return request.make_response("bad json", status=400)
        if not isinstance(envelope, dict):
            return request.make_response("bad json", status=400)
        event_id = envelope.get("event_id")
        event = envelope.get("event")
        if not isinstance(event_id, str) or not event_id or not isinstance(event, str) or not event:
            return request.make_response("mismatch", status=400)
        delivery_hdr = headers.get("X-AiPBX-Delivery")
        event_hdr = headers.get("X-AiPBX-Event")
        # The signed body and the headers must agree.
        if not delivery_hdr or not event_hdr or event_id != delivery_hdr or event != event_hdr:
            return request.make_response("mismatch", status=400)
        Event = env["aipbx.event"]
        old = Event.search([("connection_id", "=", conn.id), ("event_id", "=", event_id)], limit=1)
        if old:
            # A repeat after a lost answer: fine. Another body under the same id: refused.
            return request.make_response("ok" if old.payload == payload_text else "conflict", status=200 if old.payload == payload_text else 409)
        try:
            with env.cr.savepoint():
                rec = Event.create({"connection_id": conn.id, "event_id": event_id, "event": event, "payload": payload_text})
        except tools.IntegrityError as e:
            if getattr(e, "pgcode", None) != "23505":
                raise
            old = Event.search([("connection_id", "=", conn.id), ("event_id", "=", event_id)], limit=1)
            if old and old.payload != payload_text:
                return request.make_response("conflict", status=409)
            return request.make_response("ok", status=200)
        rec._process()
        return request.make_response("ok", status=200)

    @http.route("/aipbx/recording/<int:call_id>", type="http", auth="user")
    def recording(self, call_id, **_kw):
        """The recording of a call the user may see, fetched by the server with the
        connection's key: the browser never gets the key or the PBX address of the file."""
        call = request.env["aipbx.call"].browse(call_id).exists()
        try:
            tools.check_record_access(call, "read")
        except AccessError:
            raise NotFound()
        if not call or not call.recording_id:
            raise NotFound()
        try:
            resp = call.connection_id.sudo()._api("GET", f"recordings/{call.recording_id}", stream=True)
        except UserError:
            raise NotFound()
        if resp.status_code != 200:
            resp.close()
            raise NotFound()
        raw_ct = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        content_type = raw_ct if _AUDIO_CT_RE.fullmatch(raw_ct) else "audio/mpeg"
        headers = [
            ("Content-Type", content_type),
            ("Cache-Control", "private, no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", "sandbox"),
            ("Content-Disposition", "inline"),
        ]
        cl = (resp.headers.get("Content-Length") or "").strip()
        if cl.isdigit():
            headers.append(("Content-Length", cl))

        return request.make_response(_ClosingStream(resp), headers=headers)
