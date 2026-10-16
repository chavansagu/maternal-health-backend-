"""
Reader for the CLIENT's "User_template.xlsx" (Users page -> Bulk Upload).

One workbook, 4 sheets (one centre type per sheet). Every row can carry a user.
For Sub-centres the "Assigned ward/village" column lists the wards of that sub-centre,
one ward per row (the list is usually longer than the number of users).

    Subcenter : District | Block | Sub center     | Full Name | Mobile no | Email ID | User Name | Password | Assigned ward/village
    PMSMA     : District | Block | PMSMA Center    | Full Name | Mobile no | Email ID | User Name | Password
    Usg User  : District | Block | USG Center      | Full Name | Mobile no | Email ID | User Name | Password
    DP User   : District |         Delivery Point   | Full Name | Mobile no | Email ID | User Name | Password

This module ONLY reads + validates the file shape. It never touches the database.
Step 3 (DB matching / creation) consumes the `groups` returned by parse_client_workbook().

District / Block / Centre may be written only on the first row of a group; the blank
cells below are filled down (same as the client's sample).
"""
import io
import re

import pandas as pd

import admin_bulk_upload_service as base

MAX_ROWS_TOTAL = base.MAX_ROWS

# sheet (normalised name) -> config
SHEETS = {
    "subcenter":  {"type": "sub_centre",     "label": "Sub-Centre",     "block": True,  "wards": True},
    "pmsma":      {"type": "pmsma",          "label": "PMSMA Centre",   "block": True,  "wards": False},
    "usguser":    {"type": "usg",            "label": "USG Centre",     "block": True,  "wards": False},
    "dpuser":     {"type": "delivery_point", "label": "Delivery Point", "block": False, "wards": False},
}
_SHEET_ALIASES = {
    "subcentre": "subcenter", "subcenteruser": "subcenter", "subcentreuser": "subcenter",
    "usg": "usguser", "usgcentre": "usguser", "usgcenter": "usguser",
    "dp": "dpuser", "deliverypoint": "dpuser",
    "pmsmauser": "pmsma",
}

# normalised header -> canonical column
_HEADERS = {
    "district": "district", "districtname": "district",
    "block": "block", "blockname": "block",
    "subcenter": "center", "subcentre": "center", "subcentername": "center",
    "pmsmacenter": "center", "pmsmacentre": "center",
    "usgcenter": "center", "usgcentre": "center",
    "deliverypoint": "center", "dp": "center",
    "fullname": "full_name", "name": "full_name",
    "mobileno": "mobile", "mobile": "mobile", "mobilenumber": "mobile", "phone": "mobile",
    "emailid": "email", "email": "email",
    "username": "username", "userid": "username",
    "password": "password",
    "assignedwardvillage": "ward", "assignedward": "ward", "assignedvillage": "ward",
    "wardvillage": "ward", "ward": "ward", "village": "ward",
}
_REQUIRED_COLS = ["center", "full_name", "mobile", "username", "password"]
_USER_FIELDS = ["full_name", "mobile", "email", "username", "password"]
_FILL_DOWN = ["district", "block", "center"]

_SPECIAL = "!@#$%^&*()_+-=[]{}|;:,.<>?"


def _password_error(pw):
    """Same rules as schemas.UserCreate.validate_password (kept in sync on purpose)."""
    if len(pw) < 8:
        return "Password must be at least 8 characters long"
    if not any(c.isupper() for c in pw):
        return "Password must contain at least one uppercase letter"
    if not any(c.islower() for c in pw):
        return "Password must contain at least one lowercase letter"
    if not any(c.isdigit() for c in pw):
        return "Password must contain at least one number"
    if not any(c in _SPECIAL for c in pw):
        return "Password must contain at least one special character"
    return None


def _sheet_key(name):
    k = base._norm_header(name)
    return _SHEET_ALIASES.get(k, k)


def _read_workbook(filename, contents):
    if len(contents) > base.MAX_FILE_BYTES:
        raise ValueError(f"File is too large (max {base.MAX_FILE_BYTES // (1024 * 1024)} MB)")
    if not (filename or "").lower().endswith((".xlsx", ".xlsm", ".xls")):
        raise ValueError("Please upload the Excel template (.xlsx). CSV is not supported because the "
                         "template has more than one sheet.")
    try:
        return pd.read_excel(io.BytesIO(contents), sheet_name=None, dtype=str, keep_default_na=False)
    except Exception as e:
        raise ValueError(f"Could not read file: {e}")


def parse_client_workbook(filename, contents):
    """
    Returns {
      "groups": [ {type, label, sheet, district, block, center, row_numbers,
                   users:[{row_number, full_name, mobile, email, username, password}],
                   wards:[names]} ],
      "errors": [ {sheet, row_number, reason} ],      # row-level problems (row is skipped)
      "sheets_found": [...], "sheets_ignored": [...], "total_rows": n
    }
    Raises ValueError for file-level problems (wrong type, no known sheet, missing column, too big).
    """
    book = _read_workbook(filename, contents)

    groups, errors = {}, []
    seen_usernames, seen_emails = {}, {}
    found, ignored, total_rows = [], [], 0

    for sheet_name, df in book.items():
        cfg = SHEETS.get(_sheet_key(sheet_name))
        if not cfg:
            ignored.append(sheet_name)
            continue
        found.append(sheet_name)

        df = df.rename(columns={c: _HEADERS.get(base._norm_header(c), str(c).strip()) for c in df.columns})
        df = df.replace(r"^\s*$", pd.NA, regex=True)
        df = df.dropna(how="all", subset=[c for c in df.columns if c != "district"])
        missing = [c for c in _REQUIRED_COLS + (["block"] if cfg["block"] else []) if c not in df.columns]
        if cfg["wards"] and "ward" not in df.columns:
            missing.append("ward")
        if missing:
            nice = {"center": cfg["label"], "full_name": "Full Name", "mobile": "Mobile no", "username": "User Name",
                    "password": "Password", "block": "Block", "ward": "Assigned ward/village"}
            raise ValueError(f"Sheet '{sheet_name}': required column(s) missing: "
                             + ", ".join(nice[m] for m in missing) + ". Please use the downloaded template.")

        total_rows += len(df)
        if total_rows > MAX_ROWS_TOTAL:
            raise ValueError(f"Too many rows (maximum {MAX_ROWS_TOTAL} per upload)")

        # fill District / Block / Centre downwards (excel row number = index + 2)
        for col in _FILL_DOWN:
            if col in df.columns:
                df[col] = df[col].ffill()

        for idx, row in df.iterrows():
            rn = int(idx) + 2

            def err(reason, rn=rn, sheet_name=sheet_name):
                errors.append({"sheet": sheet_name, "row_number": rn, "reason": reason})

            district = base._clean_str(row.get("district"))
            block = base._clean_str(row.get("block"))
            center = base._clean_str(row.get("center"))
            ward = base._clean_str(row.get("ward")) if cfg["wards"] else None
            has_user = any(base._clean_str(row.get(f)) for f in _USER_FIELDS)

            if not has_user and not ward:
                continue
            if not center:
                err(f"{cfg['label']} name is required")
                continue
            if cfg["block"] and not block:
                err("Block is required")
                continue

            key = (cfg["type"], base._norm_name(block or ""), base._norm_name(center))
                  
            g = groups.setdefault(key, {
                "type": cfg["type"], "label": cfg["label"], "sheet": sheet_name,
                "district": district, "block": block, "center": center,
                "row_numbers": [], "users": [], "wards": [], "_ward_keys": set(),
            })
            g["row_numbers"].append(rn)

            if ward:
                wk = base._norm_name(ward)
                if wk not in g["_ward_keys"]:
                    g["_ward_keys"].add(wk)
                    g["wards"].append(ward)

            if not has_user:
                continue

            # ---- user fields ----
            full_name = base._clean_str(row.get("full_name"))
            username = base._clean_str(row.get("username"))
            password = base._clean_str(row.get("password"))
            email_raw = row.get("email")
            problems = []
            if not full_name:
                problems.append("Full Name is required")
            if not username:
                problems.append("User Name is required")
            elif not re.match(r"^[A-Za-z0-9_.\-@]+$", username):
                problems.append("User Name may only contain letters, numbers and _ . - @")
            if not password:
                problems.append("Password is required")
            else:
                pe = _password_error(password)
                if pe:
                    problems.append(pe)
            mobile = email = None
            try:
                mobile = base._parse_phone(row.get("mobile"), "Mobile no")
                if not mobile:
                    problems.append("Mobile no is required")
            except ValueError as e:
                problems.append(str(e))
            try:
                email = base._parse_email(email_raw, "Email ID")
            except ValueError as e:
                problems.append(str(e))

            if username:
                u = username.lower()
                if u in seen_usernames:
                    problems.append(f"User Name '{username}' is repeated (also in {seen_usernames[u]})")
                else:
                    seen_usernames[u] = f"sheet '{sheet_name}' row {rn}"
            if email:
                em = email.lower()
                if em in seen_emails:
                    problems.append(f"Email '{email}' is repeated (also in {seen_emails[em]})")
                else:
                    seen_emails[em] = f"sheet '{sheet_name}' row {rn}"

            if problems:
                err("; ".join(problems))
                continue
            g["users"].append({"row_number": rn, "full_name": full_name, "mobile": mobile,
                               "email": email, "username": username, "password": password})

    if not found:
        raise ValueError("No known sheet found. Expected sheets: Subcenter, PMSMA, Usg User, DP User")

    out_groups = []
    for g in groups.values():
        g.pop("_ward_keys", None)
        out_groups.append(g)
    errors.sort(key=lambda e: (e["sheet"], e["row_number"]))
    return {"groups": out_groups, "errors": errors, "sheets_found": found,
            "sheets_ignored": ignored, "total_rows": total_rows}