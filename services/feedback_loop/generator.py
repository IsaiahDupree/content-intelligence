"""
Content Generator for Twitter Feedback Loop Agent.
Uses OpenAI to generate posts from templates with real AI calls.
"""

import os
import json
from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass
import openai
from loguru import logger

from .templates import (
    Template, 
    TEMPLATE_LIBRARY, 
    get_template_by_id,
    get_templates_by_awareness,
    AwarenessLevel
)
from .models import (
    Brand, 
    Offer, 
    ICP, 
    CreatorProfile,
    PromptRun,
    Slot,
    generate_id
)


@dataclass
class GenerationResult:
    """Result of a content generation."""
    success: bool
    prompt_run: Optional[PromptRun] = None
    generated_text: str = ""
    variants: List[str] = None
    error: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "prompt_run": self.prompt_run.to_dict() if self.prompt_run else None,
            "generated_text": self.generated_text,
            "variants": self.variants or [],
            "error": self.error
        }


class ContentGenerator:
    """
    AI-powered content generator using Awareness × FATE templates.
    Uses real OpenAI API calls for generation.
    """
    
    def __init__(self, 
                 creator_profile: Optional[CreatorProfile] = None,
                 model: str = "gpt-4o",
                 temperature: float = 0.7):
        """
        Initialize the content generator.
        
        Args:
            creator_profile: Voice and style rules for the creator
            model: OpenAI model to use
            temperature: Generation temperature (0-1)
        """
        self.creator_profile = creator_profile
        self.model = model
        self.temperature = temperature
        
        # Initialize OpenAI client
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning("OPENAI_API_KEY not set - generation will fail")
        self.client = openai.OpenAI(api_key=api_key) if api_key else None
    
    def generate_post(self,
                      template: Template,
                      brand: Brand,
                      offer: Offer,
                      icp: ICP,
                      slot: Optional[Slot] = None,
                      additional_context: Optional[Dict] = None,
                      num_variants: int = 1) -> GenerationResult:
        """
        Generate a post using a template and brand/offer/ICP context.
        
        Args:
            template: The template to use for generation
            brand: Brand context
            offer: Offer context
            icp: ICP (audience) context
            slot: Optional content plan slot
            additional_context: Optional extra variables
            num_variants: Number of variants to generate (1-3)
        
        Returns:
            GenerationResult with generated content
        """
        if not self.client:
            return GenerationResult(
                success=False,
                error="OpenAI client not initialized - check OPENAI_API_KEY"
            )
        
        # Build the input variables
        inputs = self._build_inputs(template, brand, offer, icp, additional_context)
        
        # Build the system prompt
        system_prompt = self._build_system_prompt(brand, offer, icp)
        
        # Fill the template with variables
        try:
            filled_prompt = template.prompt_text.format(**inputs)
        except KeyError as e:
            return GenerationResult(
                success=False,
                error=f"Missing variable in template: {e}"
            )
        
        # Generate with OpenAI
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": filled_prompt}
            ]
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=500,
                n=min(num_variants, 3)
            )
            
            # Extract generated content
            generated_text = response.choices[0].message.content.strip()
            variants = [
                choice.message.content.strip() 
                for choice in response.choices
            ]
            
            # Run quality checks
            quality_checks = self._run_quality_checks(generated_text, brand, icp)
            
            # Create prompt run record
            prompt_run = PromptRun(
                prompt_run_id=generate_id("run_"),
                template_id=template.template_id,
                slot_id=slot.slot_id if slot else None,
                inputs=inputs,
                model_info={
                    "model": self.model,
                    "temperature": self.temperature,
                    "max_tokens": 500
                },
                generated_text=generated_text,
                variants=variants,
                quality_checks=quality_checks,
                created_at=datetime.now()
            )
            
            logger.info(f"✅ Generated post using template {template.template_id}")
            
            return GenerationResult(
                success=True,
                prompt_run=prompt_run,
                generated_text=generated_text,
                variants=variants
            )
            
        except Exception as e:
            logger.error(f"OpenAI generation failed: {e}")
            return GenerationResult(
                success=False,
                error=str(e)
            )
    
    def generate_for_slot(self,
                          slot: Slot,
                          brand: Brand,
                          offer: Offer,
                          icp: ICP,
                          template_override: Optional[Template] = None) -> GenerationResult:
        """
        Generate content for a specific content plan slot.
        
        Args:
            slot: The content plan slot
            brand: Brand context
            offer: Offer context
            icp: ICP context
            template_override: Optional template to use instead of slot's template
        
        Returns:
            GenerationResult
        """
        # Get template from slot or override
        if template_override:
            template = template_override
        elif slot.template_id:
            template = get_template_by_id(slot.template_id)
        else:
            # Pick best template for awareness level
            awareness = AwarenessLevel(slot.awareness_level)
            templates = get_templates_by_awareness(awareness)
            if not templates:
                return GenerationResult(
                    success=False,
                    error=f"No templates for awareness level: {slot.awareness_level}"
                )
            # Pick first one (in production, use leaderboard)
            template = templates[0]
        
        if not template:
            return GenerationResult(
                success=False,
                error=f"Template not found: {slot.template_id}"
            )
        
        return self.generate_post(
            template=template,
            brand=brand,
            offer=offer,
            icp=icp,
            slot=slot,
            num_variants=3
        )
    
    def _build_inputs(self,
                      template: Template,
                      brand: Brand,
                      offer: Offer,
                      icp: ICP,
                      additional_context: Optional[Dict] = None) -> Dict[str, str]:
        """Build the input variables for template filling."""
        
        # Start with basic mappings
        inputs = {
            "brand": brand.name,
            "offer": offer.name,
            "icp": icp.name,
            "cta_link": offer.landing_url,
        }
        
        # Add ICP-specific context
        if icp.pains:
            inputs["pain"] = icp.pains[0]
        if icp.desired_outcomes:
            inputs["desired_outcome"] = icp.desired_outcomes[0]
        if icp.objections:
            inputs["objection"] = icp.objections[0]
        
        # Add offer context
        inputs["mechanism"] = offer.promise
        inputs["proof"] = f"for {icp.name}"
        
        # Override with additional context
        if additional_context:
            inputs.update(additional_context)
        
        return inputs
    
    def _build_system_prompt(self, 
                              brand: Brand,
                              offer: Offer,
                              icp: ICP) -> str:
        """Build the system prompt with creator voice and context."""
        
        parts = [
            "You are a content writer for X/Twitter.",
            "Generate a single tweet that follows the template structure exactly.",
            "Keep it under 280 characters unless it's a thread.",
            "Be direct, specific, and avoid fluff.",
            "",
            f"Brand: {brand.name}",
            f"Positioning: {brand.positioning}",
            f"Target audience: {icp.name}",
        ]
        
        # Add creator voice rules if available
        if self.creator_profile:
            parts.append("")
            parts.append("Voice rules:")
            if self.creator_profile.tone_descriptors:
                parts.append(f"- Tone: {', '.join(self.creator_profile.tone_descriptors)}")
            if self.creator_profile.banned_phrases:
                parts.append(f"- Never use: {', '.join(self.creator_profile.banned_phrases[:5])}")
        
        # Add ICP language guidance
        if icp.language_to_use:
            parts.append(f"- Use phrases like: {', '.join(icp.language_to_use[:3])}")
        if icp.language_to_avoid:
            parts.append(f"- Avoid phrases like: {', '.join(icp.language_to_avoid[:3])}")
        
        # Add disallowed topics
        if brand.disallowed_topics:
            parts.append(f"- Do not mention: {', '.join(brand.disallowed_topics[:3])}")
        
        parts.append("")
        parts.append("Output ONLY the tweet text. No quotes, no labels, no explanations.")
        
        return "\n".join(parts)
    
    def _run_quality_checks(self,
                            text: str,
                            brand: Brand,
                            icp: ICP) -> Dict[str, Any]:
        """Run quality checks on generated content."""
        
        checks = {
            "length_ok": len(text) <= 280,
            "no_banned_words": True,
            "cta_relevant": True,
            "clarity_score": 0.8
        }
        
        # Check for banned phrases
        if self.creator_profile and self.creator_profile.banned_phrases:
            text_lower = text.lower()
            for phrase in self.creator_profile.banned_phrases:
                if phrase.lower() in text_lower:
                    checks["no_banned_words"] = False
                    break
        
        # Check brand disallowed topics
        if brand.disallowed_topics:
            text_lower = text.lower()
            for topic in brand.disallowed_topics:
                if topic.lower() in text_lower:
                    checks["cta_relevant"] = False
                    break
        
        return checks
    
    def batch_generate(self,
                       slots: List[Slot],
                       brand: Brand,
                       offers: List[Offer],
                       icps: List[ICP]) -> List[GenerationResult]:
        """
        Generate content for multiple slots.
        
        Args:
            slots: List of content plan slots
            brand: Brand context
            offers: Available offers (matched by ID)
            icps: Available ICPs (matched by ID)
        
        Returns:
            List of GenerationResults
        """
        results = []
        
        # Build lookup maps
        offer_map = {o.offer_id: o for o in offers}
        icp_map = {i.icp_id: i for i in icps}
        
        for slot in slots:
            # Get offer and ICP for this slot
            offer_id = slot.target_offer_ids[0] if slot.target_offer_ids else None
            icp_id = slot.target_icp_ids[0] if slot.target_icp_ids else None
            
            offer = offer_map.get(offer_id)
            icp = icp_map.get(icp_id)
            
            if not offer or not icp:
                results.append(GenerationResult(
                    success=False,
                    error=f"Missing offer ({offer_id}) or ICP ({icp_id}) for slot {slot.slot_id}"
                ))
                continue
            
            result = self.generate_for_slot(slot, brand, offer, icp)
            results.append(result)
        
        return results
