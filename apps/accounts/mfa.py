import base64
import hashlib
import os

import pyotp
from cryptography.fernet import Fernet
from django.conf import settings


def _fernet():
    # Use a dedicated MFA_ENCRYPTION_KEY if set; never share the Django SECRET_KEY.
    raw = os.getenv("MFA_ENCRYPTION_KEY") or getattr(settings, "MFA_ENCRYPTION_KEY", None)
    if not raw:
        raise RuntimeError(
            "MFA_ENCRYPTION_KEY environment variable must be set. "
            "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    # Accept either a raw Fernet key (44 chars, base64url) or an arbitrary string
    # (we hash it to get a consistent 32-byte key).
    try:
        return Fernet(raw.encode() if isinstance(raw, str) else raw)
    except Exception:
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_mfa_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_mfa_secret(token: str) -> str:
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(email: str, secret: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=email, issuer_name=settings.AUTH_MFA_ISSUER
    )


def verify_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code, valid_window=1)
