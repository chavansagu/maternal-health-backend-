"""
AI Engine Factory - Selects AI engine based on .env configuration
Similar to SMS provider pattern
"""
import os
from typing import Optional
from dotenv import load_dotenv
from .base_engine import BaseAIEngine

load_dotenv()

class AIEngineFactory:
    """Factory to create AI engine based on AI_PROVIDER in .env"""
    
    _instance = None
    _current_engine: Optional[BaseAIEngine] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def get_engine(self) -> BaseAIEngine:
        """
        Get AI engine based on AI_PROVIDER in .env
        Caches the engine instance for reuse
        """
        if self._current_engine:
            return self._current_engine
        
        provider = os.getenv("AI_PROVIDER", "gemini").lower()
        
        # Lazy import to avoid circular dependencies
        if provider == "gemini":
            from .engines.gemini_engine import GeminiEngine
            self._current_engine = GeminiEngine()
        elif provider == "ollama":
            from .engines.ollama_engine import OllamaEngine
            self._current_engine = OllamaEngine()
        elif provider == "vanna":
            from .engines.vanna_engine import VannaEngine
            self._current_engine = VannaEngine()
        elif provider == "openai":
            from .engines.openai_engine import OpenAIEngine
            self._current_engine = OpenAIEngine()
        elif provider == "hybrid":
            from .engines.hybrid_engine import HybridEngine
            self._current_engine = HybridEngine()
        else:
            raise ValueError(f"Unknown AI provider: {provider}")
        
        # Verify engine is available
        if not self._current_engine.is_available():
            raise RuntimeError(
                f"AI engine '{provider}' is not available. "
                f"Check configuration in .env"
            )
        
        return self._current_engine
    
    def reload_engine(self):
        """Force reload engine (useful after .env changes)"""
        self._current_engine = None
        return self.get_engine()
    
    def get_available_engines(self) -> list:
        """Get list of all available engines"""
        available = []
        
        engines_to_check = [
            ("gemini", "GeminiEngine"),
            ("ollama", "OllamaEngine"),
            ("vanna", "VannaEngine"),
            ("openai", "OpenAIEngine")
        ]
        
        for name, class_name in engines_to_check:
            try:
                if name == "gemini":
                    from .engines.gemini_engine import GeminiEngine
                    engine = GeminiEngine()
                elif name == "ollama":
                    from .engines.ollama_engine import OllamaEngine
                    engine = OllamaEngine()
                elif name == "vanna":
                    from .engines.vanna_engine import VannaEngine
                    engine = VannaEngine()
                elif name == "openai":
                    from .engines.openai_engine import OpenAIEngine
                    engine = OpenAIEngine()
                
                if engine.is_available():
                    available.append({
                        "name": name,
                        "config": engine.get_config()
                    })
            except Exception as e:
                print(f"Engine {name} not available: {e}")
                pass
        
        return available

# Global instance
ai_factory = AIEngineFactory()
