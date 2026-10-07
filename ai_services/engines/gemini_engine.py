"""
Google Gemini AI Engine - Text-to-SQL using Gemini API
"""
import os
import json
from typing import Dict
from dotenv import load_dotenv

load_dotenv()

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from ..base_engine import BaseAIEngine

class GeminiEngine(BaseAIEngine):
    """Google Gemini AI Engine for Text-to-SQL"""
    
    def __init__(self):
        super().__init__()
        self.name = "gemini"
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-pro")
        self.temperature = float(os.getenv("GEMINI_TEMPERATURE", "0.1"))
        self.max_tokens = int(os.getenv("GEMINI_MAX_TOKENS", "1000"))
        
        if GEMINI_AVAILABLE and self.api_key and self.api_key != "your_gemini_api_key_here":
            try:
                genai.configure(api_key=self.api_key)
                self.model = genai.GenerativeModel(self.model_name)
                self.enabled = True
            except Exception as e:
                print(f"Gemini initialization error: {e}")
                self.enabled = False
        else:
            self.enabled = False
    
    def generate_sql(
        self, 
        question: str, 
        schema: Dict, 
        user_role: str,
        user_context: Dict
    ) -> str:
        """Generate SQL from natural language using Gemini"""
        
        if not self.enabled:
            raise RuntimeError("Gemini engine is not available")
        
        prompt = self._build_prompt(question, schema, user_role, user_context)
        
        try:
            response = self.model.generate_content(
                prompt,
                generation_config={
                    "temperature": self.temperature,
                    "max_output_tokens": self.max_tokens,
                }
            )
            
            sql = self._extract_sql(response.text)
            return sql
            
        except Exception as e:
            raise RuntimeError(f"Gemini API error: {str(e)}")
    
    def _build_prompt(
        self,
        question: str,
        schema: Dict,
        user_role: str,
        user_context: Dict
    ) -> str:
        """Build prompt for Gemini"""

        schema_text = self._format_schema(schema)
        important_notes = "\n".join(schema.get("important_notes", []))

        prompt = f"""You are a SQL expert. Convert the natural language question to a MySQL SELECT query.

DATABASE SCHEMA:
{schema_text}

IMPORTANT NOTES:
{important_notes}

USER ROLE: {user_role}
USER CONTEXT: {json.dumps(user_context)}

RULES:
1. Generate ONLY SELECT queries
2. Use proper JOINs when needed
3. ALWAYS use table aliases (pw, b, dp, dout, dr, dob, etc.)
4. Return ONLY the SQL query, no explanations
5. Do not add role-based filters (they will be added automatically)
6. Use MySQL syntax
7. Format dates as YYYY-MM-DD
8. Use COUNT(*) for counting
9. Use proper GROUP BY when using aggregations
10. NEVER use district_id on usg_appointments, anc_visits, delivery_outcomes, or delivery_referrals
11. To filter delivery_outcomes by district: JOIN delivery_points dp ON dout.dp_id = dp.id
12. To filter delivery_referrals by district: JOIN delivery_points dp ON dr.dp_id = dp.id
13. For delivery counts per DP: use delivery_outcomes.dp_id directly
14. For baby-level stats: use delivery_outcome_babies joined to delivery_outcomes
15. ALWAYS filter delivery_points.is_active = 1 unless asked otherwise
16. NEVER use 'do' as alias for delivery_outcomes — DO is a reserved MySQL keyword. Always use 'dout'
17. GEOGRAPHIC NAME MATCHING: When the user mentions a block, ward, district, sub-centre, or delivery point by name, ALWAYS match against the 'name' column using LOWER(column) LIKE LOWER('%value%'). NEVER match geographic names against the 'code' column. Example: "GOP block" → LOWER(b.name) LIKE LOWER('%GOP%'), NOT b.code = 'GOP'
18. ALWAYS JOIN a table before using its alias in WHERE. If you reference b.name in WHERE, you MUST have JOIN blocks b in the FROM clause. If you reference d.name in WHERE, you MUST have JOIN districts d. NEVER use an alias that has no corresponding JOIN.
19. When the user says "in [city/district name]" for delivery points (e.g. "DPs in Puri"), join the districts table: JOIN districts d ON dp.district_id = d.id WHERE LOWER(d.name) LIKE LOWER('%Puri%'). Do NOT use b.name for district-level filtering.

EXAMPLES:
Q: "Total deliveries per delivery point"
A: SELECT dp.name, COUNT(dout.id) as total_deliveries FROM delivery_outcomes dout JOIN delivery_points dp ON dout.dp_id = dp.id WHERE dp.is_active = 1 GROUP BY dp.id, dp.name

Q: "Maternal deaths by block"
A: SELECT b.name, COUNT(dout.id) as maternal_deaths FROM delivery_outcomes dout JOIN delivery_points dp ON dout.dp_id = dp.id JOIN blocks b ON dp.block_id = b.id WHERE dout.delivery_type = 'maternal_death' GROUP BY b.id, b.name

Q: "Baby gender distribution"
A: SELECT dob.gender, COUNT(dob.id) as count FROM delivery_outcome_babies dob GROUP BY dob.gender

Q: "Monthly delivery trends"
A: SELECT DATE_FORMAT(dout.delivery_date, '%Y-%m') as month, COUNT(dout.id) as deliveries FROM delivery_outcomes dout GROUP BY month ORDER BY month

Q: "Total high-risk pregnant women in GOP block"
A: SELECT COUNT(pw.id) as total_high_risk FROM pregnant_women pw JOIN blocks b ON pw.block_id = b.id WHERE LOWER(b.name) LIKE LOWER('%GOP%') AND pw.is_high_risk = 1

Q: "Pregnant women in Nashik district"
A: SELECT COUNT(pw.id) as total FROM pregnant_women pw JOIN districts d ON pw.district_id = d.id WHERE LOWER(d.name) LIKE LOWER('%Nashik%')

Q: "Total DPs available in Puri"
A: SELECT COUNT(dp.id) as total_dps FROM delivery_points dp JOIN districts d ON dp.district_id = d.id WHERE LOWER(d.name) LIKE LOWER('%Puri%') AND dp.is_active = 1

Q: "Delivery points in Nashik block"
A: SELECT COUNT(dp.id) as total_dps FROM delivery_points dp JOIN blocks b ON dp.block_id = b.id WHERE LOWER(b.name) LIKE LOWER('%Nashik%') AND dp.is_active = 1

QUESTION: {question}

SQL QUERY:"""
        
        return prompt
    
    def _format_schema(self, schema: Dict) -> str:
        """Format schema for prompt"""
        lines = []
        for table, info in schema.get("tables", {}).items():
            columns = ", ".join(info.get("columns", []))
            desc = info.get("description", "")
            lines.append(f"Table: {table}")
            lines.append(f"  Description: {desc}")
            lines.append(f"  Columns: {columns}")
            for col, vals in info.get("enum_values", {}).items():
                lines.append(f"  ENUM {col}: {vals}")
            if info.get("important"):
                lines.append(f"  NOTE: {info['important']}")
            lines.append("")
        return "\n".join(lines)
    
    def _extract_sql(self, response: str) -> str:
        """Extract SQL from Gemini response"""
        # Remove markdown code blocks
        import re
        sql = re.sub(r'```sql\n?', '', response)
        sql = re.sub(r'```\n?', '', sql)
        
        # Get first SELECT statement
        lines = sql.split('\n')
        sql_lines = []
        in_query = False
        
        for line in lines:
            line_upper = line.strip().upper()
            if line_upper.startswith('SELECT'):
                in_query = True
            if in_query:
                sql_lines.append(line)
                if ';' in line:
                    break
        
        return '\n'.join(sql_lines).strip().rstrip(';')
    
    def is_available(self) -> bool:
        """Check if Gemini is available"""
        return self.enabled and GEMINI_AVAILABLE
    
    def get_config(self) -> Dict:
        """Get engine configuration"""
        return {
            "name": self.name,
            "model": self.model_name,
            "enabled": self.enabled,
            "requires_api_key": True,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }
