# SMS Templates for Janani Jyoti System
# All templates are in Odia language as per requirement

# long SMS templates with variable placeholders (e.g. {DATE}, {FACILITY})
# SMS_TEMPLATES = {
#     "pregnancy_registration": {
#         "message": "ନିରୀକ୍ଷଣ - ପୁରୀ ନମସ୍କାର, ଆପଣଙ୍କ ଗର୍ଭାବସ୍ଥା ପଞ୍ଜୀକରଣ ସଫଳ ହୋଇଛି। ଆଶା (ASHA) କିମ୍ବା ଏଏନ୍ଏମ୍ (ANM) ଦିଦି ଖୁବଶୀଘ୍ର ଆପଣଙ୍କ ସହ ଯୋଗାଯୋଗ କରିବେ। NHM PURI",
#         "variables": []
#     },
    
#     "anc_reminder": {
#         "message": "ନିରୀକ୍ଷଣ - ପୁରୀ ଆପଣଙ୍କର ANC ଯାଞ୍ଚ ଆସନ୍ତା {DATE} ତାରିଖ ଦିନ {FACILITY} ଠାରେ ନିର୍ଦ୍ଧାରିତ ହୋଇଛି। ଦୟାକରି ସମୟରେଯାଇ ସ୍ୱାସ୍ଥ୍ୟ ପରୀକ୍ଷା କରାନ୍ତୁ। NHM PURI",
#         "variables": ["DATE", "FACILITY"]
#     },
    
#     "usg_booking_confirmation": {
#         "message": "ନିରୀକ୍ଷଣ - ପୁରୀ ଆପଣଙ୍କ USG (ଅଲଟ୍ରାସାଉଣ୍ଡ) ପରୀକ୍ଷା ଆସନ୍ତା {DATE} ତାରିଖ ଦିନ {USG_CENTRE} ଠାରେ କରାଯିବ।ଦୟାକରି ରିପୋର୍ଟ ସାଙ୍ଗରେ ନେଇ ଯିବେ। NHM PURI",
#         "variables": ["DATE", "USG_CENTRE"]
#     },
    
#     "high_risk_alert": {
#         "message": "ନିରୀକ୍ଷଣ - ପୁରୀ ଆପଣଙ୍କ ଗର୍ଭାବସ୍ଥାକୁ 'ବିପଦ ସଙ୍କୁଳ' (High Risk) ଭାବେ ଚିହ୍ନଟ କରାଯାଇଛି। ଭୟ କରନ୍ତୁ ନାହିଁ, କିନ୍ତୁ ସତର୍କ ରୁହନ୍ତୁ। ASHA/ANM ଆପଣଙ୍କର ବିଶେଷ ଯତ୍ନ ନେବେ। NHM PURI",
#         "variables": []
#     },
    
#     "usg_completed_feedback": {
#         "message": "ନିରୀକ୍ଷଣ - ପୁରୀ ଆପଣଙ୍କ USG ପରୀକ୍ଷା ଶେଷ ହୋଇଛି। ଆପଣ ପାଇଥିବା ସେବା ସମ୍ପର୍କରେ ମତାମତ ଦେବା ପାଇଁ ଆପଣଙ୍କ ପାଖକୁ ଏକ ଫୋନ୍ କଲ୍ ଯିବ। NHM PURI",
#         "variables": []
#     },
    
#     "grievance_acknowledgement": {
#         "message": "ନିରୀକ୍ଷଣ - ପୁରୀ ଆପଣଙ୍କ ଅଭିଯୋଗ ଆମେ ଗ୍ରହଣ କରିଛୁ। ସମ୍ପୃକ୍ତ ଅଧିକାରୀ ଶୀଘ୍ର ଏହାର ଯାଞ୍ଚ କରିବେ। ଧନ୍ୟବାଦ। NHM PURI",
#         "variables": []
#     }
# }

#short SMS templates without variables
SMS_TEMPLATES = {
    "pregnancy_registration": {
        "message": "ନିରୀକ୍ଷଣ-ପୁରୀ, ଆପଣଙ୍କ ଗର୍ଭାବସ୍ଥା ପଞ୍ଜୀକରଣ ସଫଳ ହୋଇଛି । NHM PURI",
        "variables": []
    },
    
    "anc_reminder": {
        "message": "ନିରୀକ୍ଷଣ-ପୁରୀ, ଆପଣଙ୍କର ANC ଯାଞ୍ଚ ଆସନ୍ତା {DATE} ତାରିଖ ଦିନ ନିର୍ଦ୍ଧାରିତ ହୋଇଛି। ଆପଣଙ୍କୁ ଆଶା ଦିଦି ଯୋଗାଯୋଗ କରିବେ। NHM PURI",
        "variables": ["DATE", "FACILITY"]
    },
    
    "usg_booking_confirmation": {
        "message": "ନିରୀକ୍ଷଣ-ପୁରୀ, ଆପଣଙ୍କ ultrasound ପରୀକ୍ଷା ଆସନ୍ତା {DATE} ତାରିଖ ଦିନ {USG_CENTRE} ଠାରେ କରାଯିବ। ଦୟାକରି ରିପୋର୍ଟ ସାଙ୍ଗରେ ନେଇ ଯିବେ। NHM PURI",
        "variables": ["DATE", "USG_CENTRE"]
    },
    
    "high_risk_alert": {
        "message": "ନିରୀକ୍ଷଣ-ପୁରୀ, ଆପଣଙ୍କ ଗର୍ଭାବସ୍ଥାକୁ ବିପଦ ସଙ୍କୁଳ ଭାବେ ଚିହ୍ନଟ କରାଯାଇଛି। ଭୟ କରନ୍ତୁ ନାହିଁ, ସତର୍କ ରୁହନ୍ତୁ। ଆପଣଙ୍କୁ ଆଶା/ANM ଯୋଗାଯୋଗ କରିବେ। NHM PURI",
        "variables": []
    },
    
    "usg_completed_feedback": {
        "message": "ନିରୀକ୍ଷଣ-ପୁରୀ, ଆପଣଙ୍କ ultrasound ପରୀକ୍ଷା ଶେଷ ହୋଇଛି। ପରୀକ୍ଷା ସମ୍ପର୍କରେ ମତାମତ ଦେବା ପାଇଁ ଆପଣଙ୍କୁ ଏକ ଫୋନ୍ କଲ୍ ଯିବ। NHM PURI",
        "variables": []
    },
    
    "grievance_acknowledgement": {
        "message": "ନିରୀକ୍ଷଣ-ପୁରୀ, ଆପଣଙ୍କ ଅଭିଯୋଗ ଆମେ ଗ୍ରହଣ କରିଛୁ। ସମ୍ପୃକ୍ତ ଅଧିକାରୀ ଶୀଘ୍ର ଏହାର ଯାଞ୍ଚ କରିବେ। ଧନ୍ୟବାଦ। NHM PURI",
        "variables": []
    }
}

#long SMS templates DLT Template ID mapping — Whiteray CPaaS
# DLT Template ID mapping — Whiteray CPaaS
# Maps internal template key → registered DLT template ID
# SMS_TEMPLATE_IDS = {
#     "pregnancy_registration":   "1707177582603884431",
#     "high_risk_alert":          "1707177582618165305",
#     "usg_completed_feedback":   "1707177582628873658",
#     "grievance_acknowledgement": "1707177582636833590",
#     "usg_booking_confirmation": "1707177640934315890",
#     "anc_reminder":             "1707177641026616738",
# }

# shoert SMS templates DLT Template ID mapping — Whiteray CPaaS
SMS_TEMPLATE_IDS = {
    "pregnancy_registration":   "1707177651405712798",
    "anc_reminder":             "1707177651426785321",
    "usg_completed_feedback":   "1707177651456179605",
    "high_risk_alert":          "1707177651452839127",
    "usg_booking_confirmation": "1707177651444011215",
    "grievance_acknowledgement": "1707177651458920956",
}


def get_sms_template(template_name: str, **kwargs) -> dict:
    """
    Get SMS template with variable substitution.
    Returns dict: {"message": str, "template_id": str}
    Backward compatible: callers that used the string return value
    should update to use result["message"] instead.
    """
    if template_name not in SMS_TEMPLATES:
        raise ValueError(f"Template '{template_name}' not found")
    
    template = SMS_TEMPLATES[template_name]
    message = template["message"]
    
    # Replace variables
    for var in template["variables"]:
        if var in kwargs:
            message = message.replace(f"{{{var}}}", str(kwargs[var]))
    
    return {
        "message": message,
        "template_id": SMS_TEMPLATE_IDS.get(template_name, ""),
    }

def get_available_templates():
    """
    Get list of available SMS templates
    """
    return list(SMS_TEMPLATES.keys())