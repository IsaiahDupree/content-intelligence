"""
Competitor Report Generator
===========================
Generates comprehensive strategy reports combining all analysis components.
Outputs structured data, markdown reports, and actionable playbooks.
"""
import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict
from loguru import logger

from openai import OpenAI
from sqlalchemy import create_engine, text

from .deep_audit import AccountDeepAudit, PostDeepAudit
from .funnel_mapper import FunnelMap
from .post_ranker import RankingResult, PostScore


@dataclass
class UniqueFactors:
    """What makes this creator stand out"""
    positioning_statement: str  # "They help X achieve Y using Z"
    differentiators: List[str]
    emotional_promise: str
    credibility_signals: List[str]
    contrarian_beliefs: List[str]
    signature_elements: List[str]  # Editing style, catchphrases, etc.


@dataclass
class StrategyDecomposition:
    """Breakdown of content strategy"""
    content_pillars: Dict[str, float]  # {pillar: percentage}
    angle_library: Dict[str, int]  # {angle: count}
    hook_system: List[str]  # Top hook patterns
    retention_tactics: List[str]
    posting_frequency: str
    best_content_types: List[str]


@dataclass
class FunnelSummary:
    """Simplified funnel overview"""
    top_ctas: List[str]
    lead_capture_method: str
    offer_ladder: List[Dict[str, str]]  # [{tier, name, price_hint}]
    proof_types: List[str]
    funnel_clarity_score: float


@dataclass 
class TopPostAnalysis:
    """Analysis of a top-performing post"""
    post_id: str
    rank: int
    permalink: Optional[str]
    caption_preview: str
    scores: Dict[str, float]
    why_it_works: List[str]
    replication_notes: str


@dataclass
class Playbook:
    """Actionable implementation plan"""
    templates_to_replicate: List[Dict[str, Any]]  # 5 templates
    experiment_ideas: List[str]  # 10 A/B test ideas
    seven_day_plan: List[Dict[str, str]]  # Day-by-day content plan
    thirty_day_roadmap: List[str]  # Weekly milestones


@dataclass
class CompetitorAuditReport:
    """Complete competitor analysis report"""
    report_id: Optional[str] = None
    account_id: str = ""
    
    # Account info
    platform: str = ""
    handle: str = ""
    display_name: Optional[str] = None
    follower_count: int = 0
    
    # Analysis metadata
    posts_analyzed: int = 0
    time_window: str = "30d"
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    # Report sections
    unique_factors: Optional[UniqueFactors] = None
    strategy: Optional[StrategyDecomposition] = None
    funnel_summary: Optional[FunnelSummary] = None
    top_posts: List[TopPostAnalysis] = field(default_factory=list)
    playbook: Optional[Playbook] = None
    
    # Scores
    overall_strategy_score: float = 0.0
    funnel_clarity_score: float = 0.0
    content_consistency_score: float = 0.0
    
    # Markdown output
    report_markdown: str = ""


class CompetitorReportGenerator:
    """
    Generates comprehensive competitor analysis reports.
    Combines deep audit, funnel map, and rankings into actionable insights.
    """
    
    def __init__(
        self,
        db_url: Optional[str] = None,
        openai_api_key: Optional[str] = None
    ):
        self.db_url = db_url or os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
        self.engine = create_engine(self.db_url)
        
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=self.openai_api_key) if self.openai_api_key else None
    
    async def generate_report(
        self,
        account_id: str,
        platform: str,
        handle: str,
        display_name: Optional[str],
        follower_count: int,
        account_audit: AccountDeepAudit,
        funnel_map: FunnelMap,
        ranking: RankingResult,
        posts_data: List[Dict[str, Any]]
    ) -> CompetitorAuditReport:
        """
        Generate a complete competitor analysis report.
        
        Args:
            account_id: Database account ID
            platform: Platform name
            handle: Username
            display_name: Display name
            follower_count: Follower count
            account_audit: Aggregated deep audit
            funnel_map: Funnel analysis
            ranking: Post rankings
            posts_data: Raw post data for context
        """
        report = CompetitorAuditReport(
            account_id=account_id,
            platform=platform,
            handle=handle,
            display_name=display_name,
            follower_count=follower_count,
            posts_analyzed=account_audit.posts_analyzed
        )
        
        # 1. Build Unique Factors section
        report.unique_factors = UniqueFactors(
            positioning_statement=account_audit.positioning_statement,
            differentiators=account_audit.differentiators,
            emotional_promise=account_audit.emotional_promise,
            credibility_signals=account_audit.credibility_signals,
            contrarian_beliefs=[],  # Will be filled by AI
            signature_elements=[]
        )
        
        # 2. Build Strategy section
        report.strategy = StrategyDecomposition(
            content_pillars=account_audit.content_pillars,
            angle_library={k: v for k, v in sorted(
                account_audit.angle_distribution.items(),
                key=lambda x: x[1], reverse=True
            )},
            hook_system=account_audit.top_hooks,
            retention_tactics=account_audit.retention_tactics,
            posting_frequency=self._estimate_posting_frequency(posts_data),
            best_content_types=list(account_audit.content_pillars.keys())[:3]
        )
        
        # 3. Build Funnel Summary
        report.funnel_summary = FunnelSummary(
            top_ctas=funnel_map.top_cta_types,
            lead_capture_method=self._identify_lead_capture(funnel_map),
            offer_ladder=[
                {"tier": o.tier, "name": o.name, "price_hint": o.price_hint or ""}
                for o in funnel_map.offer_stack
            ],
            proof_types=funnel_map.proof_types,
            funnel_clarity_score=funnel_map.funnel_clarity_score
        )
        
        # 4. Build Top Posts Analysis
        for post_score in ranking.rankings[:5]:
            post_data = next(
                (p for p in posts_data if str(p.get("post_id")) == post_score.post_id),
                {}
            )
            
            report.top_posts.append(TopPostAnalysis(
                post_id=post_score.post_id,
                rank=post_score.rank,
                permalink=post_data.get("permalink"),
                caption_preview=(post_data.get("caption_text", "") or "")[:150] + "...",
                scores={
                    "velocity": post_score.velocity_score,
                    "engagement": post_score.engagement_score,
                    "viral_potential": post_score.viral_potential_score,
                    "overall": post_score.overall_score
                },
                why_it_works=self._analyze_why_it_works(post_score, post_data),
                replication_notes=""
            ))
        
        # 5. Generate Playbook with AI
        report.playbook = await self._generate_playbook(report, posts_data)
        
        # 6. Calculate scores
        report.overall_strategy_score = self._calculate_strategy_score(account_audit)
        report.funnel_clarity_score = funnel_map.funnel_clarity_score
        report.content_consistency_score = account_audit.style_consistency_score
        
        # 7. Generate markdown report
        report.report_markdown = self._generate_markdown(report)
        
        return report
    
    def _estimate_posting_frequency(self, posts: List[Dict]) -> str:
        """Estimate posting frequency from post timestamps"""
        if len(posts) < 2:
            return "Unknown"
        
        # Sort by date
        dated_posts = [p for p in posts if p.get("posted_at")]
        if len(dated_posts) < 2:
            return "Unknown"
        
        try:
            dates = []
            for p in dated_posts:
                dt_str = p["posted_at"]
                if isinstance(dt_str, str):
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                else:
                    dt = dt_str
                dates.append(dt)
            
            dates.sort()
            
            # Calculate average gap
            gaps = [(dates[i+1] - dates[i]).days for i in range(len(dates)-1)]
            avg_gap = sum(gaps) / len(gaps) if gaps else 7
            
            if avg_gap <= 1:
                return "Daily or more"
            elif avg_gap <= 2:
                return "Every 1-2 days"
            elif avg_gap <= 4:
                return "2-3x per week"
            elif avg_gap <= 7:
                return "Weekly"
            else:
                return f"Every {int(avg_gap)} days"
        except:
            return "Unknown"
    
    def _identify_lead_capture(self, funnel: FunnelMap) -> str:
        """Identify primary lead capture method"""
        if funnel.lead_magnets:
            magnet_types = [m.type for m in funnel.lead_magnets]
            if "dm_trigger" in magnet_types or "comment_trigger" in magnet_types:
                return "DM automation"
            elif "newsletter" in magnet_types or "signup" in magnet_types:
                return "Email newsletter"
            elif "community" in magnet_types:
                return "Community"
            elif "freebie" in magnet_types:
                return "Free resource"
        
        if "comment_keyword" in funnel.top_cta_types:
            return "Comment triggers → DM"
        elif "link_bio" in funnel.top_cta_types:
            return "Link in bio"
        elif "dm_me" in funnel.top_cta_types:
            return "Direct DM requests"
        
        return "Not clearly defined"
    
    def _analyze_why_it_works(
        self,
        score: PostScore,
        post_data: Dict
    ) -> List[str]:
        """Generate reasons why this post performed well"""
        reasons = []
        
        if score.velocity_score > 70:
            reasons.append(f"High velocity: {score.views_per_hour:.0f} views/hour")
        
        if score.engagement_rate and score.engagement_rate > 0.03:
            reasons.append(f"Strong engagement: {score.engagement_rate:.1%} interaction rate")
        
        if score.viral_potential_score > 60:
            reasons.append("High share/comment ratio indicates viral potential")
        
        caption = post_data.get("caption_text", "")
        if caption:
            if len(caption) < 100:
                reasons.append("Concise caption - easy to consume")
            if "?" in caption:
                reasons.append("Uses questions to drive comments")
            if any(kw in caption.lower() for kw in ["dm", "comment", "link"]):
                reasons.append("Clear call-to-action in caption")
        
        if not reasons:
            reasons.append("Solid overall metrics")
        
        return reasons
    
    def _calculate_strategy_score(self, audit: AccountDeepAudit) -> float:
        """Calculate overall strategy effectiveness score"""
        score = 0
        
        # Content variety (multiple pillars is good)
        pillars_count = len(audit.content_pillars)
        if 3 <= pillars_count <= 5:
            score += 25
        elif pillars_count >= 2:
            score += 15
        
        # Hook consistency
        if len(audit.top_hooks) >= 3:
            score += 20
        
        # Clear positioning
        if audit.positioning_statement:
            score += 20
        
        # Retention tactics
        if audit.avg_retention_score > 60:
            score += 20
        elif audit.avg_retention_score > 40:
            score += 10
        
        # CTA consistency
        if audit.most_effective_cta:
            score += 15
        
        return min(100, score)
    
    async def _generate_playbook(
        self,
        report: CompetitorAuditReport,
        posts_data: List[Dict]
    ) -> Playbook:
        """Generate actionable playbook using AI"""
        
        if not self.client:
            return Playbook(
                templates_to_replicate=[],
                experiment_ideas=["Configure OpenAI to generate playbook"],
                seven_day_plan=[],
                thirty_day_roadmap=[]
            )
        
        # Build context for AI
        context = {
            "handle": report.handle,
            "platform": report.platform,
            "positioning": report.unique_factors.positioning_statement if report.unique_factors else "",
            "content_pillars": list(report.strategy.content_pillars.keys()) if report.strategy else [],
            "top_hooks": report.strategy.hook_system if report.strategy else [],
            "top_ctas": report.funnel_summary.top_ctas if report.funnel_summary else [],
            "top_posts": [
                {"rank": p.rank, "caption": p.caption_preview, "scores": p.scores}
                for p in report.top_posts[:3]
            ]
        }
        
        prompt = f"""Based on this competitor analysis, create an actionable playbook for replicating their success.

COMPETITOR ANALYSIS:
{json.dumps(context, indent=2)}

Generate a playbook with:
1. 5 specific templates to replicate (format: hook style + structure + CTA)
2. 10 A/B test experiment ideas
3. 7-day content plan using their patterns
4. 30-day roadmap with weekly milestones

Return JSON:
{{
    "templates_to_replicate": [
        {{
            "name": "Template name",
            "hook_pattern": "The hook formula",
            "structure": "beat-by-beat structure",
            "cta_style": "how to end",
            "example_topic": "topic to apply it to"
        }}
    ],
    "experiment_ideas": ["specific test idea 1", "specific test idea 2", ...],
    "seven_day_plan": [
        {{"day": 1, "content_type": "type", "topic": "topic", "hook_style": "style"}}
    ],
    "thirty_day_roadmap": ["Week 1: ...", "Week 2: ...", "Week 3: ...", "Week 4: ..."]
}}"""

        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a content strategist creating actionable playbooks from competitor analysis."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.7
            )
            
            result = json.loads(response.choices[0].message.content)
            
            return Playbook(
                templates_to_replicate=result.get("templates_to_replicate", []),
                experiment_ideas=result.get("experiment_ideas", []),
                seven_day_plan=result.get("seven_day_plan", []),
                thirty_day_roadmap=result.get("thirty_day_roadmap", [])
            )
            
        except Exception as e:
            logger.error(f"Playbook generation failed: {e}")
            return Playbook(
                templates_to_replicate=[],
                experiment_ideas=[],
                seven_day_plan=[],
                thirty_day_roadmap=[]
            )
    
    def _generate_markdown(self, report: CompetitorAuditReport) -> str:
        """Generate markdown report"""
        md = f"""# Competitor Analysis Report: @{report.handle}

**Platform:** {report.platform.title()}  
**Followers:** {report.follower_count:,}  
**Posts Analyzed:** {report.posts_analyzed}  
**Generated:** {report.generated_at}

---

## 🎯 Unique Factors

"""
        if report.unique_factors:
            md += f"""**Positioning:** {report.unique_factors.positioning_statement}

**Emotional Promise:** {report.unique_factors.emotional_promise}

**Differentiators:**
"""
            for d in report.unique_factors.differentiators:
                md += f"- {d}\n"
            
            md += "\n**Credibility Signals:**\n"
            for c in report.unique_factors.credibility_signals:
                md += f"- {c}\n"
        
        md += "\n---\n\n## 📊 Strategy Decomposition\n\n"
        
        if report.strategy:
            md += "**Content Pillars:**\n"
            for pillar, pct in report.strategy.content_pillars.items():
                md += f"- {pillar}: {pct:.0f}%\n"
            
            md += f"\n**Posting Frequency:** {report.strategy.posting_frequency}\n"
            
            md += "\n**Top Hook Patterns:**\n"
            for hook in report.strategy.hook_system[:5]:
                md += f"- \"{hook}\"\n"
            
            md += "\n**Retention Tactics:**\n"
            for tactic in report.strategy.retention_tactics:
                md += f"- {tactic}\n"
        
        md += "\n---\n\n## 🔄 Funnel Setup\n\n"
        
        if report.funnel_summary:
            md += f"**Lead Capture:** {report.funnel_summary.lead_capture_method}\n"
            md += f"**Funnel Clarity:** {report.funnel_summary.funnel_clarity_score:.0f}/100\n\n"
            
            md += "**Top CTAs:**\n"
            for cta in report.funnel_summary.top_ctas:
                md += f"- {cta}\n"
            
            md += "\n**Offer Ladder:**\n"
            for offer in report.funnel_summary.offer_ladder:
                md += f"- [{offer['tier'].upper()}] {offer['name']} {offer.get('price_hint', '')}\n"
        
        md += "\n---\n\n## 🔥 Top Performing Posts\n\n"
        
        for post in report.top_posts:
            md += f"""### #{post.rank} - Score: {post.scores.get('overall', 0):.0f}/100

**Caption:** {post.caption_preview}

**Why It Works:**
"""
            for reason in post.why_it_works:
                md += f"- {reason}\n"
            
            if post.permalink:
                md += f"\n[View Post]({post.permalink})\n"
            md += "\n"
        
        md += "---\n\n## 📋 Playbook\n\n"
        
        if report.playbook:
            md += "### Templates to Replicate\n\n"
            for i, template in enumerate(report.playbook.templates_to_replicate, 1):
                md += f"**{i}. {template.get('name', 'Template')}**\n"
                md += f"- Hook: {template.get('hook_pattern', '')}\n"
                md += f"- Structure: {template.get('structure', '')}\n"
                md += f"- CTA: {template.get('cta_style', '')}\n\n"
            
            md += "### Experiment Ideas\n\n"
            for idea in report.playbook.experiment_ideas[:10]:
                md += f"- {idea}\n"
            
            md += "\n### 7-Day Content Plan\n\n"
            for day in report.playbook.seven_day_plan:
                md += f"- **Day {day.get('day', '?')}:** {day.get('content_type', '')} - {day.get('topic', '')}\n"
            
            md += "\n### 30-Day Roadmap\n\n"
            for milestone in report.playbook.thirty_day_roadmap:
                md += f"- {milestone}\n"
        
        md += f"""
---

## 📈 Scores

| Metric | Score |
|--------|-------|
| Strategy Effectiveness | {report.overall_strategy_score:.0f}/100 |
| Funnel Clarity | {report.funnel_clarity_score:.0f}/100 |
| Content Consistency | {report.content_consistency_score:.0f}/100 |

---

*Report generated by MediaPoster Competitor Audit System*
"""
        
        return md
    
    async def save_report(self, report: CompetitorAuditReport) -> str:
        """Save report to database"""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text("""
                    INSERT INTO competitor_audit_report (
                        account_id, report_version, posts_analyzed, time_window,
                        unique_factors, strategy, funnel_summary, top_posts_analysis,
                        playbook, overall_strategy_score, funnel_clarity_score,
                        content_consistency_score, report_markdown
                    ) VALUES (
                        :account_id, '1.0', :posts_analyzed, :time_window,
                        :unique_factors, :strategy, :funnel_summary, :top_posts,
                        :playbook, :strategy_score, :funnel_score,
                        :consistency_score, :markdown
                    )
                    RETURNING report_id
                """), {
                    "account_id": report.account_id,
                    "posts_analyzed": report.posts_analyzed,
                    "time_window": report.time_window,
                    "unique_factors": asdict(report.unique_factors) if report.unique_factors else None,
                    "strategy": asdict(report.strategy) if report.strategy else None,
                    "funnel_summary": asdict(report.funnel_summary) if report.funnel_summary else None,
                    "top_posts": [asdict(p) for p in report.top_posts],
                    "playbook": asdict(report.playbook) if report.playbook else None,
                    "strategy_score": report.overall_strategy_score,
                    "funnel_score": report.funnel_clarity_score,
                    "consistency_score": report.content_consistency_score,
                    "markdown": report.report_markdown
                })
                conn.commit()
                row = result.fetchone()
                report.report_id = str(row[0])
                return report.report_id
        except Exception as e:
            logger.error(f"Failed to save report: {e}")
            raise
