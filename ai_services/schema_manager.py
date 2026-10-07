"""
Schema Manager - Provides database schema information to AI engines
"""
import os

def get_database_schema() -> dict:
    """
    Returns database schema information for AI Text-to-SQL
    """
    return {
        "tables": {
            # ── Existing tables (extended with missing columns) ──────────────
            "pregnant_women": {
                "columns": [
                    "id", "full_name", "mobile_number", "age", "address",
                    "ward_id", "block_id", "district_id", "sub_centre_id",
                    "lmp_date", "edd_date", "gravida", "para", "blood_group",
                    "husband_name", "rch_id", "abha_id", # "aadhaar_masked",
                    "is_high_risk", "risk_factors", "is_active",
                    "is_self_registered", "registration_approved",
                    "pregnancy_outcome", "outcome_date", "created_at", "updated_at"
                ],
                "description": "Pregnant women registered in the system",
                "key_columns": "id (primary), block_id (foreign), district_id (foreign), sub_centre_id (foreign), ward_id (foreign)",
                "enum_values": {
                    "pregnancy_outcome": "Values: referred, safe_delivery, live_birth, still_birth, infant_death, maternal_death, miscarriage — set when delivery outcome is recorded"
                }
            },
            "blocks": {
                "columns": ["id", "name", "code", "district_id", "is_active", "created_at"],
                "description": "Administrative blocks within districts",
                "key_columns": "id (primary), district_id (foreign)"
            },
            "districts": {
                "columns": ["id", "name", "code", "is_active", "created_at"],
                "description": "Administrative districts",
                "key_columns": "id (primary)"
            },
            "wards": {
                "columns": ["id", "name", "code", "block_id", "is_active", "created_at"],
                "description": "Wards within blocks",
                "key_columns": "id (primary), block_id (foreign)"
            },
            "sub_centres": {
                "columns": ["id", "name", "code", "block_id", "address", "contact_number", "is_active", "created_at"],
                "description": "Sub-centres providing healthcare services",
                "key_columns": "id (primary), block_id (foreign)"
            },
            "usg_centres": {
                "columns": [
                    "id", "name", "code", "district_id", "block_id", "ward_id",
                    "address", "contact_number", "contact_person_name",
                    "is_empanelled", "is_private", "is_active", "created_at"
                ],
                "description": "USG/Ultrasound centres",
                "key_columns": "id (primary), district_id (foreign), block_id (foreign)"
            },
            "usg_appointments": {
                "columns": [
                    "id", "pregnant_woman_id", "usg_centre_id", "scheduled_date",
                    "appointment_type", "status", "scan_type", "trimester",
                    "gestational_age", "findings", "abnormal_findings",
                    "doctor_name", "technician_name", "scan_date",
                    "completed_date", "reschedule_count", "is_high_risk", "created_at"
                ],
                "description": "USG appointment bookings — NO district_id or block_id columns",
                "key_columns": "id (primary), pregnant_woman_id (foreign to pregnant_women), usg_centre_id (foreign to usg_centres)",
                "join_info": "JOIN pregnant_women ON usg_appointments.pregnant_woman_id = pregnant_women.id",
                "enum_values": {
                    "status": "scheduled, accepted, rescheduled, completed, cancelled",
                    "appointment_type": "regular, emergency",
                    "findings": "Normal, Abnormal"
                }
            },
            "anc_visits": {
                "columns": [
                    "id", "pregnant_woman_id", "visit_number", "visit_date",
                    "weight", "blood_pressure", "hemoglobin", "fundal_height",
                    "fetal_heart_rate", "referred_for_usg", "is_emergency",
                    "doctor_notes", "next_visit_date", "attended_by",
                    "facility_name", "created_at"
                ],
                "description": "Antenatal care visit records — NO district_id or block_id columns",
                "key_columns": "id (primary), pregnant_woman_id (foreign to pregnant_women)",
                "join_info": "JOIN pregnant_women ON anc_visits.pregnant_woman_id = pregnant_women.id",
                "important": "visit_number column EXISTS (1, 2, 3, 4 for 1st-4th ANC). Use it to filter specific ANC visits."
            },
            "grievances": {
                "columns": [
                    "id", "ticket_number", "pregnant_woman_id", "name",
                    "ward_id", "block_id", "district_id", "status",
                    "escalated_to_district", "resolution_note", "resolved_at", "created_at"
                ],
                "description": "Grievances submitted by beneficiaries",
                "key_columns": "id (primary), block_id (foreign), district_id (foreign)",
                "enum_values": {
                    "status": "pending, in_progress, resolved, escalated"
                }
            },

            # ── NEW: Delivery Point tables ───────────────────────────────────
            "delivery_points": {
                "columns": [
                    "id", "name", "code", "address", "contact_number",
                    "contact_person_name", "district_id", "block_id", "is_active",
                    "created_at", "updated_at"
                ],
                "description": "Delivery Points (DPs) — facilities where deliveries happen. Has BOTH district_id and block_id.",
                "key_columns": "id (primary), district_id (foreign to districts), block_id (foreign to blocks)",
                "important": "ALWAYS filter is_active = 1 unless specifically asked for inactive DPs"
            },
            "delivery_referrals": {
                "columns": [
                    "id", "pregnant_woman_id", "referred_by_user_id",
                    "sub_centre_id", "dp_id", "previous_referral_id",
                    "observation_notes", "status", "re_refer_reason",
                    "accepted_by_user_id", "accepted_at", "created_at", "updated_at"
                ],
                "description": "Delivery referrals from Sub-Centres to Delivery Points. A referral can be re-referred (chained via previous_referral_id).",
                "key_columns": "id (primary), pregnant_woman_id (foreign to pregnant_women), dp_id (foreign to delivery_points), sub_centre_id (foreign to sub_centres)",
                "join_info": "JOIN delivery_points ON delivery_referrals.dp_id = delivery_points.id | JOIN pregnant_women ON delivery_referrals.pregnant_woman_id = pregnant_women.id",
                "enum_values": {
                    "status": "pending, accepted, re_referred, completed"
                },
                "important": "previous_referral_id is a self-referential FK for re-referral chains — do NOT join recursively. For re-referral rate: COUNT WHERE status = 're_referred'."
            },
            "delivery_outcomes": {
                "columns": [
                    "id", "referral_id", "pregnant_woman_id", "dp_id",
                    "delivery_type", "delivery_date", "baby_count",
                    "remarks", "recorded_by", "created_at"
                ],
                "description": "Delivery outcomes recorded by Delivery Point users. One outcome per referral (1:1). dp_id is a DIRECT FK to delivery_points — use it for DP-level aggregation without joining delivery_referrals.",
                "key_columns": "id (primary), referral_id (foreign to delivery_referrals, unique), pregnant_woman_id (foreign to pregnant_women), dp_id (foreign to delivery_points)",
                "join_info": "JOIN delivery_points ON delivery_outcomes.dp_id = delivery_points.id | JOIN pregnant_women ON delivery_outcomes.pregnant_woman_id = pregnant_women.id",
                "enum_values": {
                    "delivery_type": "safe_delivery, live_birth, still_birth, infant_death, maternal_death"
                },
                "important": "PREFER delivery_outcomes.dp_id for DP-level delivery counts — no need to join delivery_referrals. Use delivery_date (DateTime) for time-based filtering. baby_count is total babies in this delivery."
            },
            "delivery_outcome_babies": {
                "columns": [
                    "id", "outcome_id", "baby_number", "gender", "status", "created_at"
                ],
                "description": "Individual baby records per delivery outcome. One row per baby. Use for baby-level analytics (gender ratio, baby status breakdown).",
                "key_columns": "id (primary), outcome_id (foreign to delivery_outcomes)",
                "join_info": "JOIN delivery_outcomes ON delivery_outcome_babies.outcome_id = delivery_outcomes.id",
                "enum_values": {
                    "gender": "male, female, other",
                    "status": "live_birth, still_birth, infant_death"
                },
                "important": "For baby counts: COUNT(delivery_outcome_babies.id). For gender ratio: GROUP BY gender. Do NOT use delivery_outcomes.baby_count for gender breakdown — use this table instead."
            }
        },
        "relationships": {
            # ── Existing relationships (unchanged) ──────────────────────────
            "pregnant_women": "Has block_id and district_id. Join blocks ON pregnant_women.block_id = blocks.id",
            "usg_appointments": "NO district_id! Must JOIN pregnant_women first to filter by district",
            "anc_visits": "NO district_id! Must JOIN pregnant_women first to filter by district",
            "blocks": "Has district_id. Join districts ON blocks.district_id = districts.id",
            "wards": "Has block_id. Join blocks ON wards.block_id = blocks.id",
            # ── New delivery relationships ───────────────────────────────────
            "delivery_points": "Has BOTH district_id and block_id directly. Join: JOIN delivery_points dp ON dp.district_id = X",
            "delivery_referrals": "NO district_id or block_id directly. Scope by district/block via: JOIN delivery_points dp ON dr.dp_id = dp.id WHERE dp.district_id = X  OR  JOIN pregnant_women pw ON dr.pregnant_woman_id = pw.id WHERE pw.district_id = X",
            "delivery_outcomes": "NO district_id or block_id directly. PREFERRED scope: JOIN delivery_points dp ON dout.dp_id = dp.id WHERE dp.district_id = X. Alternative: JOIN pregnant_women pw ON dout.pregnant_woman_id = pw.id WHERE pw.district_id = X",
            "delivery_outcome_babies": "No geographic columns. Must JOIN delivery_outcomes first, then JOIN delivery_points for geographic scope"
        },
        "important_notes": [
            # ── Existing notes (unchanged) ───────────────────────────────────
            "usg_appointments table does NOT have district_id or block_id columns",
            "anc_visits table does NOT have district_id or block_id columns",
            "To filter usg_appointments by district: JOIN pregnant_women ON usg_appointments.pregnant_woman_id = pregnant_women.id",
            "To filter anc_visits by district: JOIN pregnant_women ON anc_visits.pregnant_woman_id = pregnant_women.id",
            "Always use table aliases to avoid ambiguous column names",
            "When joining multiple tables with district_id, specify which table: pw.district_id or b.district_id",
            # ── New delivery notes ───────────────────────────────────────────
            "delivery_outcomes and delivery_referrals do NOT have district_id or block_id columns",
            "To filter delivery_outcomes by district: JOIN delivery_points dp ON dout.dp_id = dp.id WHERE dp.district_id = X",
            "To filter delivery_outcomes by block: JOIN delivery_points dp ON dout.dp_id = dp.id WHERE dp.block_id = X",
            "delivery_outcomes.dp_id is a DIRECT FK to delivery_points — use it for DP-level aggregation, no need to join delivery_referrals",
            "NEVER use 'do' as alias for delivery_outcomes — DO is a reserved MySQL keyword. Always use alias 'dout' for delivery_outcomes",
            "delivery_outcome_babies has one row per baby — use COUNT(dob.id) for baby counts, GROUP BY dob.gender for gender ratio",
            "delivery_outcomes.delivery_type ENUM: safe_delivery, live_birth, still_birth, infant_death, maternal_death",
            "delivery_referrals.status ENUM: pending, accepted, re_referred, completed",
            "For re-referral rate: COUNT referrals WHERE status = 're_referred' divided by total referrals for that DP",
            "anc_visits.visit_number column EXISTS — values 1, 2, 3, 4 for 1st through 4th ANC visit"
        ]
    }

def get_allowed_tables() -> list:
    """Get list of tables AI can query"""
    allowed = os.getenv("AI_ALLOWED_TABLES", "").split(",")
    return [t.strip() for t in allowed if t.strip()]

def get_blocked_keywords() -> list:
    """Get list of SQL keywords that are blocked"""
    blocked = os.getenv("AI_BLOCKED_KEYWORDS", "").split(",")
    return [k.strip().upper() for k in blocked if k.strip()]
