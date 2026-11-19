import ipaddress
import json
import logging
import socket
from urllib.parse import urlparse
import uuid

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from . import tools

_logger = logging.getLogger(__name__)
TIMEOUT = 8
_PROTECTED_CONN_FIELDS = frozenset({"pbx_url", "connection_uuid", "instance_id", "token_enc", "secret_enc", "state"})
_BLOCKED_HOSTS = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata.google.internal", "metadata",
})


def _is_blocked_ip(ip):
    if getattr(ip, "ipv4_mapped", None) and _is_blocked_ip(ip.ipv4_mapped):
        return True
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved


def _validate_pbx_url(self, url):
    # self (a record) only so that _() finds the language: Odoo 19 looks for it in the caller.
    url = (url or "").strip().rstrip("/")
    if not url or any(c.isspace() or ord(c) < 32 for c in url):
        raise UserError(_("The PBX address is not valid."))
    if "?" in url or "#" in url or ";" in url:
        raise UserError(_("The PBX address must not contain a query string, parameters, or fragment."))
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        raise UserError(_("The PBX address is not valid."))
    if parsed.scheme.lower() != "https":
        raise UserError(_("The PBX address must start with https://."))
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise UserError(_("The PBX address must not contain credentials."))
    if parsed.query or parsed.params or parsed.fragment:
        raise UserError(_("The PBX address must not contain a query string or fragment."))
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UserError(_("The PBX address is missing a host name."))
    if port is not None and not (1 <= port <= 65535):
        raise UserError(_("The PBX address port is not valid."))
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise UserError(_("The PBX address cannot target a loopback or metadata host."))
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            socket.inet_aton(host)
            raise UserError(_("The PBX address host is not a valid address."))
        except OSError:
            pass
        try:
            for info in socket.getaddrinfo(host, None):
                resolved = info[4][0].split("%", 1)[0]
                if _is_blocked_ip(ipaddress.ip_address(resolved)):
                    raise UserError(_("The PBX address cannot resolve to a loopback or metadata IP."))
        except OSError:
            pass
    else:
        if _is_blocked_ip(ip):
            raise UserError(_("The PBX address cannot target a loopback, link-local, or reserved IP."))
    return url


class AipbxConnection(models.Model):
    _name = "aipbx.connection"
    _description = "AiPBX connection"
    _order = "name, id"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    pbx_url = fields.Char("PBX address", readonly=True, copy=False)
    connection_uuid = fields.Char("Connection id", readonly=True, index=True, copy=False)
    instance_id = fields.Char("PBX id", readonly=True, copy=False)
    token_enc = fields.Char(groups="base.group_system", copy=False)
    secret_enc = fields.Char(groups="base.group_system", copy=False)
    state = fields.Selection([("new", "Waiting for the pack"), ("ready", "Connected")], default="new", readonly=True, copy=False)
    webhook_url = fields.Char(compute="_compute_webhook_url")
    last_event_at = fields.Datetime("Last event", readonly=True)
    last_test_at = fields.Datetime("Last test event", readonly=True)
    missed_call_user_id = fields.Many2one(
        "res.users", "Missed calls go to",
        help="A missed inbound call from a known contact becomes a call-back activity for this user "
             "(or for the extension's user when the call rang one).")
    push_contacts = fields.Boolean(
        "Send contacts to the PBX address book",
        help="Every few minutes, contacts with a phone number are sent to the PBX address book, so "
             "desk phones and the PBX show the caller's name. Odoo stays the source; changes made "
             "on the PBX side are overwritten.")
    contacts_synced_at = fields.Datetime(readonly=True, copy=False)
    extension_ids = fields.One2many("aipbx.extension", "connection_id", "Extensions")
    outside_key = fields.Boolean(compute="_compute_outside_key")

    _sql_constraints = [("uuid_unique", "unique(connection_uuid)", "This connection pack is already in use.")]

    def _check_manager(self):
        if not self.env.su:
            if not self.env.user.has_group("aipbx_connector.group_aipbx_manager"):
                raise AccessError(_("Only AiPBX administrators can manage connections."))
            tools.check_record_access(self, "write")

    @api.model_create_multi
    def create(self, vals_list):
        privileged = self.env.su or self.env.context.get("aipbx_apply_pack") or self.env.user.has_group("base.group_system")
        cleaned = []
        for vals in vals_list:
            if not privileged:
                if any(vals.get(k) for k in ("pbx_url", "connection_uuid", "instance_id", "token_enc", "secret_enc")) or vals.get("state") not in (None, False, "new"):
                    raise AccessError(_("Connection endpoints and credentials can only be set via a connection pack."))
            if vals.get("pbx_url"):
                vals = dict(vals, pbx_url=_validate_pbx_url(self, vals["pbx_url"]))
            cleaned.append(vals)
        return super().create(cleaned)

    def write(self, vals):
        if _PROTECTED_CONN_FIELDS & vals.keys():
            if not self.env.su and not self.env.context.get("aipbx_apply_pack") and not self.env.user.has_group("base.group_system"):
                raise AccessError(_("Connection endpoints and credentials can only be changed via a connection pack."))
        if vals.get("pbx_url"):
            vals = dict(vals, pbx_url=_validate_pbx_url(self, vals["pbx_url"]))
        return super().write(vals)

    def _compute_webhook_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for rec in self:
            rec.webhook_url = f"{base}/aipbx/webhook/{rec.connection_uuid}" if rec.connection_uuid else False

    def _compute_outside_key(self):
        for rec in self:
            rec.outside_key = tools.has_outside_key()

    # ------------------------------------------------------------------ the pack

    def _apply_pack(self, text):
        """Store what the PBX handed over once. The key and the secret are encrypted."""
        self.ensure_one()
        self._check_manager()
        try:
            pack = json.loads(text or "")
        except ValueError:
            raise UserError(_("This is not a connection pack: paste the JSON the PBX showed."))
        need = ("aipbx_connection", "pbx_url", "connection_id", "api_token", "webhook_secret")
        if not isinstance(pack, dict) or any(not str(pack.get(k) or "").strip() for k in need):
            raise UserError(_("The connection pack is incomplete."))
        try:
            version = int(pack["aipbx_connection"])
        except (TypeError, ValueError):
            raise UserError(_("This is not a valid connection pack."))
        if version != 1:
            raise UserError(_("This connection pack is of a newer kind; update the AiPBX Connector app."))
        url = _validate_pbx_url(self, str(pack["pbx_url"]))
        self.with_context(aipbx_apply_pack=True).sudo().write({
            "pbx_url": url,
            "connection_uuid": str(pack["connection_id"]).strip(),
            "instance_id": str(pack.get("instance_id") or "").strip(),
            "token_enc": tools.encrypt(self.env, str(pack["api_token"]).strip()),
            "secret_enc": tools.encrypt(self.env, str(pack["webhook_secret"]).strip()),
            "state": "ready",
            "name": self.name or pack.get("name") or url,
        })

    def _token(self):
        return tools.decrypt(self.env, self.sudo().token_enc)

    def _secret(self):
        return tools.decrypt(self.env, self.sudo().secret_enc)

    # ------------------------------------------------------------------ talking to the PBX

    def _api(self, method, path, params=None, payload=None, stream=False):
        """One request to the PBX REST API with this connection's key (server side only:
        the browser never sees the key)."""
        self.ensure_one()
        token = self._token()
        if not self.pbx_url or not token:
            raise UserError(_("Paste the connection pack first."))
        pbx_url = _validate_pbx_url(self, self.pbx_url)
        try:
            resp = requests.request(
                method, f"{pbx_url}/api/v1/{path}", params=params, json=payload, stream=stream,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=(TIMEOUT, 30) if stream else TIMEOUT, allow_redirects=False,
            )
        except requests.RequestException as e:
            raise UserError(_("The PBX cannot be reached: %s", e.__class__.__name__))
        if stream:
            return resp
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or not body.get("success", False):
            msg = (body.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
            raise UserError(_("The PBX refused the request: %s", msg))
        return body

    def action_test(self):
        """Odoo -> PBX: list the extensions. The PBX -> Odoo way is the PBX's "Send a test event"."""
        self.ensure_one()
        self._check_manager()
        count = len(self._api("GET", "extensions").get("data") or [])
        return self._notify(_("The PBX answered: %s extensions.", count))

    def action_sync_extensions(self):
        """Bring the PBX extensions in; users are matched by e-mail where nobody chose one."""
        self._check_manager()
        for rec in self:
            rows = rec._api("GET", "extensions", params={"per_page": 500}).get("data") or []
            Ext = self.env["aipbx.extension"].sudo()
            for row in rows:
                number = str(row.get("extension") or "").strip()
                if not number:
                    continue
                ext = Ext.search([("connection_id", "=", rec.id), ("extension", "=", number)], limit=1)
                is_active = row.get("is_active", 1)
                if is_active in (0, False, "0", "false", "False", None, ""):
                    if ext:
                        ext.unlink()
                    continue
                email = str(row.get("email") or "").strip() or False
                vals = {"name": row.get("full_name") or number, "email": email}
                if not ext:
                    ext = Ext.create(dict(vals, connection_id=rec.id, extension=number))
                else:
                    ext.write(vals)
                if not ext.user_id and ext.email:
                    user = self.env["res.users"].sudo().search([
                        ("share", "=", False),
                        ("active", "=", True),
                        ("company_ids", "in", [rec.company_id.id]),
                        "|", ("login", "=ilike", ext.email), ("email", "=ilike", ext.email),
                    ], limit=1)
                    if user:
                        ext.user_id = user
        return self._notify(_("Extensions brought in from the PBX."))

    def _originate(self, user, number):
        """Click-to-call: the user's own extension rings first, then the number is dialled.
        The extension comes from the user's mapping on the server, never from the browser."""
        self.ensure_one()
        user = user or self.env.user
        if not self.env.su and user != self.env.user:
            raise AccessError(_("You can only originate calls for yourself."))
        if not user.active or user.share or not user.has_group("aipbx_connector.group_aipbx_user"):
            raise AccessError(_("Only AiPBX users can place calls."))
        conn = self.sudo()
        if not conn.active or conn.state != "ready":
            raise UserError(_("This PBX connection is not connected."))
        if conn.company_id not in (self.env.companies & user.company_ids):
            raise AccessError(_("You do not have access to this company's PBX connection."))
        ext = self.env["aipbx.extension"].sudo().search(
            [("connection_id", "=", conn.id), ("user_id", "=", user.id)], limit=1)
        if not ext:
            raise UserError(_("You have no extension on %s. Ask an administrator to assign one.", conn.name))
        to = "".join(c for c in (number or "") if c.isdigit() or c in "+*#")
        if len(tools.digits(to)) < 2:
            raise UserError(_("There is no number to call."))
        conn._api("POST", "calls/originate", payload={
            "from_extension": ext.extension, "to_number": to, "client_request_id": str(uuid.uuid4())})
        return ext

    # ------------------------------------------------------------------ contacts to the PBX

    @api.model
    def _cron_push_contacts(self):
        db_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid", "")
        Partner = self.env["res.partner"].sudo()
        phone_fields = [f for f in ("phone", "mobile") if f in Partner._fields]
        if not phone_fields:
            return
        for rec in self.search([("push_contacts", "=", True), ("state", "=", "ready")]):
            domain = ["|"] * (len(phone_fields) - 1) + [(f, "!=", False) for f in phone_fields]
            if rec.contacts_synced_at:
                domain = ["&", ("write_date", ">", rec.contacts_synced_at)] + domain
            domain = ["&", ("active", "=", True), "&", "|", ("company_id", "=", False), ("company_id", "=", rec.company_id.id)] + domain
            started = fields.Datetime.now()
            failures = 0
            partners = Partner.search(domain, limit=500, order="write_date asc, id asc")
            for partner in partners:
                numbers = [{"label": "mobile" if f == "mobile" else "work", "number": partner[f]}
                           for f in phone_fields if len(tools.digits(partner[f])) >= 3]
                if not numbers:
                    continue  # nothing the PBX could match
                try:
                    rec._api("POST", "contacts", payload={
                        "display_name": partner.name or numbers[0]["number"],
                        "company": partner.parent_id.name or "" if partner.parent_id else "",
                        "email": tools.clean_email(partner.email),
                        "external_id": f"odoo:{db_uuid}:{partner.id}",
                        "numbers": numbers,
                    })
                    failures = 0
                except UserError as e:
                    # One refused contact is skipped; several in a row is the connection: try later.
                    failures += 1
                    _logger.warning("AiPBX contact push: partner %s not sent: %s", partner.id, e)
                    if failures >= 5:
                        break
            else:
                rec.sudo().contacts_synced_at = partners[-1].write_date if len(partners) == 500 else started

    def _notify(self, message, kind="success"):
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"message": message, "type": kind, "sticky": False}}

    def action_open_pack_wizard(self):
        self.ensure_one()
        self._check_manager()
        return {"type": "ir.actions.act_window", "res_model": "aipbx.pack.wizard", "view_mode": "form",
                "target": "new", "context": {"default_connection_id": self.id}}

    @api.model
    def _for_user(self, user):
        """The connection a user calls through: the one with their extension in allowed companies."""
        allowed = self.env.companies & user.company_ids
        if not allowed:
            return self.browse()
        Ext = self.env["aipbx.extension"].sudo()
        base = [("user_id", "=", user.id), ("connection_id.state", "=", "ready"), ("connection_id.active", "=", True)]
        if self.env.company in allowed:
            ext = Ext.search(base + [("company_id", "=", self.env.company.id)], limit=1)
            if ext:
                return self.browse(ext.connection_id.id)
        ext = Ext.search(base + [("company_id", "in", allowed.ids)], limit=1)
        return self.browse(ext.connection_id.id)
