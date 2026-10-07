import os
import logging
import requests
from typing import Optional, Dict, Any
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy.orm import Session

from models import SMSLog

load_dotenv()

logger = logging.getLogger(__name__)

class SMSService:
    def __init__(self):
        self.provider = os.getenv("SMS_PROVIDER", "generic")
        self.api_key = os.getenv("SMS_API_KEY")
        self.api_url = os.getenv("SMS_API_URL")
        self.sender_id = os.getenv("SMS_SENDER_ID", "JANANI")
        self.auth_token = os.getenv("SMS_AUTH_TOKEN")
        self.template_id = os.getenv("SMS_TEMPLATE_ID")
        self.entity_id = os.getenv("SMS_ENTITY_ID")
        self.enabled = os.getenv("SMS_ENABLED", "true").lower() == "true"

    async def send_sms(self, mobile_number: str, message: str, message_type: str = "general",
                      pregnant_woman_id: Optional[int] = None,
                      usg_appointment_id: Optional[int] = None,
                      db: Optional[Session] = None,
                      template_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Send SMS using configured provider.
        template_id is optional — existing callers without it continue to work.
        """
        if not self.enabled:
            return {"success": False, "message": "SMS service is disabled"}

        if not self.api_key or not self.api_url:
            return {"success": False, "message": "SMS configuration incomplete"}

        try:
            if self.provider == "whiteray":
                if not template_id:
                    logger.warning(f"send_sms called for type '{message_type}' without template_id — SMS may be rejected by DLT")
                result = await self._send_whiteray(mobile_number, message, template_id or "")
            elif self.provider == "twilio":
                result = await self._send_twilio(mobile_number, message)
            elif self.provider == "msg91":
                result = await self._send_msg91(mobile_number, message)
            elif self.provider == "textlocal":
                result = await self._send_textlocal(mobile_number, message)
            elif self.provider == "fast2sms":
                result = await self._send_fast2sms(mobile_number, message)
            else:
                result = await self._send_generic(mobile_number, message)

            if db:
                await self._log_sms(db, mobile_number, message, message_type,
                                    pregnant_woman_id, usg_appointment_id, result)

            return result

        except Exception as e:
            error_result = {"success": False, "message": f"SMS sending failed: {str(e)}"}
            logger.error(f"SMS send error for {mobile_number} [{message_type}]: {str(e)}")
            if db:
                await self._log_sms(db, mobile_number, message, message_type,
                                    pregnant_woman_id, usg_appointment_id, error_result)
            return error_result

    async def _send_whiteray(self, mobile_number: str, message: str, template_id: str) -> Dict[str, Any]:
        """Send SMS via Dovesoft/Whiteray CPaaS — GET request, UTF-8 encoding via requests params"""
        try:
            # Safety checks
            if not mobile_number or not mobile_number.strip():
                return {"success": False, "provider": "whiteray", "message": "Mobile number is empty"}
            if not template_id:
                logger.warning(f"template_id missing for mobile {mobile_number} — DLT may reject")

            # params dict — requests handles UTF-8 percent encoding automatically
            # DO NOT manually quote/encode the message — causes double encoding
            params = {
                "mobiles": mobile_number.strip(),
                "sms": message,
                "senderid": self.sender_id,
                "entityid": self.entity_id,
                "tempid": template_id,
                "Key": self.api_key,
                "unicode": "1",
            }

            # Log raw message for DLT template match verification
            print(f"[SMS DEBUG] Raw message: {message}")

            # GET request — confirmed from platform DevTools (Request Method: GET)
            response = requests.get(self.api_url, params=params, timeout=15)

            # Log actual URL built by requests (shows encoding)
            print(f"[SMS DEBUG] Request URL: {response.request.url}")
            print(f"[SMS DEBUG] Response [{response.status_code}]: {response.text}")

            try:
                response_json = response.json()
            except Exception:
                response_json = {}

            success = False
            message_id = None
            smslist = response_json.get("smslist", {})
            sms = smslist.get("sms", {})
            if isinstance(sms, list):
                sms = sms[0] if sms else {}
            if sms.get("status") == "success" or sms.get("code") == "000":
                success = True
                message_id = sms.get("messageid")

            if not success:
                logger.error(f"Whiteray SMS failed for {mobile_number}: {response.text}")
            else:
                logger.info(f"Whiteray SMS sent to {mobile_number}, messageid={message_id}")
                print(f"[SMS DEBUG] SUCCESS — messageid={message_id}")

            return {
                "success": success,
                "provider": "whiteray",
                "message_id": message_id,
                "status_code": response.status_code,
                "response": response.text,
            }
        except requests.exceptions.Timeout:
            logger.error(f"Whiteray SMS timeout for {mobile_number}")
            print(f"[SMS DEBUG] TIMEOUT for {mobile_number}")
            return {"success": False, "provider": "whiteray", "message": "Request timed out"}
        except Exception as e:
            logger.error(f"Whiteray _send_whiteray exception for {mobile_number}: {str(e)}")
            print(f"[SMS DEBUG] EXCEPTION: {str(e)}")
            return {"success": False, "provider": "whiteray", "message": str(e)}

    async def _send_twilio(self, mobile_number: str, message: str) -> Dict[str, Any]:
        """Send SMS via Twilio"""
        from twilio.rest import Client
        client = Client(self.api_key, self.auth_token)
        response = client.messages.create(
            body=message,
            from_=self.sender_id,
            to=mobile_number
        )
        return {
            "success": True,
            "provider": "twilio",
            "message_id": response.sid,
            "status": response.status
        }

    async def _send_msg91(self, mobile_number: str, message: str) -> Dict[str, Any]:
        """Send SMS via MSG91"""
        payload = {
            "authkey": self.api_key,
            "mobiles": mobile_number,
            "message": message,
            "sender": self.sender_id,
            "route": "4"
        }
        response = requests.post(self.api_url, data=payload)
        return {
            "success": response.status_code == 200,
            "provider": "msg91",
            "response": response.text,
            "status_code": response.status_code
        }

    async def _send_textlocal(self, mobile_number: str, message: str) -> Dict[str, Any]:
        """Send SMS via TextLocal"""
        payload = {
            "apikey": self.api_key,
            "numbers": mobile_number,
            "message": message,
            "sender": self.sender_id
        }
        response = requests.post(self.api_url, data=payload)
        return {
            "success": response.status_code == 200,
            "provider": "textlocal",
            "response": response.text,
            "status_code": response.status_code
        }

    async def _send_fast2sms(self, mobile_number: str, message: str) -> Dict[str, Any]:
        """Send SMS via Fast2SMS"""
        headers = {
            "authorization": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded"
        }
        payload = {
            "sender_id": self.sender_id,
            "message": message,
            "numbers": mobile_number,
            "route": "p"
        }
        response = requests.post(self.api_url, headers=headers, data=payload)
        return {
            "success": response.status_code == 200,
            "provider": "fast2sms",
            "response": response.text,
            "status_code": response.status_code
        }

    async def _send_generic(self, mobile_number: str, message: str) -> Dict[str, Any]:
        """Send SMS via Generic HTTP API"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "to": mobile_number,
            "message": message,
            "sender": self.sender_id
        }
        response = requests.post(self.api_url, headers=headers, json=payload)
        return {
            "success": response.status_code == 200,
            "provider": "generic",
            "response": response.text,
            "status_code": response.status_code
        }

    async def _log_sms(self, db: Session, mobile_number: str, message: str,
                      message_type: str, pregnant_woman_id: Optional[int],
                      usg_appointment_id: Optional[int], result: Dict[str, Any]):
        """Log SMS to database"""
        sms_log = SMSLog(
            mobile_number=mobile_number,
            message=message,
            message_type=message_type,
            pregnant_woman_id=pregnant_woman_id,
            usg_appointment_id=usg_appointment_id,
            sent_at=datetime.now(),
            delivery_status="sent" if result["success"] else "failed",
            provider_response=str(result)
        )
        db.add(sms_log)
        db.commit()


# Global SMS service instance
sms_service = SMSService()
