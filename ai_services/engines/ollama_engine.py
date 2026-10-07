"""
Ollama AI Engine - Local CPU-based Text-to-SQL
Runs entirely on your server CPU, no API costs
"""
import os
import json
import time
import requests
from typing import Dict, Optional
from dotenv import load_dotenv

load_dotenv()

from ..base_engine import BaseAIEngine
from ..ai_logger import logger as ai_logger


class OllamaEngine(BaseAIEngine):
    """Ollama Local AI Engine - Runs on CPU"""

    def __init__(self):
        super().__init__()
        self.name = "ollama"
        self.host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.model = os.getenv("OLLAMA_MODEL", "llama3")
        self.timeout = int(os.getenv("OLLAMA_TIMEOUT", "30"))
        self.enabled = self._check_availability()

    def _check_availability(self) -> bool:
        """Check if Ollama is running and model is available"""
        try:
            response = requests.get(f"{self.host}/api/tags", timeout=5)
            if response.status_code == 200:
                models = response.json().get("models", [])
                return any(self.model in m.get("name", "") for m in models)
        except Exception:
            pass
        return False

    def generate_sql(
        self,
        question: str,
        schema: Dict,
        user_role: str,
        user_context: Dict,
        request_id: Optional[str] = None
    ) -> str:
        """Generate SQL using Ollama local model"""

        if not self.enabled:
            raise RuntimeError("Ollama is not available. Make sure Ollama is running and model is downloaded.")

        # ── Prompt build ──────────────────────────────────────────────────
        t_prompt = time.perf_counter()
        prompt = self._build_prompt(question, schema, user_role, user_context)
        prompt_ms = int((time.perf_counter() - t_prompt) * 1000)
        prompt_chars = len(prompt)

        ai_logger.info(
            f"[AI-REPORT] [request_id={request_id or 'n/a'}] [step=PROMPT_BUILD] "
            f"[status=SUCCESS] [time_ms={prompt_ms}] "
            f"[prompt_chars={prompt_chars}] [model={self.model}]"
        )

        # ── HTTP call to Ollama ───────────────────────────────────────────
        t_http = time.perf_counter()
        try:
            response = requests.post(
                f"{self.host}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "top_p": 0.9
                    }
                },
                timeout=self.timeout
            )

            if response.status_code != 200:
                raise RuntimeError(f"Ollama API error: {response.status_code}")

            http_ms = int((time.perf_counter() - t_http) * 1000)

            # ── Response parsing ──────────────────────────────────────────
            t_parse = time.perf_counter()
            result = response.json()
            raw_response = result.get("response", "")
            sql = self._extract_sql(raw_response)
            parse_ms = int((time.perf_counter() - t_parse) * 1000)

            ai_logger.info(
                f"[AI-REPORT] [request_id={request_id or 'n/a'}] [step=OLLAMA_HTTP] "
                f"[status=SUCCESS] [time_ms={http_ms}] "
                f"[response_chars={len(raw_response)}] [sql_chars={len(sql)}] "
                f"[parse_ms={parse_ms}]"
            )

            return sql

        except requests.Timeout:
            http_ms = int((time.perf_counter() - t_http) * 1000)
            ai_logger.error(
                f"[AI-REPORT] [request_id={request_id or 'n/a'}] [step=OLLAMA_HTTP] "
                f"[status=TIMEOUT] [time_ms={http_ms}] [timeout_sec={self.timeout}]"
            )
            raise RuntimeError("Ollama request timeout. Query too complex or server overloaded.")
        except Exception as e:
            http_ms = int((time.perf_counter() - t_http) * 1000)
            ai_logger.error(
                f"[AI-REPORT] [request_id={request_id or 'n/a'}] [step=OLLAMA_HTTP] "
                f"[status=ERROR] [time_ms={http_ms}] [error={type(e).__name__}: {e}]"
            )
            raise RuntimeError(f"Ollama error: {str(e)}")

    def _build_prompt(
        self,
        question: str,
        schema: Dict,
        user_role: str,
        user_context: Dict
    ) -> str:
        """Build prompt for Ollama"""

        schema_text = self._format_schema(schema)
        important_notes = "\n".join(schema.get("important_notes", []))

        prompt = f"""You are a SQL expert. Convert the natural language question to a MySQL SELECT query.

DATABASE SCHEMA:
{schema_text}

IMPORTANT NOTES:
{important_notes}

USER ROLE: {user_role}

CRITICAL RULES - READ CAREFULLY:
1. Generate ONLY SELECT queries
2. ALWAYS use table aliases (e.g., pw for pregnant_women, b for blocks, dp for delivery_points, dout for delivery_outcomes, dr for delivery_referrals, dob for delivery_outcome_babies)
3. Use ONLY columns that exist in the schema above - DO NOT invent columns
4. NEVER use district_id on usg_appointments or anc_visits tables - they don't have it
5. NEVER use district_id or block_id on delivery_outcomes or delivery_referrals - they don't have it
6. To filter usg_appointments by district: JOIN pregnant_women first
7. To filter anc_visits by district: JOIN pregnant_women first
8. To filter delivery_outcomes by district: JOIN delivery_points dp ON dout.dp_id = dp.id WHERE dp.district_id = X
9. To filter delivery_referrals by district: JOIN delivery_points dp ON dr.dp_id = dp.id WHERE dp.district_id = X
10. When multiple tables have district_id, specify which: pw.district_id NOT just district_id
11. Use proper JOIN syntax: JOIN table_name alias ON alias.column = other.column
12. Return ONLY the SQL query, no explanations
13. Use MySQL syntax (CURDATE(), MONTH(), YEAR(), DATE_FORMAT())
14. For GROUP BY queries, include all non-aggregate columns in GROUP BY
15. NEVER use columns that don't exist: check schema carefully
16. In subqueries: ALWAYS use the SAME alias in outer query as you defined
    Example: SELECT AVG(sub.count) FROM (...) AS sub  -- alias matches!
    WRONG: SELECT AVG(x.count) FROM (...) AS sub  -- aliases don't match!
17. For delivery counts per DP: use delivery_outcomes.dp_id directly — no need to join delivery_referrals
18. For baby-level stats (gender, baby status): use delivery_outcome_babies joined to delivery_outcomes
19. ALWAYS filter delivery_points.is_active = 1 unless asked for inactive DPs
20. GEOGRAPHIC NAME MATCHING: When the user mentions a block, ward, district, sub-centre, or delivery point by name, ALWAYS match against the 'name' column using LOWER(column) LIKE LOWER('%value%'). NEVER match geographic names against the 'code' column. Example: "GOP block" → LOWER(b.name) LIKE LOWER('%GOP%'), NOT b.code = 'GOP'
21. ALWAYS JOIN a table before using its alias in WHERE. If you reference b.name in WHERE, you MUST have JOIN blocks b in the FROM clause. If you reference d.name in WHERE, you MUST have JOIN districts d. NEVER use an alias that has no corresponding JOIN.
22. When the user says "in [city/district name]" for delivery points (e.g. "DPs in Puri"), join the districts table: JOIN districts d ON dp.district_id = d.id WHERE LOWER(d.name) LIKE LOWER('%Puri%'). Do NOT use b.name for district-level filtering.

COMMON MISTAKES TO AVOID:
- NEVER use 'do' as alias for delivery_outcomes — 'DO' is a reserved MySQL keyword and will cause errors. Always use 'dout'
- Don't use 'district_id' on usg_appointments, anc_visits, delivery_outcomes, or delivery_referrals
- Don't use 'mobile_number' on sub_centres (doesn't exist)
- Don't use ambiguous column names without table prefix
- Don't use 'sub_centre_id' on grievances (doesn't exist)
- To count ANC visits per woman: COUNT(av.id) grouped by pw.id
- NEVER use AVG(COUNT(...)) - use subquery instead: SELECT AVG(cnt) FROM (SELECT COUNT(*) as cnt ... GROUP BY ...) sub
- In subqueries: use subquery alias, NOT inner table alias (sub.count NOT av.count)
- In subqueries: outer query alias MUST MATCH the AS alias (if AS sub, use sub.column)
- For re-referral rate: COUNT WHERE dr.status = 're_referred' divided by total referrals
- delivery_outcome_babies.status values: live_birth, still_birth, infant_death
- delivery_outcomes.delivery_type values: safe_delivery, live_birth, still_birth, infant_death, maternal_death

EXAMPLES:
Q: "How many pregnant women?"
A: SELECT COUNT(*) FROM pregnant_women pw

Q: "USG appointments this month"
A: SELECT COUNT(*) FROM usg_appointments ua JOIN pregnant_women pw ON ua.pregnant_woman_id = pw.id WHERE MONTH(ua.scheduled_date) = MONTH(CURDATE())

Q: "Pregnant women by block"
A: SELECT b.name, COUNT(pw.id) FROM pregnant_women pw JOIN blocks b ON pw.block_id = b.id GROUP BY b.name

Q: "Count ANC visits by block"
A: SELECT b.name, COUNT(av.id) FROM anc_visits av JOIN pregnant_women pw ON av.pregnant_woman_id = pw.id JOIN blocks b ON pw.block_id = b.id GROUP BY b.name

Q: "Total deliveries per delivery point"
A: SELECT dp.name, COUNT(dout.id) as total_deliveries FROM delivery_outcomes dout JOIN delivery_points dp ON dout.dp_id = dp.id WHERE dp.is_active = 1 GROUP BY dp.id, dp.name ORDER BY total_deliveries DESC

Q: "Maternal deaths by block"
A: SELECT b.name, COUNT(dout.id) as maternal_deaths FROM delivery_outcomes dout JOIN delivery_points dp ON dout.dp_id = dp.id JOIN blocks b ON dp.block_id = b.id WHERE dout.delivery_type = 'maternal_death' GROUP BY b.id, b.name

Q: "Baby gender distribution"
A: SELECT dob.gender, COUNT(dob.id) as count FROM delivery_outcome_babies dob GROUP BY dob.gender

Q: "Monthly delivery trends"
A: SELECT DATE_FORMAT(dout.delivery_date, '%Y-%m') as month, COUNT(dout.id) as deliveries FROM delivery_outcomes dout GROUP BY month ORDER BY month

Q: "Still births and infant deaths per delivery point"
A: SELECT dp.name, SUM(CASE WHEN dout.delivery_type = 'still_birth' THEN 1 ELSE 0 END) as still_births, SUM(CASE WHEN dout.delivery_type = 'infant_death' THEN 1 ELSE 0 END) as infant_deaths FROM delivery_outcomes dout JOIN delivery_points dp ON dout.dp_id = dp.id WHERE dp.is_active = 1 GROUP BY dp.id, dp.name

Q: "Average ANC visits per pregnant woman"
A: SELECT AVG(sub.visit_count) as avg_visits FROM (SELECT pw.id, COUNT(av.id) as visit_count FROM pregnant_women pw LEFT JOIN anc_visits av ON pw.id = av.pregnant_woman_id GROUP BY pw.id) as sub

Q: "Total high-risk pregnant women in GOP block"
A: SELECT COUNT(pw.id) as total_high_risk FROM pregnant_women pw JOIN blocks b ON pw.block_id = b.id WHERE LOWER(b.name) LIKE LOWER('%GOP%') AND pw.is_high_risk = 1

Q: "Pregnant women in Nashik district"
A: SELECT COUNT(pw.id) as total FROM pregnant_women pw JOIN districts d ON pw.district_id = d.id WHERE LOWER(d.name) LIKE LOWER('%Nashik%')

Q: "Delivery points in Nashik block"
A: SELECT COUNT(dp.id) as total_dps FROM delivery_points dp JOIN blocks b ON dp.block_id = b.id WHERE LOWER(b.name) LIKE LOWER('%Nashik%') AND dp.is_active = 1

QUESTION: {question}

SQL QUERY (return only the query, nothing else):"""

        return prompt

    def _format_schema(self, schema: Dict) -> str:
        """Format schema for prompt - emphasize which tables have district_id"""
        lines = []
        for table, info in schema.get("tables", {}).items():
            columns = info.get("columns", [])
            desc = info.get("description", "")

            lines.append(f"Table: {table}")
            lines.append(f"  Description: {desc}")

            if "district_id" in columns:
                lines.append(f"  \u2705 HAS district_id column")
            else:
                lines.append(f"  \u274c NO district_id column - must JOIN other tables")

            lines.append(f"  Columns: {', '.join(columns)}")

            for col, vals in info.get("enum_values", {}).items():
                lines.append(f"  ENUM {col}: {vals}")

            if info.get("important"):
                lines.append(f"  NOTE: {info['important']}")

            lines.append("")
        return "\n".join(lines)

    def _extract_sql(self, response: str) -> str:
        """Extract SQL from Ollama response"""
        import re

        sql = re.sub(r'```sql\n?', '', response)
        sql = re.sub(r'```\n?', '', sql)

        lines = sql.split('\n')
        sql_lines = []
        in_query = False

        for line in lines:
            line_stripped = line.strip()
            if line_stripped.upper().startswith('SELECT'):
                in_query = True
            if in_query:
                sql_lines.append(line)
                if ';' in line:
                    break

        result = '\n'.join(sql_lines).strip().rstrip(';')
        return result if result else sql.strip()

    def is_available(self) -> bool:
        """Check if Ollama is available"""
        return self.enabled

    def get_config(self) -> Dict:
        """Get engine configuration"""
        return {
            "name": self.name,
            "model": self.model,
            "host": self.host,
            "enabled": self.enabled,
            "requires_api_key": False,
            "runs_locally": True,
            "timeout": self.timeout
        }
