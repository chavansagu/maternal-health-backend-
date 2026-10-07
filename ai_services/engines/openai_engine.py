"""
OpenAI GPT Engine - Text-to-SQL using GPT-4
"""
import os
import json
from typing import Dict
from dotenv import load_dotenv

load_dotenv()

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

from ..base_engine import BaseAIEngine

class OpenAIEngine(BaseAIEngine):
    """OpenAI GPT Engine for Text-to-SQL"""
    
    def __init__(self):
        super().__init__()
        self.name = "openai"
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("OPENAI_MODEL", "gpt-4")
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.1"))
        
        if OPENAI_AVAILABLE and self.api_key and self.api_key != "your_openai_key_here":
            try:
                self.client = OpenAI(api_key=self.api_key)
                self.enabled = True
            except Exception as e:
                print(f"OpenAI initialization error: {e}")
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
        """Generate SQL using OpenAI GPT"""
        
        if not self.enabled:
            raise RuntimeError("OpenAI is not available. Check API key.")
        
        prompt = self._build_prompt(question, schema, user_role, user_context)
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a SQL expert. Generate only SELECT queries in MySQL syntax."},
                    {"role": "user", "content": prompt}
                ],
                temperature=self.temperature,
                max_tokens=1000
            )
            
            sql = response.choices[0].message.content
            return self._extract_sql(sql)
            
        except Exception as e:
            raise RuntimeError(f"OpenAI API error: {str(e)}")
    
    def _build_prompt(
        self,
        question: str,
        schema: Dict,
        user_role: str,
        user_context: Dict
    ) -> str:
        """Build prompt for OpenAI"""

        schema_text = self._format_schema(schema)
        important_notes = "\n".join(schema.get("important_notes", []))

        prompt = f"""Convert this question to a MySQL SELECT query.

DATABASE SCHEMA:
{schema_text}

IMPORTANT NOTES:
{important_notes}

USER ROLE: {user_role}

RULES:
1. Only SELECT queries
2. Use proper JOINs with table aliases
3. MySQL syntax
4. Return only SQL, no explanation
5. NEVER use district_id on usg_appointments, anc_visits, delivery_outcomes, or delivery_referrals
6. To filter delivery_outcomes by district: JOIN delivery_points dp ON dout.dp_id = dp.id
7. For delivery counts per DP: use delivery_outcomes.dp_id directly
8. For baby stats: use delivery_outcome_babies joined to delivery_outcomes
9. ALWAYS filter delivery_points.is_active = 1 unless asked otherwise
10. NEVER use 'do' as alias for delivery_outcomes — DO is a reserved MySQL keyword. Always use 'dout'
11. GEOGRAPHIC NAME MATCHING: When the user mentions a block, ward, district, sub-centre, or delivery point by name, ALWAYS match against the 'name' column using LOWER(column) LIKE LOWER('%value%'). NEVER match geographic names against the 'code' column. Example: "GOP block" → LOWER(b.name) LIKE LOWER('%GOP%'), NOT b.code = 'GOP'
12. ALWAYS JOIN a table before using its alias in WHERE. If you reference b.name in WHERE, you MUST have JOIN blocks b. If you reference d.name in WHERE, you MUST have JOIN districts d. NEVER use an alias that has no corresponding JOIN.
13. When the user says "in [city/district name]" for delivery points (e.g. "DPs in Puri"), join the districts table: JOIN districts d ON dp.district_id = d.id WHERE LOWER(d.name) LIKE LOWER('%Puri%'). Do NOT use b.name for district-level filtering.

EXAMPLES:
Q: "Total deliveries per delivery point"
A: SELECT dp.name, COUNT(dout.id) as total_deliveries FROM delivery_outcomes dout JOIN delivery_points dp ON dout.dp_id = dp.id WHERE dp.is_active = 1 GROUP BY dp.id, dp.name

Q: "Maternal deaths by block"
A: SELECT b.name, COUNT(dout.id) as maternal_deaths FROM delivery_outcomes dout JOIN delivery_points dp ON dout.dp_id = dp.id JOIN blocks b ON dp.block_id = b.id WHERE dout.delivery_type = 'maternal_death' GROUP BY b.id, b.name

Q: "Baby gender distribution"
A: SELECT dob.gender, COUNT(dob.id) as count FROM delivery_outcome_babies dob GROUP BY dob.gender

Q: "Total high-risk pregnant women in GOP block"
A: SELECT COUNT(pw.id) as total_high_risk FROM pregnant_women pw JOIN blocks b ON pw.block_id = b.id WHERE LOWER(b.name) LIKE LOWER('%GOP%') AND pw.is_high_risk = 1

Q: "Total DPs available in Puri"
A: SELECT COUNT(dp.id) as total_dps FROM delivery_points dp JOIN districts d ON dp.district_id = d.id WHERE LOWER(d.name) LIKE LOWER('%Puri%') AND dp.is_active = 1

QUESTION: {question}

SQL:"""
        
        return prompt
    
    def _format_schema(self, schema: Dict) -> str:
        """Format schema for prompt"""
        lines = []
        for table, info in schema.get("tables", {}).items():
            columns = ", ".join(info.get("columns", []))
            desc = info.get("description", "")
            lines.append(f"{table}: {desc} ({columns})")
            for col, vals in info.get("enum_values", {}).items():
                lines.append(f"  ENUM {col}: {vals}")
            if info.get("important"):
                lines.append(f"  NOTE: {info['important']}")
        return "\n".join(lines)
    
    def _extract_sql(self, response: str) -> str:
        """Extract SQL from response"""
        import re
        sql = re.sub(r'```sql\n?', '', response)
        sql = re.sub(r'```\n?', '', sql)
        
        lines = sql.split('\n')
        sql_lines = []
        in_query = False
        
        for line in lines:
            if line.strip().upper().startswith('SELECT'):
                in_query = True
            if in_query:
                sql_lines.append(line)
                if ';' in line:
                    break
        
        return '\n'.join(sql_lines).strip().rstrip(';')
    
    def is_available(self) -> bool:
        """Check if OpenAI is available"""
        return self.enabled and OPENAI_AVAILABLE
    
    def get_config(self) -> Dict:
        """Get engine configuration"""
        return {
            "name": self.name,
            "model": self.model,
            "enabled": self.enabled,
            "requires_api_key": True,
            "temperature": self.temperature
        }
