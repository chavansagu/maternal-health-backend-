"""
Small helper to send a templated (DLT) SMS from places that have no request-scoped db
(background tasks, APScheduler jobs). Never raises: SMS is best-effort.
"""
import logging
from typing import Optional

from sms_service import sms_service
from sms_templates import get_sms_template

logger = logging.getLogger(__name__)


async def send_template_sms(template_name: str, mobile_number: Optional[str],
                            pregnant_woman_id: Optional[int] = None, **variables):
    if not mobile_number:
        return None
    try:
        tpl = get_sms_template(template_name, **variables)
        if not tpl["template_id"]:
            logger.warning(f"[SMS] '{template_name}' skipped: DLT template id not configured")
            return None
        from database import SessionLocal
        db = SessionLocal()
        try:
            return await sms_service.send_sms(
                mobile_number=mobile_number,
                message=tpl["message"],
                message_type=template_name,
                pregnant_woman_id=pregnant_woman_id,
                db=db,
                template_id=tpl["template_id"],
            )
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[SMS] '{template_name}' failed: {e}")
        return None


def send_template_sms_sync(template_name: str, mobile_number: Optional[str],
                           pregnant_woman_id: Optional[int] = None, **variables):
    """For sync callers (scheduler thread)."""
    import asyncio
    try:
        return asyncio.run(send_template_sms(template_name, mobile_number, pregnant_woman_id, **variables))
    except Exception as e:
        logger.warning(f"[SMS] sync send '{template_name}' failed: {e}")
        return None
