"""
Query Optimizer - Adds role-based filters to SQL queries
"""
import re
from typing import Dict

# Tables that have district_id as a direct column
_DIRECT_DISTRICT_TABLES = ["pregnant_women", "blocks", "districts", "usg_centres", "delivery_points"]

# Tables that have block_id as a direct column
_DIRECT_BLOCK_TABLES = ["pregnant_women", "blocks", "wards", "sub_centres", "usg_centres", "delivery_points"]

# Delivery tables that have NO geographic columns — must scope via delivery_points
_DELIVERY_TABLES_NO_GEO = ["delivery_outcomes", "delivery_referrals", "delivery_outcome_babies"]

# Known alias → table mapping used by the sanitizer
_ALIAS_TABLE_MAP = {
    "pw": "pregnant_women",
    "b": "blocks",
    "d": "districts",
    "w": "wards",
    "sc": "sub_centres",
    "dp": "delivery_points",
    "dout": "delivery_outcomes",
    "dr": "delivery_referrals",
    "dob": "delivery_outcome_babies",
    "ua": "usg_appointments",
    "uc": "usg_centres",
    "av": "anc_visits",
    "g": "grievances",
}


class QueryOptimizer:
    """Optimizes queries by adding role-based filters"""

    def add_role_filters(self, sql: str, user_role: str, user_context: Dict) -> str:
        """
        Add role-based WHERE clauses to SQL query

        Args:
            sql: Original SQL query
            user_role: district, block, sub_centre
            user_context: User's district_id, block_id, etc.
        """
        # First sanitize dangling aliases (safety net for AI errors)
        sql = self.sanitize_dangling_aliases(sql)
        
        if user_role == "district":
            return self._add_district_filter(sql, user_context.get("district_id"))
        elif user_role == "block":
            return self._add_block_filter(sql, user_context.get("block_id"))
        elif user_role == "sub_centre":
            return self._add_subcentre_filter(sql, user_context.get("sub_centre_id"))

        return sql

    # ── District filter ──────────────────────────────────────────────────────

    def _add_district_filter(self, sql: str, district_id: int) -> str:
        """Add district_id filter with smart table detection"""
        if not district_id:
            return sql

        sql_lower = sql.lower()
        filter_clause = None

        # Check if pregnant_women is in the query (most common)
        pw_alias = self._extract_table_alias(sql, "pregnant_women")
        if pw_alias or "pregnant_women" in sql_lower:
            alias = pw_alias if pw_alias else "pregnant_women"
            filter_clause = f"{alias}.district_id = {district_id}"

        # Check for delivery_outcomes or delivery_referrals — scope via delivery_points
        elif any(t in sql_lower for t in ["delivery_outcomes", "delivery_referrals", "delivery_outcome_babies"]):
            dp_alias = self._extract_table_alias(sql, "delivery_points")
            if dp_alias or "delivery_points" in sql_lower:
                # delivery_points is already joined — use its alias
                alias = dp_alias if dp_alias else "delivery_points"
                filter_clause = f"{alias}.district_id = {district_id}"
            else:
                # delivery_points not joined — cannot safely inject filter, return as-is
                # The AI should have joined delivery_points per schema instructions
                return sql

        # Check for blocks table
        elif "blocks" in sql_lower:
            b_alias = self._extract_table_alias(sql, "blocks")
            alias = b_alias if b_alias else "blocks"
            filter_clause = f"{alias}.district_id = {district_id}"

        # Check for districts table
        elif "districts" in sql_lower:
            d_alias = self._extract_table_alias(sql, "districts")
            alias = d_alias if d_alias else "districts"
            filter_clause = f"{alias}.id = {district_id}"

        # Check for usg_centres table
        elif "usg_centres" in sql_lower:
            u_alias = self._extract_table_alias(sql, "usg_centres")
            alias = u_alias if u_alias else "usg_centres"
            filter_clause = f"{alias}.district_id = {district_id}"

        # Check for delivery_points directly (e.g. "show all DPs in district")
        elif "delivery_points" in sql_lower:
            dp_alias = self._extract_table_alias(sql, "delivery_points")
            alias = dp_alias if dp_alias else "delivery_points"
            filter_clause = f"{alias}.district_id = {district_id}"

        # No direct table found — do not inject (avoid ambiguity)
        if not filter_clause:
            return sql

        # Guard: do not inject if this exact filter already exists
        if f"district_id = {district_id}" in sql:
            return sql

        return self._inject_where(sql, filter_clause)

    # ── Block filter ─────────────────────────────────────────────────────────

    def _add_block_filter(self, sql: str, block_id: int) -> str:
        """
        Add block_id filter with qualified alias.
        FIX: Previously injected bare 'block_id = X' which caused ambiguous column
        errors in multi-table JOINs. Now always uses a qualified alias.
        """
        if not block_id:
            return sql

        sql_lower = sql.lower()
        filter_clause = None

        # pregnant_women has block_id directly
        pw_alias = self._extract_table_alias(sql, "pregnant_women")
        if pw_alias or "pregnant_women" in sql_lower:
            alias = pw_alias if pw_alias else "pregnant_women"
            filter_clause = f"{alias}.block_id = {block_id}"

        # delivery_outcomes / delivery_referrals — scope via delivery_points
        elif any(t in sql_lower for t in ["delivery_outcomes", "delivery_referrals", "delivery_outcome_babies"]):
            dp_alias = self._extract_table_alias(sql, "delivery_points")
            if dp_alias or "delivery_points" in sql_lower:
                alias = dp_alias if dp_alias else "delivery_points"
                filter_clause = f"{alias}.block_id = {block_id}"
            else:
                return sql

        # blocks table
        elif "blocks" in sql_lower:
            b_alias = self._extract_table_alias(sql, "blocks")
            alias = b_alias if b_alias else "blocks"
            filter_clause = f"{alias}.id = {block_id}"

        # sub_centres has block_id
        elif "sub_centres" in sql_lower:
            sc_alias = self._extract_table_alias(sql, "sub_centres")
            alias = sc_alias if sc_alias else "sub_centres"
            filter_clause = f"{alias}.block_id = {block_id}"

        # wards has block_id
        elif "wards" in sql_lower:
            w_alias = self._extract_table_alias(sql, "wards")
            alias = w_alias if w_alias else "wards"
            filter_clause = f"{alias}.block_id = {block_id}"

        # delivery_points has block_id directly
        elif "delivery_points" in sql_lower:
            dp_alias = self._extract_table_alias(sql, "delivery_points")
            alias = dp_alias if dp_alias else "delivery_points"
            filter_clause = f"{alias}.block_id = {block_id}"

        # usg_centres has block_id
        elif "usg_centres" in sql_lower:
            u_alias = self._extract_table_alias(sql, "usg_centres")
            alias = u_alias if u_alias else "usg_centres"
            filter_clause = f"{alias}.block_id = {block_id}"

        if not filter_clause:
            return sql

        # Guard: do not inject if this exact filter already exists
        if f"block_id = {block_id}" in sql:
            return sql

        return self._inject_where(sql, filter_clause)

    # ── Sub-centre filter ────────────────────────────────────────────────────

    def _add_subcentre_filter(self, sql: str, sub_centre_id: int) -> str:
        """
        Add sub_centre_id filter with qualified alias.
        FIX: Previously injected bare 'sub_centre_id = X' causing ambiguous column errors.
        """
        if not sub_centre_id:
            return sql

        sql_lower = sql.lower()
        filter_clause = None

        # pregnant_women has sub_centre_id
        pw_alias = self._extract_table_alias(sql, "pregnant_women")
        if pw_alias or "pregnant_women" in sql_lower:
            alias = pw_alias if pw_alias else "pregnant_women"
            filter_clause = f"{alias}.sub_centre_id = {sub_centre_id}"

        # delivery_referrals has sub_centre_id directly
        elif "delivery_referrals" in sql_lower:
            dr_alias = self._extract_table_alias(sql, "delivery_referrals")
            alias = dr_alias if dr_alias else "delivery_referrals"
            filter_clause = f"{alias}.sub_centre_id = {sub_centre_id}"

        # sub_centres table itself
        elif "sub_centres" in sql_lower:
            sc_alias = self._extract_table_alias(sql, "sub_centres")
            alias = sc_alias if sc_alias else "sub_centres"
            filter_clause = f"{alias}.id = {sub_centre_id}"

        if not filter_clause:
            return sql

        # Guard: do not inject if this exact filter already exists
        if f"sub_centre_id = {sub_centre_id}" in sql:
            return sql

        return self._inject_where(sql, filter_clause)

    # ── Shared helpers ───────────────────────────────────────────────────────

    def sanitize_dangling_aliases(self, sql: str) -> str:
        """
        Remove WHERE/HAVING conditions that reference an alias not present
        in any FROM or JOIN clause.

        Example: WHERE ... AND LOWER(b.name) LIKE '%puri%'
        If 'b' has no JOIN blocks b, this condition is stripped to prevent
        MySQL error 1054 (Unknown column).

        Only strips conditions for known aliases in _ALIAS_TABLE_MAP.
        Unknown aliases are left untouched (conservative approach).
        """
        # Collect all aliases actually defined in FROM/JOIN
        defined_aliases = set()
        for alias in _ALIAS_TABLE_MAP:
            table = _ALIAS_TABLE_MAP[alias]
            pattern = rf'\b(?:FROM|JOIN)\s+{re.escape(table)}\s+(?:AS\s+)?{re.escape(alias)}\b'
            if re.search(pattern, sql, re.IGNORECASE):
                defined_aliases.add(alias)
            # Also catch unaliased table references (table used directly)
            pattern_no_alias = rf'\b(?:FROM|JOIN)\s+{re.escape(table)}\b'
            if re.search(pattern_no_alias, sql, re.IGNORECASE):
                defined_aliases.add(table)  # table name itself is valid

        # Find all alias prefixes used in WHERE/HAVING conditions: alias.column
        used_aliases = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\.\w+', sql))

        # Determine which known aliases are used but NOT defined
        dangling = {
            a for a in used_aliases
            if a in _ALIAS_TABLE_MAP and a not in defined_aliases
        }

        if not dangling:
            return sql

        # Strip each AND/OR condition that contains a dangling alias reference.
        # Pattern: (AND|OR) <condition containing dangling_alias.anything>
        # Also handles the case where the dangling condition is the first condition
        # after WHERE (no leading AND/OR).
        for alias in dangling:
            # Remove: AND <expr containing alias.col>
            sql = re.sub(
                rf'\s+AND\s+[^\n]*\b{re.escape(alias)}\.[^\s,)\n]+[^\n]*',
                '',
                sql,
                flags=re.IGNORECASE
            )
            # Remove: OR <expr containing alias.col>
            sql = re.sub(
                rf'\s+OR\s+[^\n]*\b{re.escape(alias)}\.[^\s,)\n]+[^\n]*',
                '',
                sql,
                flags=re.IGNORECASE
            )
            # Remove: WHERE <expr containing alias.col> (first condition, no AND/OR prefix)
            sql = re.sub(
                rf'(\bWHERE\b\s+)[^\n]*\b{re.escape(alias)}\.[^\s,)\n]+[^\n]*\s+AND\s+',
                r'\1',
                sql,
                flags=re.IGNORECASE
            )
            sql = re.sub(
                rf'(\bWHERE\b\s+)[^\n]*\b{re.escape(alias)}\.[^\s,)\n]+[^\n]*$',
                '',
                sql,
                flags=re.IGNORECASE | re.MULTILINE
            )

        # Clean up any orphaned WHERE with nothing after it
        sql = re.sub(r'\bWHERE\s*$', '', sql, flags=re.IGNORECASE | re.MULTILINE)
        sql = re.sub(r'\bWHERE\s+(AND|OR)\b', 'WHERE', sql, flags=re.IGNORECASE)

        return sql.strip()

    def _inject_where(self, sql: str, filter_clause: str) -> str:
        """Inject a WHERE condition into SQL — shared by all filter methods."""
        if re.search(r'\bWHERE\b', sql, re.IGNORECASE):
            # Add to existing WHERE clause
            sql = re.sub(
                r'(\bWHERE\b)',
                f'\\1 {filter_clause} AND',
                sql,
                count=1,
                flags=re.IGNORECASE
            )
        else:
            # Add new WHERE before GROUP BY, ORDER BY, or LIMIT
            insert_before = r'(\b(?:GROUP BY|ORDER BY|LIMIT)\b)'
            if re.search(insert_before, sql, re.IGNORECASE):
                sql = re.sub(
                    insert_before,
                    f'WHERE {filter_clause} \\1',
                    sql,
                    count=1,
                    flags=re.IGNORECASE
                )
            else:
                sql = sql.rstrip(';') + f' WHERE {filter_clause}'
        return sql

    def _extract_table_alias(self, sql: str, table_name: str) -> str:
        """Extract alias for a table from SQL query"""
        patterns = [
            rf'\b(?:FROM|JOIN)\s+{table_name}\s+(?:AS\s+)?(\w+)\b',
            rf'\b(?:FROM|JOIN)\s+{table_name}\s+(\w+)\s*(?:ON|WHERE|JOIN|GROUP|ORDER|LIMIT|$)',
        ]

        for pattern in patterns:
            match = re.search(pattern, sql, re.IGNORECASE)
            if match:
                alias = match.group(1)
                if alias.upper() not in ['ON', 'WHERE', 'JOIN', 'INNER', 'LEFT', 'RIGHT', 'GROUP', 'ORDER', 'LIMIT', 'AS']:
                    return alias

        return None
