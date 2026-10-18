"""
Competitor Audit System
=======================
Comprehensive competitor/influencer analysis with multi-tier data collection.

Tiers:
- Tier A: Public/visible data (profile, posts, visible metrics)
- Tier B: Authorized metrics (requires OAuth - impressions, retention, demographics)
- Tier C: AI inference (positioning, hooks, funnels, templates)

Components:
- CompetitorCollector: Fetch profile and posts from platforms
- CompetitorDeepAudit: AI analysis of content patterns
- FunnelMapper: Infer sales/marketing funnel structure
- PostRanker: Score and rank posts by various metrics
- ReportGenerator: Generate comprehensive strategy reports
- TemplateExporter: Create Remotion-ready template packs
"""

from .collector import CompetitorCollector
from .deep_audit import CompetitorDeepAuditService
from .funnel_mapper import FunnelMapper
from .post_ranker import PostRanker
from .report_generator import CompetitorReportGenerator
from .template_exporter import TemplateExporter
from .posting_time_analyzer import PostingTimeAnalyzer, PostingTimeRecommendation
from .hook_generator import HookGenerator, HookGenerationResult, HookIdea

__all__ = [
    "CompetitorCollector",
    "CompetitorDeepAuditService", 
    "FunnelMapper",
    "PostRanker",
    "CompetitorReportGenerator",
    "TemplateExporter",
    "PostingTimeAnalyzer",
    "PostingTimeRecommendation",
    "HookGenerator",
    "HookGenerationResult",
    "HookIdea",
]
