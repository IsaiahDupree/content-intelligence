"""
Content Intelligence Service - AI/ML for content analysis and generation.
Port: 6006
"""
import os
from flask import Flask, jsonify, request
from datetime import datetime
from typing import List, Dict, Any

app = Flask(__name__)

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

SERVICE_NAME = "content-intelligence"
SERVICE_VERSION = "1.0.0"
SERVICE_PORT = int(os.getenv("PORT", 6006))


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "timestamp": datetime.utcnow().isoformat()
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
    """Analyze image/frame content."""
    data = request.get_json()
    
    image_path = data.get("image_path")
    
    if not image_path:
        return jsonify({"error": "image_path required"}), 400
    
    # TODO: Implement actual vision analysis
    return jsonify({
        "status": "success",
        "image_path": image_path,
        "analysis": {
            "objects": [],
            "text": [],
            "faces": 0,
            "scene": "unknown"
        }
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


if __name__ == "__main__":
    print(f"🧠 {SERVICE_NAME} starting on port {SERVICE_PORT}")
    app.run(host="0.0.0.0", port=SERVICE_PORT, debug=True)
