import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


class TokenError(ValueError):
    pass


@dataclass(frozen=True)
class UploadClaims:
    task_id: str
    node_id: str
    expires_at: int
    expected_size: int | None = None
    expected_sha256: str | None = None


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_upload_token(
    task_id: str,
    node_id: str,
    secret: str,
    ttl_seconds: int,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> str:
    payload = {
        "task_id": task_id,
        "node_id": node_id,
        "exp": int(time.time()) + ttl_seconds,
        "expected_size": expected_size,
        "expected_sha256": expected_sha256,
    }
    encoded = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _encode(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_upload_token(token: str, secret: str) -> UploadClaims:
    try:
        encoded, signature = token.split(".", 1)
        expected = _encode(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise TokenError("invalid signature")
        payload = json.loads(_decode(encoded))
        claims = UploadClaims(
            task_id=str(payload["task_id"]),
            node_id=str(payload["node_id"]),
            expires_at=int(payload["exp"]),
            expected_size=int(payload["expected_size"]) if payload.get("expected_size") is not None else None,
            expected_sha256=str(payload["expected_sha256"]) if payload.get("expected_sha256") else None,
        )
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        if isinstance(exc, TokenError):
            raise
        raise TokenError("malformed token") from exc
    if claims.expires_at < int(time.time()):
        raise TokenError("expired token")
    return claims
