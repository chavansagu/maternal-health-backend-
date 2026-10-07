"""
Vanna.AI Engine - Specialized Text-to-SQL with training
"""
import os
import json
from typing import Dict
from dotenv import load_dotenv

load_dotenv()

try:
    from vanna.remote import VannaDefault
    VANNA_AVAILABLE = True
except ImportError:
    VANNA_AVAILABLE = False

from ..base_engine import BaseAIEngine

class VannaEngine(BaseAIEngine):
    """Vanna.AI Engine - Learns from your database"""
    
    def __init__(self):
        super().__init__()
        self.name = "vanna"
        self.model_name = os.getenv("VANNA_MODEL_NAME", "janani_jyoti_model")
        self.enabled = VANNA_AVAILABLE and os.getenv("VANNA_ENABLED", "false").lower() == "true"
        
        if self.enabled:
            try:
                self.vn = VannaDefault(model=self.model_name)
                self._is_trained = self._check_training()
            except Exception as e:
                print(f"Vanna initialization error: {e}")
                self.enabled = False
                self._is_trained = False
        else:
            self._is_trained = False
    
    def _check_training(self) -> bool:
        """Check if model has training data"""
        try:
            # Try a simple query to check if trained
            test_result = self.vn.get_training_data()
            return len(test_result) > 0
        except:
            return False
    
    def generate_sql(
        self, 
        question: str, 
        schema: Dict, 
        user_role: str,
        user_context: Dict
    ) -> str:
        """Generate SQL using Vanna.AI"""
        
        if not self.enabled:
            raise RuntimeError("Vanna.AI is not available. Install: pip install vanna")
        
        if not self._is_trained:
            # Auto-train on first use
            self._train_on_schema(schema)
        
        try:
            # Generate SQL
            sql = self.vn.generate_sql(question)
            
            if not sql:
                raise RuntimeError("Vanna returned empty SQL")
            
            return self._clean_sql(sql)
            
        except Exception as e:
            raise RuntimeError(f"Vanna error: {str(e)}")
    
    def _train_on_schema(self, schema: Dict):
        """Train Vanna on database schema"""
        try:
            # Add DDL statements
            for table, info in schema.get("tables", {}).items():
                columns = info.get("columns", [])
                ddl = f"CREATE TABLE {table} (\n"
                ddl += ",\n".join([f"  {col} VARCHAR(255)" for col in columns])
                ddl += "\n);"
                self.vn.train(ddl=ddl)
            
            # Add sample questions
            sample_questions = [
                ("How many pregnant women are there?", "SELECT COUNT(*) FROM pregnant_women"),
                ("Show me all blocks", "SELECT * FROM blocks"),
                ("Total USG appointments", "SELECT COUNT(*) FROM usg_appointments")
            ]
            
            for question, sql in sample_questions:
                self.vn.train(question=question, sql=sql)
            
            self._is_trained = True
            
        except Exception as e:
            print(f"Vanna training error: {e}")
    
    def _clean_sql(self, sql: str) -> str:
        """Clean SQL output"""
        import re
        sql = re.sub(r'```sql\n?', '', sql)
        sql = re.sub(r'```\n?', '', sql)
        return sql.strip().rstrip(';')
    
    def is_available(self) -> bool:
        """Check if Vanna is available"""
        return self.enabled and VANNA_AVAILABLE
    
    def get_config(self) -> Dict:
        """Get engine configuration"""
        return {
            "name": self.name,
            "model": self.model_name,
            "enabled": self.enabled,
            "requires_api_key": False,
            "is_trained": self._is_trained,
            "requires_training": True
        }
