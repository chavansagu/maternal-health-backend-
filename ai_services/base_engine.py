"""
Base AI Engine - Abstract class for all AI engines
All engines must implement these methods
"""
from abc import ABC, abstractmethod
from typing import Dict, Optional

class BaseAIEngine(ABC):
    """Abstract base class for all AI Text-to-SQL engines"""
    
    def __init__(self):
        self.name = "base"
        self.enabled = False
    
    @abstractmethod
    def generate_sql(
        self, 
        question: str, 
        schema: Dict, 
        user_role: str,
        user_context: Dict
    ) -> str:
        """
        Convert natural language to SQL
        
        Args:
            question: User's question in natural language
            schema: Database schema information
            user_role: district, block, sub_centre
            user_context: User's district_id, block_id, etc.
            
        Returns:
            SQL query string
        """
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if engine is configured and available"""
        pass
    
    @abstractmethod
    def get_config(self) -> Dict:
        """Get engine configuration"""
        pass
    
    def validate_response(self, sql: str) -> bool:
        """Basic validation"""
        return sql and len(sql) > 0 and sql.strip().upper().startswith('SELECT')
