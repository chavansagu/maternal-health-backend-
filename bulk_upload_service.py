"""
Bulk upload service for pregnant women  (maternal_portal version).

Two phases, both driven by the same analysis so they can never disagree:

  analyze_dataframe()  -> DRY RUN. Parses + validates every row, classifies it,
                          computes field-level diffs for existing records.
                          Writes NOTHING to the database.
  execute_upload()     -> re-uses the analysis result, inserts new rows and
                          (optionally) updates existing rows, one SAVEPOINT per row.

Row statuses
  new                 -> will be inserted
  duplicate_identical -> already exists, file adds nothing new (skipped)
  duplicate_update    -> already exists, file has new / different values (see `changes`)
  file_duplicate      -> same woman appears earlier in the same file (skipped)
  conflict            -> identifiers contradict each other (never auto-applied)
  invalid             -> validation error (skipped, reason given)

Matching rules (a woman is the "same" only when identifiers agree):
  1. ABHA ID  (digits only, hyphens/spaces ignored)
  2. RCH ID   (trimmed, case-insensitive)
  3. Mobile + Full Name - fallback when the ID(s) in the row matched nobody.

Business rules kept from the existing maternal_portal bulk upload
  * Full Name + valid 10-digit Mobile are required for NEW rows
  * Age 15..60 (Date of Birth wins over the Age column)
  * EDD defaults to LMP + 280 days; EDD must be after LMP
  * Para cannot exceed Gravida; no negative numbers; no future DOB / LMP
  * District / Block / Ward must resolve inside the uploader's jurisdiction
  * Sub-centre is auto-assigned from ward mapping, then block mapping
  * An EDDHistory row is written whenever an EDD is set
  * A mobile number that is already registered to a DIFFERENT woman is treated
    as a duplicate (see ALLOW_SHARED_MOBILE below)
"""
import io
import re
import logging
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import (
    PregnantWoman, Block, Ward, SubCentre, EDDHistory,
    WardSubcentreMapping, BlockSubcentreMapping,
)

logger = logging.getLogger(__name__)

MAX_ROWS = 5000
MAX_FILE_BYTES = 10 * 1024 * 1024
VALID_BLOOD_GROUPS = {"A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"}
UPDATE_MODES = ("skip", "fill_empty", "overwrite")

# This district's existing behaviour: a mobile number already registered to a
# different woman => the row is a duplicate and is skipped.
# Set to True to allow families sharing one phone (row is then added as a NEW
# woman and only a warning is shown in the preview).
ALLOW_SHARED_MOBILE = False

# The existing maternal_portal upload never enforced the 14-digit ABHA rule.
ENFORCE_ABHA_14_DIGITS = False

# Human-readable template header (and DB field name) -> internal field name.
COLUMN_MAP = {
    "Full Name": "full_name",
    "Mobile Number": "mobile_number",
    "ABHA ID": "abha_id",
    "RCH ID": "rch_id",
    "Husband Name": "husband_name",
    "Age": "age",
    "District Name": "district_name",
    "Block Name": "block_name",
    "Ward Name": "ward_name",
    "Sub Centre Name": "sub_centre_name",
    "Date of Birth": "date_of_birth",
    "LMP Date": "lmp_date",
    "EDD Date": "edd_date",
    "Gravida": "gravida",
    "Para": "para",
    "Blood Group": "blood_group",
    "Address": "address",
    "HPR ID": "hpr_id",
    # optional legacy ID columns (validated against the user's jurisdiction)
    "District ID": "district_id",
    "Block ID": "block_id",
    "Ward ID": "ward_id",
    "Sub Centre ID": "sub_centre_id",
}
REQUIRED_COLUMNS = ["full_name", "mobile_number"]


def _norm_header(h) -> str:
    return "".join(ch for ch in str(h).lower() if ch.isalnum())


# case / space / underscore-insensitive header lookup
_HEADER_LOOKUP = {}
for _human, _field in COLUMN_MAP.items():
    _HEADER_LOOKUP[_norm_header(_human)] = _field
    _HEADER_LOOKUP[_norm_header(_field)] = _field

# field -> (label, fill_only)
# fill_only=True  : only ever fills an EMPTY value in the DB, never overwrites.
# fill_only=False : a different value in the file is reported as a "conflict"
#                   and is applied only when update_mode == "overwrite".
UPDATABLE_FIELDS = {
    "husband_name": ("Husband Name", False),
    "date_of_birth": ("Date of Birth", False),
    "age": ("Age", True),            # derived / drifts over time
    "lmp_date": ("LMP Date", False),
    "edd_date": ("EDD Date", False),
    "gravida": ("Gravida", False),
    "para": ("Para", False),
    "blood_group": ("Blood Group", False),
    "address": ("Address", False),
    "hpr_id": ("HPR ID", False),
    "rch_id": ("RCH ID", True),      # identifier: never overwritten
    "abha_id": ("ABHA ID", True),    # identifier: never overwritten
    "mobile_number": ("Mobile Number", False),
    "ward_id": ("Ward", False),          # empty -> filled; different -> only in overwrite mode
    "sub_centre_id": ("Sub Centre", True),
}


# ----------------------------------------------------------------------------
# Small parsing helpers
# ----------------------------------------------------------------------------
def _clean_str(v):
    """NaN/blank -> None. Excel numbers like 9876543210.0 -> '9876543210'."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return None
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _parse_date(value):
    s = _clean_str(value)
    if s is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date") and callable(value.date):  # pandas Timestamp
        return value.date()
    s = s.split(" ")[0].split("T")[0]  # drop any time part
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date '{s}' (use YYYY-MM-DD or DD-MM-YYYY)")


def _parse_int(value, label):
    s = _clean_str(value)
    if s is None:
        return None
    try:
        f = float(s)
    except ValueError:
        raise ValueError(f"{label} must be a whole number (got '{s}')")
    if not f.is_integer():
        raise ValueError(f"{label} must be a whole number (got '{s}')")
    if f < 0:
        raise ValueError(f"{label} cannot be negative (got '{s}')")
    return int(f)


def _normalize_mobile(raw):
    if raw is None:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def _abha_key(raw):
    if raw is None:
        return None
    if re.fullmatch(r"[\d\-\s]+", raw):
        return re.sub(r"\D", "", raw)
    return raw.lower()  # ABHA address style (name@abdm)


def _rch_key(raw):
    return raw.upper() if raw else None


def _calc_age(dob):
    t = date.today()
    return t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))


def _mask_mobile(m) -> str:
    m = str(m or "")
    return ("*" * max(len(m) - 4, 0)) + m[-4:]


def _same(a, b):
    if isinstance(a, str) or isinstance(b, str):
        return str(a).strip().lower() == str(b).strip().lower()
    return a == b


def _display(v):
    if v is None:
        return ""
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return str(v)


# ----------------------------------------------------------------------------
# File reading
# ----------------------------------------------------------------------------
def read_upload_dataframe(filename: str, contents: bytes) -> pd.DataFrame:
    """Parse .xlsx/.xls/.csv into a DataFrame with canonical column names.
    Everything is read as text (dtype=str) so mobile / ABHA / RCH are kept exactly
    as typed. Raises ValueError with a user-friendly message."""
    if len(contents) > MAX_FILE_BYTES:
        raise ValueError(f"File is too large (max {MAX_FILE_BYTES // (1024 * 1024)} MB)")
    name = (filename or "").lower()
    try:
        if name.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(contents), dtype=str)
        else:
            df = pd.read_excel(io.BytesIO(contents), dtype=str)
    except Exception as e:
        raise ValueError(f"Could not read file: {e}")

    df = df.rename(columns={c: _HEADER_LOOKUP.get(_norm_header(c), str(c).strip()) for c in df.columns})
    df = df.dropna(how="all").reset_index(drop=True)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "Required column(s) missing: "
            + ", ".join("Full Name" if m == "full_name" else "Mobile Number" for m in missing)
            + ". Please use the downloaded template."
        )
    if len(df) == 0:
        raise ValueError("The file has no data rows")
    if len(df) > MAX_ROWS:
        raise ValueError(f"Too many rows ({len(df)}). Maximum is {MAX_ROWS} per upload")
    return df


# ----------------------------------------------------------------------------
# Row parsing / validation (no DB writes)
# ----------------------------------------------------------------------------
def _parse_row(row):
    """Returns (data dict of NON-empty values, list of error strings)."""
    errors = []
    d = {}

    def safe(fn, *a):
        try:
            return fn(*a)
        except ValueError as e:
            errors.append(str(e))
            return None

    d["full_name"] = _clean_str(row.get("full_name"))
    if d["full_name"] and len(d["full_name"]) > 255:
        errors.append("Full Name is too long (max 255 characters)")

    mobile_raw = _clean_str(row.get("mobile_number"))
    d["mobile_number"] = _normalize_mobile(mobile_raw)
    if d["mobile_number"] is not None and len(d["mobile_number"]) != 10:
        errors.append(f"Invalid mobile number '{mobile_raw}'. Must be 10 digits")
        d["mobile_number"] = None

    abha_raw = _clean_str(row.get("abha_id"))
    d["abha_id"] = abha_raw
    d["_abha_key"] = _abha_key(abha_raw)
    if abha_raw and len(abha_raw) > 50:
        errors.append("ABHA ID is too long (max 50 characters)")
    if (ENFORCE_ABHA_14_DIGITS and abha_raw and re.fullmatch(r"[\d\-\s]+", abha_raw)
            and len(d["_abha_key"]) != 14):
        errors.append(f"ABHA ID must have 14 digits (got '{abha_raw}')")
    rch_raw = _clean_str(row.get("rch_id"))
    d["rch_id"] = rch_raw
    d["_rch_key"] = _rch_key(rch_raw)
    if rch_raw and len(rch_raw) > 50:
        errors.append("RCH ID is too long (max 50 characters)")

    d["husband_name"] = _clean_str(row.get("husband_name"))
    d["address"] = _clean_str(row.get("address"))
    d["hpr_id"] = _clean_str(row.get("hpr_id"))

    bg = _clean_str(row.get("blood_group"))
    if bg:
        bg = bg.upper().replace(" ", "")
        if bg not in VALID_BLOOD_GROUPS:
            errors.append(f"Invalid blood group '{bg}'")
            bg = None
    d["blood_group"] = bg

    today = date.today()
    dob = safe(_parse_date, row.get("date_of_birth"))
    lmp = safe(_parse_date, row.get("lmp_date"))
    edd = safe(_parse_date, row.get("edd_date"))
    if dob and dob > today:
        errors.append("Date of Birth cannot be in the future")
    if lmp and lmp > today:
        errors.append("LMP Date cannot be in the future")
    d["_edd_auto"] = False
    if lmp and not edd:
        edd = lmp + timedelta(days=280)   # Naegele's rule (same as existing upload)
        d["_edd_auto"] = True
    if lmp and edd and edd <= lmp:
        errors.append("EDD Date must be after LMP Date")
    d["date_of_birth"], d["lmp_date"], d["edd_date"] = dob, lmp, edd

    d["gravida"] = safe(_parse_int, row.get("gravida"), "Gravida")
    d["para"] = safe(_parse_int, row.get("para"), "Para")
    if d["gravida"] is not None and d["para"] is not None and d["para"] > d["gravida"]:
        errors.append(f"Para ({d['para']}) cannot be greater than Gravida ({d['gravida']})")

    age = _calc_age(dob) if dob else safe(_parse_int, row.get("age"), "Age")
    if age is not None and age < 15:
        errors.append(f"Age must be at least 15 years (found {age})")
    if age is not None and age > 60:
        errors.append(f"Age {age} looks invalid (max 60)")
    d["age"] = age
    return d, errors


# ----------------------------------------------------------------------------
# Matching against existing records
# ----------------------------------------------------------------------------
def _find_match(db, d):
    """Returns (record, matched_on, conflict_reason)."""
    abha_k, rch_k = d["_abha_key"], d["_rch_key"]
    by_abha = by_rch = None
    if abha_k:
        by_abha = db.query(PregnantWoman).filter(
            func.replace(func.replace(func.lower(PregnantWoman.abha_id), "-", ""), " ", "") == abha_k
        ).first()
    if rch_k:
        by_rch = db.query(PregnantWoman).filter(
            func.upper(func.trim(PregnantWoman.rch_id)) == rch_k
        ).first()

    if by_abha and by_rch and by_abha.id != by_rch.id:
        return None, None, "ABHA ID and RCH ID belong to two different existing records"

    rec = by_abha or by_rch
    if rec:
        matched_on = "ABHA ID" if by_abha else "RCH ID"
        # Cross-check the other identifier: a different value => not the same woman.
        if by_abha and rch_k and rec.rch_id and _rch_key(rec.rch_id.strip()) != rch_k:
            return rec, matched_on, (
                f"ABHA ID matches an existing record but RCH ID differs "
                f"(existing '{rec.rch_id}', file '{d['rch_id']}')")
        if by_rch and not by_abha and abha_k and rec.abha_id and _abha_key(rec.abha_id.strip()) != abha_k:
            return rec, matched_on, (
                f"RCH ID matches an existing record but ABHA ID differs "
                f"(existing '{rec.abha_id}', file '{d['abha_id']}')")
        return rec, matched_on, None

    # Fall back to Mobile + Name when the IDs in the row did not match anyone
    # (or the row has no IDs at all). This lets a file ADD an ABHA/RCH ID to an
    # older record that never had one.
    if d["mobile_number"] and d["full_name"]:
        rec = db.query(PregnantWoman).filter(
            PregnantWoman.mobile_number == d["mobile_number"],
            func.lower(func.trim(PregnantWoman.full_name)) == d["full_name"].strip().lower(),
        ).first()
        if rec:
            # Only the same woman if no identifier contradicts.
            if abha_k and rec.abha_id and rec.abha_id.strip() and _abha_key(rec.abha_id.strip()) != abha_k:
                return None, None, None
            if rch_k and rec.rch_id and rec.rch_id.strip() and _rch_key(rec.rch_id.strip()) != rch_k:
                return None, None, None
            return rec, "Mobile + Name", None
    return None, None, None


def _in_scope(rec, user):
    if user.role == "block":
        return rec.block_id == user.block_id
    if user.role == "district":
        return rec.district_id == user.district_id
    return True


def _id_label(db, field, value, d, is_new):
    """Human-readable value for ward / sub-centre ids in the preview."""
    if value is None:
        return ""
    if is_new:
        return d.get("_ward_label" if field == "ward_id" else "_sc_label") or str(value)
    model = Ward if field == "ward_id" else SubCentre
    obj = db.query(model).filter(model.id == value).first()
    return obj.name if obj else str(value)


def _mobile_used_by_other(db, mobile, rec_id):
    return db.query(PregnantWoman.id).filter(
        PregnantWoman.mobile_number == mobile, PregnantWoman.id != rec_id
    ).first() is not None


def _build_changes(db, rec, d):
    changes = []
    for field, (label, fill_only) in UPDATABLE_FIELDS.items():
        new = d.get(field)
        if new is None:
            continue
        fo = fill_only or (field == "edd_date" and d.get("_edd_auto"))
        old = getattr(rec, field)
        if old is None or (isinstance(old, str) and not old.strip()):
            kind = "fill"
        elif _same(old, new):
            continue
        elif fo:
            continue
        else:
            kind = "conflict"
        # never move a woman onto a mobile that belongs to someone else
        if (field == "mobile_number" and not ALLOW_SHARED_MOBILE
                and _mobile_used_by_other(db, new, rec.id)):
            continue
        if field in ("ward_id", "sub_centre_id"):
            old_txt, new_txt = _id_label(db, field, old, d, False), _id_label(db, field, new, d, True)
        else:
            old_txt, new_txt = _display(old), _display(new)
        changes.append({
            "field": field, "label": label,
            "old": old_txt, "new": new_txt, "type": kind,
        })
    return changes


def _norm_name(v):
    return " ".join(str(v).split()).lower()


def _resolve_ward_for_record(db, row, rec, d):
    """For an EXISTING record: look the file's Ward Name (and Sub Centre Name) up inside
    the record's own block. Sets d['ward_id'] / d['sub_centre_id']. Returns error text or None."""
    ward_name = _clean_str(row.get("ward_name"))
    sc_name = _clean_str(row.get("sub_centre_name"))
    if not ward_name and not sc_name:
        return None
    if not rec.block_id:
        return "Existing record has no block, so ward cannot be assigned"
    block = db.query(Block).filter(Block.id == rec.block_id).first()
    block_label = block.name if block else f"id {rec.block_id}"

    if ward_name:
        wards = db.query(Ward).filter(Ward.block_id == rec.block_id).all()
        ward = next((w for w in wards if _norm_name(w.name) == _norm_name(ward_name)), None)
        if not ward:
            avail = ", ".join(sorted(w.name for w in wards)[:8])
            return (f"Ward '{ward_name}' not found in block '{block_label}'"
                    + (f". Available wards: {avail}" if avail else ""))
        d["ward_id"], d["_ward_label"] = ward.id, ward.name

    if sc_name:
        scs = db.query(SubCentre).filter(SubCentre.block_id == rec.block_id).all()
        sc = next((x for x in scs if _norm_name(x.name) == _norm_name(sc_name)), None)
        if not sc:
            return f"Sub Centre '{sc_name}' not found in block '{block_label}'"
        d["sub_centre_id"], d["_sc_label"] = sc.id, sc.name
    elif d.get("ward_id") and not rec.sub_centre_id:
        # Record has no sub-centre: auto-assign from mappings, same rule as new rows
        wm = db.query(WardSubcentreMapping).filter(WardSubcentreMapping.ward_id == d["ward_id"]).first()
        if not wm:
            wm = db.query(BlockSubcentreMapping).filter(BlockSubcentreMapping.block_id == rec.block_id).first()
        if wm:
            sc = db.query(SubCentre).filter(SubCentre.id == wm.sub_centre_id).first()
            d["sub_centre_id"], d["_sc_label"] = wm.sub_centre_id, (sc.name if sc else None)
    return None


# ----------------------------------------------------------------------------
# Administrative resolution for NEW rows
# ----------------------------------------------------------------------------
def _resolve_admin(db, row, user, resolve_fn, scope, cache):
    """resolve_fn = resolve_administrative_ids(db, row, user, scope, cache) from the
    routes module (name / id lookup + jurisdiction checks). Returns (ids, error)."""
    r = resolve_fn(db, row, user, scope, cache)
    if r["errors"]:
        return None, ", ".join(r["errors"])
    if not r["district_id"]:
        return None, "District is required but not provided or not found"
    if not r["block_id"]:
        return None, "Block is required but not provided or not found"
    if not r["ward_id"]:
        return None, "Ward Name is required but not provided or not found"

    if not r["sub_centre_id"]:
        wm = db.query(WardSubcentreMapping).filter(WardSubcentreMapping.ward_id == r["ward_id"]).first()
        if wm:
            r["sub_centre_id"] = wm.sub_centre_id
        else:
            bm = db.query(BlockSubcentreMapping).filter(BlockSubcentreMapping.block_id == r["block_id"]).first()
            if bm:
                r["sub_centre_id"] = bm.sub_centre_id
        if not r["sub_centre_id"]:
            return None, "Sub Centre Name is required but not provided or not found"
    return r, None


# ----------------------------------------------------------------------------
# Phase 1: analysis (dry run)
# ----------------------------------------------------------------------------
def analyze_dataframe(db, df, user, resolve_fn, scope=None):
    results = []
    seen = {}    # key -> first excel row
    cache = {}   # name/id lookups shared by all rows of this upload
    scope = scope or {}

    for index, row in df.iterrows():
        n = index + 2  # excel row number (header is row 1)
        d, errors = _parse_row(row)
        res = {
            "row_number": n,
            "full_name": d.get("full_name") or "",
            "mobile_number": d.get("mobile_number") or _clean_str(row.get("mobile_number")) or "",
            "abha_id": d.get("abha_id") or "",
            "rch_id": d.get("rch_id") or "",
            "status": None, "reason": "", "matched_on": None,
            "match_id": None, "changes": [], "warning": "",
            "_data": d, "_ids": None,
        }
        results.append(res)

        if errors:
            res["status"], res["reason"] = "invalid", "; ".join(errors)
            continue

        has_ids = bool(d["_abha_key"] or d["_rch_key"])
        if not d["full_name"]:
            res["status"], res["reason"] = "invalid", "Full Name is required"
            continue
        if not d["mobile_number"] and not has_ids:
            res["status"], res["reason"] = "invalid", "Mobile number is required"
            continue

        # ---- duplicate inside the same file ----
        keys = []
        if d["_abha_key"]:
            keys.append(("abha", d["_abha_key"]))
        if d["_rch_key"]:
            keys.append(("rch", d["_rch_key"]))
        if d["mobile_number"]:
            if ALLOW_SHARED_MOBILE:
                if not has_ids:
                    keys.append(("mn", d["mobile_number"], d["full_name"].strip().lower()))
            else:
                keys.append(("mobile", d["mobile_number"]))
        first = next((seen[k] for k in keys if k in seen), None)
        for k in keys:
            seen.setdefault(k, n)
        if first:
            res["status"] = "file_duplicate"
            res["reason"] = f"Duplicate ABHA / RCH / Mobile: already appears in row {first} of this file"
            continue

        # ---- match against database ----
        rec, matched_on, conflict = _find_match(db, d)
        if conflict:
            res["status"], res["reason"] = "conflict", conflict
            res["match_id"], res["matched_on"] = (rec.id if rec else None), matched_on
            continue
        if rec:
            res["match_id"], res["matched_on"] = rec.id, matched_on
            if not _in_scope(rec, user):
                res["status"] = "invalid"
                res["reason"] = "Record already exists under another block/district; you cannot update it"
                continue
            ward_err = _resolve_ward_for_record(db, row, rec, d)
            if ward_err:
                res["status"], res["reason"] = "invalid", ward_err
                continue
            changes = _build_changes(db, rec, d)
            res["changes"] = changes
            if changes:
                res["status"] = "duplicate_update"
                nf = sum(1 for c in changes if c["type"] == "fill")
                nc = len(changes) - nf
                res["reason"] = f"Existing record found ({matched_on}): {nf} empty field(s) can be filled, {nc} field(s) differ"
            else:
                res["status"] = "duplicate_identical"
                res["reason"] = f"Already registered ({matched_on}); nothing new in file"
            continue

        # ---- not the same woman, but is the mobile already used by someone else? ----
        if d["mobile_number"]:
            others = db.query(PregnantWoman).filter(
                PregnantWoman.mobile_number == d["mobile_number"]).limit(3).all()
            if others and not ALLOW_SHARED_MOBILE:
                names = ", ".join(f"{o.full_name} (ID {o.id})" for o in others)
                res["status"] = "duplicate_identical"
                res["reason"] = f"Already registered (duplicate Mobile) for: {names}"
                continue

        # ---- the new row must have a mobile (DB column is NOT NULL) ----
        if not d["mobile_number"]:
            res["status"], res["reason"] = "invalid", "Mobile number is required"
            continue

        # ---- new record: needs full administrative validation ----
        ids, err = _resolve_admin(db, row, user, resolve_fn, scope, cache)
        if err:
            res["status"], res["reason"] = "invalid", err
            continue
        res["_ids"] = ids
        res["status"] = "new"
        if d["mobile_number"] and ALLOW_SHARED_MOBILE:
            others = db.query(PregnantWoman).filter(
                PregnantWoman.mobile_number == d["mobile_number"]).limit(3).all()
            if others:
                names = ", ".join(f"{o.full_name} (ID {o.id})" for o in others)
                res["warning"] = (f"Mobile already registered for: {names}. This row will be ADDED AS A NEW woman - "
                                  f"if it is the same person, check the name spelling or add her ABHA/RCH ID")
    return results


def summarize(results):
    keys = ["new", "duplicate_update", "duplicate_identical", "file_duplicate", "conflict", "invalid"]
    s = {k: 0 for k in keys}
    for r in results:
        s[r["status"]] += 1
    s["total"] = len(results)
    return s


def serialize_rows(results):
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]


# ----------------------------------------------------------------------------
# Phase 2: execution
# ----------------------------------------------------------------------------
def execute_upload(db, results, user, update_mode, selected_rows=None):
    """Applies the analysis. One SAVEPOINT per row so a single bad row can't
    poison the rest. Returns counters + failed_rows + audit entries.
    The caller commits."""
    if update_mode not in UPDATE_MODES:
        raise ValueError("Invalid update mode")

    out = {"inserted": 0, "updated": 0, "duplicate": 0, "failed": 0,
           "failed_rows": [], "audit": [], "errors": []}

    def fail(r, reason):
        out["failed"] += 1
        out["failed_rows"].append({
            "row_number": r["row_number"], "full_name": r["full_name"],
            "mobile_number": r["mobile_number"], "error_reason": reason})
        out["errors"].append(
            f"Row {r['row_number']} | Name: {r['full_name']} | "
            f"Mobile: {_mask_mobile(r['mobile_number'])} | Reason: {reason}")

    for r in results:
        st, d = r["status"], r["_data"]

        if st in ("duplicate_identical", "file_duplicate"):
            out["duplicate"] += 1
            continue
        if st in ("invalid", "conflict"):
            fail(r, r["reason"])
            continue

        if st == "new":
            ids = r["_ids"]
            try:
                with db.begin_nested():
                    new_pw = PregnantWoman(
                        full_name=d["full_name"], mobile_number=d["mobile_number"],
                        abha_id=d["abha_id"], rch_id=d["rch_id"],
                        husband_name=d["husband_name"], age=d["age"],
                        date_of_birth=d["date_of_birth"], lmp_date=d["lmp_date"],
                        edd_date=d["edd_date"], gravida=d["gravida"], para=d["para"],
                        address=d["address"], hpr_id=d["hpr_id"], blood_group=d["blood_group"],
                        ward_id=ids["ward_id"], sub_centre_id=ids["sub_centre_id"],
                        block_id=ids["block_id"], district_id=ids["district_id"],
                        registered_by=user.id, registration_approved=True,
                    )
                    db.add(new_pw)
                    db.flush()   # new_pw.id is needed for the EDD history FK
                    if d["edd_date"]:
                        db.add(EDDHistory(
                            pregnant_woman_id=new_pw.id, previous_edd=None,
                            new_edd=d["edd_date"], source="LMP", changed_by=user.id,
                        ))
                        db.flush()
                out["inserted"] += 1
            except IntegrityError:
                fail(r, "Database rejected the row (ABHA ID / RCH ID already exists)")
            except Exception as e:
                logger.error(f"[BULK UPLOAD] Row {r['row_number']} insert failed: {e}")
                fail(r, str(e)[:150])
            continue

        if st == "duplicate_update":
            allowed = update_mode != "skip" and (selected_rows is None or r["row_number"] in selected_rows)
            if not allowed:
                out["duplicate"] += 1
                continue
            try:
                with db.begin_nested():
                    rec = db.query(PregnantWoman).filter(PregnantWoman.id == r["match_id"]).first()
                    if rec is None:
                        raise ValueError("Matched record no longer exists")
                    old_vals, new_vals = {}, {}
                    prev_edd = rec.edd_date
                    for c in r["changes"]:
                        if c["type"] == "conflict" and update_mode != "overwrite":
                            continue
                        old_vals[c["field"]] = c["old"]
                        new_vals[c["field"]] = c["new"]
                        setattr(rec, c["field"], d[c["field"]])
                    if "date_of_birth" in new_vals and rec.date_of_birth:
                        rec.age = _calc_age(rec.date_of_birth)
                    if "edd_date" in new_vals and rec.edd_date:
                        db.add(EDDHistory(
                            pregnant_woman_id=rec.id, previous_edd=prev_edd,
                            new_edd=rec.edd_date, source="LMP", changed_by=user.id,
                        ))
                    db.flush()
                if new_vals:
                    out["updated"] += 1
                    out["audit"].append((rec.id, old_vals, new_vals))
                else:
                    out["duplicate"] += 1
            except IntegrityError:
                fail(r, "Database rejected the update (value already used by another record)")
            except Exception as e:
                logger.error(f"[BULK UPLOAD] Row {r['row_number']} update failed: {e}")
                fail(r, str(e)[:150])
    return out
