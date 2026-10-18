import os
from typing import Dict, List, Optional
from pydantic import BaseModel
import openai

class ContentBrief(BaseModel):
    title: str
    hook: str
    core_message: str
    format: str # video, carousel, text
    platform_focus: List[str]
    target_segment: str
    rationale: str

class ContentBriefGenerator:
    """
    Generates data-driven content briefs using AI + Analytics.
    """
    
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.client = openai.AsyncOpenAI(api_key=self.api_key) if self.api_key else None

    async def generate_brief(
        self, 
        segment_name: str, 
        segment_insights: Dict,
        past_top_content: List[Dict]
    ) -> ContentBrief:
        """
        Generate a content brief for a specific segment.
        """
        if not self.client:
            return self._mock_brief(segment_name)

        prompt = self._construct_prompt(segment_name, segment_insights, past_top_content)
        
        try:
            response = await self.client.chat.completions.create(
                model="gpt-4-turbo-preview",
                messages=[
                    {"role": "system", "content": "You are an expert content strategist. Generate a viral content brief based on data."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
            )
            
            content = response.choices[0].message.content
            # In a real app, we'd parse JSON output or use function calling
            # For now, just returning a structured object with raw content
            
            return ContentBrief(
                title=f"AI Generated Strategy for {segment_name}",
                hook="See generated rationale",
                core_message=content[:100] + "...",
                format="video",
                platform_focus=["instagram", "linkedin"],
                target_segment=segment_name,
                rationale=content
            )
            
        except Exception as e:
            print(f"Error generating brief: {e}")
            return self._mock_brief(segment_name)

    def _construct_prompt(self, segment_name, insights, past_content):
        return f"""
        Create a content brief for segment: {segment_name}.
        
        Insights:
        - Top Topics: {insights.get('top_topics')}
        - Engagement Style: {insights.get('engagement_style')}
        
        Past Top Content:
        {past_content}
        
        Output a strategy with a Hook, Core Message, and Recommended Format.
        """

    def _mock_brief(self, segment_name) -> ContentBrief:
        return ContentBrief(
            title=f"Mock Strategy for {segment_name}",
            hook="Stop doing X, start doing Y",
            core_message="The old way is dead. Here is the new way.",
            format="Reel/Short",
            platform_focus=["instagram", "tiktok"],
            target_segment=segment_name,
            rationale="Mocked because OpenAI key missing or error."
        )
