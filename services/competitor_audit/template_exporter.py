"""
Template Exporter Service
=========================
Exports competitor post formats as Remotion-ready template packs.
Creates reusable templates with placeholders for user content.
"""
import os
import json
import re
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict
from loguru import logger

from openai import OpenAI
from sqlalchemy import create_engine, text

from .deep_audit import PostDeepAudit, BeatSheetEntry, StyleFingerprint


@dataclass
class PlaceholderDef:
    """Definition of a template placeholder"""
    key: str  # e.g., "{{HOOK_TEXT}}"
    type: str  # text, video, image, audio
    description: str
    example: str
    required: bool = True


@dataclass
class SwapRule:
    """Rule for swapping content without breaking pacing"""
    element: str  # What can be swapped
    constraints: Dict[str, Any]  # Min/max duration, format requirements
    tips: str


@dataclass
class TemplatePack:
    """Complete Remotion-ready template"""
    template_id: Optional[str] = None
    account_id: str = ""
    source_post_id: str = ""
    
    # Identity
    template_name: str = ""
    template_slug: str = ""
    
    # Style
    style_fingerprint: Dict[str, Any] = field(default_factory=dict)
    
    # Beat sheet with placeholders
    beat_sheet_template: List[Dict[str, Any]] = field(default_factory=list)
    
    # Remotion spec with {{PLACEHOLDERS}}
    remotion_render_spec: Dict[str, Any] = field(default_factory=dict)
    
    # Placeholder definitions
    placeholders: List[PlaceholderDef] = field(default_factory=list)
    
    # Swap rules
    swap_rules: List[SwapRule] = field(default_factory=list)
    
    # Usage guidance
    best_for: List[str] = field(default_factory=list)
    difficulty_level: str = "intermediate"
    estimated_production_time: str = "30 minutes"
    
    # Preview
    preview_thumbnail_url: Optional[str] = None
    
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class TemplateExporter:
    """
    Exports competitor post formats as reusable Remotion templates.
    Creates templated beat sheets and render specs with placeholders.
    """
    
    # Standard placeholders
    STANDARD_PLACEHOLDERS = {
        "{{HOOK_TEXT}}": PlaceholderDef(
            key="{{HOOK_TEXT}}",
            type="text",
            description="Opening hook/attention grabber",
            example="Stop doing this one thing...",
            required=True
        ),
        "{{MAIN_CONTENT}}": PlaceholderDef(
            key="{{MAIN_CONTENT}}",
            type="text",
            description="Main body content/script",
            example="Here's what you should do instead...",
            required=True
        ),
        "{{CTA_TEXT}}": PlaceholderDef(
            key="{{CTA_TEXT}}",
            type="text",
            description="Call to action",
            example="Follow for more tips!",
            required=True
        ),
        "{{BROLL_1}}": PlaceholderDef(
            key="{{BROLL_1}}",
            type="video",
            description="B-roll footage clip 1",
            example="Stock footage of person working",
            required=False
        ),
        "{{BACKGROUND_MUSIC}}": PlaceholderDef(
            key="{{BACKGROUND_MUSIC}}",
            type="audio",
            description="Background music track",
            example="Upbeat electronic, 120 BPM",
            required=False
        ),
        "{{PROOF_CLIP}}": PlaceholderDef(
            key="{{PROOF_CLIP}}",
            type="video",
            description="Social proof/testimonial clip",
            example="Screenshot of results or testimonial",
            required=False
        )
    }
    
    def __init__(
        self,
        db_url: Optional[str] = None,
        openai_api_key: Optional[str] = None
    ):
        self.db_url = db_url or os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
        self.engine = create_engine(self.db_url)
        
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=self.openai_api_key) if self.openai_api_key else None
    
    def create_template_from_audit(
        self,
        post_audit: PostDeepAudit,
        post_data: Dict[str, Any],
        account_id: str
    ) -> TemplatePack:
        """
        Create a template pack from a deep audit of a post.
        
        Args:
            post_audit: Deep audit of the source post
            post_data: Raw post data (for media URLs, duration, etc.)
            account_id: Account this template is from
        """
        # Generate template name from hook archetype and style
        template_name = self._generate_template_name(post_audit)
        template_slug = self._slugify(template_name)
        
        # Build style fingerprint
        style = self._build_style_fingerprint(post_audit)
        
        # Convert beat sheet to template format with placeholders
        beat_template = self._templatize_beat_sheet(post_audit.beat_sheet)
        
        # Build Remotion render spec with placeholders
        duration_sec = post_data.get("duration_sec", 30)
        remotion_spec = self._build_remotion_spec_template(
            beat_template,
            style,
            duration_sec
        )
        
        # Identify needed placeholders
        placeholders = self._identify_placeholders(beat_template, remotion_spec)
        
        # Generate swap rules
        swap_rules = self._generate_swap_rules(beat_template, style)
        
        # Determine best use cases
        best_for = self._determine_best_for(post_audit)
        
        return TemplatePack(
            account_id=account_id,
            source_post_id=post_audit.post_id,
            template_name=template_name,
            template_slug=template_slug,
            style_fingerprint=style,
            beat_sheet_template=beat_template,
            remotion_render_spec=remotion_spec,
            placeholders=placeholders,
            swap_rules=swap_rules,
            best_for=best_for,
            difficulty_level=self._assess_difficulty(style, beat_template),
            estimated_production_time=self._estimate_production_time(beat_template),
            preview_thumbnail_url=post_data.get("thumbnail_url")
        )
    
    def _generate_template_name(self, audit: PostDeepAudit) -> str:
        """Generate a descriptive template name"""
        parts = []
        
        # Hook style
        hook_arch = audit.hook.archetype if audit.hook else ""
        if "stop" in hook_arch.lower():
            parts.append("StopDoing")
        elif "mistake" in hook_arch.lower():
            parts.append("Mistakes")
        elif "secret" in hook_arch.lower():
            parts.append("Secrets")
        elif "tried" in hook_arch.lower():
            parts.append("ITried")
        elif "truth" in hook_arch.lower():
            parts.append("Truth")
        else:
            parts.append("Hook")
        
        # Angle type
        angle = audit.angle_type
        if angle == "tutorial":
            parts.append("Tutorial")
        elif angle == "listicle":
            parts.append("Listicle")
        elif angle == "case-study":
            parts.append("CaseStudy")
        elif angle == "myth-bust":
            parts.append("MythBust")
        else:
            parts.append(angle.title().replace("-", "") if angle else "Content")
        
        # Style indicator
        if audit.style_fingerprint:
            if audit.style_fingerprint.cut_density == "high":
                parts.append("Fast")
            elif audit.style_fingerprint.caption_style == "fast_captions":
                parts.append("Captions")
        
        # Version
        parts.append("V1")
        
        return "_".join(parts)
    
    def _slugify(self, name: str) -> str:
        """Convert name to URL-safe slug"""
        slug = name.lower()
        slug = re.sub(r'[^a-z0-9]+', '-', slug)
        slug = slug.strip('-')
        return slug
    
    def _build_style_fingerprint(self, audit: PostDeepAudit) -> Dict[str, Any]:
        """Build style fingerprint dict"""
        style = audit.style_fingerprint
        
        return {
            "caption_style": style.caption_style if style else "minimal",
            "cut_density": style.cut_density if style else "medium",
            "color_scheme": style.color_scheme if style else "neutral",
            "motion_presets": style.motion_presets if style else [],
            "pattern_interrupts": style.pattern_interrupts if style else [],
            "text_animations": style.text_animations if style else [],
            "energy_level": style.cut_density if style else "medium"
        }
    
    def _templatize_beat_sheet(
        self,
        beat_sheet: List[BeatSheetEntry]
    ) -> List[Dict[str, Any]]:
        """Convert beat sheet to template format with placeholders"""
        template = []
        
        for i, beat in enumerate(beat_sheet):
            beat_dict = {
                "beat_index": i,
                "role": beat.role,
                "start_sec": beat.start_sec,
                "end_sec": beat.end_sec,
                "duration_sec": beat.end_sec - beat.start_sec,
                "summary": beat.summary,
                "emotion": beat.emotion,
                "placeholder": None,
                "notes": ""
            }
            
            # Assign placeholders based on role
            if beat.role == "hook":
                beat_dict["placeholder"] = "{{HOOK_TEXT}}"
                beat_dict["notes"] = "Attention-grabbing opening. Keep under 3 seconds."
            elif beat.role == "problem":
                beat_dict["placeholder"] = "{{PROBLEM_TEXT}}"
                beat_dict["notes"] = "State the problem/pain point clearly."
            elif beat.role == "solution":
                beat_dict["placeholder"] = "{{MAIN_CONTENT}}"
                beat_dict["notes"] = "Your main value/content goes here."
            elif beat.role == "proof":
                beat_dict["placeholder"] = "{{PROOF_CLIP}}"
                beat_dict["notes"] = "Social proof, results, or testimonial."
            elif beat.role == "cta":
                beat_dict["placeholder"] = "{{CTA_TEXT}}"
                beat_dict["notes"] = "Clear call to action."
            
            template.append(beat_dict)
        
        # If no beat sheet, create a default structure
        if not template:
            template = [
                {"beat_index": 0, "role": "hook", "start_sec": 0, "end_sec": 3, 
                 "duration_sec": 3, "placeholder": "{{HOOK_TEXT}}", 
                 "notes": "Opening hook - grab attention immediately"},
                {"beat_index": 1, "role": "problem", "start_sec": 3, "end_sec": 8, 
                 "duration_sec": 5, "placeholder": "{{PROBLEM_TEXT}}", 
                 "notes": "State the problem or challenge"},
                {"beat_index": 2, "role": "solution", "start_sec": 8, "end_sec": 25, 
                 "duration_sec": 17, "placeholder": "{{MAIN_CONTENT}}", 
                 "notes": "Main content and value delivery"},
                {"beat_index": 3, "role": "cta", "start_sec": 25, "end_sec": 30, 
                 "duration_sec": 5, "placeholder": "{{CTA_TEXT}}", 
                 "notes": "Call to action"}
            ]
        
        return template
    
    def _build_remotion_spec_template(
        self,
        beat_template: List[Dict],
        style: Dict[str, Any],
        duration_sec: float
    ) -> Dict[str, Any]:
        """Build Remotion render spec with placeholders"""
        
        # Calculate frames
        fps = 30
        duration_frames = int(duration_sec * fps)
        
        # Build timeline from beats
        timeline = []
        
        # Background video placeholder
        timeline.append({
            "startSec": 0,
            "endSec": duration_sec,
            "type": "background_video",
            "src": "{{BACKGROUND_VIDEO}}",
            "params": {"fit": "cover"}
        })
        
        # Add beat-based elements
        for beat in beat_template:
            if beat.get("placeholder"):
                if beat["placeholder"] in ["{{HOOK_TEXT}}", "{{PROBLEM_TEXT}}", 
                                           "{{MAIN_CONTENT}}", "{{CTA_TEXT}}"]:
                    timeline.append({
                        "startSec": beat["start_sec"],
                        "endSec": beat["end_sec"],
                        "type": "text_overlay",
                        "text": beat["placeholder"],
                        "preset": self._get_text_preset(beat["role"], style),
                        "params": {
                            "position": "center",
                            "animation": "fade_in"
                        }
                    })
                elif beat["placeholder"] == "{{PROOF_CLIP}}":
                    timeline.append({
                        "startSec": beat["start_sec"],
                        "endSec": beat["end_sec"],
                        "type": "broll",
                        "src": "{{PROOF_CLIP}}",
                        "params": {"fit": "contain"}
                    })
        
        # Add captions layer
        timeline.append({
            "startSec": 0,
            "endSec": duration_sec,
            "type": "captions",
            "preset": style.get("caption_style", "default"),
            "src": "{{CAPTION_DATA}}"
        })
        
        # Add music layer
        timeline.append({
            "startSec": 0,
            "endSec": duration_sec,
            "type": "music",
            "src": "{{BACKGROUND_MUSIC}}",
            "params": {"gain_db": -12}
        })
        
        return {
            "schema": "remotion_render_spec_v1",
            "compositionId": "TemplateComposition",
            "fps": fps,
            "width": 1080,
            "height": 1920,
            "durationInFrames": duration_frames,
            "audio": {
                "musicUrl": "{{BACKGROUND_MUSIC}}",
                "narrationUrl": "{{NARRATION_AUDIO}}",
                "ducking": [{"startSec": 0, "endSec": duration_sec, "amountDb": 10}]
            },
            "timeline": timeline,
            "export": {
                "format": "mp4",
                "crf": 18,
                "audioCodec": "aac"
            }
        }
    
    def _get_text_preset(self, role: str, style: Dict) -> str:
        """Get text overlay preset based on role and style"""
        energy = style.get("energy_level", "medium")
        
        presets = {
            "hook": "HookPop" if energy == "high" else "HookSubtle",
            "problem": "ProblemHighlight",
            "solution": "ContentDefault",
            "proof": "ProofCallout",
            "cta": "CTABold"
        }
        
        return presets.get(role, "ContentDefault")
    
    def _identify_placeholders(
        self,
        beat_template: List[Dict],
        remotion_spec: Dict
    ) -> List[PlaceholderDef]:
        """Identify all placeholders used in the template"""
        placeholders = []
        found_keys = set()
        
        # Scan beat template
        for beat in beat_template:
            ph = beat.get("placeholder")
            if ph and ph not in found_keys:
                found_keys.add(ph)
                if ph in self.STANDARD_PLACEHOLDERS:
                    placeholders.append(self.STANDARD_PLACEHOLDERS[ph])
        
        # Scan remotion spec
        spec_str = json.dumps(remotion_spec)
        for key, placeholder in self.STANDARD_PLACEHOLDERS.items():
            if key in spec_str and key not in found_keys:
                found_keys.add(key)
                placeholders.append(placeholder)
        
        # Add any custom placeholders found
        custom_pattern = r'\{\{([A-Z_]+)\}\}'
        for match in re.findall(custom_pattern, spec_str):
            key = f"{{{{{match}}}}}"
            if key not in found_keys:
                found_keys.add(key)
                placeholders.append(PlaceholderDef(
                    key=key,
                    type="text",
                    description=f"Custom placeholder: {match}",
                    example="",
                    required=False
                ))
        
        return placeholders
    
    def _generate_swap_rules(
        self,
        beat_template: List[Dict],
        style: Dict
    ) -> List[SwapRule]:
        """Generate rules for content swapping"""
        rules = []
        
        # Hook swap rule
        hook_beat = next((b for b in beat_template if b["role"] == "hook"), None)
        if hook_beat:
            rules.append(SwapRule(
                element="hook",
                constraints={
                    "max_duration_sec": hook_beat.get("duration_sec", 3) + 1,
                    "min_duration_sec": max(1, hook_beat.get("duration_sec", 3) - 1)
                },
                tips="Keep hook punchy. Under 3 seconds is ideal. Strong pattern interrupt."
            ))
        
        # Main content swap rule
        main_beat = next((b for b in beat_template if b["role"] == "solution"), None)
        if main_beat:
            rules.append(SwapRule(
                element="main_content",
                constraints={
                    "min_duration_sec": 10,
                    "max_duration_sec": 45
                },
                tips="Can expand or contract based on content. Keep pacing consistent."
            ))
        
        # Music swap rule
        rules.append(SwapRule(
            element="background_music",
            constraints={
                "tempo_range": "90-130 BPM",
                "mood": style.get("energy_level", "medium")
            },
            tips="Match energy to content. Upbeat for tutorials, chill for storytelling."
        ))
        
        # B-roll swap rule
        rules.append(SwapRule(
            element="broll",
            constraints={
                "clip_duration": "2-5 seconds each",
                "quantity": "3-5 clips for 30s video"
            },
            tips="Use b-roll on every beat change. Supports retention."
        ))
        
        return rules
    
    def _determine_best_for(self, audit: PostDeepAudit) -> List[str]:
        """Determine what content types this template works best for"""
        best_for = []
        
        angle = audit.angle_type
        pillar = audit.content_pillar
        
        angle_mapping = {
            "tutorial": ["how-to content", "educational videos", "tips & tricks"],
            "listicle": ["top X lists", "roundups", "recommendations"],
            "case-study": ["success stories", "results showcase", "testimonials"],
            "myth-bust": ["controversial takes", "myth debunking", "hot takes"],
            "behind-the-scenes": ["process content", "day-in-the-life", "authenticity"],
            "transformation": ["before/after", "journey content", "progress updates"]
        }
        
        if angle in angle_mapping:
            best_for.extend(angle_mapping[angle])
        
        if pillar:
            best_for.append(f"{pillar} content")
        
        return best_for[:5] if best_for else ["general content"]
    
    def _assess_difficulty(
        self,
        style: Dict,
        beat_template: List[Dict]
    ) -> str:
        """Assess production difficulty"""
        complexity_score = 0
        
        # More beats = more complex
        complexity_score += len(beat_template) * 5
        
        # High cut density = more work
        if style.get("cut_density") == "high":
            complexity_score += 20
        
        # Many pattern interrupts = more editing
        complexity_score += len(style.get("pattern_interrupts", [])) * 5
        
        if complexity_score > 40:
            return "advanced"
        elif complexity_score > 20:
            return "intermediate"
        else:
            return "beginner"
    
    def _estimate_production_time(self, beat_template: List[Dict]) -> str:
        """Estimate production time"""
        beat_count = len(beat_template)
        
        if beat_count <= 3:
            return "15-20 minutes"
        elif beat_count <= 5:
            return "25-35 minutes"
        else:
            return "45-60 minutes"
    
    async def save_template(self, template: TemplatePack) -> str:
        """Save template pack to database"""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text("""
                    INSERT INTO competitor_template_pack (
                        account_id, source_post_id, template_name, template_slug,
                        style_fingerprint, beat_sheet_template, remotion_render_spec,
                        placeholders, swap_rules, best_for, difficulty_level,
                        estimated_production_time, preview_thumbnail_url
                    ) VALUES (
                        :account_id, :source_post_id, :template_name, :template_slug,
                        :style_fingerprint, :beat_sheet_template, :remotion_render_spec,
                        :placeholders, :swap_rules, :best_for, :difficulty_level,
                        :estimated_time, :preview_url
                    )
                    ON CONFLICT (template_slug) DO UPDATE SET
                        style_fingerprint = :style_fingerprint,
                        remotion_render_spec = :remotion_render_spec,
                        updated_at = NOW()
                    RETURNING template_id
                """), {
                    "account_id": template.account_id,
                    "source_post_id": template.source_post_id,
                    "template_name": template.template_name,
                    "template_slug": template.template_slug,
                    "style_fingerprint": template.style_fingerprint,
                    "beat_sheet_template": template.beat_sheet_template,
                    "remotion_render_spec": template.remotion_render_spec,
                    "placeholders": [asdict(p) for p in template.placeholders],
                    "swap_rules": [asdict(r) for r in template.swap_rules],
                    "best_for": template.best_for,
                    "difficulty_level": template.difficulty_level,
                    "estimated_time": template.estimated_production_time,
                    "preview_url": template.preview_thumbnail_url
                })
                conn.commit()
                row = result.fetchone()
                template.template_id = str(row[0])
                return template.template_id
        except Exception as e:
            logger.error(f"Failed to save template: {e}")
            raise
