"""
YouTube Enrichment Helpers
===========================
Phase 1 + 2 implementation:
- Description parsing (products, links, CTAs)
- Engagement rate calculation
- Topic clustering with OpenAI embeddings
"""
import re
from typing import List, Dict, Any, Optional
from collections import Counter, defaultdict
import numpy as np


# Common product/tool names to detect
KNOWN_PRODUCTS = {
    "notion", "chatgpt", "midjourney", "canva", "figma", "slack", "trello",
    "asana", "clickup", "monday", "airtable", "zapier", "make", "n8n",
    "obsidian", "roam", "logseq", "evernote", "onenote", "todoist",
    "iphone", "ipad", "macbook", "airpods", "apple watch", "samsung",
    "sony", "bose", "logitech", "razer", "corsair", "nvidia", "amd",
}

# Common CTA patterns
CTA_PATTERNS = [
    r"get (?:my|the|your) (?:free|full) (.+?)(?:\.|!|$)",
    r"download (?:my|the|your) (.+?)(?:\.|!|$)",
    r"join (?:my|the|our) (.+?)(?:\.|!|$)",
    r"check out (?:my|the) (.+?)(?:\.|!|$)",
    r"grab (?:my|the|your) (.+?)(?:\.|!|$)",
    r"sign up for (.+?)(?:\.|!|$)",
    r"subscribe to (.+?)(?:\.|!|$)",
]


def extract_description_metadata(description: str) -> Dict[str, Any]:
    """
    Extract structured data from video description.
    
    Returns:
        - links: All URLs found
        - affiliate_links: Affiliate URLs
        - timestamps: Chapter timestamps
        - products: Mentioned products/tools
        - ctas: Call-to-action phrases
    """
    if not description:
        return {
            "links": [],
            "affiliate_links": [],
            "timestamps": [],
            "products": [],
            "ctas": [],
        }
    
    # Extract links
    links = re.findall(r'https?://\S+', description)
    
    # Identify affiliate links
    affiliate_patterns = ['amzn.to', 'geni.us', 'bit.ly', 'go.', '/ref=', '?aff=', '?ref=']
    affiliate_links = [l for l in links if any(p in l.lower() for p in affiliate_patterns)]
    
    # Extract timestamps (chapter markers)
    timestamps = re.findall(r'(\d{1,2}:\d{2})\s*-?\s*(.+)', description)
    
    # Extract products mentioned
    desc_lower = description.lower()
    products = [p for p in KNOWN_PRODUCTS if p in desc_lower]
    
    # Extract CTAs
    ctas = []
    for pattern in CTA_PATTERNS:
        matches = re.findall(pattern, description.lower(), re.IGNORECASE)
        ctas.extend(matches)
    
    return {
        "links": links,
        "affiliate_links": affiliate_links,
        "timestamps": timestamps,
        "products": list(set(products)),
        "ctas": [c.strip() for c in ctas if c.strip()],
    }


def calculate_engagement_metrics(video: Dict, channel_baseline: Dict) -> Dict[str, float]:
    """
    Calculate engagement metrics normalized against channel baseline.
    
    Args:
        video: Video data with statistics
        channel_baseline: Channel's median metrics
    
    Returns:
        Engagement metrics including rates and uplifts
    """
    stats = video.get("statistics", {})
    
    views = int(stats.get("viewCount", 0))
    likes = int(stats.get("likeCount", 0))
    comments = int(stats.get("commentCount", 0))
    
    # Basic rates
    like_rate = likes / views if views > 0 else 0
    comment_rate = comments / views if views > 0 else 0
    engagement_rate = (likes + comments) / views if views > 0 else 0
    
    # Normalized against channel baseline
    baseline_like_rate = channel_baseline.get("median_like_rate", 0.01)
    baseline_comment_rate = channel_baseline.get("median_comment_rate", 0.001)
    
    like_rate_uplift = like_rate / baseline_like_rate if baseline_like_rate > 0 else 1.0
    comment_rate_uplift = comment_rate / baseline_comment_rate if baseline_comment_rate > 0 else 1.0
    
    return {
        "like_rate": like_rate,
        "comment_rate": comment_rate,
        "engagement_rate": engagement_rate,
        "like_rate_uplift": like_rate_uplift,
        "comment_rate_uplift": comment_rate_uplift,
        "engagement_score": (like_rate_uplift + comment_rate_uplift) / 2,
    }


def calculate_channel_engagement_baseline(videos: List[Dict]) -> Dict[str, Dict]:
    """
    Calculate median engagement rates per channel.
    
    Returns:
        Dict mapping channel_id to baseline metrics
    """
    from collections import defaultdict
    
    channel_metrics = defaultdict(lambda: {
        "like_rates": [],
        "comment_rates": [],
    })
    
    for video in videos:
        channel_id = video.get("_channel_id", "")
        if not channel_id:
            continue
        
        stats = video.get("statistics", {})
        views = int(stats.get("viewCount", 0))
        likes = int(stats.get("likeCount", 0))
        comments = int(stats.get("commentCount", 0))
        
        if views > 0:
            like_rate = likes / views
            comment_rate = comments / views
            
            channel_metrics[channel_id]["like_rates"].append(like_rate)
            channel_metrics[channel_id]["comment_rates"].append(comment_rate)
    
    # Calculate medians
    baselines = {}
    for channel_id, metrics in channel_metrics.items():
        like_rates = sorted(metrics["like_rates"])
        comment_rates = sorted(metrics["comment_rates"])
        
        baselines[channel_id] = {
            "median_like_rate": like_rates[len(like_rates) // 2] if like_rates else 0.01,
            "median_comment_rate": comment_rates[len(comment_rates) // 2] if comment_rates else 0.001,
        }
    
    return baselines


def cluster_by_embeddings(embeddings: List[List[float]], min_cluster_size: int = 3) -> List[int]:
    """
    Cluster embeddings using simple k-means.
    
    Args:
        embeddings: List of embedding vectors
        min_cluster_size: Minimum videos per cluster
    
    Returns:
        List of cluster IDs (same length as embeddings)
    """
    if len(embeddings) < min_cluster_size:
        return [-1] * len(embeddings)  # No clustering
    
    try:
        from sklearn.cluster import KMeans
        
        # Determine number of clusters (roughly 1 cluster per 10 videos)
        n_clusters = max(2, min(len(embeddings) // 10, 10))
        
        kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
        cluster_ids = kmeans.fit_predict(embeddings)
        
        return cluster_ids.tolist()
    
    except ImportError:
        # Fallback: no clustering
        return [-1] * len(embeddings)


def extract_common_elements(items: List[str], min_frequency: int = 2) -> List[str]:
    """
    Extract common elements from a list.
    
    Args:
        items: List of strings
        min_frequency: Minimum times an item must appear
    
    Returns:
        List of common items sorted by frequency
    """
    counter = Counter(items)
    common = [item for item, count in counter.most_common() if count >= min_frequency]
    return common[:10]  # Top 10


# =========================================
# Phase 3: Comment Analysis
# =========================================

QUESTION_PATTERNS = [
    r'\?',  # Contains question mark
    r'^(how|what|why|when|where|which|who|can|could|should|would|is|are|do|does|will)\b',
    r'^(any\s+tips|any\s+advice|any\s+recommendations)',
]

REQUEST_PATTERNS = [
    r'(please|pls)\s+(make|do|create|show)',
    r'(can|could)\s+you\s+(make|do|create|show)',
    r'tutorial\s+(on|for|about)',
    r'video\s+(on|about)',
]


def extract_comment_themes(comments: List[Dict]) -> Dict[str, Any]:
    """
    Extract themes and patterns from video comments.
    
    Returns:
        - questions: List of question comments
        - requests: List of content requests
        - top_liked: Most liked comments
        - common_words: Frequently used terms
        - sentiment_signals: Positive/negative indicators
    """
    if not comments:
        return {
            "questions": [],
            "requests": [],
            "top_liked": [],
            "common_words": [],
            "sentiment_signals": {"positive": 0, "negative": 0},
        }
    
    questions = []
    requests = []
    all_words = []
    positive_count = 0
    negative_count = 0
    
    # Positive/negative indicators
    positive_words = {'love', 'great', 'amazing', 'awesome', 'helpful', 'thanks', 'thank', 'best', 'perfect', 'excellent'}
    negative_words = {'hate', 'bad', 'wrong', 'terrible', 'worst', 'useless', 'boring', 'clickbait', 'disappointed'}
    
    for comment in comments:
        text = comment.get("text_original", "") or comment.get("text", "")
        text_lower = text.lower()
        likes = comment.get("likes", 0)
        
        # Check for questions
        for pattern in QUESTION_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                questions.append({
                    "text": text[:200],
                    "likes": likes,
                })
                break
        
        # Check for content requests
        for pattern in REQUEST_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                requests.append({
                    "text": text[:200],
                    "likes": likes,
                })
                break
        
        # Count sentiment
        words = set(re.findall(r'\b\w+\b', text_lower))
        if words & positive_words:
            positive_count += 1
        if words & negative_words:
            negative_count += 1
        
        # Collect words for frequency analysis
        meaningful_words = [w for w in words if len(w) > 3 and w not in {'this', 'that', 'have', 'with', 'from', 'your', 'will', 'would', 'could', 'should', 'about', 'just', 'like', 'what', 'when', 'where', 'which', 'their', 'there', 'they', 'been', 'were', 'being', 'some', 'very', 'only', 'also', 'into', 'more', 'than', 'other'}]
        all_words.extend(meaningful_words)
    
    # Sort questions/requests by likes
    questions.sort(key=lambda x: x["likes"], reverse=True)
    requests.sort(key=lambda x: x["likes"], reverse=True)
    
    # Get top liked comments overall
    top_liked = sorted(comments, key=lambda x: x.get("likes", 0), reverse=True)[:5]
    top_liked = [{"text": c.get("text_original", c.get("text", ""))[:200], "likes": c.get("likes", 0)} for c in top_liked]
    
    # Common words
    common_words = extract_common_elements(all_words, min_frequency=3)
    
    return {
        "questions": questions[:10],
        "requests": requests[:5],
        "top_liked": top_liked,
        "common_words": common_words,
        "sentiment_signals": {
            "positive": positive_count,
            "negative": negative_count,
            "ratio": positive_count / max(negative_count, 1),
        },
    }


def aggregate_comment_themes(all_video_comments: Dict[str, List[Dict]]) -> Dict[str, Any]:
    """
    Aggregate comment themes across multiple videos.
    
    Args:
        all_video_comments: Dict mapping video_id -> list of comments
    
    Returns:
        Aggregated themes and patterns
    """
    all_questions = []
    all_requests = []
    all_words = []
    total_positive = 0
    total_negative = 0
    
    for video_id, comments in all_video_comments.items():
        themes = extract_comment_themes(comments)
        
        all_questions.extend(themes["questions"])
        all_requests.extend(themes["requests"])
        all_words.extend(themes["common_words"])
        total_positive += themes["sentiment_signals"]["positive"]
        total_negative += themes["sentiment_signals"]["negative"]
    
    # Deduplicate and sort
    all_questions.sort(key=lambda x: x["likes"], reverse=True)
    all_requests.sort(key=lambda x: x["likes"], reverse=True)
    
    return {
        "top_questions": all_questions[:20],
        "top_requests": all_requests[:10],
        "trending_words": extract_common_elements(all_words, min_frequency=2),
        "overall_sentiment": {
            "positive": total_positive,
            "negative": total_negative,
            "ratio": total_positive / max(total_negative, 1),
        },
        "videos_analyzed": len(all_video_comments),
    }


# =========================================
# Phase 4: Thumbnail Visual Analysis
# =========================================

async def download_thumbnail(thumbnail_url: str, client=None) -> Optional[bytes]:
    """Download thumbnail image from URL."""
    import httpx
    
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(thumbnail_url)
                if response.status_code == 200:
                    return response.content
        else:
            response = await client.get(thumbnail_url)
            if response.status_code == 200:
                return response.content
    except Exception:
        pass
    
    return None


def analyze_thumbnail_colors(image_bytes: bytes) -> Dict[str, Any]:
    """
    Analyze dominant colors in thumbnail.
    
    Returns:
        - dominant_colors: List of top 5 hex colors
        - brightness: Average brightness (0-255)
        - saturation: Average saturation (0-1)
        - color_scheme: Detected scheme (warm/cool/neutral)
    """
    try:
        from PIL import Image
        from io import BytesIO
        
        img = Image.open(BytesIO(image_bytes)).convert('RGB')
        img_small = img.resize((50, 50))  # Resize for faster processing
        
        pixels = list(img_small.getdata())
        
        # Count color frequencies
        color_counts = Counter(pixels)
        top_colors = color_counts.most_common(5)
        
        # Convert to hex
        dominant_colors = [f"#{r:02x}{g:02x}{b:02x}" for (r, g, b), _ in top_colors]
        
        # Calculate brightness
        all_brightness = [sum(p) / 3 for p in pixels]
        avg_brightness = sum(all_brightness) / len(all_brightness) if all_brightness else 128
        
        # Calculate saturation
        def get_saturation(r, g, b):
            max_c = max(r, g, b)
            min_c = min(r, g, b)
            return (max_c - min_c) / max_c if max_c > 0 else 0
        
        saturations = [get_saturation(r, g, b) for r, g, b in pixels]
        avg_saturation = sum(saturations) / len(saturations) if saturations else 0.5
        
        # Determine color scheme (warm vs cool)
        warm_count = sum(1 for r, g, b in pixels if r > b)
        cool_count = len(pixels) - warm_count
        color_scheme = "warm" if warm_count > cool_count * 1.2 else ("cool" if cool_count > warm_count * 1.2 else "neutral")
        
        return {
            "dominant_colors": dominant_colors,
            "brightness": round(avg_brightness, 1),
            "brightness_category": "bright" if avg_brightness > 170 else ("dark" if avg_brightness < 85 else "medium"),
            "saturation": round(avg_saturation, 2),
            "color_scheme": color_scheme,
        }
    
    except Exception as e:
        return {"error": str(e)}


def detect_text_in_thumbnail(image_bytes: bytes) -> Dict[str, Any]:
    """
    Detect text overlays in thumbnail using OCR.
    
    Returns:
        - has_text: Boolean
        - text_content: Extracted text
        - text_amount: Estimated % of image covered by text
    """
    try:
        import pytesseract
        from PIL import Image
        from io import BytesIO
        
        img = Image.open(BytesIO(image_bytes))
        
        # Run OCR
        text = pytesseract.image_to_string(img, config='--psm 11')
        text = text.strip()
        
        # Clean up
        words = [w for w in text.split() if len(w) > 1 and w.isalnum()]
        clean_text = " ".join(words)
        
        return {
            "has_text": len(clean_text) > 2,
            "text_content": clean_text[:200] if clean_text else "",
            "word_count": len(words),
            "text_amount": "heavy" if len(words) > 10 else ("moderate" if len(words) > 3 else "minimal"),
        }
    
    except Exception as e:
        # OCR not available, try fallback
        return {
            "has_text": None,
            "text_content": "",
            "word_count": 0,
            "text_amount": "unknown",
            "error": str(e),
        }


def detect_faces_in_thumbnail(image_bytes: bytes) -> Dict[str, Any]:
    """
    Detect faces in thumbnail using OpenCV.
    
    Returns:
        - face_count: Number of faces detected
        - has_face: Boolean
        - face_positions: List of face bounding boxes
    """
    try:
        import cv2
        import numpy as np
        from io import BytesIO
        
        # Convert bytes to numpy array
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if img is None:
            return {"face_count": 0, "has_face": False, "error": "Failed to decode image"}
        
        # Convert to grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # Load face cascade
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        
        # Detect faces
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
        
        face_positions = []
        for (x, y, w, h) in faces:
            face_positions.append({
                "x": int(x),
                "y": int(y),
                "width": int(w),
                "height": int(h),
            })
        
        return {
            "face_count": len(faces),
            "has_face": len(faces) > 0,
            "face_positions": face_positions[:5],  # Limit to 5
        }
    
    except Exception as e:
        return {"face_count": 0, "has_face": False, "error": str(e)}


def analyze_thumbnail_composition(image_bytes: bytes) -> Dict[str, Any]:
    """
    Analyze thumbnail composition and layout.
    
    Returns:
        - aspect_ratio: Width/height ratio
        - has_border: Detected border
        - contrast: High/medium/low contrast
    """
    try:
        from PIL import Image
        from io import BytesIO
        
        img = Image.open(BytesIO(image_bytes)).convert('RGB')
        width, height = img.size
        
        # Aspect ratio
        aspect = width / height if height > 0 else 1.78
        
        # Check for borders (compare edge pixels to center)
        pixels = list(img.getdata())
        edge_pixels = pixels[:width] + pixels[-width:]  # Top and bottom rows
        center_start = (height // 2) * width
        center_pixels = pixels[center_start:center_start + width]
        
        edge_brightness = sum(sum(p) / 3 for p in edge_pixels) / len(edge_pixels) if edge_pixels else 128
        center_brightness = sum(sum(p) / 3 for p in center_pixels) / len(center_pixels) if center_pixels else 128
        
        has_border = abs(edge_brightness - center_brightness) > 50
        
        # Contrast estimation
        all_brightness = [sum(p) / 3 for p in pixels]
        if all_brightness:
            min_b = min(all_brightness)
            max_b = max(all_brightness)
            contrast_range = max_b - min_b
            contrast = "high" if contrast_range > 180 else ("medium" if contrast_range > 100 else "low")
        else:
            contrast = "medium"
        
        return {
            "width": width,
            "height": height,
            "aspect_ratio": round(aspect, 2),
            "has_border": has_border,
            "contrast": contrast,
        }
    
    except Exception as e:
        return {"error": str(e)}


async def analyze_thumbnail_full(thumbnail_url: str, client=None) -> Dict[str, Any]:
    """
    Full thumbnail analysis combining all methods.
    """
    image_bytes = await download_thumbnail(thumbnail_url, client)
    
    if not image_bytes:
        return {"error": "Failed to download thumbnail"}
    
    results = {
        "url": thumbnail_url,
        "colors": analyze_thumbnail_colors(image_bytes),
        "text": detect_text_in_thumbnail(image_bytes),
        "faces": detect_faces_in_thumbnail(image_bytes),
        "composition": analyze_thumbnail_composition(image_bytes),
    }
    
    # Generate summary
    results["summary"] = {
        "style": _classify_thumbnail_style(results),
        "elements": _get_thumbnail_elements(results),
    }
    
    return results


def _classify_thumbnail_style(analysis: Dict) -> str:
    """Classify thumbnail into a style category."""
    has_face = analysis.get("faces", {}).get("has_face", False)
    has_text = analysis.get("text", {}).get("has_text", False)
    brightness = analysis.get("colors", {}).get("brightness_category", "medium")
    
    if has_face and has_text:
        return "face_with_text"
    elif has_face:
        return "face_focused"
    elif has_text:
        return "text_heavy"
    elif brightness == "bright":
        return "bright_minimal"
    else:
        return "dark_cinematic"


def _get_thumbnail_elements(analysis: Dict) -> List[str]:
    """List detected elements in thumbnail."""
    elements = []
    
    if analysis.get("faces", {}).get("has_face"):
        count = analysis["faces"].get("face_count", 1)
        elements.append(f"{count}_face{'s' if count > 1 else ''}")
    
    text_amount = analysis.get("text", {}).get("text_amount", "")
    if text_amount in ["moderate", "heavy"]:
        elements.append(f"{text_amount}_text")
    
    scheme = analysis.get("colors", {}).get("color_scheme", "")
    if scheme:
        elements.append(f"{scheme}_colors")
    
    if analysis.get("composition", {}).get("contrast") == "high":
        elements.append("high_contrast")
    
    return elements


def extract_audience_interests(comments_data: Dict[str, Any]) -> List[str]:
    """
    Extract audience interests from aggregated comment data.
    
    Returns list of interest topics derived from questions and requests.
    """
    interests = []
    
    # From questions
    for q in comments_data.get("top_questions", [])[:10]:
        text = q["text"].lower()
        
        # Extract "how to X" patterns
        match = re.search(r'how\s+(?:do\s+(?:i|you)|to|can\s+i)\s+(.+?)(?:\?|$)', text)
        if match:
            interests.append(match.group(1).strip()[:50])
        
        # Extract "what is X" patterns
        match = re.search(r'what\s+(?:is|are)\s+(.+?)(?:\?|$)', text)
        if match:
            interests.append(match.group(1).strip()[:50])
    
    # From requests
    for r in comments_data.get("top_requests", [])[:5]:
        text = r["text"].lower()
        
        # Extract "video about X" patterns
        match = re.search(r'(?:video|tutorial)\s+(?:on|about|for)\s+(.+?)(?:\?|!|$)', text)
        if match:
            interests.append(match.group(1).strip()[:50])
    
    # Deduplicate while preserving order
    seen = set()
    unique_interests = []
    for interest in interests:
        if interest not in seen and len(interest) > 3:
            seen.add(interest)
            unique_interests.append(interest)
    
    return unique_interests[:15]


# =========================================
# Phase 5: Keyword Analysis & Google Ads Integration
# =========================================

# Common stopwords to filter out
KEYWORD_STOPWORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'been',
    'be', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare', 'ought',
    'used', 'i', 'you', 'he', 'she', 'it', 'we', 'they', 'what', 'which',
    'who', 'whom', 'this', 'that', 'these', 'those', 'am', 'my', 'your',
    'his', 'her', 'its', 'our', 'their', 'me', 'him', 'us', 'them', 'if',
    'then', 'else', 'when', 'where', 'why', 'how', 'all', 'each', 'every',
    'both', 'few', 'more', 'most', 'other', 'some', 'such', 'no', 'nor',
    'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very', 'just',
    'also', 'now', 'here', 'there', 'out', 'about', 'up', 'down', 'off',
    'over', 'under', 'again', 'further', 'once', 'video', 'watch', 'new',
}


def extract_keywords_from_text(text: str, max_keywords: int = 20) -> List[str]:
    """
    Extract meaningful keywords from text (title or description).
    
    Returns list of keywords/phrases sorted by relevance.
    """
    if not text:
        return []
    
    text_lower = text.lower()
    
    # Remove special characters but keep spaces
    clean_text = re.sub(r'[^\w\s]', ' ', text_lower)
    
    # Split into words
    words = clean_text.split()
    
    # Filter stopwords and short words
    keywords = [w for w in words if w not in KEYWORD_STOPWORDS and len(w) > 2]
    
    # Also extract 2-word phrases (bigrams)
    bigrams = []
    for i in range(len(words) - 1):
        if words[i] not in KEYWORD_STOPWORDS and words[i+1] not in KEYWORD_STOPWORDS:
            if len(words[i]) > 2 and len(words[i+1]) > 2:
                bigrams.append(f"{words[i]} {words[i+1]}")
    
    # Combine and deduplicate
    all_keywords = keywords + bigrams
    
    # Count frequency
    keyword_counts = Counter(all_keywords)
    
    # Return top keywords
    return [kw for kw, _ in keyword_counts.most_common(max_keywords)]


def extract_keywords_from_videos(videos: List[Dict]) -> Dict[str, Dict]:
    """
    Extract and aggregate keywords from multiple videos.
    
    Returns dict with keyword stats:
    - count: Number of videos using this keyword
    - total_views: Sum of views for videos with this keyword
    - channels: Set of channels using it
    """
    keyword_data = defaultdict(lambda: {
        "count": 0,
        "total_views": 0,
        "channels": set(),
        "example_titles": [],
    })
    
    for video in videos:
        snippet = video.get("snippet", {})
        title = snippet.get("title", "")
        description = snippet.get("description", "")[:500]  # First 500 chars
        
        views = int(video.get("statistics", {}).get("viewCount", 0))
        channel_id = video.get("_channel_id", "")
        
        # Extract keywords from title (higher weight)
        title_keywords = extract_keywords_from_text(title, max_keywords=10)
        
        # Extract from description
        desc_keywords = extract_keywords_from_text(description, max_keywords=10)
        
        # Combine with title keywords having priority
        all_keywords = list(set(title_keywords + desc_keywords))
        
        for kw in all_keywords:
            keyword_data[kw]["count"] += 1
            keyword_data[kw]["total_views"] += views
            keyword_data[kw]["channels"].add(channel_id)
            
            if len(keyword_data[kw]["example_titles"]) < 3:
                keyword_data[kw]["example_titles"].append(title[:60])
    
    # Convert sets to counts for JSON serialization
    for kw in keyword_data:
        keyword_data[kw]["unique_channels"] = len(keyword_data[kw]["channels"])
        del keyword_data[kw]["channels"]
    
    return dict(keyword_data)


def calculate_keyword_opportunity_score(
    keyword: str,
    keyword_stats: Dict,
    google_ads_data: Optional[Dict] = None
) -> Dict[str, Any]:
    """
    Calculate opportunity score for a keyword.
    
    Factors:
    - Video performance (views per video using keyword)
    - Cross-channel usage (breadth)
    - Google Ads metrics if available (volume, competition, CPC)
    
    Returns opportunity analysis.
    """
    count = keyword_stats.get("count", 0)
    total_views = keyword_stats.get("total_views", 0)
    unique_channels = keyword_stats.get("unique_channels", 0)
    
    # Base metrics from YouTube data
    avg_views = total_views / count if count > 0 else 0
    
    # Performance score (0-1) based on avg views
    performance_score = min(avg_views / 500000, 1.0)  # 500K views = max score
    
    # Breadth score (0-1) based on channel diversity
    breadth_score = min(unique_channels / 5, 1.0)  # 5+ channels = max score
    
    # Frequency score (0-1)
    frequency_score = min(count / 10, 1.0)  # 10+ mentions = max score
    
    result = {
        "keyword": keyword,
        "video_count": count,
        "total_views": total_views,
        "avg_views_per_video": round(avg_views),
        "unique_channels": unique_channels,
        "performance_score": round(performance_score, 2),
        "breadth_score": round(breadth_score, 2),
        "frequency_score": round(frequency_score, 2),
    }
    
    # Add Google Ads data if available
    if google_ads_data:
        monthly_searches = google_ads_data.get("avg_monthly_searches", 0)
        competition = google_ads_data.get("competition", "UNKNOWN")
        competition_index = google_ads_data.get("competition_index", 50)
        cpc_low = google_ads_data.get("cpc_low_micros", 0) / 1_000_000
        cpc_high = google_ads_data.get("cpc_high_micros", 0) / 1_000_000
        
        result["google_ads"] = {
            "monthly_searches": monthly_searches,
            "competition": competition,
            "competition_index": competition_index,
            "cpc_range": f"${cpc_low:.2f} - ${cpc_high:.2f}",
        }
        
        # Opportunity = high volume + low competition
        volume_score = min(monthly_searches / 50000, 1.0)  # 50K/mo = max
        competition_score = 1 - (competition_index / 100)  # Lower competition = higher score
        
        result["volume_score"] = round(volume_score, 2)
        result["competition_score"] = round(competition_score, 2)
        
        # Combined opportunity score
        result["opportunity_score"] = round(
            (performance_score * 0.25) + 
            (breadth_score * 0.15) + 
            (volume_score * 0.35) + 
            (competition_score * 0.25),
            2
        )
    else:
        # Without Google Ads data, use YouTube metrics only
        result["opportunity_score"] = round(
            (performance_score * 0.4) + 
            (breadth_score * 0.3) + 
            (frequency_score * 0.3),
            2
        )
    
    return result


async def fetch_google_ads_keywords(
    keywords: List[str],
    api_key: str = None
) -> Dict[str, Dict]:
    """
    Fetch keyword metrics from Google Ads API.
    
    Note: Requires Google Ads API credentials and a developer token.
    This is a placeholder that returns estimated data based on YouTube performance.
    
    For full implementation, you need:
    1. Google Ads developer token
    2. OAuth2 credentials
    3. Customer ID with Keyword Planner access
    """
    # Placeholder - return empty dict
    # Full implementation would use google-ads-python library
    return {}


def rank_keywords_by_opportunity(
    keyword_data: Dict[str, Dict],
    google_ads_data: Optional[Dict[str, Dict]] = None,
    min_count: int = 2
) -> List[Dict]:
    """
    Rank all keywords by opportunity score.
    
    Returns sorted list of keyword analyses.
    """
    ranked = []
    
    for keyword, stats in keyword_data.items():
        if stats.get("count", 0) < min_count:
            continue
        
        ads_data = google_ads_data.get(keyword) if google_ads_data else None
        analysis = calculate_keyword_opportunity_score(keyword, stats, ads_data)
        ranked.append(analysis)
    
    # Sort by opportunity score
    ranked.sort(key=lambda x: x["opportunity_score"], reverse=True)
    
    return ranked[:30]  # Top 30 keywords
