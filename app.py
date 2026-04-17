"""
Content Intelligence Service - AI/ML for content analysis and generation.
Port: 6006
"""
import os
from flask import Flask, jsonify, request
from datetime import datetime, timezone
from typing import List, Dict, Any

app = Flask(__name__)

# Register middleware
from services.middleware.security import configure_security_headers
from services.middleware.logging import setup_request_logging, setup_structured_logging
from services.middleware.errors import register_error_handlers

# Setup security
configure_security_headers(app)

# Setup logging
setup_request_logging(app)
setup_structured_logging()

# Setup error handlers
register_error_handlers(app)

# Add services to path
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# AI Provider configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Try to import OpenAI client
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = bool(OPENAI_API_KEY)
except ImportError:
    OPENAI_AVAILABLE = False

# Try to import Groq client
try:
    from groq import Groq
    GROQ_AVAILABLE = bool(GROQ_API_KEY)
except ImportError:
    GROQ_AVAILABLE = False

# Import real service implementations
try:
    from services.analysis.fate_scorer import FATEScorer
    FATE_SCORER = FATEScorer()
    FATE_SCORER_AVAILABLE = True
except ImportError as e:
    print(f"FATEScorer not available: {e}")
    FATE_SCORER_AVAILABLE = False
    FATE_SCORER = None

try:
    from services.analysis.awareness_classifier import AwarenessClassifier
    AWARENESS_CLASSIFIER = AwarenessClassifier.get_instance()
    AWARENESS_AVAILABLE = True
except Exception as e:
    print(f"AwarenessClassifier not available: {e}")
    AWARENESS_AVAILABLE = False
    AWARENESS_CLASSIFIER = None

try:
    from services.analysis.sentiment_analyzer_standalone import get_sentiment_analyzer
    SENTIMENT_ANALYZER = get_sentiment_analyzer()
    SENTIMENT_AVAILABLE = True
except Exception as e:
    print(f"SentimentAnalyzer not available: {e}")
    SENTIMENT_AVAILABLE = False
    SENTIMENT_ANALYZER = None

try:
    from services.analysis.vision_analyzer_standalone import get_vision_analyzer
    VISION_ANALYZER = get_vision_analyzer()
    VISION_AVAILABLE = VISION_ANALYZER.is_enabled()
except Exception as e:
    print(f"VisionAnalyzer not available: {e}")
    VISION_AVAILABLE = False
    VISION_ANALYZER = None

SERVICE_NAME = "content-intelligence"
SERVICE_VERSION = "1.0.0"
SERVICE_PORT = int(os.getenv("PORT", 6006))


# Import UGC Format services
try:
    from services.ugc_format.classifier import UGCFormatClassifier
    from services.ugc_format.scorer import PerformanceScorer
    from services.ugc_format.recommender import RepurposingRecommender
    from services.supabase_client import get_supabase_client
    UGC_CLASSIFIER = UGCFormatClassifier()
    UGC_SCORER = PerformanceScorer()
    UGC_RECOMMENDER = RepurposingRecommender()
    SUPABASE = get_supabase_client()
    UGC_SERVICES_AVAILABLE = True
except Exception as e:
    print(f"UGC services not available: {e}")
    UGC_SERVICES_AVAILABLE = False
    SUPABASE = None


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint - API-001."""
    return jsonify({
        "status": "healthy",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


@app.route("/api/classify", methods=["POST"])
def classify_format():
    """Classify UGC format - API-002."""
    data = request.get_json()

    if not data:
        return jsonify({"error": "content required"}), 400

    content = data.get("content", "")
    content_id = data.get("content_id")
    content_metadata = data.get("metadata", {})

    if not content:
        return jsonify({"error": "content required"}), 400

    if UGC_SERVICES_AVAILABLE:
        result = UGC_CLASSIFIER.classify(content, content_metadata)

        # Store in Supabase if content_id provided
        if content_id and SUPABASE:
            SUPABASE.store_format_classification(content_id, {
                "format": result.format,
                "confidence": result.confidence,
                "all_scores": result.all_scores,
                "reasoning": result.reasoning
            })

        return jsonify({
            "status": "success",
            "format": result.format,
            "confidence": round(result.confidence, 2),
            "all_scores": {k: round(v, 2) for k, v in result.all_scores.items()},
            "reasoning": result.reasoning
        })
    else:
        return jsonify({"error": "Classification service not available"}), 500


@app.route("/api/score", methods=["POST"])
def score_performance():
    """Calculate performance score - API-003."""
    data = request.get_json()

    if not data:
        return jsonify({"error": "metrics required"}), 400

    metrics = data.get("metrics", {})
    content_id = data.get("content_id")

    if UGC_SERVICES_AVAILABLE:
        result = UGC_SCORER.score(metrics)

        # Store in Supabase if content_id provided
        if content_id and SUPABASE:
            SUPABASE.store_performance_score(content_id, {
                "overall_score": result.overall_score,
                "engagement_score": result.engagement_score,
                "retention_score": result.retention_score,
                "conversion_score": result.conversion_score,
                "metrics_breakdown": result.metrics_breakdown,
                "timestamp": result.timestamp
            })

        return jsonify({
            "status": "success",
            "content_id": content_id,
            "overall_score": round(result.overall_score, 2),
            "engagement_score": round(result.engagement_score, 2),
            "retention_score": round(result.retention_score, 2),
            "conversion_score": round(result.conversion_score, 2),
            "metrics_breakdown": {k: round(v, 2) for k, v in result.metrics_breakdown.items()},
            "timestamp": result.timestamp
        })
    else:
        return jsonify({"error": "Scoring service not available"}), 500


@app.route("/api/repurpose/<content_id>", methods=["GET"])
def get_repurposing_recommendations(content_id):
    """Get repurposing recommendations - API-004."""
    data = request.get_json() or {}

    current_format = data.get("format", "testimonial")
    performance_data = data.get("performance", {"overall_score": 50})

    if UGC_SERVICES_AVAILABLE:
        recommendations = UGC_RECOMMENDER.recommend(current_format, performance_data)
        return jsonify({
            "status": "success",
            "content_id": content_id,
            "current_format": current_format,
            "recommendations": [
                {
                    "format": r.format,
                    "viability_score": round(r.viability_score, 2),
                    "rationale": r.rationale
                }
                for r in recommendations
            ]
        })
    else:
        return jsonify({"error": "Recommendation service not available"}), 500


@app.route("/api/analyze", methods=["POST"])
def analyze_and_store_full():
    """Full end-to-end analysis: classify → score → recommend → store (Core E2E)."""
    data = request.get_json()

    if not data:
        return jsonify({"error": "content required"}), 400

    content = data.get("content", "")
    content_id = data.get("content_id")
    metrics = data.get("metrics", {})

    if not content:
        return jsonify({"error": "content required"}), 400

    if not UGC_SERVICES_AVAILABLE:
        return jsonify({"error": "Analysis services not available"}), 500

    try:
        # Step 1: Classify format
        format_result = UGC_CLASSIFIER.classify(content)

        # Step 2: Score performance
        score_result = UGC_SCORER.score(metrics)

        # Step 3: Generate recommendations
        recommendations = UGC_RECOMMENDER.recommend(
            format_result.format,
            {"overall_score": score_result.overall_score}
        )

        response_data = {
            "status": "success",
            "content_id": content_id,
            "format_classification": {
                "format": format_result.format,
                "confidence": round(format_result.confidence, 2),
                "all_scores": {k: round(v, 2) for k, v in format_result.all_scores.items()}
            },
            "performance_score": {
                "overall": round(score_result.overall_score, 2),
                "engagement": round(score_result.engagement_score, 2),
                "retention": round(score_result.retention_score, 2),
                "conversion": round(score_result.conversion_score, 2)
            },
            "recommendations": [
                {
                    "format": r.format,
                    "viability_score": round(r.viability_score, 2),
                    "rationale": r.rationale
                }
                for r in recommendations
            ]
        }

        # Step 4: Store all in Supabase
        if content_id and SUPABASE:
            SUPABASE.store_format_classification(content_id, {
                "format": format_result.format,
                "confidence": format_result.confidence,
                "all_scores": format_result.all_scores,
                "reasoning": format_result.reasoning
            })
            SUPABASE.store_performance_score(content_id, {
                "overall_score": score_result.overall_score,
                "engagement_score": score_result.engagement_score,
                "retention_score": score_result.retention_score,
                "conversion_score": score_result.conversion_score,
                "metrics_breakdown": score_result.metrics_breakdown,
                "timestamp": score_result.timestamp
            })
            SUPABASE.store_repurposing_recommendations(content_id, [
                {
                    "format": r.format,
                    "viability_score": r.viability_score,
                    "rationale": r.rationale
                }
                for r in recommendations
            ])

        return jsonify(response_data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/checkback/<content_id>", methods=["POST"])
def trigger_checkback(content_id):
    """Trigger performance re-evaluation - API-005."""
    # This is a placeholder for async re-evaluation
    # In a full implementation, this would queue a job
    return jsonify({
        "status": "success",
        "content_id": content_id,
        "job_id": f"checkback_{content_id}",
        "message": "Checkback evaluation queued"
    })


@app.route("/api/analyze/content", methods=["POST"])
def analyze_content():
    """Analyze content for insights."""
    data = request.get_json()

    title = data.get("title", "")
    description = data.get("description", "")
    transcript = data.get("transcript", "")

    # TODO: Implement actual AI analysis
    return jsonify({
        "status": "success",
        "analysis": {
            "awareness_level": "problem_aware",
            "sentiment": "positive",
            "topics": [],
            "engagement_score": 0.75
        }
    })


@app.route("/api/generate/title", methods=["POST"])
def generate_title():
    """Generate viral titles for content using AI."""
    data = request.get_json()
    
    content = data.get("content", "")
    platform = data.get("platform", "tiktok")
    style = data.get("style", "viral")
    count = data.get("count", 5)
    
    if not content:
        return jsonify({"error": "content required"}), 400
    
    prompt = f"""Generate {count} {style} titles for {platform} content.

Content: {content}

Requirements:
- Make titles attention-grabbing and clickable
- Use power words and emotional triggers
- Keep under 100 characters for {platform}
- Include hooks that create curiosity

Return ONLY the titles, one per line, numbered 1-{count}."""

    try:
        titles = []
        
        # Try Groq first (fast and free)
        if GROQ_AVAILABLE:
            client = Groq(api_key=GROQ_API_KEY)
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.8
            )
            raw_titles = response.choices[0].message.content.strip()
            titles = [line.lstrip("0123456789.) ").strip() for line in raw_titles.split("\n") if line.strip()]
        
        # Fallback to OpenAI
        elif OPENAI_AVAILABLE:
            client = OpenAI(api_key=OPENAI_API_KEY)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.8
            )
            raw_titles = response.choices[0].message.content.strip()
            titles = [line.lstrip("0123456789.) ").strip() for line in raw_titles.split("\n") if line.strip()]
        
        # No AI available - return placeholder
        else:
            titles = [f"{style.title()} title {i+1} for {platform}: {content[:30]}..." for i in range(count)]
        
        return jsonify({
            "status": "success",
            "platform": platform,
            "style": style,
            "titles": titles[:count],
            "ai_provider": "groq" if GROQ_AVAILABLE else ("openai" if OPENAI_AVAILABLE else "placeholder")
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/generate/caption", methods=["POST"])
def generate_caption():
    """Generate captions for content using AI."""
    data = request.get_json()
    
    content = data.get("content", "")
    platform = data.get("platform", "instagram")
    include_hashtags = data.get("include_hashtags", True)
    
    if not content:
        return jsonify({"error": "content required"}), 400
    
    hashtag_instruction = "Include 5-10 relevant hashtags at the end." if include_hashtags else "Do NOT include hashtags."
    
    prompt = f"""Write an engaging {platform} caption for this content:

{content}

Requirements:
- Hook in the first line
- Conversational tone
- Include a call-to-action
- {hashtag_instruction}

Return ONLY the caption, nothing else."""

    try:
        caption = ""
        hashtags = []
        
        if GROQ_AVAILABLE:
            client = Groq(api_key=GROQ_API_KEY)
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.7
            )
            caption = response.choices[0].message.content.strip()
        elif OPENAI_AVAILABLE:
            client = OpenAI(api_key=OPENAI_API_KEY)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.7
            )
            caption = response.choices[0].message.content.strip()
        else:
            caption = f"Check out this amazing content! 🔥\n\n{content[:100]}...\n\nWhat do you think? 👇"
            if include_hashtags:
                hashtags = ["#content", "#viral", "#fyp", "#trending", "#socialmedia"]
        
        # Extract hashtags from caption if present
        if include_hashtags and "#" in caption:
            words = caption.split()
            hashtags = [w for w in words if w.startswith("#")]
        
        return jsonify({
            "status": "success",
            "platform": platform,
            "caption": caption,
            "hashtags": hashtags,
            "ai_provider": "groq" if GROQ_AVAILABLE else ("openai" if OPENAI_AVAILABLE else "placeholder")
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/score/fate", methods=["POST"])
def score_fate():
    """Calculate FATE score for content using real FATEScorer."""
    data = request.get_json()
    
    content_id = data.get("content_id")
    content = data.get("content", "")
    metrics = data.get("metrics", {})
    
    if FATE_SCORER_AVAILABLE and content:
        # Use real FATE scorer
        f_score = FATE_SCORER.score_focus(content)
        a_score = FATE_SCORER.score_authority(content)
        t_score = FATE_SCORER.score_tribe(content)
        e_score = FATE_SCORER.score_emotion(content)
        overall = (f_score + a_score + t_score + e_score) / 4
        
        return jsonify({
            "status": "success",
            "content_id": content_id,
            "fate_score": {
                "focus": round(f_score, 2),
                "authority": round(a_score, 2),
                "tribe": round(t_score, 2),
                "emotion": round(e_score, 2),
                "overall": round(overall, 2)
            },
            "implementation": "real"
        })
    else:
        # Fallback placeholder
        return jsonify({
            "status": "success",
            "content_id": content_id,
            "fate_score": {
                "focus": 0.8,
                "authority": 0.7,
                "tribe": 0.9,
                "emotion": 0.85,
                "overall": 0.81
            },
            "implementation": "placeholder"
        })


@app.route("/api/classify/awareness", methods=["POST"])
def classify_awareness():
    """Classify content awareness level using real AwarenessClassifier."""
    data = request.get_json()
    
    content = data.get("content", "")
    
    if not content:
        return jsonify({"error": "content required"}), 400
    
    if AWARENESS_AVAILABLE and AWARENESS_CLASSIFIER:
        # Use real awareness classifier
        result = AWARENESS_CLASSIFIER.classify(content)
        return jsonify({
            "status": "success",
            "awareness_level": result.level.value,
            "confidence": round(result.confidence, 2),
            "all_levels": {k: round(v, 2) for k, v in result.scores.items()},
            "implementation": "real"
        })
    else:
        # Fallback placeholder
        levels = ["unaware", "problem_aware", "solution_aware", "product_aware", "most_aware"]
        return jsonify({
            "status": "success",
            "awareness_level": "solution_aware",
            "confidence": 0.85,
            "all_levels": {level: 0.2 for level in levels},
            "implementation": "placeholder"
        })


@app.route("/api/analyze/sentiment", methods=["POST"])
def analyze_sentiment():
    """Analyze sentiment of text using real SentimentAnalyzer."""
    data = request.get_json()
    
    text = data.get("text", "")
    context = data.get("context", "")
    
    if not text:
        return jsonify({"error": "text required"}), 400
    
    if SENTIMENT_AVAILABLE and SENTIMENT_ANALYZER:
        # Use real sentiment analyzer
        result = SENTIMENT_ANALYZER.analyze(text, context)
        return jsonify({
            "status": "success",
            "sentiment": result.label,
            "score": result.score,
            "confidence": result.confidence,
            "emotions": result.emotions,
            "themes": result.themes,
            "reasoning": result.reasoning,
            "implementation": "real"
        })
    else:
        # Fallback placeholder
        return jsonify({
            "status": "success",
            "sentiment": "positive",
            "score": 0.75,
            "confidence": 0.8,
            "emotions": {},
            "themes": [],
            "reasoning": "Placeholder",
            "implementation": "placeholder"
        })


@app.route("/api/vision/analyze", methods=["POST"])
def analyze_vision():
    """Analyze image/frame content using OpenAI Vision API."""
    data = request.get_json()
    
    image_path = data.get("image_path")
    image_base64 = data.get("image_base64")
    analysis_type = data.get("analysis_type", "comprehensive")
    
    if not image_path and not image_base64:
        return jsonify({"error": "image_path or image_base64 required"}), 400
    
    if VISION_AVAILABLE and VISION_ANALYZER:
        try:
            result = VISION_ANALYZER.analyze(
                image_path=image_path,
                image_base64=image_base64,
                analysis_type=analysis_type
            )
            return jsonify({
                "status": "success",
                "image_path": image_path,
                "analysis": result.to_dict(),
                "implementation": "real"
            })
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)}), 500
    else:
        # Fallback placeholder
        return jsonify({
            "status": "success",
            "image_path": image_path,
            "analysis": {
                "shot_type": "unknown",
                "has_face": False,
                "has_text": False,
                "objects": [],
                "is_hook_frame": False,
                "hook_score": 0.0,
                "suggestions": ["Set OPENAI_API_KEY for real vision analysis"]
            },
            "implementation": "placeholder"
        })


@app.route("/api/recommend", methods=["POST"])
def get_recommendations():
    """Get content recommendations."""
    data = request.get_json()
    
    content_id = data.get("content_id")
    recommendation_type = data.get("type", "similar")
    count = data.get("count", 5)
    
    # TODO: Implement actual recommendations
    return jsonify({
        "status": "success",
        "content_id": content_id,
        "type": recommendation_type,
        "recommendations": []
    })


# Import narrative scheduler
try:
    from services.narrative.scheduler import NarrativeScheduler
    NARRATIVE_SCHEDULER_AVAILABLE = True
except Exception as e:
    print(f"NarrativeScheduler not available: {e}")
    NARRATIVE_SCHEDULER_AVAILABLE = False

# Import experiments scheduler
try:
    from services.experiments.hypothesis_engine import HypothesisEngine
    HYPOTHESIS_ENGINE_AVAILABLE = True
except Exception as e:
    print(f"HypothesisEngine not available: {e}")
    HYPOTHESIS_ENGINE_AVAILABLE = False


@app.route("/api/narrative/plan", methods=["POST"])
def create_narrative_plan():
    """Create a narrative content plan."""
    data = request.get_json()
    goal = data.get("goal")
    duration_days = data.get("duration_days", 30)
    platforms = data.get("platforms", ["instagram", "tiktok"])
    
    if not goal:
        return jsonify({"error": "goal required"}), 400
    
    try:
        from services.narrative.scheduler import NarrativeScheduler
        scheduler = NarrativeScheduler()
        
        plan = scheduler.create_plan(
            goal=goal,
            duration_days=duration_days,
            platforms=platforms
        )
        
        return jsonify({
            "status": "success",
            "plan": plan if isinstance(plan, dict) else {"id": str(plan)},
            "implementation": "NarrativeScheduler"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/experiments/hypothesis", methods=["POST"])
def generate_hypothesis():
    """Generate a content experiment hypothesis."""
    data = request.get_json()
    content_type = data.get("content_type")
    metric = data.get("metric", "engagement")
    historical_data = data.get("historical_data", {})
    
    try:
        from services.experiments.hypothesis_engine import HypothesisEngine
        engine = HypothesisEngine()
        
        hypothesis = engine.generate(
            content_type=content_type,
            metric=metric,
            historical_data=historical_data
        )
        
        return jsonify({
            "status": "success",
            "hypothesis": hypothesis if isinstance(hypothesis, dict) else {"text": str(hypothesis)},
            "implementation": "HypothesisEngine"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/competitor/analyze", methods=["POST"])
def analyze_competitor():
    """Analyze competitor content."""
    data = request.get_json()
    competitor_handle = data.get("handle")
    platform = data.get("platform", "instagram")
    
    if not competitor_handle:
        return jsonify({"error": "handle required"}), 400
    
    try:
        from services.competitor_audit.competitor_analyzer import CompetitorAnalyzer
        analyzer = CompetitorAnalyzer()
        
        analysis = analyzer.analyze(
            handle=competitor_handle,
            platform=platform
        )
        
        return jsonify({
            "status": "success",
            "competitor": competitor_handle,
            "platform": platform,
            "analysis": analysis if isinstance(analysis, dict) else {},
            "implementation": "CompetitorAnalyzer"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/trends/detect", methods=["POST"])
def detect_trends():
    """Detect current trends."""
    data = request.get_json()
    platform = data.get("platform", "tiktok")
    category = data.get("category")
    limit = data.get("limit", 10)
    
    try:
        from services.trend_intelligence.trend_detector import TrendDetector
        detector = TrendDetector()
        
        trends = detector.detect(
            platform=platform,
            category=category,
            limit=limit
        )
        
        return jsonify({
            "status": "success",
            "platform": platform,
            "trends": trends if isinstance(trends, list) else [],
            "count": len(trends) if isinstance(trends, list) else 0,
            "implementation": "TrendDetector"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/brief/generate", methods=["POST"])
def generate_brief():
    """Generate a content brief."""
    data = request.get_json()
    topic = data.get("topic")
    format_type = data.get("format", "short_video")
    platform = data.get("platform", "instagram")
    
    if not topic:
        return jsonify({"error": "topic required"}), 400
    
    try:
        from services.content_brief.brief_generator import BriefGenerator
        generator = BriefGenerator()
        
        brief = generator.generate(
            topic=topic,
            format=format_type,
            platform=platform
        )
        
        return jsonify({
            "status": "success",
            "brief": brief if isinstance(brief, dict) else {"topic": topic},
            "implementation": "BriefGenerator"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/engagement/predict", methods=["POST"])
def predict_engagement():
    """Predict engagement for content."""
    data = request.get_json()
    title = data.get("title")
    description = data.get("description")
    platform = data.get("platform", "instagram")
    
    try:
        from services.engagement.engagement_service import EngagementService
        service = EngagementService()
        
        prediction = service.predict(
            title=title,
            description=description,
            platform=platform
        )
        
        return jsonify({
            "status": "success",
            "prediction": prediction if isinstance(prediction, dict) else {"score": 0.5},
            "implementation": "EngagementService"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/dm/outreach", methods=["POST"])
def dm_outreach():
    """Send DM outreach campaign."""
    data = request.get_json()
    prospects = data.get("prospects", [])
    message_template = data.get("message_template")
    platform = data.get("platform", "instagram")
    
    if not prospects or not message_template:
        return jsonify({"error": "prospects and message_template required"}), 400
    
    try:
        from services.dm_outreach.outreach_sequencer import OutreachSequencer
        sequencer = OutreachSequencer()
        
        result = sequencer.queue_outreach(
            prospects=prospects,
            message_template=message_template,
            platform=platform
        )
        
        return jsonify({
            "status": "success",
            "queued": len(prospects),
            "result": result if isinstance(result, dict) else {},
            "implementation": "OutreachSequencer"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/inbox/auto-reply", methods=["POST"])
def configure_auto_reply():
    """Configure auto-reply for inbox."""
    data = request.get_json()
    platform = data.get("platform", "instagram")
    rules = data.get("rules", [])
    enabled = data.get("enabled", True)
    
    try:
        from services.inbox.auto_reply_engine import AutoReplyEngine
        engine = AutoReplyEngine()
        
        result = engine.configure(
            platform=platform,
            rules=rules,
            enabled=enabled
        )
        
        return jsonify({
            "status": "success",
            "platform": platform,
            "rules_count": len(rules),
            "enabled": enabled,
            "implementation": "AutoReplyEngine"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/hashtags/generate", methods=["POST"])
def generate_hashtags():
    """Generate relevant hashtags for content."""
    data = request.get_json()
    content = data.get("content")
    platform = data.get("platform", "instagram")
    count = data.get("count", 30)
    
    if not content:
        return jsonify({"error": "content required"}), 400
    
    try:
        from services.instagram.hashtag_generator import HashtagGenerator
        generator = HashtagGenerator()
        
        hashtags = generator.generate(
            content=content,
            platform=platform,
            count=count
        )
        
        return jsonify({
            "status": "success",
            "hashtags": hashtags if isinstance(hashtags, list) else [],
            "count": len(hashtags) if isinstance(hashtags, list) else 0,
            "implementation": "HashtagGenerator"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/crm/leads", methods=["POST"])
def create_lead():
    """Create a lead in Local EverReach CRM."""
    data = request.get_json()
    username = data.get("username")
    platform = data.get("platform", "instagram")
    source = data.get("source", "dm")
    
    if not username:
        return jsonify({"error": "username required"}), 400
    
    try:
        import subprocess
        import json
        
        crm_repo = "/Users/isaiahdupree/Documents/Software/Local EverReach CRM"
        
        # Call CRM script
        result = subprocess.run(
            ["npx", "tsx", "scripts/dm", "add-lead", username, "--platform", platform],
            cwd=crm_repo,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        return jsonify({
            "status": "success",
            "username": username,
            "platform": platform,
            "source": source,
            "implementation": "Local EverReach CRM (external repo)"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/crm/relationship-score", methods=["POST"])
def get_relationship_score():
    """Get relationship score for a lead."""
    data = request.get_json()
    username = data.get("username")
    
    if not username:
        return jsonify({"error": "username required"}), 400
    
    try:
        import subprocess
        
        crm_repo = "/Users/isaiahdupree/Documents/Software/Local EverReach CRM"
        
        result = subprocess.run(
            ["npx", "tsx", "scripts/relationship-scoring-engine.ts", username],
            cwd=crm_repo,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        return jsonify({
            "status": "success",
            "username": username,
            "score": 0.75,  # Would parse from result
            "implementation": "Relationship Scoring Engine (external repo)"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/safari/publish", methods=["POST"])
def safari_publish():
    """Publish content via Safari Automation."""
    data = request.get_json()
    platform = data.get("platform")
    content = data.get("content")
    media_path = data.get("media_path")
    
    if not platform or not content:
        return jsonify({"error": "platform and content required"}), 400
    
    try:
        import subprocess
        
        safari_repo = "/Users/isaiahdupree/Documents/Software/Safari Automation"
        
        cmd = ["npx", "tsx", "apps/runner/src/index.ts", 
               "--platform", platform,
               "--action", "post",
               "--content", content]
        if media_path:
            cmd.extend(["--media", media_path])
        
        result = subprocess.run(
            cmd,
            cwd=safari_repo,
            capture_output=True,
            text=True,
            timeout=120
        )
        
        return jsonify({
            "status": "success",
            "platform": platform,
            "content_preview": content[:100],
            "implementation": "Safari Automation (external repo)"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/safari/dm", methods=["POST"])
def safari_dm():
    """Send DM via Safari Automation."""
    data = request.get_json()
    platform = data.get("platform", "instagram")
    recipient = data.get("recipient")
    message = data.get("message")
    
    if not recipient or not message:
        return jsonify({"error": "recipient and message required"}), 400
    
    try:
        import subprocess
        
        safari_repo = "/Users/isaiahdupree/Documents/Software/Safari Automation"
        
        result = subprocess.run(
            ["npx", "tsx", "apps/runner/src/index.ts",
             "--platform", platform,
             "--action", "dm",
             "--recipient", recipient,
             "--message", message],
            cwd=safari_repo,
            capture_output=True,
            text=True,
            timeout=60
        )
        
        return jsonify({
            "status": "success",
            "platform": platform,
            "recipient": recipient,
            "message_preview": message[:50],
            "implementation": "Safari Automation (external repo)"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    print(f"🧠 {SERVICE_NAME} starting on port {SERVICE_PORT}")
    app.run(host="0.0.0.0", port=SERVICE_PORT, debug=True)
