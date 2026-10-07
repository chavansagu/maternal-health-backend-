"""
SQL Validator - Security validation for AI-generated SQL queries
"""
import re
import sqlparse
from typing import Tuple
from .schema_manager import get_allowed_tables, get_blocked_keywords

class SQLValidator:
    """Validates SQL queries for security"""
    
    def __init__(self):
        self.allowed_tables = get_allowed_tables()
        self.blocked_keywords = get_blocked_keywords()
    
    def validate(self, sql: str) -> Tuple[bool, str]:
        """
        Validate SQL query for security
        
        Returns:
            (is_valid, error_message)
        """
        if not sql or not sql.strip():
            return False, "Empty SQL query"
        
        sql_upper = sql.upper()
        
        # Check 1: Must be SELECT only
        if not sql_upper.strip().startswith('SELECT'):
            return False, "Only SELECT queries are allowed"
        
        # Check 2: Block dangerous keywords
        for keyword in self.blocked_keywords:
            if keyword in sql_upper:
                return False, f"Blocked keyword detected: {keyword}"
        
        # Check 3: Block nested aggregate functions (AVG(COUNT(...)))
        if 'AVG(COUNT' in sql_upper or 'SUM(COUNT' in sql_upper or 'COUNT(COUNT' in sql_upper:
            return False, "Nested aggregate functions not allowed. Use subquery: SELECT AVG(cnt) FROM (SELECT COUNT(*) as cnt ... GROUP BY ...) sub"
        
        # Check 4: Validate table names
        tables_in_query = self._extract_tables(sql)
        for table in tables_in_query:
            if table not in self.allowed_tables:
                return False, f"Table not allowed: {table}"
        
        # Check 5: Must have at least one table
        if not tables_in_query:
            return False, "No valid tables found in query"

        # Check 6: Validate all aliases used in WHERE/JOIN/ON/HAVING are defined
        alias_error = self._validate_aliases(sql)
        if alias_error:
            return False, alias_error

        return True, "Valid"
    
    def _validate_aliases(self, sql: str) -> str:
        """
        Check that every alias used in WHERE/ON/HAVING/SELECT clauses
        is actually defined in FROM/JOIN.
        Returns error string if invalid alias found, else None.
        """
        sql_lower = sql.lower()

        # Extract defined aliases: FROM table alias, JOIN table alias
        defined_aliases = set()

        # Match: FROM table_name alias  OR  FROM table_name AS alias
        for m in re.finditer(r'from\s+(\w+)(?:\s+as)?\s+(\w+)', sql_lower):
            table, alias = m.group(1), m.group(2)
            # skip SQL keywords that appear after table name (WHERE, WHERE, etc.)
            if alias not in ('where', 'join', 'on', 'group', 'order', 'having', 'limit', 'inner', 'left', 'right', 'outer'):
                defined_aliases.add(alias)
                defined_aliases.add(table)  # table name itself is also valid

        # Match: JOIN table_name alias  OR  JOIN table_name AS alias
        for m in re.finditer(r'join\s+(\w+)(?:\s+as)?\s+(\w+)', sql_lower):
            table, alias = m.group(1), m.group(2)
            if alias not in ('on', 'where', 'inner', 'left', 'right', 'outer'):
                defined_aliases.add(alias)
                defined_aliases.add(table)

        # Also add bare table names (no alias) from FROM/JOIN
        for m in re.finditer(r'(?:from|join)\s+(\w+)(?:\s*(?:where|on|group|order|limit|;|$))', sql_lower):
            defined_aliases.add(m.group(1))

        if not defined_aliases:
            return None  # can't determine aliases, skip check

        # Find all alias.column patterns used in the query
        used_aliases = set(re.findall(r'(\w+)\.\w+', sql_lower))

        # Check each used alias is defined
        for alias in used_aliases:
            # skip subquery aliases and SQL functions
            if alias in ('sub', 'subquery', 'sq', 't', 't1', 't2'):
                continue
            if alias not in defined_aliases:
                return (
                    f"Undefined alias '{alias}' used in query. "
                    f"Defined aliases are: {sorted(defined_aliases)}. "
                    f"Please use the correct alias."
                )
        return None

    def _extract_tables(self, sql: str) -> list:
        """Extract table names from SQL query"""
        tables = []
        sql_lower = sql.lower()
        
        # Simple regex to find table names after FROM and JOIN
        from_pattern = r'from\s+(\w+)'
        join_pattern = r'join\s+(\w+)'
        
        from_matches = re.findall(from_pattern, sql_lower)
        join_matches = re.findall(join_pattern, sql_lower)
        
        tables.extend(from_matches)
        tables.extend(join_matches)
        
        return list(set(tables))
    
    def clean_sql(self, sql: str) -> str:
        """Clean and format SQL query"""
        # Remove markdown code blocks if present
        sql = re.sub(r'```sql\n?', '', sql)
        sql = re.sub(r'```\n?', '', sql)
        
        # Format SQL
        try:
            formatted = sqlparse.format(
                sql, 
                reindent=True, 
                keyword_case='upper'
            )
            return formatted.strip()
        except:
            return sql.strip()
