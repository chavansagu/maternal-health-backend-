"""
Push notifications (spec M) via Firebase Cloud Messaging (works for web, Android and iOS tokens).

Setup:
  1. pip install firebase-admin
  2. Download the service-account JSON from Firebase console and set in .env:
         FIREBASE_CREDENTIALS_PATH=/path/to/service-account.json
  3. Frontend/app registers its token with POST /api/v2/notifications/devices/register

If firebase-admin or the credentials are missing, push is silently skipped
(in-app notifications keep working). Sending runs in a daemon thread so the
API request that raised the alert is never slowed down or broken by it.
"""
import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

PUSH_ENABLED = os.getenv("PUSH_ENABLED", "true").lower() == "true"
_CRED_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH", "")

_app = None
_init_done = False
_lock = threading.Lock()


def _get_app():
    """Lazily initialise firebase_admin; returns None when push is not configured."""
    global _app, _init_done
    if _init_done:
        return _app
    with _lock:
        if _init_done:
            return _app
        _init_done = True
        if not (PUSH_ENABLED and _CRED_PATH and os.path.exists(_CRED_PATH)):
            logger.info("[Push] disabled (no FIREBASE_CREDENTIALS_PATH or PUSH_ENABLED=false)")
            return None
        try:
            import firebase_admin
            from firebase_admin import credentials
            _app = firebase_admin.initialize_app(credentials.Certificate(_CRED_PATH))
            logger.info("[Push] Firebase initialised")
        except Exception as e:
            logger.warning(f"[Push] Firebase init failed: {e}")
            _app = None
    return _app


def is_configured() -> bool:
    return _get_app() is not None


def dispatch_push(items: List[Dict[str, Any]]) -> None:
    """items: [{user_id, title, body, data{}}]. Fire-and-forget."""
    if not items or not PUSH_ENABLED or not _CRED_PATH:
        return
    threading.Thread(target=_send, args=(items,), daemon=True).start()


def _send(items: List[Dict[str, Any]]) -> None:
    try:
        app = _get_app()
        if app is None:
            return
        from firebase_admin import messaging
        from database import SessionLocal
        from models import DeviceToken

        db = SessionLocal()
        try:
            for it in items:
                tokens = db.query(DeviceToken).filter(
                    DeviceToken.user_id == it["user_id"], DeviceToken.is_active == True
                ).all()
                if not tokens:
                    continue
                data = {k: str(v) for k, v in (it.get("data") or {}).items()}
                msgs = [
                    messaging.Message(
                        token=t.token,
                        notification=messaging.Notification(title=it["title"], body=it["body"]),
                        data=data,
                    )
                    for t in tokens
                ]
                resp = messaging.send_each(msgs, app=app)
                for t, r in zip(tokens, resp.responses):
                    if r.success:
                        continue
                    if isinstance(r.exception, (messaging.UnregisteredError, messaging.SenderIdMismatchError)):
                        t.is_active = False  # token is dead -> stop using it
                    else:
                        logger.warning(f"[Push] send failed for user {it['user_id']}: {r.exception}")
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[Push] dispatch error: {e}")


def register_token(db, user_id: int, token: str, platform: str = "web") -> None:
    """Upsert a device token for the user (a token belongs to one user at a time)."""
    from models import DeviceToken
    now = datetime.now()
    row = db.query(DeviceToken).filter(DeviceToken.token == token).first()
    if row:
        row.user_id, row.platform, row.is_active, row.last_seen_at = user_id, platform, True, now
    else:
        db.add(DeviceToken(user_id=user_id, token=token, platform=platform,
                           is_active=True, last_seen_at=now))
    db.commit()


def unregister_token(db, user_id: int, token: str) -> int:
    from models import DeviceToken
    n = db.query(DeviceToken).filter(
        DeviceToken.token == token, DeviceToken.user_id == user_id
    ).update({"is_active": False})
    db.commit()
    return n
