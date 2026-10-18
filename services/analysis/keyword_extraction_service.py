"""
Keyword Extraction Service
Extracts trending keywords, hooks, and patterns from Instagram captions.
"""
import re
from collections import Counter
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from loguru import logger


@dataclass
class ExtractedKeyword:
    """Extracted keyword/phrase with metadata"""
    keyword: str
    keyword_type: str  # 'hook', 'phrase', 'hashtag', 'format'
    frequency: int
    avg_engagement: float = 0.0
    examples: List[str] = None
    
    def __post_init__(self):
        if self.examples is None:
            self.examples = []


class KeywordExtractionService:
    """
    Service for extracting trending keywords and patterns from captions.
    
    Detects:
    - Hook phrases ("Nobody talks about...", "POV:", "Hot take:")
    - Format markers ("3 things I wish...", "Day in the life")
    - Engagement phrases ("Save this", "Comment YES")
    - Trending n-grams
    """
    
    # Common hook patterns to detect
    HOOK_PATTERNS = [
        (r"^(nobody talks about|no one talks about)", "hook"),
        (r"^(pov:|pov )", "hook"),
        (r"^(hot take:?)", "hook"),
        (r"^(unpopular opinion:?)", "hook"),
        (r"^(controversial opinion:?)", "hook"),
        (r"^(hear me out:?)", "hook"),
        (r"^(plot twist:?)", "hook"),
        (r"^(reminder:?)", "hook"),
        (r"^(friendly reminder:?)", "hook"),
        (r"^(stop scrolling)", "hook"),
        (r"^(wait for it)", "hook"),
        (r"^(this is your sign)", "hook"),
    ]
    
    # Format patterns
    FORMAT_PATTERNS = [
        (r"(\d+)\s*(things?|ways?|tips?|reasons?|signs?)\s*(i wish|you need|to|that)", "format"),
        (r"(day in the life|ditl|a day in my life)", "format"),
        (r"(what i eat in a day|wieiad)", "format"),
        (r"(get ready with me|grwm)", "format"),
        (r"(morning routine|night routine)", "format"),
        (r"(how to|how i)", "format"),
        (r"(before and after|before vs after)", "format"),
    ]
    
    # Engagement bait patterns
    ENGAGEMENT_PATTERNS = [
        (r"(save this|save for later)", "engagement"),
        (r"(comment|drop a|leave a).{0,20}(below|if you)", "engagement"),
        (r"(follow for more|follow me for)", "engagement"),
        (r"(share this with|send this to)", "engagement"),
        (r"(tag someone|tag a friend)", "engagement"),
        (r"(double tap if)", "engagement"),
    ]
    
    def __init__(self):
        self.all_patterns = (
            self.HOOK_PATTERNS + 
            self.FORMAT_PATTERNS + 
            self.ENGAGEMENT_PATTERNS
        )
    
    def extract_from_caption(self, caption: str) -> List[ExtractedKeyword]:
        """Extract keywords and patterns from a single caption"""
        if not caption:
            return []
        
        caption_lower = caption.lower().strip()
        results = []
        
        # Check for pattern matches
        for pattern, keyword_type in self.all_patterns:
            match = re.search(pattern, caption_lower, re.IGNORECASE)
            if match:
                keyword = match.group(0).strip()
                results.append(ExtractedKeyword(
                    keyword=keyword,
                    keyword_type=keyword_type,
                    frequency=1,
                    examples=[caption[:200]]
                ))
        
        # Extract hashtags
        hashtags = re.findall(r'#(\w+)', caption)
        for tag in hashtags[:10]:  # Limit to 10
            results.append(ExtractedKeyword(
                keyword=f"#{tag.lower()}",
                keyword_type="hashtag",
                frequency=1
            ))
        
        return results
    
    def extract_from_captions(
        self, 
        captions: List[Dict[str, Any]]
    ) -> List[ExtractedKeyword]:
        """
        Extract and aggregate keywords from multiple captions.
        
        Args:
            captions: List of dicts with 'caption', 'play_count', 'like_count'
        
        Returns:
            Aggregated keywords sorted by frequency and engagement
        """
        keyword_stats: Dict[str, Dict[str, Any]] = {}
        
        for item in captions:
            caption = item.get("caption", "")
            engagement = (
                item.get("play_count", 0) + 
                item.get("like_count", 0) * 10  # Weight likes higher
            )
            
            keywords = self.extract_from_caption(caption)
            
            for kw in keywords:
                key = f"{kw.keyword_type}:{kw.keyword}"
                
                if key not in keyword_stats:
                    keyword_stats[key] = {
                        "keyword": kw.keyword,
                        "keyword_type": kw.keyword_type,
                        "frequency": 0,
                        "total_engagement": 0,
                        "examples": []
                    }
                
                keyword_stats[key]["frequency"] += 1
                keyword_stats[key]["total_engagement"] += engagement
                
                if len(keyword_stats[key]["examples"]) < 3:
                    keyword_stats[key]["examples"].append(caption[:200])
        
        # Convert to ExtractedKeyword objects
        results = []
        for key, stats in keyword_stats.items():
            avg_engagement = (
                stats["total_engagement"] / stats["frequency"] 
                if stats["frequency"] > 0 else 0
            )
            results.append(ExtractedKeyword(
                keyword=stats["keyword"],
                keyword_type=stats["keyword_type"],
                frequency=stats["frequency"],
                avg_engagement=avg_engagement,
                examples=stats["examples"]
            ))
        
        # Sort by frequency * engagement score
        results.sort(
            key=lambda x: x.frequency * (1 + x.avg_engagement / 1000000),
            reverse=True
        )
        
        return results
    
    def extract_ngrams(
        self, 
        captions: List[str], 
        n_range: Tuple[int, int] = (2, 4),
        min_frequency: int = 3
    ) -> List[ExtractedKeyword]:
        """
        Extract common n-grams (2-4 word phrases) from captions.
        
        This finds emergent trends like:
        - "i'm obsessed with"
        - "this changed my"
        - "you need to try"
        """
        from collections import Counter
        
        all_ngrams = Counter()
        
        for caption in captions:
            if not caption:
                continue
            
            # Clean and tokenize
            words = re.findall(r'\b[a-z]+\b', caption.lower())
            
            # Generate n-grams
            for n in range(n_range[0], n_range[1] + 1):
                for i in range(len(words) - n + 1):
                    ngram = " ".join(words[i:i+n])
                    # Skip very common phrases
                    if not self._is_stopword_ngram(ngram):
                        all_ngrams[ngram] += 1
        
        # Filter by frequency and convert
        results = []
        for ngram, freq in all_ngrams.most_common(100):
            if freq >= min_frequency:
                results.append(ExtractedKeyword(
                    keyword=ngram,
                    keyword_type="phrase",
                    frequency=freq
                ))
        
        return results
    
    def _is_stopword_ngram(self, ngram: str) -> bool:
        """Check if ngram is mostly stopwords"""
        stopwords = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to",
            "for", "of", "with", "by", "from", "is", "are", "was", "were",
            "be", "been", "being", "have", "has", "had", "do", "does", "did",
            "will", "would", "could", "should", "may", "might", "can", "it",
            "this", "that", "these", "those", "i", "you", "he", "she", "we",
            "they", "my", "your", "his", "her", "our", "their", "me", "him"
        }
        words = ngram.split()
        stopword_count = sum(1 for w in words if w in stopwords)
        return stopword_count >= len(words) - 1  # At least 2 non-stopwords needed
    
    def get_trending_keywords(
        self,
        captions: List[Dict[str, Any]],
        limit: int = 20
    ) -> Dict[str, List[ExtractedKeyword]]:
        """
        Get all trending keywords grouped by type.
        
        Returns:
            {
                "hooks": [...],
                "formats": [...],
                "hashtags": [...],
                "phrases": [...]
            }
        """
        # Extract pattern-based keywords
        pattern_keywords = self.extract_from_captions(captions)
        
        # Extract n-grams
        caption_texts = [c.get("caption", "") for c in captions]
        ngram_keywords = self.extract_ngrams(caption_texts)
        
        # Group by type
        grouped = {
            "hooks": [],
            "formats": [],
            "hashtags": [],
            "engagement": [],
            "phrases": []
        }
        
        for kw in pattern_keywords:
            if kw.keyword_type == "hook":
                grouped["hooks"].append(kw)
            elif kw.keyword_type == "format":
                grouped["formats"].append(kw)
            elif kw.keyword_type == "hashtag":
                grouped["hashtags"].append(kw)
            elif kw.keyword_type == "engagement":
                grouped["engagement"].append(kw)
        
        grouped["phrases"] = ngram_keywords[:limit]
        
        # Limit each category
        for key in grouped:
            grouped[key] = grouped[key][:limit]
        
        return grouped


# Singleton instance
_keyword_service: Optional[KeywordExtractionService] = None


def get_keyword_service() -> KeywordExtractionService:
    """Get singleton keyword extraction service"""
    global _keyword_service
    if _keyword_service is None:
        _keyword_service = KeywordExtractionService()
    return _keyword_service
