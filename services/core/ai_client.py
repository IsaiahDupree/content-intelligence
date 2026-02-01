"""
AI Client - Unified interface for all AI providers
Abstracts provider-specific APIs into a common interface
With automatic fallback on rate limits (429 errors)
"""
import os
from typing import List, Dict, Any, Optional
from loguru import logger

from config.model_registry import ModelConfig, ModelRegistry


# Fallback model chain for rate limit handling
# Priority order: Groq (fast/free) -> Google Gemini (free tier) -> OpenAI (paid)
# Updated 2025-12-28: Removed decommissioned models (llama-3.1-70b-versatile, mixtral-8x7b-32768)
# Note: Many Groq models have been decommissioned. Only these are confirmed working as of 2025-12-28
GROQ_FALLBACK_MODELS = [
    "llama-3.3-70b-versatile",  # Primary - best quality (confirmed working)
    "llama-3.1-8b-instant",     # Fallback - smaller, faster (confirmed working)
]

GOOGLE_FALLBACK_MODELS = [
    "gemini-2.5-flash",         # Fast, free tier friendly
    "gemini-2.0-flash-exp",     # Experimental but capable
    "gemini-1.5-flash",         # Stable fallback
]

OPENAI_FALLBACK_MODELS = [
    "gpt-4o-mini",              # Primary OpenAI fallback
    "gpt-3.5-turbo",            # Final fallback
]


class AIClient:
    """
    Unified client for all AI providers
    
    Usage:
        from config.model_registry import TaskType, ModelRegistry
        from services.ai_client import AIClient
        
        config = ModelRegistry.get_model_config(TaskType.CONTENT_ANALYSIS)
        client = AIClient(config)
        
        response = client.chat_completion([
            {"role": "user", "content": "Analyze this video..."}
        ])
    """
    
    def __init__(self, config: ModelConfig):
        """
        Initialize AI client with model configuration
        
        Args:
            config: ModelConfig from ModelRegistry
        """
        self.config = config
        self.client = self._init_client()
        
        logger.info(f"AIClient initialized: {config.provider}/{config.model}")
    
    def _init_client(self):
        """Initialize provider-specific client"""
        api_key = os.getenv(self.config.api_key_env)
        
        if not api_key:
            raise ValueError(f"{self.config.api_key_env} not found in environment")
        
        if self.config.provider == "openai":
            from openai import OpenAI
            return OpenAI(api_key=api_key)
        
        elif self.config.provider == "groq":
            from groq import Groq
            return Groq(api_key=api_key)
        
        elif self.config.provider == "anthropic":
            from anthropic import Anthropic
            return Anthropic(api_key=api_key)
        
        elif self.config.provider == "google":
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            return genai
        
        raise ValueError(f"Unknown provider: {self.config.provider}")
    
    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> str:
        """
        Unified chat completion interface with automatic fallback on rate limits
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            temperature: Override default temperature
            max_tokens: Override default max_tokens
            **kwargs: Additional provider-specific parameters
        
        Returns:
            Response text from model
        """
        temperature = temperature if temperature is not None else self.config.temperature
        max_tokens = max_tokens if max_tokens is not None else self.config.max_tokens
        
        # Build fallback chain based on primary provider
        models_to_try = self._build_fallback_chain()
        last_error = None
        
        for provider, model, client in models_to_try:
            try:
                content = self._execute_chat_completion(
                    client, provider, model, messages, temperature, max_tokens, **kwargs
                )
                return content
                
            except Exception as e:
                error_str = str(e)
                last_error = e
                
                # Check if rate limited (429)
                is_rate_limited = "429" in error_str or "rate_limit" in error_str.lower()
                
                if is_rate_limited:
                    logger.warning(f"[AIClient] Rate limited on {provider}/{model}, trying fallback...")
                    continue
                else:
                    # Non-rate-limit error, re-raise immediately
                    logger.error(f"Chat completion failed ({provider}/{model}): {e}")
                    raise
        
        # All fallbacks exhausted
        logger.error(f"[AIClient] All fallback models exhausted. Last error: {last_error}")
        raise last_error
    
    def _build_fallback_chain(self) -> List[tuple]:
        """
        Build ordered list of (provider, model, client) to try.
        
        Fallback priority:
        1. Primary provider models
        2. Google Gemini (free tier)
        3. OpenAI (paid, reliable)
        """
        chain = []
        
        # Start with primary model
        chain.append((self.config.provider, self.config.model, self.client))
        
        if self.config.provider == "groq":
            # Add other Groq models as fallbacks
            for fallback_model in GROQ_FALLBACK_MODELS:
                if fallback_model != self.config.model:
                    chain.append(("groq", fallback_model, self.client))
            
            # Add Google Gemini as intermediate fallback (free tier)
            google_key = os.getenv("GOOGLE_API_KEY")
            if google_key:
                try:
                    for google_model in GOOGLE_FALLBACK_MODELS:
                        chain.append(("google", google_model, google_key))
                    logger.debug("[AIClient] Google Gemini fallback chain added")
                except Exception as e:
                    logger.warning(f"[AIClient] Could not add Google fallback: {e}")
            
            # Add OpenAI as final fallback (paid, most reliable)
            openai_key = os.getenv("OPENAI_API_KEY")
            if openai_key:
                try:
                    from openai import OpenAI
                    openai_client = OpenAI(api_key=openai_key)
                    for openai_model in OPENAI_FALLBACK_MODELS:
                        chain.append(("openai", openai_model, openai_client))
                    logger.debug("[AIClient] OpenAI fallback chain added")
                except Exception as e:
                    logger.warning(f"[AIClient] Could not initialize OpenAI fallback: {e}")
        
        elif self.config.provider == "google":
            # Add other Google models as fallbacks
            for fallback_model in GOOGLE_FALLBACK_MODELS:
                if fallback_model != self.config.model:
                    chain.append(("google", fallback_model, self.client))
            
            # Add OpenAI as final fallback
            openai_key = os.getenv("OPENAI_API_KEY")
            if openai_key:
                try:
                    from openai import OpenAI
                    openai_client = OpenAI(api_key=openai_key)
                    for openai_model in OPENAI_FALLBACK_MODELS:
                        chain.append(("openai", openai_model, openai_client))
                except Exception as e:
                    logger.warning(f"[AIClient] Could not initialize OpenAI fallback: {e}")
        
        elif self.config.provider == "openai":
            # Add other OpenAI models as fallbacks
            for fallback_model in OPENAI_FALLBACK_MODELS:
                if fallback_model != self.config.model:
                    chain.append(("openai", fallback_model, self.client))
        
        logger.info(f"[AIClient] Fallback chain: {[(p, m) for p, m, _ in chain]}")
        return chain
    
    def _execute_chat_completion(
        self,
        client,
        provider: str,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs
    ) -> str:
        """Execute a single chat completion call"""
        content = None
        
        if provider in ["openai", "groq"]:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )
            content = response.choices[0].message.content
        
        elif provider == "google":
            # Google Gemini uses REST API directly via httpx
            import httpx
            
            # client is the API key for Google
            api_key = client
            
            # Convert messages to Gemini format
            gemini_contents = []
            for msg in messages:
                role = "user" if msg["role"] == "user" else "model"
                gemini_contents.append({
                    "role": role,
                    "parts": [{"text": msg["content"]}]
                })
            
            # Make sync request (using httpx sync client)
            with httpx.Client(timeout=60.0) as http_client:
                response = http_client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                    json={
                        "contents": gemini_contents,
                        "generationConfig": {
                            "temperature": temperature,
                            "maxOutputTokens": max_tokens,
                        }
                    }
                )
                
                if response.status_code == 429:
                    raise Exception("429 rate_limit_exceeded")
                elif response.status_code != 200:
                    raise Exception(f"Google API error: {response.status_code} - {response.text[:200]}")
                
                data = response.json()
                candidates = data.get("candidates", [])
                if candidates:
                    content = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        
        else:
            raise NotImplementedError(f"Provider {provider} not supported in fallback chain")
        
        # Handle empty or None responses
        if not content or content.strip() == "":
            logger.warning(f"[AIClient] Empty response from {provider}/{model}")
            raise ValueError(f"Empty response from {provider}")
        
        # Log successful fallback
        if model != self.config.model or provider != self.config.provider:
            logger.success(f"[AIClient] Fallback successful: {provider}/{model}")
        
        # Strip markdown code blocks if present (common with JSON responses)
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        
        return content.strip()
    
    def transcribe(self, audio_path: str, language: str = "en") -> Dict[str, Any]:
        """
        Unified transcription interface
        
        Args:
            audio_path: Path to audio file
            language: Language code (default: "en")
        
        Returns:
            Dict with transcript and metadata
        """
        if self.config.provider not in ["openai", "groq"]:
            raise NotImplementedError(f"Transcription not supported for {self.config.provider}")
        
        try:
            with open(audio_path, "rb") as audio_file:
                response = self.client.audio.transcriptions.create(
                    model=self.config.model,
                    file=audio_file,
                    response_format="verbose_json",
                    language=language
                )
                
                return {
                    "text": response.text,
                    "language": response.language,
                    "duration": response.duration,
                    "segments": response.segments if hasattr(response, 'segments') else []
                }
        
        except Exception as e:
            logger.error(f"Transcription failed ({self.config.provider}/{self.config.model}): {e}")
            raise
    
    def vision_analysis(
        self,
        image_path: str,
        prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None
    ) -> str:
        """
        Unified vision/image analysis interface
        
        Args:
            image_path: Path to image file
            prompt: Analysis prompt
            temperature: Override default temperature
            max_tokens: Override default max_tokens
        
        Returns:
            Analysis text from model
        """
        if self.config.provider not in ["openai", "google", "anthropic"]:
            raise NotImplementedError(f"Vision analysis not supported for {self.config.provider}")
        
        temperature = temperature if temperature is not None else self.config.temperature
        max_tokens = max_tokens if max_tokens is not None else self.config.max_tokens
        
        try:
            if self.config.provider == "openai":
                import base64
                
                # Encode image to base64
                with open(image_path, "rb") as image_file:
                    image_data = base64.b64encode(image_file.read()).decode('utf-8')
                
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{image_data}"
                                    }
                                }
                            ]
                        }
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens
                )
                return response.choices[0].message.content
            
            elif self.config.provider == "google":
                import PIL.Image
                
                model = self.client.GenerativeModel(self.config.model)
                image = PIL.Image.open(image_path)
                
                response = model.generate_content(
                    [prompt, image],
                    generation_config={
                        "temperature": temperature,
                        "max_output_tokens": max_tokens
                    }
                )
                return response.text
            
            elif self.config.provider == "anthropic":
                import base64
                
                with open(image_path, "rb") as image_file:
                    image_data = base64.b64encode(image_file.read()).decode('utf-8')
                
                response = self.client.messages.create(
                    model=self.config.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/jpeg",
                                        "data": image_data
                                    }
                                },
                                {
                                    "type": "text",
                                    "text": prompt
                                }
                            ]
                        }
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens
                )
                return response.content[0].text
            
            raise NotImplementedError(f"Vision analysis not implemented for {self.config.provider}")
            
        except Exception as e:
            logger.error(f"Vision analysis failed ({self.config.provider}/{self.config.model}): {e}")
            raise
    
    def embeddings(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for text
        
        Args:
            texts: List of texts to embed
        
        Returns:
            List of embedding vectors
        """
        if self.config.provider != "openai":
            raise NotImplementedError(f"Embeddings not supported for {self.config.provider}")
        
        try:
            response = self.client.embeddings.create(
                model=self.config.model,
                input=texts
            )
            return [item.embedding for item in response.data]
        
        except Exception as e:
            logger.error(f"Embeddings failed ({self.config.provider}/{self.config.model}): {e}")
            raise


# Convenience functions
def create_client_for_task(task_type) -> AIClient:
    """
    Create AIClient for a specific task
    
    Args:
        task_type: TaskType enum value
    
    Returns:
        Configured AIClient instance
    """
    from config.model_registry import ModelRegistry
    config = ModelRegistry.get_model_config(task_type)
    return AIClient(config)
