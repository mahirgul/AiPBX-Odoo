"""Small helpers shared by the models: number matching, the secret box, webhook signatures."""
import base64
from email.utils import getaddresses
import hashlib
import hmac
import logging
import os
import re
import time

from odoo import SUPERUSER_ID
from odoo.exceptions import AccessError
from odoo.tools import config

try:
    from psycopg2 import IntegrityError
except ImportError:  # pragma: no cover
    class IntegrityError(Exception):
        pgcode = None

_logger = logging.getLogger(__name__)
MATCH_DIGITS = 10
SIGNATURE_TOLERANCE = 300  # seconds either way
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")


def digits(number):
    """Digits only; an international 00 is read as +."""
    d = re.sub(r"\D", "", number or "")
    return d[2:] if d.startswith("00") else d


def match_key(number, length=MATCH_DIGITS):
    """The key two spellings of one number share: +90 532..., 0532... and 0090 532... alike.
    The same rule as the PBX address book (last 10 digits; shorter numbers whole)."""
    d = digits(number)
    return d[-length:] if length and len(d) > length else d


def clean_email(value):
    """Return a single valid email address, or '' when empty/invalid (PBX returns 422 on bad email)."""
    raw = (value or "").strip()
    if not raw:
        return ""
    addrs = getaddresses([raw])
    if len(addrs) != 1:
        return ""
    addr = addrs[0][1].strip()
    if not addr or len(addr) > 191 or ".." in addr or not _EMAIL_RE.fullmatch(addr):
        return ""
    return addr


def check_record_access(record, mode="read"):
    """Verify CRUD + record-rule access across Odoo 17 and Odoo 18+."""
    if hasattr(record, "check_access"):  # Odoo 18+
        record.check_access(mode)
    else:
        record.check_access_rights(mode)
        record.check_access_rule(mode)


def user_can_read(record, user):
    """Return True if `user` has read access (ACL + ir.rule) to `record`."""
    if not record or not user:
        return False
    r = record.with_user(user).sudo(user.id == SUPERUSER_ID).with_context(allowed_company_ids=user.company_ids.ids)
    try:
        check_record_access(r, "read")
        return True
    except AccessError:
        return False


def _key_material(env):
    # Outside keys (env var or odoo.conf) are preferred; without one the database secret is used.
    material = (
        os.environ.get("AIPBX_SECRET_KEY")
        or config.get("aipbx_secret_key")
        or env["ir.config_parameter"].sudo().get_param("database.secret")
        or ""
    )
    if not material:
        raise ValueError("AiPBX encryption key material is not configured.")
    return material


def _fernet(env):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    material = _key_material(env)
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"aipbx-fernet-v1",
        info=b"aipbx-secret-box",
    ).derive(material.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def _legacy_fernet(env):
    from cryptography.fernet import Fernet

    material = _key_material(env)
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("aipbx:" + material).encode()).digest()))


def has_outside_key():
    return bool(os.environ.get("AIPBX_SECRET_KEY") or config.get("aipbx_secret_key"))


def encrypt(env, value):
    return _fernet(env).encrypt((value or "").encode()).decode() if value else False


def decrypt(env, value):
    from cryptography.fernet import InvalidToken

    if not value:
        return ""
    token = value.encode()
    try:
        return _fernet(env).decrypt(token).decode()
    except InvalidToken:
        try:
            return _legacy_fernet(env).decrypt(token).decode()
        except InvalidToken:  # the key changed: the pack must be pasted again
            _logger.warning("AiPBX: stored secret could not be decrypted; paste the connection pack again.")
            return ""


def check_signature(secret, timestamp, body, signature, now=None):
    """X-AiPBX-Signature-256 = "sha256=" + HMAC-SHA256(secret, timestamp + "." + raw body),
    and the timestamp within five minutes. The raw bytes are signed: never re-serialised JSON."""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now or time.time()) - ts) > SIGNATURE_TOLERANCE:
        return False
    expected = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    given = signature[7:] if signature.startswith("sha256=") else signature
    return hmac.compare_digest(expected, given)
