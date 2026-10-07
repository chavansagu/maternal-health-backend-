"""
IVR Service - Dovesoft OBD Integration
Handles automated outbound calls for:
  - High Risk Advisory calls (Broadcast API)
  - Feedback Call 1 (24-28 hours after USG completion)
  - Feedback Call 2 (48 hours after Call 1 if needed)
"""
import os
import json
import logging
import requests
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Dovesoft config from .env
IVR_ENABLED = os.getenv("IVR_ENABLED", "false").lower() == "true"
IVR_API_BASE_URL = os.getenv("IVR_API_BASE_URL", "https://obd.dovesoft.ltd/vb-api/v2")
IVR_USER_ID = os.getenv("IVR_USER_ID", "cev6jit6g")
IVR_USER_TOKEN = os.getenv("IVR_USER_TOKEN", "")
IVR_HIGH_RISK_ANNOUNCEMENT_ID = os.getenv("IVR_HIGH_RISK_ANNOUNCEMENT_ID", "4825")
# Temporarily use same announcement ID for testing
IVR_FEEDBACK_CALL_1_ANNOUNCEMENT_ID = os.getenv("IVR_FEEDBACK_CALL_1_ANNOUNCEMENT_ID", "4825")  # Same as high-risk for now
IVR_FEEDBACK_CALL_2_ANNOUNCEMENT_ID = os.getenv("IVR_FEEDBACK_CALL_2_ANNOUNCEMENT_ID", "4825")  # Same as high-risk for now
IVR_PLAN_ID = os.getenv("IVR_PLAN_ID", "12")
IVR_PLAN_TYPE = os.getenv("IVR_PLAN_TYPE", "C")
IVR_WEBHOOK_URL = os.getenv("IVR_WEBHOOK_URL", "")


def _trigger_ivr_call(mobile_number: str, announcement_id: str, call_type: str, pregnant_woman_id: int, usg_appointment_id: Optional[int] = None) -> dict:
    """
    Generic function to trigger IVR calls via Dovesoft Broadcast API.
    
    Args:
        mobile_number: PW's mobile number (10 digits, no country code)
        announcement_id: Dovesoft announcement ID for the call type
        call_type: Type of call (high_risk_advisory, feedback_call_1, feedback_call_2)
        pregnant_woman_id: for logging purposes
        usg_appointment_id: for feedback calls
    
    Returns:
        dict with keys: success (bool), campaign_id (str|None), error (str|None)
    """
    if not IVR_ENABLED:
        logger.info(f"[IVR] IVR_ENABLED=false — skipping {call_type} call for PW {pregnant_woman_id}")
        return {"success": False, "campaign_id": None, "error": "IVR disabled"}

    if not IVR_USER_TOKEN:
        logger.error("[IVR] IVR_USER_TOKEN not configured in .env")
        return {"success": False, "campaign_id": None, "error": "IVR token not configured"}

    # Dovesoft contacts format: JSON array of objects
    contacts = json.dumps([{"mobile": mobile_number}])

    params = {
        "userId": IVR_USER_ID,
        "userToken": IVR_USER_TOKEN,
        "apiType": "broadcasting",
        "announcementId": announcement_id,
        "planId": IVR_PLAN_ID,
        "planType": IVR_PLAN_TYPE,
        "inputWaitTime": "0",
        "cli": "[]",
        "includesCountryCode": "N",
        "contacts": contacts,
        "callback_url": IVR_WEBHOOK_URL,
        "extraParameters": "{}",
        "extraParametersIndex": "N",
    }

    url = f"{IVR_API_BASE_URL}/broadcasting"

    try:
        logger.info(f"[IVR] Triggering {call_type} call for PW {pregnant_woman_id} → {mobile_number}")
        response = requests.get(url, params=params, timeout=30)
        response_data = response.json()

        logger.info(f"[IVR] Dovesoft response for PW {pregnant_woman_id} ({call_type}): {response_data}")

        # Dovesoft returns campaignId on success
        if response.status_code == 200 and response_data.get("message") == "Success" and response_data.get("successCount", 0) > 0:
            # Extract uniqueId from first item in data array
            data_list = response_data.get("data", [])
            unique_id = data_list[0].get("uniqueId", "") if data_list else ""
            logger.info(f"[IVR] {call_type} call triggered successfully — uniqueId={unique_id} for PW {pregnant_woman_id}")
            return {
                "success": True,
                "campaign_id": unique_id,
                "error": None,
                "raw_response": json.dumps(response_data)
            }
        else:
            error_msg = response_data.get("message") or response_data.get("error") or str(response_data)
            logger.error(f"[IVR] {call_type} call failed for PW {pregnant_woman_id}: {error_msg}")
            return {
                "success": False,
                "campaign_id": None,
                "error": error_msg,
                "raw_response": json.dumps(response_data)
            }

    except requests.exceptions.Timeout:
        logger.error(f"[IVR] Timeout triggering {call_type} call for PW {pregnant_woman_id}")
        return {"success": False, "campaign_id": None, "error": "Request timeout"}
    except requests.exceptions.ConnectionError:
        logger.error(f"[IVR] Connection error triggering {call_type} call for PW {pregnant_woman_id}")
        return {"success": False, "campaign_id": None, "error": "Connection error"}
    except Exception as e:
        logger.error(f"[IVR] Unexpected error for {call_type} call PW {pregnant_woman_id}: {str(e)}")
        return {"success": False, "campaign_id": None, "error": str(e)}


def trigger_high_risk_call(mobile_number: str, pregnant_woman_id: int) -> dict:
    """
    Trigger a High Risk Advisory call via Dovesoft Broadcast API.
    
    Args:
        mobile_number: PW's mobile number (10 digits, no country code)
        pregnant_woman_id: for logging purposes
    
    Returns:
        dict with keys: success (bool), campaign_id (str|None), error (str|None)
    """
    return _trigger_ivr_call(
        mobile_number=mobile_number,
        announcement_id=IVR_HIGH_RISK_ANNOUNCEMENT_ID,
        call_type="high_risk_advisory",
        pregnant_woman_id=pregnant_woman_id
    )


def trigger_feedback_call_1(mobile_number: str, pregnant_woman_id: int, usg_appointment_id: int) -> dict:
    """
    Trigger Feedback Call 1 (24-28 hours after USG completion).
    
    Args:
        mobile_number: PW's mobile number (10 digits, no country code)
        pregnant_woman_id: for logging purposes
        usg_appointment_id: USG appointment reference
    
    Returns:
        dict with keys: success (bool), campaign_id (str|None), error (str|None)
    """
    return _trigger_ivr_call(
        mobile_number=mobile_number,
        announcement_id=IVR_FEEDBACK_CALL_1_ANNOUNCEMENT_ID,
        call_type="feedback_call_1",
        pregnant_woman_id=pregnant_woman_id,
        usg_appointment_id=usg_appointment_id
    )


def trigger_feedback_call_2(mobile_number: str, pregnant_woman_id: int, usg_appointment_id: int) -> dict:
    """
    Trigger Feedback Call 2 (48 hours after Call 1 if needed).
    
    Args:
        mobile_number: PW's mobile number (10 digits, no country code)
        pregnant_woman_id: for logging purposes
        usg_appointment_id: USG appointment reference
    
    Returns:
        dict with keys: success (bool), campaign_id (str|None), error (str|None)
    """
    return _trigger_ivr_call(
        mobile_number=mobile_number,
        announcement_id=IVR_FEEDBACK_CALL_2_ANNOUNCEMENT_ID,
        call_type="feedback_call_2",
        pregnant_woman_id=pregnant_woman_id,
        usg_appointment_id=usg_appointment_id
    )
