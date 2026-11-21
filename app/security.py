"""Password hashing, session tokens, two-step codes (TOTP) and at-rest encryption of Southwest sessions."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, want = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                            n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def temp_password() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def _fernet() -> Fernet:
    key = hashlib.sha256(("swa-state:" + settings.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_json(obj) -> bytes:
    return _fernet().encrypt(json.dumps(obj).encode())


def decrypt_json(blob):
    if not blob:
        return None
    try:
        return json.loads(_fernet().decrypt(bytes(blob)))
    except (InvalidToken, ValueError):
        return None


# ---------- two-step sign-in (RFC 6238 TOTP: SHA-1, 6 digits, 30 s, as every authenticator app uses) ----------
def totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def totp_code(secret: str, step: int) -> str:
    h = hmac.new(base64.b32decode(secret), struct.pack(">Q", step), "sha1").digest()
    o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_match(secret: str, code: str, now: float | None = None, after: int = -1):
    """The time step a 6-digit code is valid for (this one or a neighbour, for clock drift), else None.
    Steps up to `after` (the last one used) are refused, so a seen code can't be replayed."""
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return None
    step = int((time.time() if now is None else now) // 30)
    for s in (step - 1, step, step + 1):
        if s > after and hmac.compare_digest(totp_code(secret, s), code):
            return s
    return None


def recovery_codes(n: int = 8) -> list[str]:
    return [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(n)]


def recovery_hash(code: str) -> str:
    return hashlib.sha256(re.sub(r"[^0-9a-f]", "", (code or "").lower()).encode()).hexdigest()
