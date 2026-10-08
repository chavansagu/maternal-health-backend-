"""
Combined "masters" bulk upload (Users page -> Bulk Upload).

ONE file (csv / xls / xlsx) carries Blocks, Wards/Villages, Sub-Centres, USG Centres,
Delivery Points and PMSMA Centres. A `record_type` column says what each row is.

This module does NOT replace anything. The per-tab bulk upload and the single-record
create/edit stay exactly as they are. Here we only:
  1. read the file and split rows by record_type
  2. run the EXISTING engine (admin_bulk_upload_service) once per type
  3. process in dependency order: block -> ward -> sub-centre -> usg -> delivery point -> pmsma
     (so a block / ward created in the same file is visible to the rows that depend on it)
"""
import io
import logging

import pandas as pd

import admin_bulk_upload_service as base

logger = logging.getLogger(__name__)

# record_type -> engine entity, in processing order
ORDER = ["blocks", "wards", "sub-centres", "usg-centres", "delivery-points", "pmsma-centres"]

TYPE_ALIASES = {
    "block": "blocks", "blocks": "blocks",
    "ward": "wards", "wards": "wards", "village": "wards", "ward/village": "wards",
    "wardvillage": "wards", "villageward": "wards",
    "subcentre": "sub-centres", "subcentres": "sub-centres", "subcenter": "sub-centres",
    "subcenters": "sub-centres", "hsc": "sub-centres",
    "usg": "usg-centres", "usgcentre": "usg-centres", "usgcentres": "usg-centres",
    "usgcenter": "usg-centres",
    "dp": "delivery-points", "deliverypoint": "delivery-points", "deliverypoints": "delivery-points",
    "pmsma": "pmsma-centres", "pmsmacentre": "pmsma-centres", "pmsmacentres": "pmsma-centres",
    "pmsmacenter": "pmsma-centres",
}

TEMPLATE_COLUMNS = [
    "record_type", "name", "name_regional", "code", "district_name", "block_name", "covered_blocks",
    "address", "contact_number", "contact_person_name", "email", "is_private", "is_sdh_dhh",
]


def _entity_for(value):
    s = base._clean_str(value)
    if not s:
        return None
    key = "".join(ch for ch in s.lower() if ch.isalnum() or ch == "/")
    key = key.replace("/", "") if key not in TYPE_ALIASES else key
    return TYPE_ALIASES.get(key) or TYPE_ALIASES.get(key.replace("/", ""))


def _union_lookup():
    lk = {}
    for e in ORDER:
        lk.update(base.ENTITIES[e]["_lookup"])
    lk["recordtype"] = "record_type"
    lk["type"] = "record_type"
    lk["coveredblocks"] = "block_names"
    lk["coveredblock"] = "block_names"
    return lk


_LOOKUP = _union_lookup()


# ----------------------------------------------------------------------------
# File reading
# ----------------------------------------------------------------------------
def read_masters_dataframe(filename: str, contents: bytes) -> pd.DataFrame:
    if len(contents) > base.MAX_FILE_BYTES:
        raise ValueError(f"File is too large (max {base.MAX_FILE_BYTES // (1024 * 1024)} MB)")
    name = (filename or "").lower()
    try:
        if name.endswith(".csv"):
            try:
                text = contents.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = contents.decode("cp1252")
            df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
        else:
            # first sheet only -> the template keeps 'Masters Data' as sheet 1
            df = pd.read_excel(io.BytesIO(contents), dtype=str, keep_default_na=False)
    except Exception as e:
        raise ValueError(f"Could not read file: {e}")

    df = df.rename(columns={c: _LOOKUP.get(base._norm_header(c), str(c).strip()) for c in df.columns})
    df = df.replace(r"^\s*$", pd.NA, regex=True)
    df = df.dropna(how="all").reset_index(drop=True)  # index + 2 == row number in the sheet

    if "record_type" not in df.columns:
        raise ValueError("Required column missing: record_type. Please use the downloaded template.")
    if len(df) == 0:
        raise ValueError("The file has no data rows")
    if len(df) > base.MAX_ROWS:
        raise ValueError(f"Too many rows ({len(df)}). Maximum is {base.MAX_ROWS} per upload")
    return df


# ----------------------------------------------------------------------------
# Phase 1: dry run
# ----------------------------------------------------------------------------
class _NewBlock:
    """Stand-in for a block that is in the file but not yet in the database."""
    id = None

    def __init__(self, name):
        self.name = name


def _bad_row(n, d_row, reason):
    return {
        "row_number": n, "name": base._clean_str(d_row.get("name")) or "",
        "code": base._clean_str(d_row.get("code")) or "", "status": "invalid", "reason": reason,
        "matched_on": None, "match_id": None, "changes": [], "warning": "", "_data": {},
    }


def analyze_masters(db, df, user):
    """Returns (rows_in_file_order, per_entity_results). Writes nothing."""
    cache = {}
    per_entity = {e: [] for e in ORDER}
    bad_rows = []

    # split rows by type (original index kept so row numbers stay correct)
    buckets = {e: [] for e in ORDER}
    for index, row in df.iterrows():
        ent = _entity_for(row.get("record_type"))
        if ent is None:
            raw = base._clean_str(row.get("record_type"))
            msg = ("record_type is required (block, ward, sub_centre, usg, delivery_point, pmsma)"
                   if not raw else f"Unknown record_type '{raw}'")
            bad_rows.append(_bad_row(index + 2, row, msg))
            continue
        buckets[ent].append(index)

    for ent in ORDER:
        if not buckets[ent]:
            continue
        sub = df.loc[buckets[ent]]
        res = base.analyze_dataframe(db, ent, sub, user, cache)
        per_entity[ent] = res
        if ent == "blocks":
            # blocks that this file will create are visible to the rows below (dry run only)
            for r in res:
                if r["status"] == "new":
                    nm = r["_data"].get("name")
                    cache.setdefault(("block", base._norm_name(nm)), _NewBlock(nm))
    return _flatten(per_entity, bad_rows), per_entity


def _flatten(per_entity, bad_rows):
    rows = list(bad_rows)
    for ent in ORDER:
        for r in per_entity[ent]:
            r["record_type"] = base.ENTITIES[ent]["label"]
            rows.append(r)
    for r in bad_rows:
        r.setdefault("record_type", "")
    rows.sort(key=lambda r: r["row_number"])
    return rows


def summarize(rows):
    return base.summarize(rows)


def serialize_rows(rows):
    return base.serialize_rows(rows)


# ----------------------------------------------------------------------------
# Phase 2: execute
# ----------------------------------------------------------------------------
def execute_masters(db, per_entity, user, update_mode, selected_rows=None, bad_rows=None):
    """Runs the existing execute_upload per entity in dependency order. Caller commits."""
    out = {"inserted": 0, "updated": 0, "duplicate": 0, "failed": 0,
           "failed_rows": [], "audit": [], "created": [], "errors": [], "by_type": {}}

    for ent in ORDER:
        results = per_entity.get(ent) or []
        if not results:
            continue
        # blocks / wards created earlier in this run now exist (flushed) -> resolve real ids
        fresh = {}
        for r in results:
            if r["status"] in ("new", "duplicate_update"):
                err = base._resolve_relations(db, ent, r["_data"], user, fresh)
                if err:
                    r["status"], r["reason"] = "invalid", err
        part = base.execute_upload(db, ent, results, user, update_mode, selected_rows)

        label = base.ENTITIES[ent]["label"]
        out["by_type"][label] = {k: part[k] for k in ("inserted", "updated", "duplicate", "failed")}
        for k in ("inserted", "updated", "duplicate", "failed"):
            out[k] += part[k]
        for fr in part["failed_rows"]:
            fr["record_type"] = label
            out["failed_rows"].append(fr)
        out["errors"] += part["errors"]
        out["created"] += [(ent, i) for i in part["created"]]
        out["audit"] += [(base.ENTITIES[ent]["audit"], rid, old, new) for rid, old, new in part["audit"]]

    for r in (bad_rows or []):
        out["failed"] += 1
        out["failed_rows"].append({"row_number": r["row_number"], "name": r["name"], "code": r["code"],
                                   "error_reason": r["reason"], "record_type": r.get("record_type", "")})
        out["errors"].append(f"Row {r['row_number']} | Reason: {r['reason']}")
    out["failed_rows"].sort(key=lambda x: x["row_number"])
    return out


# ----------------------------------------------------------------------------
# Template
# ----------------------------------------------------------------------------
def template_rows():
    def row(**kw):
        d = {c: "" for c in TEMPLATE_COLUMNS}
        d.update(kw)
        return d

    return [
        row(record_type="block", name="Agalpur", name_regional="", code="AGP", district_name="Balangir"),
        row(record_type="ward", name="Aenlapali", code="AENLA", block_name="Agalpur"),
        row(record_type="sub_centre", name="Agalpur SC", code="668", block_name="Agalpur",
            address="Agalpur", contact_number="9876543210"),
        row(record_type="usg", name="Krishna Healthcare", code="5454", covered_blocks="Agalpur,Balangir",
            address="Main Road", contact_number="9876543210", contact_person_name="Dr. Example",
            email="usg@example.com", is_private="false"),
        row(record_type="delivery_point", name="CHC Agalpur", code="124", block_name="Agalpur",
            address="Hospital Road", contact_number="9876543210", contact_person_name="Dr. Example",
            is_sdh_dhh="false"),
        row(record_type="pmsma", name="PHC (N) Jogisarda", code="52554", block_name="Agalpur",
            address="Main Road", contact_number="9876543210", contact_person_name="Dr. Example"),
    ]


def build_template_xlsx(district_names, block_names) -> bytes:
    """Excel template with 2 sheets:
       Sheet 1 'Masters Data'  - header + sample rows (the sheet the user fills)
       Sheet 2 'Instructions'  - how to fill, record types, columns, hierarchy, reference lists.
    The upload reads the FIRST sheet only, so 'Masters Data' must stay first."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="F3E3D3")
    wrap = Alignment(wrap_text=True, vertical="top")

    wb = Workbook()

    # ---------------- Sheet 1: data ----------------
    ws = wb.active
    ws.title = "Masters Data"
    ws.append(TEMPLATE_COLUMNS)
    for c in ws[1]:
        c.font = bold
        c.fill = head_fill
    for r in template_rows():
        ws.append([r.get(col, "") for col in TEMPLATE_COLUMNS])
    # keep codes / phone numbers as TEXT so Excel does not turn 001 into 1
    text_cols = [i + 1 for i, col in enumerate(TEMPLATE_COLUMNS) if col in ("code", "contact_number")]
    for col_idx in text_cols:
        for row_idx in range(2, 502):
            ws.cell(row=row_idx, column=col_idx).number_format = "@"
    for i, col in enumerate(TEMPLATE_COLUMNS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(16, len(col) + 4)
    ws.freeze_panes = "A2"

    # ---------------- Sheet 2: instructions ----------------
    ins = wb.create_sheet("Instructions")

    def section(title):
        ins.append([])
        ins.append([title])
        ins.cell(row=ins.max_row, column=1).font = Font(bold=True, size=12)

    def table(headers, rows):
        ins.append(headers)
        for c in ins[ins.max_row]:
            if c.value is not None:
                c.font = bold
                c.fill = head_fill
        for r in rows:
            ins.append(r)
            for c in ins[ins.max_row]:
                c.alignment = wrap

    ins.append(["HOW TO FILL THE 'Masters Data' SHEET"])
    ins.cell(row=1, column=1).font = Font(bold=True, size=14)
    for line in [
        "1. One row = one record. Put the type of the row in the record_type column (see table below).",
        "2. The first 6 rows are SAMPLES. Delete them and enter your own data.",
        "3. You can put Blocks, Wards, Sub-Centres, USG, Delivery Points and PMSMA Centres in the SAME file.",
        "4. Names written in block_name / covered_blocks must match a block name exactly (spelling) - either "
        "an existing block (see list below) or a block that is also in this file.",
        "5. Code must be unique for each record. Keep code and contact_number as text (do not remove leading zeros).",
        "6. Do not rename or delete the header row. Do not add extra sheets before 'Masters Data'.",
        "7. Upload the file; a review screen shows what is new / existing / invalid before anything is saved.",
    ]:
        ins.append([line])

    section("RECORD TYPES (record_type column - write exactly one of these)")
    table(["record_type", "Creates", "Linked to (columns to fill)"], [
        ["block", "Block", "district_name (blank = your own district)"],
        ["ward", "Ward / Village", "block_name (required)"],
        ["sub_centre", "Sub-Centre", "block_name (required). Auto-linked to all wards of that block"],
        ["usg", "USG Centre", "district_name + covered_blocks (one or more blocks, comma separated)"],
        ["delivery_point", "Delivery Point", "district_name + block_name"],
        ["pmsma", "PMSMA Centre", "district_name + block_name"],
    ])

    section("COLUMNS")
    table(["Column", "Required", "Description", "Example"], [
        ["record_type", "Yes", "Type of the row (see table above)", "sub_centre"],
        ["name", "Yes", "Name of the block / ward / centre. Any text is allowed", "Pawan Nagar HSC"],
        ["name_regional", "No", "Name in the local language (optional)", ""],
        ["code", "Yes", "Unique code of the record. Cannot be changed later", "HSC001"],
        ["district_name", "No", "District. Leave blank to use your own district", "Balangir"],
        ["block_name", "Ward, Sub-Centre: Yes", "Block the record belongs to. Must match a block name exactly", "Agalpur"],
        ["covered_blocks", "USG only", "Blocks covered by the USG centre, comma separated, inside quotes", "\"Agalpur,Balangir\""],
        ["address", "No", "Address", "Main Road"],
        ["contact_number", "No", "10-digit mobile number", "9876543210"],
        ["contact_person_name", "No", "Contact person (USG, Delivery Point, PMSMA)", "Dr. Example"],
        ["email", "No", "Email (USG only)", "usg@example.com"],
        ["is_private", "No", "USG only: TRUE for private, FALSE for government", "FALSE"],
        ["is_sdh_dhh", "No", "Delivery Point only: TRUE if SDH/DHH", "FALSE"],
    ])

    section("HIERARCHY")
    for line in [
        "District -> Block -> Ward / Village and Sub-Centre (sub-centre is linked to its block and its wards).",
        "USG Centre covers one or more blocks. Delivery Point and PMSMA Centre belong to a block.",
        "Districts cannot be created from this file; the district must already exist.",
    ]:
        ins.append([line])

    section("DATABASE REFERENCE LISTS (copy exact names from here)")
    ins.append(["District Names", "Existing Block Names"])
    for c in ins[ins.max_row]:
        c.font = bold
        c.fill = head_fill
    n = max(len(district_names), len(block_names), 1)
    for i in range(n):
        ins.append([district_names[i] if i < len(district_names) else "",
                    block_names[i] if i < len(block_names) else ""])

    ins.column_dimensions["A"].width = 26
    ins.column_dimensions["B"].width = 34
    ins.column_dimensions["C"].width = 62
    ins.column_dimensions["D"].width = 22

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()