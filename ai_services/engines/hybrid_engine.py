"""
Hybrid AI Engine - Fallback between multiple engines
Tries primary engine first, falls back to secondary if fails
"""
import os
from typing import Dict
from dotenv import load_dotenv

load_dotenv()

from ..base_engine import BaseAIEngine
from .gemini_engine import GeminiEngine
from .ollama_engine import OllamaEngine
from .vanna_engine import VannaEngine
from .openai_engine import OpenAIEngine

class HybridEngine(BaseAIEngine):
    """Hybrid Engine with automatic fallback"""
    
    def __init__(self):
        super().__init__()
        self.name = "hybrid"
        
        # Get priority order from .env
        self.primary = os.getenv("HYBRID_PRIMARY", "gemini").lower()
        self.fallback = os.getenv("HYBRID_FALLBACK", "vanna").lower()
        self.offline = os.getenv("HYBRID_OFFLINE", "ollama").lower()
        
        # Initialize engines
        self.engines = {
            "gemini": GeminiEngine(),
            "ollama": OllamaEngine(),
            "vanna": VannaEngine(),
            "openai": OpenAIEngine()
        }
        
        # Check if at least one engine is available
        self.enabled = any(engine.is_available() for engine in self.engines.values())
    
    def generate_sql(
        self, 
        question: str, 
        schema: Dict, 
        user_role: str,
        user_context: Dict
    ) -> str:
        """Generate SQL with automatic fallback"""
        
        if not self.enabled:
            raise RuntimeError("No AI engines available in hybrid mode")
        
        # Try engines in priority order
        priority_order = [self.primary, self.fallback, self.offline]
        errors = []
        
        for engine_name in priority_order:
            engine = self.engines.get(engine_name)
            
            if not engine or not engine.is_available():
                errors.append(f"{engine_name}: not available")
                continue
            
            try:
                sql = engine.generate_sql(question, schema, user_role, user_context)
                
                # Log which engine was used
                print(f"Hybrid mode: Used {engine_name} engine")
                
                return sql
                
            except Exception as e:
                errors.append(f"{engine_name}: {str(e)}")
                continue
        
        # All engines failed
        error_msg = "; ".join(errors)
        raise RuntimeError(f"All engines failed in hybrid mode: {error_msg}")
    
    def is_available(self) -> bool:
        """Check if any engine is available"""
        return self.enabled
    
    def get_config(self) -> Dict:
        """Get hybrid configuration"""
        available_engines = [
            name for name, engine in self.engines.items() 
            if engine.is_available()
        ]
        
        return {
            "name": self.name,
            "enabled": self.enabled,
            "primary": self.primary,
            "fallback": self.fallback,
            "offline": self.offline,
            "available_engines": available_engines,
            "priority_order": [self.primary, self.fallback, self.offline]
        }
