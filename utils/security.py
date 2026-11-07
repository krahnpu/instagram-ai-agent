import base64
import hashlib
from typing import Optional
from cryptography.fernet import Fernet


class SecretProtector:
    """
    Encrypts and decrypts Instagram & Telegram session payloads at rest when
    SESSION_ENCRYPTION_KEY is provided in .env, while gracefully reading plain JSON
    from /tmp/ig_api/session.json when unencrypted.
    """

    def __init__(self, secret_key: str = "") -> None:
        self.fernet: Optional[Fernet] = None
        if secret_key.strip():
            digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
            fernet_key = base64.urlsafe_b64encode(digest)
            self.fernet = Fernet(fernet_key)

    def encrypt_if_enabled(self, plain_text: str) -> bytes:
        raw = plain_text.encode("utf-8")
        if not self.fernet:
            return raw
        return self.fernet.encrypt(raw)

    def decrypt_if_needed(self, payload: bytes) -> str:
        stripped = payload.strip()
        if stripped.startswith(b"{"):
            return stripped.decode("utf-8")
        if self.fernet:
            return self.fernet.decrypt(stripped).decode("utf-8")
        return stripped.decode("utf-8")
