"""
Awareness Level Classifier (OPS-002)
=====================================
Classifies content text into one of Eugene Schwartz's 5 awareness levels.

Uses rule-based semantic detection with keyword/phrase matching to determine
which awareness stage a piece of content targets.

Awareness Levels:
- UNAWARE: Audience doesn't know they have a problem
- PROBLEM_AWARE: Knows problem exists, not the solution
- SOLUTION_AWARE: Knows solutions exist, comparing options
- PRODUCT_AWARE: Knows your product, needs convincing
- MOST_AWARE: Ready to buy, just needs nudge

Algorithm:
- Score each awareness level independently (0.0-1.0)
- Return level with highest score
- Requires minimum confidence threshold (default 0.3)

Performance: <2ms per classification
No AI/ML - pure rule-based pattern matching
"""

import re
from typing import Dict, Optional, Tuple
from enum import Enum
from dataclasses import dataclass
from loguru import logger


class AwarenessLevel(Enum):
    """Eugene Schwartz's 5 awareness levels"""
    UNAWARE = "unaware"
    PROBLEM_AWARE = "problem_aware"
    SOLUTION_AWARE = "solution_aware"
    PRODUCT_AWARE = "product_aware"
    MOST_AWARE = "most_aware"


@dataclass
class AwarenessScore:
    """Classification result with confidence scores"""
    level: AwarenessLevel
    confidence: float
    scores: Dict[str, float]  # All level scores

    def to_dict(self) -> Dict:
        return {
            "level": self.level.value,
            "confidence": self.confidence,
            "scores": self.scores
        }


class AwarenessClassifier:
    """
    Awareness Level Classifier

    Classifies content into one of 5 awareness stages using pattern matching.

    Usage:
        classifier = AwarenessClassifier.get_instance()
        result = classifier.classify("Are you struggling with...")
        print(result.level)  # AwarenessLevel.PROBLEM_AWARE
        print(result.confidence)  # 0.82
    """

    _instance: Optional["AwarenessClassifier"] = None

    def __init__(self):
        if AwarenessClassifier._instance is not None:
            raise RuntimeError("Use AwarenessClassifier.get_instance()")

        self._compile_patterns()
        logger.info("🎯 Awareness Classifier initialized")

    @classmethod
    def get_instance(cls) -> "AwarenessClassifier":
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _compile_patterns(self):
        """Compile regex patterns for each awareness level"""

        # UNAWARE: Surface symptoms, storytelling, "you might not realize"
        self.unaware_patterns = [
            # Surface symptoms without naming the problem
            r'\b(you might not (?:notice|realize|know))\b',
            r'\b(ever wonder why)\b',
            r'\b(here\'s what (?:most people|everyone) misses?)\b',
            r'\b(the hidden (?:truth|reality|cost))\b',
            r'\b(what if I told you)\b',
            r'\b(imagine if)\b',
            r'\b(picture this)\b',
            r'\b(here\'s a story)\b',
            r'\b(most people don\'t know)\b',
            r'\b(the secret)\b',
            r'\b(nobody talks about)\b',
            r'\b(shocking truth)\b',
            # Quiz/assessment language
            r'\b(take (?:this|our) (?:quiz|test|assessment))\b',
            r'\b(see if (?:this|it) applies)\b',
            r'\b(are you one of)\b',
        ]

        # PROBLEM_AWARE: Pain mirroring, "struggling with", "tired of"
        self.problem_aware_patterns = [
            # Direct pain language
            r'\b(struggling with|tired of|sick of|fed up with)\b',
            r'\b(can\'t seem to)\b',
            r'\b(keeps? happening)\b',
            r'\b(why (?:do|does|is|are) (?:you|your|we|I))\b',
            r'\b(the problem (?:is|with))\b',
            r'\b(if you\'re (?:like me|struggling|dealing with))\b',
            r'\b(the real (?:issue|problem|challenge))\b',
            r'\b(what\'s really (?:causing|behind))\b',
            r'\b(the root cause)\b',
            r'\b(here\'s why (?:you|it))\b',
            # Frustration markers
            r'\b(every time you try)\b',
            r'\b(wasting (?:time|money|effort))\b',
            r'\b(never (?:works|lasts|helps))\b',
            r'\b(always (?:fails|breaks|ends up))\b',
            # Pain amplification
            r'\b(makes it (?:worse|harder))\b',
            r'\b(getting worse)\b',
            r'\b(costing you)\b',
        ]

        # SOLUTION_AWARE: Comparing approaches, "there are ways", mechanisms
        self.solution_aware_patterns = [
            # Comparison language
            r'\b(there are (?:\d+ )?(?:ways|methods|approaches|solutions))\b',
            r'\b((?:most|some) people (?:use|try|choose))\b',
            r'\b(the (?:traditional|old|common) (?:way|approach|method))\b',
            r'\b(vs\.?|versus|compared to|better than)\b',
            r'\b(instead of)\b',
            r'\b(the difference between)\b',
            # Education/mechanism language
            r'\b(here\'s how)\b',
            r'\b(how (?:the best|top|successful))\b',
            r'\b(the (?:process|system|framework|methodology))\b',
            r'\b(step[- ]by[- ]step)\b',
            r'\b((?:case study|example|demo))\b',
            r'\b(how (?:it|to|you can|they))\b',
            r'\b(the mechanism)\b',
            r'\b(works by)\b',
            r'\b(find \w+ (?:topics|ideas|content))\b',
            r'\b(in (?:under|less than) \d+)\b',
            # Proof/authority
            r'\b((?:studies|research|data) shows?)\b',
            r'\b(\d+% of (?:people|users|companies))\b',
            r'\b(proven (?:to|by|in))\b',
            r'\b(backed by)\b',
        ]

        # PRODUCT_AWARE: Product mentions, differentiation, "unlike X"
        self.product_aware_patterns = [
            # Product references
            r'\b(?:our|my|this) (?:tool|app|platform|software|service|product|solution)\b',
            r'\b(?:with|using) [A-Z][a-zA-Z]+(?:App|Scout|Radar|\.(?:app|io|com))\b',  # Product names
            r'\bwith [A-Z]\w+,\s+you\s+get\b',  # "With TrendScout, you get"
            # Differentiation
            r'\b(unlike (?:other|most|traditional))\b',
            r'\b(what makes (?:us|it|this) different)\b',
            r'\b(the only (?:one|tool|platform))\b',
            r'\b(built (?:for|specifically))\b',
            r'\b(designed (?:for|to))\b',
            # Objection handling
            r'\b(you might be (?:thinking|wondering))\b',
            r'\b((?:but|yes,?) (?:what about|isn\'t))\b',
            r'\b(here\'s the thing)\b',
            r'\b(the truth is)\b',
            # Feature/benefit
            r'\b(includes|comes with|features?)\b',
            r'\b((?:you )?get (?:real-time|instant|AI-powered))\b',
            r'\b(helps? you)\b',
            r'\b(lets you)\b',
            r'\b(allows you to)\b',
        ]

        # MOST_AWARE: Strong CTAs, urgency, pricing, "get started"
        self.most_aware_patterns = [
            # Strong CTAs
            r'\b(get (?:started|it|access|yours|instant))\b',
            r'\b(sign up|join|subscribe)\b',
            r'\b(buy now)\b',
            r'\b(purchase|order)\b',
            r'\b(claim your|grab your)\b',
            r'\b(start (?:your|today|now))\b',
            r'\b((?:try|download) (?:it|now|today|free))\b',
            # Urgency
            r'\b(limited (?:time|spots|offer))\b',
            r'\b((?:only|just) \d+ (?:left|remaining|spots))\b',
            r'\b(ends (?:soon|today|tonight))\b',
            r'\b(don\'t (?:wait|miss out))\b',
            r'\b(before it\'s (?:too late|gone))\b',
            # Pricing
            r'\$\d+(?:\.\d{2})?(?:/(?:mo(?:nth)?|yr|year))?',
            r'\b(?:free|paid|premium|pro) (?:plan|tier|version)\b',
            r'\b(pricing|plans?)\b',
            # Friction removal
            r'\b(no (?:credit card|commitment|risk))\b',
            r'\b(\d+[- ]day (?:trial|guarantee))\b',
            r'\b(cancel anytime)\b',
            r'\b(money[- ]back)\b',
        ]

        # Compile all patterns
        self.compiled_patterns = {
            AwarenessLevel.UNAWARE: [re.compile(p, re.IGNORECASE) for p in self.unaware_patterns],
            AwarenessLevel.PROBLEM_AWARE: [re.compile(p, re.IGNORECASE) for p in self.problem_aware_patterns],
            AwarenessLevel.SOLUTION_AWARE: [re.compile(p, re.IGNORECASE) for p in self.solution_aware_patterns],
            AwarenessLevel.PRODUCT_AWARE: [re.compile(p, re.IGNORECASE) for p in self.product_aware_patterns],
            AwarenessLevel.MOST_AWARE: [re.compile(p, re.IGNORECASE) for p in self.most_aware_patterns],
        }

    def classify(
        self,
        text: str,
        min_confidence: float = 0.3
    ) -> AwarenessScore:
        """
        Classify text into awareness level

        Args:
            text: Content text to classify
            min_confidence: Minimum confidence threshold (default: 0.3)

        Returns:
            AwarenessScore with level, confidence, and all scores
        """
        if not text or not text.strip():
            return AwarenessScore(
                level=AwarenessLevel.UNAWARE,
                confidence=0.0,
                scores={level.value: 0.0 for level in AwarenessLevel}
            )

        # Score each level
        scores = {}
        for level, patterns in self.compiled_patterns.items():
            score = self._score_level(text, patterns)
            scores[level.value] = score

        # Find highest scoring level
        max_level = max(scores.items(), key=lambda x: x[1])
        level_name, confidence = max_level

        # Convert back to enum
        level = AwarenessLevel(level_name)

        # Check minimum confidence
        if confidence < min_confidence:
            logger.warning(
                f"Low confidence ({confidence:.2f}) for text: {text[:50]}..."
            )

        return AwarenessScore(
            level=level,
            confidence=confidence,
            scores=scores
        )

    def _score_level(self, text: str, patterns: list) -> float:
        """
        Score a single awareness level

        Args:
            text: Content text
            patterns: List of compiled regex patterns

        Returns:
            Score between 0.0 and 1.0
        """
        matches = 0
        total_patterns = len(patterns)

        for pattern in patterns:
            if pattern.search(text):
                matches += 1

        # Normalize to 0.0-1.0
        # Boost scores: each match is worth more to reach confidence threshold
        # With ~15 patterns per level, 1-2 matches should give 0.3-0.5 confidence
        raw_score = matches / total_patterns if total_patterns > 0 else 0.0
        # Amplify: multiply by 6 to make single matches more meaningful
        # 1 match out of 15 patterns = 0.067 * 6 = 0.4 confidence
        boosted_score = min(1.0, raw_score * 6.0)
        return boosted_score

    def score_all(self, text: str) -> Dict[str, float]:
        """
        Get scores for all awareness levels

        Args:
            text: Content text

        Returns:
            Dictionary mapping level names to scores
        """
        result = self.classify(text)
        return result.scores


# Singleton getter function
_classifier_instance: Optional[AwarenessClassifier] = None

def get_awareness_classifier() -> AwarenessClassifier:
    """Get singleton AwarenessClassifier instance"""
    global _classifier_instance
    if _classifier_instance is None:
        _classifier_instance = AwarenessClassifier.get_instance()
    return _classifier_instance
