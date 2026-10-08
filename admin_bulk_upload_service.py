"""
Bulk upload service for ADMINISTRATIVE data
(Blocks, Wards/Villages, Sub-Centres, USG Centres, Delivery Points, PMSMA Centres).

Same two-phase flow as the pregnant-women bulk upload (bulk_upload_service.py):

  analyze_dataframe()  -> DRY RUN. Parses + validates every row, classifies it,
                          computes field-level diffs for existing records.
                          Writes NOTHING to the database.
  execute_upload()     -> re-uses the analysis, inserts new rows and (optionally)
                          updates existing rows, one SAVEPOINT per row.

Row statuses (identical to the pregnant-women upload)
  new                 -> will be inserted
  duplicate_identical -> already exists, file adds nothing new (skipped)
  duplicate_update    -> already exists, file has new / different values (see `changes`)
  file_duplicate      -> same record appears earlier in the same file (skipped)
  conflict            -> identifiers contradict each other (never auto-applied)
  invalid             -> validation error (skipped, reason given)

Update modes (same as pregnant women)
  skip        -> only insert new rows
  fill_empty  -> also fill EMPTY fields of existing records (safe default)
  overwrite   -> also overwrite fields whose value differs
                 (the Code is the identifier and is never overwritten)

Matching rules
  blocks / sub-centres / USG centres / delivery points / PMSMA centres -> Code
  wards -> Code inside the Block  (a ward with the same Name but a different Code
           in the same block is reported as a conflict, because the pregnant-women
           upload resolves wards by name)

Jurisdiction: only district users can upload; every district / block named in the
file must belong to the uploader's own district.
"""
import io
import re
import logging
from datetime import datetime

import pandas as pd
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import (
    District, Block, Ward, SubCentre, USGCentre, DeliveryPoint, PMSMACentre,
    WardSubcentreMapping, BlockSubcentreMapping, USGCentreBlockMapping,
)

logger = logging.getLogger(__name__)

MAX_ROWS = 5000
MAX_FILE_BYTES = 10 * 1024 * 1024
UPDATE_MODES = ("skip", "fill_empty", "overwrite")


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def _norm_header(h) -> str:
    return "".join(ch for ch in str(h).lower() if ch.isalnum())


def _norm_name(v) -> str:
    return " ".join(str(v).split()).lower()


def _clean_str(v):
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


def _parse_bool(v, label):
    s = _clean_str(v)
    if s is None:
        return None
    s = s.lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n"):
        return False
    raise ValueError(f"{label} must be true/false or yes/no (got '{v}')")


def _parse_phone(v, label):
    s = _clean_str(v)
    if s is None:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError(f"Invalid {label} '{s}'. Must be 10 digits")
    return digits


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _parse_email(v, label):
    s = _clean_str(v)
    if s is None:
        return None
    if not _EMAIL_RE.match(s):
        raise ValueError(f"Invalid {label} '{s}'")
    return s


def _display(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v)


def _same(a, b):
    if isinstance(a, str) or isinstance(b, str):
        return str(a).strip().lower() == str(b).strip().lower()
    return a == b


def _code_key(code):
    return code.strip().upper() if code else None


# ----------------------------------------------------------------------------
# Entity configuration
#   field spec: (field, header label, kind, required, max_len, update_rule)
#   update_rule: "overwrite" -> empty is filled, a different value is a "conflict"
#                               (applied only in overwrite mode)
#                "fill_only" -> only ever fills an empty value
#                None        -> never updated
# ----------------------------------------------------------------------------
_COMMON_CONTACT = [
    ("address", "Address", "str", False, None, "overwrite"),
    ("contact_number", "Contact Number", "phone", False, None, "overwrite"),
]

ENTITIES = {
    "blocks": {
        "label": "Block", "model": Block, "audit": "Block",
        "fields": [
            ("name", "Name", "str", True, 255, "overwrite"),
            ("name_regional", "Name Regional", "str", False, 255, "overwrite"),
            ("code", "Code", "str", True, 50, None),
        ],
        "extra_headers": {"district_name": "District Name"},
    },
    "wards": {
        "label": "Ward / Village", "model": Ward, "audit": "Ward",
        "fields": [
            ("name", "Name", "str", True, 255, "overwrite"),
            ("name_regional", "Name Regional", "str", False, 255, "overwrite"),
            ("code", "Code", "str", True, 50, None),
        ],
        "extra_headers": {"block_name": "Block Name"},
        "required_extra": ["block_name"],
    },
    "sub-centres": {
        "label": "Sub-Centre", "model": SubCentre, "audit": "SubCentre",
        "fields": [
            ("name", "Name", "str", True, 255, "overwrite"),
            ("code", "Code", "str", True, 50, None),
        ] + _COMMON_CONTACT,
        "extra_headers": {"block_name": "Block Name"},
        "required_extra": ["block_name"],
    },
    "usg-centres": {
        "label": "USG Centre", "model": USGCentre, "audit": "USGCentre",
        "fields": [
            ("name", "Name", "str", True, 255, "overwrite"),
            ("code", "Code", "str", True, 50, None),
            ("contact_person_name", "Contact Person Name", "str", False, 255, "overwrite"),
            ("email", "Email", "email", False, 255, "overwrite"),
            ("is_private", "Is Private", "bool", False, None, "overwrite"),
        ] + _COMMON_CONTACT,
        "extra_headers": {"district_name": "District Name", "block_names": "Block Names"},
    },
    "delivery-points": {
        "label": "Delivery Point", "model": DeliveryPoint, "audit": "DeliveryPoint",
        "fields": [
            ("name", "Name", "str", True, 255, "overwrite"),
            ("code", "Code", "str", True, 50, None),
            ("contact_person_name", "Contact Person Name", "str", False, 255, "overwrite"),
            ("is_sdh_dhh", "Is SDH/DHH", "bool", False, None, "overwrite"),
        ] + _COMMON_CONTACT,
        "extra_headers": {"district_name": "District Name", "block_name": "Block Name"},
        "rel_updates": {"block_id": ("Block", Block)},
    },
    "pmsma-centres": {
        "label": "PMSMA Centre", "model": PMSMACentre, "audit": "PMSMACentre",
        "fields": [
            ("name", "Name", "str", True, 255, "overwrite"),
            ("code", "Code", "str", True, 50, None),
            ("contact_person_name", "Contact Person Name", "str", False, 255, "overwrite"),
        ] + _COMMON_CONTACT,
        "extra_headers": {"district_name": "District Name", "block_name": "Block Name"},
        "rel_updates": {"block_id": ("Block", Block)},
    },
}

# is_sdh_dhh is stored as is_sdh_dhh; header aliases are added below
_HEADER_ALIASES = {"issdhdh": "is_sdh_dhh", "issdhdhh": "is_sdh_dhh", "sdhdhh": "is_sdh_dhh",
                   "regionalname": "name_regional", "nameregional": "name_regional"}


def _header_lookup(cfg):
    lk = {}
    for field, label, *_ in cfg["fields"]:
        lk[_norm_header(label)] = field
        lk[_norm_header(field)] = field
    for field, label in cfg["extra_headers"].items():
        lk[_norm_header(label)] = field
        lk[_norm_header(field)] = field
    for k, v in _HEADER_ALIASES.items():
        if any(f[0] == v for f in cfg["fields"]):
            lk.setdefault(k, v)
    return lk


for _c in ENTITIES.values():
    _c["_lookup"] = _header_lookup(_c)


def get_config(entity):
    cfg = ENTITIES.get(entity)
    if not cfg:
        raise ValueError(f"Unknown bulk upload type '{entity}'")
    return cfg


# ----------------------------------------------------------------------------
# File reading
# ----------------------------------------------------------------------------
def read_upload_dataframe(entity: str, filename: str, contents: bytes) -> pd.DataFrame:
    cfg = get_config(entity)
    if len(contents) > MAX_FILE_BYTES:
        raise ValueError(f"File is too large (max {MAX_FILE_BYTES // (1024 * 1024)} MB)")
    name = (filename or "").lower()
    try:
        if name.endswith(".csv"):
            try:
                text = contents.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = contents.decode("cp1252")
            df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
        else:
            df = pd.read_excel(io.BytesIO(contents), dtype=str, keep_default_na=False)
    except Exception as e:
        raise ValueError(f"Could not read file: {e}")

    df = df.rename(columns={c: cfg["_lookup"].get(_norm_header(c), str(c).strip()) for c in df.columns})
    df = df.replace(r"^\s*$", pd.NA, regex=True)
    df = df.dropna(how="all").reset_index(drop=True)

    required = [f[0] for f in cfg["fields"] if f[3]] + cfg.get("required_extra", [])
    labels = {f[0]: f[1] for f in cfg["fields"]}
    labels.update(cfg["extra_headers"])
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError("Required column(s) missing: " + ", ".join(labels[m] for m in missing)
                         + ". Please use the downloaded template.")
    if len(df) == 0:
        raise ValueError("The file has no data rows")
    if len(df) > MAX_ROWS:
        raise ValueError(f"Too many rows ({len(df)}). Maximum is {MAX_ROWS} per upload")
    return df


# ----------------------------------------------------------------------------
# Row parsing (no DB access)
# ----------------------------------------------------------------------------
def _parse_row(cfg, row):
    errors, d = [], {}
    for field, label, kind, required, max_len, _rule in cfg["fields"]:
        raw = row.get(field)
        try:
            if kind == "str":
                v = _clean_str(raw)
                if v and max_len and len(v) > max_len:
                    errors.append(f"{label} is too long (max {max_len} characters)")
            elif kind == "phone":
                v = _parse_phone(raw, label)
            elif kind == "email":
                v = _parse_email(raw, label)
            elif kind == "bool":
                v = _parse_bool(raw, label)
            else:
                v = _clean_str(raw)
        except ValueError as e:
            errors.append(str(e))
            v = None
        d[field] = v
        if required and v is None and not any(label in e for e in errors):
            errors.append(f"{label} is required")
    d["_code_key"] = _code_key(d.get("code"))
    for extra in cfg["extra_headers"]:
        d[extra] = _clean_str(row.get(extra))
    return d, errors


# ----------------------------------------------------------------------------
# Relation resolution (district / block) - always inside the uploader's district
# ----------------------------------------------------------------------------
def _resolve_district(db, d, user, cache):
    """Returns (district_id, error). Missing name -> uploader's own district."""
    if not user.district_id:
        return None, "Your account is not linked to a district"
    name = d.get("district_name")
    if not name:
        return user.district_id, None
    if "district" not in cache:
        cache["district"] = db.query(District).filter(District.id == user.district_id).first()
    own = cache["district"]
    if own and _norm_name(own.name) == _norm_name(name):
        return own.id, None
    other = db.query(District).filter(func.lower(func.trim(District.name)) == _norm_name(name)).first()
    if other:
        return None, f"District '{name}' is outside your jurisdiction (you can only upload for '{own.name if own else user.district_id}')"
    return None, f"District '{name}' not found"


def _find_block(db, user, name, cache):
    key = ("block", _norm_name(name))
    if key not in cache:
        cache[key] = db.query(Block).filter(
            Block.district_id == user.district_id,
            func.lower(func.trim(Block.name)) == _norm_name(name),
            Block.is_active == True,  # noqa: E712
        ).first()
    return cache[key]


def _resolve_relations(db, cfg_key, d, user, cache):
    """Fills d['district_id'] / d['block_id'] (+ labels). Returns error text or None."""
    if not user.district_id:
        return "Your account is not linked to a district"

    if cfg_key == "blocks":
        did, err = _resolve_district(db, d, user, cache)
        if err:
            return err
        d["district_id"] = did
        return None

    if cfg_key in ("wards", "sub-centres"):
        bn = d.get("block_name")
        if not bn:
            return "Block Name is required"
        blk = _find_block(db, user, bn, cache)
        if not blk:
            return f"Block '{bn}' not found in your district"
        d["block_id"], d["_block_label"] = blk.id, blk.name
        return None

    # usg-centres / delivery-points / pmsma-centres
    did, err = _resolve_district(db, d, user, cache)
    if err:
        return err
    d["district_id"] = did

    if cfg_key == "usg-centres":
        ids, labels, bad = [], [], []
        for bn in (d.get("block_names") or "").split(","):
            bn = bn.strip()
            if not bn:
                continue
            blk = _find_block(db, user, bn, cache)
            if not blk:
                bad.append(bn)
            elif blk.id not in ids:
                ids.append(blk.id)
                labels.append(blk.name)
        if bad:
            return "Block(s) not found in your district: " + ", ".join(bad)
        d["_block_ids"], d["_block_labels"] = ids, labels
        return None

    bn = d.get("block_name")
    if bn:
        blk = _find_block(db, user, bn, cache)
        if not blk:
            return f"Block '{bn}' not found in your district"
        d["block_id"], d["_block_label"] = blk.id, blk.name
    return None


# ----------------------------------------------------------------------------
# Matching against existing records
# ----------------------------------------------------------------------------
def _find_match(db, cfg_key, cfg, d):
    """Returns (record, matched_on, conflict_reason)."""
    M = cfg["model"]
    code_k = d["_code_key"]
    if cfg_key == "wards":
        q = db.query(M).filter(M.block_id == d["block_id"])
        rec = q.filter(func.upper(func.trim(M.code)) == code_k).first()
        if rec:
            return rec, "Code", None
        same_name = q.filter(func.lower(func.trim(M.name)) == _norm_name(d["name"])).first()
        if same_name:
            return None, None, (f"A ward named '{same_name.name}' already exists in block "
                                f"'{d['_block_label']}' with a different Code ('{same_name.code}', file '{d['code']}')")
        return None, None, None

    rec = db.query(M).filter(func.upper(func.trim(M.code)) == code_k).first()
    return (rec, "Code", None) if rec else (None, None, None)


def _in_scope(db, cfg_key, rec, user):
    if cfg_key == "blocks":
        return rec.district_id == user.district_id
    if cfg_key == "wards":
        b = db.query(Block).filter(Block.id == rec.block_id).first()
        return bool(b and b.district_id == user.district_id)
    if cfg_key == "sub-centres":
        b = db.query(Block).filter(Block.id == rec.block_id).first()
        return bool(b and b.district_id == user.district_id)
    return rec.district_id in (None, user.district_id)


def _existing_conflict(db, cfg_key, rec, d):
    """Things that make the file row contradict the stored record."""
    if cfg_key in ("sub-centres",) and rec.block_id != d["block_id"]:
        b = db.query(Block).filter(Block.id == rec.block_id).first()
        return (f"Code '{d['code']}' already belongs to a sub-centre in block "
                f"'{b.name if b else rec.block_id}', but the file says '{d['_block_label']}'")
    if cfg_key == "blocks" and d.get("district_id") and rec.district_id != d["district_id"]:
        return f"Code '{d['code']}' already belongs to a block in another district"
    if cfg_key in ("usg-centres", "delivery-points", "pmsma-centres"):
        if rec.district_id and d.get("district_id") and rec.district_id != d["district_id"]:
            return f"Code '{d['code']}' already belongs to a record in another district"
    return None


# ----------------------------------------------------------------------------
# Diff
# ----------------------------------------------------------------------------
def _build_changes(db, cfg_key, cfg, rec, d):
    changes = []

    def add(field, label, old, new, kind, old_txt=None, new_txt=None):
        changes.append({"field": field, "label": label,
                        "old": _display(old) if old_txt is None else old_txt,
                        "new": _display(new) if new_txt is None else new_txt,
                        "type": kind})

    for field, label, _k, _r, _m, rule in cfg["fields"]:
        if rule is None:
            continue
        new = d.get(field)
        if new is None:
            continue
        old = getattr(rec, field)
        if old is None or (isinstance(old, str) and not old.strip()):
            kind = "fill"
        elif _same(old, new):
            continue
        elif rule == "fill_only":
            continue
        else:
            kind = "conflict"
        add(field, label, old, new, kind)

    for field, (label, model) in cfg.get("rel_updates", {}).items():
        new = d.get(field)
        if new is None:
            continue
        old = getattr(rec, field)
        if old == new:
            continue
        kind = "fill" if old is None else "conflict"
        old_obj = db.query(model).filter(model.id == old).first() if old else None
        add(field, label, old, new, kind,
            old_txt=old_obj.name if old_obj else "", new_txt=d.get("_block_label") or str(new))

    if cfg_key == "usg-centres" and d.get("_block_ids"):
        have = {m.block_id for m in db.query(USGCentreBlockMapping).filter(
            USGCentreBlockMapping.usg_centre_id == rec.id).all()}
        missing = [(i, n) for i, n in zip(d["_block_ids"], d["_block_labels"]) if i not in have]
        if missing:
            existing = db.query(Block).filter(Block.id.in_(have)).all() if have else []
            add("block_names", "Blocks", None, None, "fill",
                old_txt=", ".join(sorted(b.name for b in existing)),
                new_txt="+ " + ", ".join(n for _, n in missing))

    # re-activating a deactivated record is never done silently
    if rec.is_active is False:
        add("is_active", "Status", False, True, "conflict", old_txt="Inactive", new_txt="Active")
    return changes


# ----------------------------------------------------------------------------
# Phase 1: analysis (dry run)
# ----------------------------------------------------------------------------
def analyze_dataframe(db, entity, df, user, cache=None):
    # cfg = get_config(entity)
    # results, seen, cache = [], {}, {}
    cfg = get_config(entity)
    results, seen = [], {}
    cache = {} if cache is None else cache

    for index, row in df.iterrows():
        n = index + 2
        d, errors = _parse_row(cfg, row)
        res = {
            "row_number": n, "name": d.get("name") or "", "code": d.get("code") or "",
            "status": None, "reason": "", "matched_on": None, "match_id": None,
            "changes": [], "warning": "", "_data": d,
        }
        results.append(res)

        if errors:
            res["status"], res["reason"] = "invalid", "; ".join(errors)
            continue

        err = _resolve_relations(db, entity, d, user, cache)
        if err:
            res["status"], res["reason"] = "invalid", err
            continue

        # ---- duplicate inside the same file ----
        if entity == "wards":
            keys = [("code", d["block_id"], d["_code_key"]), ("name", d["block_id"], _norm_name(d["name"]))]
        else:
            keys = [("code", d["_code_key"])]
        first = next((seen[k] for k in keys if k in seen), None)
        for k in keys:
            seen.setdefault(k, n)
        if first:
            res["status"] = "file_duplicate"
            res["reason"] = f"Same Code{' / Name' if entity == 'wards' else ''} already appears in row {first} of this file"
            continue

        # ---- match against database ----
        rec, matched_on, conflict = _find_match(db, entity, cfg, d)
        if conflict:
            res["status"], res["reason"] = "conflict", conflict
            continue
        if rec:
            res["match_id"], res["matched_on"] = rec.id, matched_on
            if not _in_scope(db, entity, rec, user):
                res["status"] = "invalid"
                res["reason"] = f"Code '{d['code']}' already exists under another district; you cannot update it"
                continue
            c = _existing_conflict(db, entity, rec, d)
            if c:
                res["status"], res["reason"] = "conflict", c
                continue
            changes = _build_changes(db, entity, cfg, rec, d)
            res["changes"] = changes
            if changes:
                res["status"] = "duplicate_update"
                nf = sum(1 for x in changes if x["type"] == "fill")
                res["reason"] = (f"Existing record found ({matched_on}): {nf} empty field(s) can be filled, "
                                 f"{len(changes) - nf} field(s) differ")
            else:
                res["status"] = "duplicate_identical"
                res["reason"] = f"Already exists ({matched_on}); nothing new in file"
            continue

        res["status"] = "new"
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
def _create(db, entity, cfg, d, user):
    M = cfg["model"]
    vals = {f[0]: d.get(f[0]) for f in cfg["fields"] if f[0] != "is_private" and f[0] != "is_sdh_dhh"}
    if entity == "blocks":
        obj = M(**vals, district_id=d["district_id"])
        db.add(obj)
        db.flush()
    elif entity == "wards":
        obj = M(**vals, block_id=d["block_id"])
        db.add(obj)
        db.flush()
    elif entity == "sub-centres":
        obj = M(**vals, block_id=d["block_id"])
        db.add(obj)
        db.flush()
        # same auto-mapping as the single create: block + every ward of that block
        db.add(BlockSubcentreMapping(block_id=d["block_id"], sub_centre_id=obj.id))
        for w in db.query(Ward).filter(Ward.block_id == d["block_id"]).all():
            db.add(WardSubcentreMapping(ward_id=w.id, sub_centre_id=obj.id))
    elif entity == "usg-centres":
        obj = M(**vals, district_id=d["district_id"], is_private=bool(d.get("is_private")),
                is_empanelled=True, block_id=(d["_block_ids"][0] if d.get("_block_ids") else None))
        db.add(obj)
        db.flush()
        for bid in d.get("_block_ids", []):
            db.add(USGCentreBlockMapping(usg_centre_id=obj.id, block_id=bid))
    elif entity == "delivery-points":
        obj = M(**vals, district_id=d["district_id"], block_id=d.get("block_id"),
                is_sdh_dhh=bool(d.get("is_sdh_dhh")))
        db.add(obj)
    else:  # pmsma-centres
        obj = M(**vals, district_id=d["district_id"], block_id=d.get("block_id"))
        db.add(obj)
    db.flush()
    return obj


def execute_upload(db, entity, results, user, update_mode, selected_rows=None):
    """Applies the analysis. One SAVEPOINT per row. The caller commits."""
    if update_mode not in UPDATE_MODES:
        raise ValueError("Invalid update mode")
    cfg = get_config(entity)
    M = cfg["model"]
    out = {"inserted": 0, "updated": 0, "duplicate": 0, "failed": 0,
           "failed_rows": [], "audit": [], "created": [], "errors": []}

    def fail(r, reason):
        out["failed"] += 1
        out["failed_rows"].append({"row_number": r["row_number"], "name": r["name"],
                                   "code": r["code"], "error_reason": reason})
        out["errors"].append(f"Row {r['row_number']} | Name: {r['name']} | Code: {r['code']} | Reason: {reason}")

    for r in results:
        st, d = r["status"], r["_data"]

        if st in ("duplicate_identical", "file_duplicate"):
            out["duplicate"] += 1
            continue
        if st in ("invalid", "conflict"):
            fail(r, r["reason"])
            continue

        if st == "new":
            try:
                with db.begin_nested():
                    obj = _create(db, entity, cfg, d, user)
                out["inserted"] += 1
                out["created"].append(obj.id)
            except IntegrityError:
                fail(r, "Database rejected the row (Code already exists)")
            except Exception as e:
                logger.error(f"[ADMIN BULK] Row {r['row_number']} insert failed: {e}")
                fail(r, str(e)[:150])
            continue

        if st == "duplicate_update":
            allowed = update_mode != "skip" and (selected_rows is None or r["row_number"] in selected_rows)
            if not allowed:
                out["duplicate"] += 1
                continue
            try:
                with db.begin_nested():
                    rec = db.query(M).filter(M.id == r["match_id"]).first()
                    if rec is None:
                        raise ValueError("Matched record no longer exists")
                    old_vals, new_vals = {}, {}
                    for c in r["changes"]:
                        if c["type"] == "conflict" and update_mode != "overwrite":
                            continue
                        f = c["field"]
                        old_vals[f], new_vals[f] = c["old"], c["new"]
                        if f == "is_active":
                            rec.is_active = True
                        elif f == "block_names":
                            have = {m.block_id for m in db.query(USGCentreBlockMapping).filter(
                                USGCentreBlockMapping.usg_centre_id == rec.id).all()}
                            for bid in d["_block_ids"]:
                                if bid not in have:
                                    db.add(USGCentreBlockMapping(usg_centre_id=rec.id, block_id=bid))
                            if not rec.block_id and d["_block_ids"]:
                                rec.block_id = d["_block_ids"][0]
                        else:
                            setattr(rec, f, d[f])
                    if new_vals and hasattr(rec, "updated_at"):
                        rec.updated_at = datetime.now()
                    db.flush()
                if new_vals:
                    out["updated"] += 1
                    out["audit"].append((rec.id, old_vals, new_vals))
                else:
                    out["duplicate"] += 1
            except IntegrityError:
                fail(r, "Database rejected the update (value already used by another record)")
            except Exception as e:
                logger.error(f"[ADMIN BULK] Row {r['row_number']} update failed: {e}")
                fail(r, str(e)[:150])
    return out