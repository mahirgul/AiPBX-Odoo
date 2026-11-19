import hashlib
import hmac
import json
import os
import time
import uuid
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models import tools

SECRET = "whsec_test"


def pack(conn_uuid):
    return json.dumps({"aipbx_connection": 1, "pbx_url": "https://pbx.example.com", "instance_id": "i-1",
                       "connection_id": conn_uuid, "name": "Test PBX", "api_token": "apbx_test",
                       "webhook_secret": SECRET})


class Common:
    def _setup(self):
        self.uuid = str(uuid.uuid4())
        self.conn = self.env["aipbx.connection"].create({"name": "PBX"})
        self.conn._apply_pack(pack(self.uuid))
        self.agent = self.env["res.users"].with_context(no_reset_password=True).create({"name": "Agent", "login": "agent_aipbx@example.com", "email": "agent_aipbx@example.com"})
        self.env["aipbx.extension"].create({"connection_id": self.conn.id, "extension": "19000", "user_id": self.agent.id})
        self.partner = self.env["res.partner"].create({"name": "Kemal Kaya", "phone": "+90 532 111 22 33"})

    def _event(self, name, data, event_id=None):
        rec = self.env["aipbx.event"].create({
            "connection_id": self.conn.id, "event_id": event_id or str(uuid.uuid4()), "event": name,
            "payload": json.dumps({"event": name, "data": data})})
        rec._process()
        return rec


@tagged("post_install", "-at_install")
class TestAipbxTools(TransactionCase):
    def test_numbers_match_in_any_spelling(self):
        for n in ("+90 532 111 22 33", "0532 111 22 33", "0090 5321112233", "5321112233"):
            self.assertEqual(tools.match_key(n), "5321112233", n)

    def test_signature(self):
        body = b'{"a":1}'
        ts = str(int(time.time()))
        sig = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        self.assertTrue(tools.check_signature(SECRET, ts, body, sig))
        self.assertFalse(tools.check_signature(SECRET, ts, body + b" ", sig), "another body")
        self.assertFalse(tools.check_signature("other", ts, body, sig))
        self.assertFalse(tools.check_signature(SECRET, str(int(time.time()) - 600), body, sig), "too old")

    def test_secrets_are_stored_encrypted(self):
        conn = self.env["aipbx.connection"].create({"name": "X"})
        conn._apply_pack(pack(str(uuid.uuid4())))
        self.assertNotIn("apbx_test", conn.sudo().token_enc)
        self.assertEqual(conn._token(), "apbx_test")
        self.assertEqual(conn._secret(), SECRET)

    def test_secret_key_env_fail_closed_and_invalid_token_log(self):
        with patch.dict(os.environ, {"AIPBX_SECRET_KEY": "env-key-123"}):
            self.assertTrue(tools.has_outside_key())
            ct = tools.encrypt(self.env, "tok")
            self.assertEqual(tools.decrypt(self.env, ct), "tok")
        with patch.dict(os.environ, {"AIPBX_SECRET_KEY": "other-key"}):
            with self.assertLogs(tools._logger, level="WARNING"):
                self.assertEqual(tools.decrypt(self.env, ct), "")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIPBX_SECRET_KEY", None)
            with patch.object(tools.config, "get", return_value=None):
                self.env["ir.config_parameter"].sudo().set_param("database.secret", "")
                with self.assertRaises(ValueError):
                    tools.encrypt(self.env, "tok")
                with self.assertRaises(ValueError):
                    tools.decrypt(self.env, ct)


@tagged("post_install", "-at_install")
class TestAipbxCalls(TransactionCase, Common):
    def setUp(self):
        super().setUp()
        self._setup()

    def test_an_inbound_call_from_a_contact(self):
        base = {"linkedid": "1700.1", "direction": "inbound", "did": "100", "queue": "sales",
                "caller": {"number": "05321112233", "name": ""}, "started_at": "2026-11-18T10:00:00+00:00"}
        sent = []
        with patch.object(type(self.env["aipbx.call"]), "_pop_up", lambda call, user: sent.append((call.id, user.id))):
            self._event("call.ringing", base)
            self._event("call.offered", dict(base, agent_extension="19000"))
            self._event("call.offered", dict(base, agent_extension="19000"))
        call = self.env["aipbx.call"].search([("linkedid", "=", "1700.1")])
        self.assertEqual(call.partner_id, self.partner, "any spelling of the number")
        self.assertEqual(len(sent), 1, "one pop-up per user and call")
        self.assertEqual(sent[0][1], self.agent.id)

        self._event("call.answered", dict(base, agent_extension="19000"))
        self._event("call.completed", dict(base, agent_extension="19000", outcome="answered", wait_seconds=8,
                                           talk_seconds=95, answered_at="2026-11-18T10:00:08+00:00", ended_at="2026-11-18T10:01:43+00:00"))
        self._event("call.completed", dict(base, agent_extension="19000", outcome="answered", talk_seconds=95))
        self.assertEqual(call.user_id, self.agent)
        self.assertEqual((call.outcome, call.talk_seconds, call.state), ("answered", 95, "done"))
        notes = self.partner.message_ids.filtered(lambda m: "📞" in (m.body or ""))
        self.assertEqual(len(notes), 1, "one chatter line per call, even when an event comes twice")
        self.assertEqual(self.env["aipbx.call"].search_count([("linkedid", "=", "1700.1")]), 1)

        self._event("call.ai_completed", {"linkedid": "1700.1", "summary": "Asked for a service visit.", "transcript": "...", "qa_score": 88})
        self.assertEqual((call.ai_summary, call.ai_qa_score), ("Asked for a service visit.", 88))
        self.assertTrue(self.partner.message_ids.filtered(lambda m: "🤖 Asked for a service visit." in (m.body or "")))

        self._event("recording.ready", {"linkedid": "1700.1", "recording_id": 55})
        self.assertEqual(call.recording_id, 55)
        self.assertTrue(self.partner.message_ids.filtered(lambda m: "/aipbx/recording/%d" % call.id in (m.body or "")))

    def test_a_missed_call_becomes_a_call_back_activity(self):
        base = {"linkedid": "1800.1", "direction": "inbound", "caller": {"number": "+905321112233"}}
        with patch.object(type(self.env["aipbx.call"]), "_pop_up", lambda *a: None):
            self._event("call.ringing", base)
            self._event("call.offered", dict(base, agent_extension="19000"))
        self._event("call.completed", dict(base, outcome="missed"))
        act = self.env["mail.activity"].search([("res_model", "=", "res.partner"), ("res_id", "=", self.partner.id)])
        self.assertEqual(len(act), 1)
        self.assertEqual(act.user_id, self.agent, "the user whose phone rang")

    def test_an_unknown_number_has_no_contact_and_writes_no_chatter(self):
        base = {"linkedid": "1900.1", "direction": "inbound", "caller": {"number": "02129998877"}}
        self._event("call.ringing", base)
        self._event("call.completed", dict(base, outcome="missed"))
        call = self.env["aipbx.call"].search([("linkedid", "=", "1900.1")])
        self.assertFalse(call.partner_id)
        self.assertEqual(call.outcome, "missed")

    def test_a_failed_event_is_kept_for_the_retry(self):
        rec = self._event("call.completed", {"linkedid": "2000.1", "direction": "inbound", "talk_seconds": "not a number"})
        self.assertEqual(rec.state, "failed")
        self.assertTrue(rec.error)

    def test_click_to_call_uses_the_users_own_extension(self):
        sent = {}

        def fake_api(conn, method, path, params=None, payload=None, stream=False):
            sent.update(method=method, path=path, payload=payload)
            return {"success": True}

        with patch.object(type(self.conn), "_api", fake_api):
            self.partner.with_user(self.agent).action_aipbx_call()
        self.assertEqual(sent["path"], "calls/originate")
        self.assertEqual(sent["payload"]["from_extension"], "19000")
        self.assertEqual(sent["payload"]["to_number"], "+905321112233")
        self.assertTrue(sent["payload"]["client_request_id"])

    def test_contacts_go_to_the_pbx_address_book_and_bad_ones_are_skipped(self):
        self.conn.push_contacts = True
        self.env["res.partner"].create({"name": "No digits", "phone": "n/a"})
        bad = self.env["res.partner"].create({"name": "Refused", "phone": "0212 000 00 00"})
        sent = []

        def fake_api(conn, method, path, params=None, payload=None, stream=False):
            if payload["display_name"] == "Refused":
                from odoo.exceptions import UserError
                raise UserError("refused")
            sent.append(payload)
            return {"success": True}

        with patch.object(type(self.conn), "_api", fake_api):
            self.env["aipbx.connection"]._cron_push_contacts()
        names = [p["display_name"] for p in sent]
        self.assertIn("Kemal Kaya", names)
        self.assertNotIn("No digits", names)
        self.assertTrue(self.conn.contacts_synced_at, "one refused contact does not stop the sync")
        kemal = [p for p in sent if p["display_name"] == "Kemal Kaya"][0]
        self.assertTrue(kemal["external_id"].endswith(":%d" % self.partner.id))
        self.assertTrue(bad)

    def test_users_see_only_their_own_calls(self):
        other = self.env["res.users"].with_context(no_reset_password=True).create({"name": "Other", "login": "other_aipbx@example.com"})
        call = self.env["aipbx.call"].create({"connection_id": self.conn.id, "linkedid": "x1", "user_id": self.agent.id})
        self.assertTrue(call.with_user(self.agent).read(["number"]))
        self.assertFalse(self.env["aipbx.call"].with_user(other).search([("id", "=", call.id)]))

    def test_security_authorization_ssrf_and_multi_company(self):
        grp_field = "group_ids" if "group_ids" in self.env["res.users"]._fields else "groups_id"
        base_user = self.env.ref("base.group_user")
        mgr_group = self.env.ref("aipbx_connector.group_aipbx_manager")
        portal_group = self.env.ref("base.group_portal")

        # User cannot read aipbx.connection or invoke manager RPC methods.
        with self.assertRaises(AccessError):
            self.conn.with_user(self.agent).read(["pbx_url"])
        with self.assertRaises(AccessError):
            self.conn.with_user(self.agent).action_test()
        with self.assertRaises(AccessError):
            self.conn.with_user(self.agent).action_sync_extensions()
        with self.assertRaises(AccessError):
            self.conn.with_user(self.agent)._apply_pack(pack(str(uuid.uuid4())))

        # Manager without base.group_system cannot overwrite pbx_url/connection_uuid directly.
        mgr_a = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Mgr A", "login": "mgr_a_aipbx@example.com",
            grp_field: [(6, 0, [base_user.id, mgr_group.id])],
        })
        with self.assertRaises(AccessError):
            self.conn.with_user(mgr_a).write({"pbx_url": "https://evil.example.com"})

        # SSRF / bad URL checks in _apply_pack.
        for bad_url in (
            "http://pbx.example.com",
            "https://user:pass@pbx.example.com",
            "https://pbx.example.com?x=1",
            "https://pbx.example.com#frag",
            "https://127.0.0.1",
            "https://[::1]",
            "https://169.254.169.254",
            "https://localhost",
        ):
            bad_pack = json.dumps({
                "aipbx_connection": 1, "pbx_url": bad_url, "connection_id": str(uuid.uuid4()),
                "api_token": "t", "webhook_secret": "s",
            })
            with self.assertRaises(UserError, msg=bad_url):
                self.conn._apply_pack(bad_pack)

        # Multi-company isolation on extensions, events, _for_user(), and _pop_up().
        comp_b = self.env["res.company"].create({"name": "Company B"})
        conn_b = self.env["aipbx.connection"].create({"name": "PBX B", "company_id": comp_b.id})
        conn_b._apply_pack(pack(str(uuid.uuid4())))
        ext_b = self.env["aipbx.extension"].create({"connection_id": conn_b.id, "extension": "20001"})
        ev_b = self.env["aipbx.event"].create({"connection_id": conn_b.id, "event_id": "ev-b-1", "event": "webhook.test", "payload": "{}"})
        self.assertFalse(self.env["aipbx.extension"].with_user(mgr_a).search([("id", "=", ext_b.id)]))
        self.assertFalse(self.env["aipbx.event"].with_user(mgr_a).search([("id", "=", ev_b.id)]))
        with self.assertRaises(AccessError):
            ext_b.with_user(mgr_a).read(["extension"])
        with self.assertRaises(AccessError):
            ev_b.with_user(mgr_a).read(["payload"])
        self.assertNotEqual(self.env["aipbx.connection"].with_user(self.agent)._for_user(self.agent), conn_b)

        # Removed from company A -> _for_user() must not return Company A's connection.
        self.agent.write({"company_id": comp_b.id, "company_ids": [(6, 0, [comp_b.id])]})
        self.assertFalse(self.env["aipbx.connection"].with_user(self.agent)._for_user(self.agent))

        # action_sync_extensions skips inactive extensions and portal/other-company users.
        self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Portal", "login": "portal_ext@example.com", "email": "portal_ext@example.com",
            grp_field: [(6, 0, [portal_group.id])],
        })
        self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "CompB User", "login": "compb_ext@example.com", "email": "compb_ext@example.com",
            "company_id": comp_b.id, "company_ids": [(6, 0, [comp_b.id])],
        })
        ext_rows = [
            {"extension": "19010", "full_name": "Inactive", "email": "mgr_a_aipbx@example.com", "is_active": 0},
            {"extension": "19011", "full_name": "Portal", "email": "portal_ext@example.com", "is_active": 1},
            {"extension": "19012", "full_name": "CompB", "email": "compb_ext@example.com", "is_active": 1},
        ]
        with patch.object(type(self.conn), "_api", lambda *a, **kw: {"data": ext_rows}):
            self.conn.with_user(mgr_a).action_sync_extensions()
        self.assertFalse(self.env["aipbx.extension"].search([("connection_id", "=", self.conn.id), ("extension", "=", "19010")]))
        self.assertFalse(self.env["aipbx.extension"].search([("connection_id", "=", self.conn.id), ("extension", "=", "19011")]).user_id)
        self.assertFalse(self.env["aipbx.extension"].search([("connection_id", "=", self.conn.id), ("extension", "=", "19012")]).user_id)

        # Portal user cannot call action_aipbx_call.
        portal_u = self.env["res.users"].search([("login", "=", "portal_ext@example.com")])
        with self.assertRaises(AccessError):
            self.partner.with_user(portal_u).action_aipbx_call()

        # Pack wizard clears self.pack right after applying.
        wiz = self.env["aipbx.pack.wizard"].create({"connection_id": self.conn.id, "pack": pack(str(uuid.uuid4()))})
        with patch.object(type(self.conn), "action_sync_extensions", lambda s: None):
            wiz.action_apply()
        self.assertFalse(wiz.pack)

        # action_aipbx_calls resets context.
        self.assertEqual(self.partner.action_aipbx_calls()["context"], {})

    def test_out_of_order_events_and_unanswered_outbound(self):
        # Unanswered outbound call uses from_extension so caller sees it.
        out_data = {
            "linkedid": "out-1", "direction": "outbound", "from_extension": "19000",
            "callee": {"number": "05321112233"},
        }
        self._event("call.ringing", out_data)
        self._event("call.completed", dict(out_data, outcome="missed"))
        out_call = self.env["aipbx.call"].search([("linkedid", "=", "out-1")])
        self.assertIn(self.agent, out_call.offered_user_ids)
        self.assertEqual(out_call.agent_extension, "19000")
        self.assertTrue(out_call.with_user(self.agent).read(["number"]))

        # Out-of-order: ai_completed + recording.ready BEFORE ringing/completed, and late offered/answered.
        self._event("call.ai_completed", {"linkedid": "ooo-1", "summary": "Early AI summary", "qa_score": True})
        self._event("recording.ready", {"linkedid": "ooo-1", "recording_id": 77})
        base = {"linkedid": "ooo-1", "direction": "inbound", "caller": {"number": "05321112233"}}
        self._event("call.completed", dict(base, outcome="answered", talk_seconds=42, agent_extension="19000"))
        ooo = self.env["aipbx.call"].search([("linkedid", "=", "ooo-1")])
        self.assertEqual(ooo.ai_qa_score, 0, "bool qa_score must not be cast to int")
        self.assertEqual(ooo.recording_id, 77)
        self.assertTrue(self.partner.message_ids.filtered(lambda m: "Early AI summary" in (m.body or "")))
        self.assertTrue(self.partner.message_ids.filtered(lambda m: "/aipbx/recording/%d" % ooo.id in (m.body or "")))

        # Late call.answered and call.offered must not revert state='done' or trigger popup.
        popped = []
        with patch.object(type(self.env["aipbx.call"]), "_pop_up", lambda c, u: popped.append(u.id)):
            self._event("call.offered", dict(base, agent_extension="19000"))
        self._event("call.answered", dict(base, agent_extension="19000"))
        self.assertEqual(ooo.state, "done")
        self.assertFalse(popped)

    def test_contact_sync_skips_archived_and_strips_bad_email(self):
        self.conn.push_contacts = True
        self.env["res.partner"].create({"name": "Archived", "phone": "+90 532 999 00 00", "active": False})
        self.env["res.partner"].create({"name": "Bad Email", "phone": "+90 532 888 00 00", "email": "not an email"})
        sent = []

        def fake_api(conn, method, path, params=None, payload=None, stream=False):
            sent.append(payload)
            return {"success": True}

        with patch.object(type(self.conn), "_api", fake_api):
            self.env["aipbx.connection"]._cron_push_contacts()
        by_name = {p["display_name"]: p for p in sent}
        self.assertNotIn("Archived", by_name)
        self.assertIn("Bad Email", by_name)
        self.assertEqual(by_name["Bad Email"]["email"], "")


@tagged("post_install", "-at_install")
class TestAipbxWebhook(HttpCase, Common):
    def setUp(self):
        super().setUp()
        self._setup()

    def _post(self, envelope, secret=SECRET, conn_uuid=None, ts=None, headers=None):
        body = json.dumps(envelope).encode()
        return self._post_raw(body, secret=secret, conn_uuid=conn_uuid, ts=ts, headers={
            "X-AiPBX-Event": envelope.get("event", ""),
            "X-AiPBX-Delivery": envelope.get("event_id", ""),
            **(headers or {}),
        })

    def _post_raw(self, body, secret=SECRET, conn_uuid=None, ts=None, headers=None):
        ts = ts or str(int(time.time()))
        sig = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        h = {"Content-Type": "application/json", "X-AiPBX-Timestamp": ts, "X-AiPBX-Signature-256": sig}
        h.update(headers or {})
        return self.url_open("/aipbx/webhook/%s" % (conn_uuid or self.uuid), data=body, headers=h)

    def test_signed_events_are_stored_once(self):
        env = {"event_id": "e-1", "event": "webhook.test", "data": {}}
        self.assertEqual(self._post(env).status_code, 200)
        self.assertEqual(self._post(env).status_code, 200, "a repeat is fine")
        self.assertEqual(self.env["aipbx.event"].search_count([("event_id", "=", "e-1")]), 1)
        self.assertTrue(self.conn.last_test_at)
        self.assertEqual(self._post(dict(env, data={"other": 1})).status_code, 409, "another body under the same id")

    def test_a_whole_call_over_http_writes_the_chatter(self):
        base = {"linkedid": "h1", "direction": "inbound", "caller": {"number": "05321112233"}, "agent_extension": "19000"}
        for i, (name, extra) in enumerate([("call.ringing", {}), ("call.offered", {}), ("call.answered", {}),
                                           ("call.completed", {"outcome": "answered", "talk_seconds": 61})]):
            r = self._post({"event_id": "h1-%d" % i, "event": name, "data": dict(base, **extra)})
            self.assertEqual(r.status_code, 200)
        self.assertFalse(self.env["aipbx.event"].search([("state", "=", "failed")]), "every event done")
        call = self.env["aipbx.call"].search([("linkedid", "=", "h1")])
        self.assertEqual((call.outcome, call.talk_seconds, call.user_id), ("answered", 61, self.agent))
        self.assertTrue(self.partner.message_ids.filtered(lambda m: "📞" in (m.body or "")))

    def test_unsigned_or_wrong_deliveries_are_refused(self):
        env = {"event_id": "e-2", "event": "webhook.test", "data": {}}
        self.assertEqual(self._post(env, secret="wrong").status_code, 401)
        self.assertEqual(self._post(env, conn_uuid=str(uuid.uuid4())).status_code, 401, "unknown connection: the same answer")
        self.assertEqual(self._post(env, ts=str(int(time.time()) - 900)).status_code, 401, "replayed later")
        self.assertEqual(self._post(env, headers={"X-AiPBX-Event": "call.completed"}).status_code, 400)
        self.assertEqual(self._post_raw(b"[]", headers={"X-AiPBX-Event": "webhook.test", "X-AiPBX-Delivery": "x"}).status_code, 400)
        self.assertEqual(self._post_raw(b"\xff\xfe{}", headers={"X-AiPBX-Event": "webhook.test", "X-AiPBX-Delivery": "x"}).status_code, 400)
        self.assertEqual(self._post_raw(json.dumps({"event_id": "e-3", "event": "", "data": {}}).encode(),
                                        headers={"X-AiPBX-Event": "", "X-AiPBX-Delivery": "e-3"}).status_code, 400)
        self.assertEqual(self._post_raw(json.dumps(env).encode(), headers={"X-AiPBX-Event": "webhook.test"}).status_code, 400)
        self.assertEqual(self._post_raw(b"x" * (256 * 1024 + 1)).status_code, 413)
        self.assertEqual(self.env["aipbx.event"].search_count([("event_id", "=", "e-2")]), 0)

    def test_the_recording_needs_a_user_who_may_see_the_call(self):
        call = self.env["aipbx.call"].create({"connection_id": self.conn.id, "linkedid": "r1", "user_id": self.agent.id, "recording_id": 9})
        self.env["res.users"].with_context(no_reset_password=True).create({"name": "Nosy", "login": "nosy_aipbx", "password": "nosy_aipbx_pw1"})
        self.authenticate("nosy_aipbx", "nosy_aipbx_pw1")
        self.assertEqual(self.url_open("/aipbx/recording/%d" % call.id).status_code, 404)

    def test_recording_proxy_headers_and_close(self):
        call = self.env["aipbx.call"].create({"connection_id": self.conn.id, "linkedid": "r2", "user_id": self.agent.id, "recording_id": 10})
        self.agent.write({"password": "agent_aipbx_pw1"})
        self.authenticate("agent_aipbx@example.com", "agent_aipbx_pw1")

        closed = []

        class FakeResp:
            def __init__(self, status_code, ct):
                self.status_code = status_code
                self.headers = {"Content-Type": ct, "Content-Length": "4"}

            def iter_content(self, _chunk):
                yield b"MP3!"

            def close(self):
                closed.append(True)

        with patch.object(type(self.conn), "_api", lambda *a, **kw: FakeResp(200, "text/html")):
            resp = self.url_open("/aipbx/recording/%d" % call.id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "audio/mpeg")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("Content-Security-Policy"), "sandbox")
        self.assertEqual(resp.headers.get("Content-Disposition"), "inline")
        self.assertEqual(resp.headers.get("Content-Length"), "4")
        self.assertTrue(closed)

        closed.clear()
        with patch.object(type(self.conn), "_api", lambda *a, **kw: FakeResp(502, "text/plain")):
            resp_err = self.url_open("/aipbx/recording/%d" % call.id)
        self.assertEqual(resp_err.status_code, 404)
        self.assertTrue(closed)
