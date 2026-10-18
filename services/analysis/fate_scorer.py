"""
FATE Scoring Service
====================
OPS-001: Score content for Focus, Authority, Tribe, Emotion elements.

Based on Chase Hughes - SRS #253 persuasion framework:
- F (Focus): Pattern interrupt, curiosity gap, stakes
- A (Authority): Numbers, proof, mechanism
- T (Tribe): Identity markers, us-vs-them
- E (Emotion): Story beats, contrast, loss aversion, hope

This is a rule-based semantic detection system (not ML/AI).
Each element is scored independently on a 0.0-1.0 scale.

Usage:
    scorer = FATEScorer()
    scores = scorer.score_all("Your content here...")
    # Returns: {"F": 0.65, "A": 0.78, "T": 0.52, "E": 0.71}
"""

import re
from typing import Dict
from loguru import logger


class FATEScorer:
    """
    FATE Framework Scorer

    Scores content on four persuasion elements:
    - Focus (F): 0.0-1.0
    - Authority (A): 0.0-1.0
    - Tribe (T): 0.0-1.0
    - Emotion (E): 0.0-1.0
    """

    # Focus patterns (pattern interrupts, curiosity gaps, stakes)
    FOCUS_PATTERNS = [
        r'\bmost\s+\w+\s+fail',
        r'\bwhat\s+if\b',
        r'\bhere\'s\s+why\b',
        r'\bthe\s+truth\s+about\b',
        r'\bstop\s+\w+ing\b',
        r'\byou\'re\s+doing\s+\w+\s+wrong\b',
        r'\bthe\s+\w+\s+nobody\s+talks\s+about\b',
        r'\bI\s+used\s+to\s+think\b',
        r'\beveryone\s+gets\s+this\s+wrong\b',
        r'\bthis\s+changed\s+everything\b',
        r'\bhere\'s\s+what\s+\w+\s+don\'t\s+tell\s+you\b',
        r'\bthe\s+\w+\s+mistake\b',
        r'\bwant\s+to\s+know\b',
        r'\bdiscovered\s+that\b',
        r'\brealized\s+that\b',
    ]

    # Authority patterns (numbers, proof, mechanisms)
    AUTHORITY_PATTERNS = [
        r'\bI\'ve\s+helped\s+\d+',
        r'\bafter\s+\d+\s+\w+',
        r'\bhere\'s\s+how\b',
        r'\bthe\s+mechanism\s+is\b',
        r'\bthe\s+pattern\s+is\b',
        r'\bin\s+my\s+\d+\s+years\b',
        r'\b\d+%\s+of\b',
        r'\b\d+x\s+\w+',
        r'\bproved\s+by\b',
        r'\bthe\s+data\s+shows\b',
        r'\bthe\s+research\s+shows\b',
        r'\bI\s+found\s+that\b',
        r'\bthe\s+real\s+pattern\b',
        r'\btested\s+this\s+with\b',
        r'\bmeasured\s+\w+\s+and\b',
    ]

    # Tribe patterns (identity, us-vs-them)
    TRIBE_PATTERNS = [
        r'\bif\s+you\'re\s+a\s+\w+',
        r'\bpeople\s+like\s+us\b',
        r'\bwe\s+\w+\s+know\b',
        r'\bfor\s+\w+\s+who\b',
        r'\byou\'ve\s+felt\s+this\b',
        r'\bbootstrapp(ers?|ing)\b',
        r'\bfounder(s?)\b',
        r'\bbuilder(s?)\b',
        r'\bcreator(s?)\b',
        r'\bwhile\s+\w+\s+are\b',
        r'\bunlike\s+\w+\b',
        r'\bthe\s+\w+\s+won\'t\s+tell\s+you\b',
        r'\bif\s+you\'re\s+like\s+me\b',
        r'\byou\s+and\s+I\s+both\s+know\b',
    ]

    # Emotion patterns (story beats, contrast, loss aversion, hope)
    EMOTION_PATTERNS = [
        r'\bI\s+was\s+(broke|desperate|struggling|failing)\b',
        r'\bI\s+wasted\s+\d+',
        r'\bI\s+lost\s+\d+',
        r'\bthen\s+everything\s+changed\b',
        r'\bbut\s+then\s+I\b',
        r'\bimagine\s+\w+ing\b',
        r'\bpicture\s+this\b',
        r'\bthe\s+pain\s+of\b',
        r'\bthe\s+frustration\s+of\b',
        r'\bfelt\s+like\s+\w+ing\b',
        r'\bI\s+remember\s+when\b',
        r'\bnever\s+forget\s+the\s+day\b',
        r'\bfinally\s+\w+ed\b',
        r'\btransformed\s+my\b',
        r'\bthe\s+moment\s+I\b',
    ]

    def __init__(self):
        """Initialize the FATE scorer with compiled regex patterns"""
        self.focus_regexes = [re.compile(pattern, re.IGNORECASE) for pattern in self.FOCUS_PATTERNS]
        self.authority_regexes = [re.compile(pattern, re.IGNORECASE) for pattern in self.AUTHORITY_PATTERNS]
        self.tribe_regexes = [re.compile(pattern, re.IGNORECASE) for pattern in self.TRIBE_PATTERNS]
        self.emotion_regexes = [re.compile(pattern, re.IGNORECASE) for pattern in self.EMOTION_PATTERNS]

    def score_focus(self, text: str) -> float:
        """
        Score Focus element (0.0-1.0)

        Detects: Pattern interrupts, curiosity gaps, stakes
        Examples: "Most founders fail...", "What if...", "Here's why..."

        Args:
            text: Content to score

        Returns:
            Score from 0.0 to 1.0
        """
        if not text:
            return 0.0

        # Count pattern matches
        matches = sum(1 for regex in self.focus_regexes if regex.search(text))

        # Additional heuristics
        has_question = '?' in text
        has_exclamation = '!' in text
        starts_with_hook = len(text) > 10 and any(regex.match(text[:100]) for regex in self.focus_regexes)

        # Combine signals
        base_score = min(matches * 0.3, 0.7)  # Each match adds 0.3, cap at 0.7
        hook_bonus = 0.25 if starts_with_hook else 0.0
        punctuation_bonus = 0.05 if has_question or has_exclamation else 0.0

        score = min(base_score + hook_bonus + punctuation_bonus, 1.0)

        logger.debug(f"Focus score: {score:.2f} (matches={matches}, hook={starts_with_hook})")
        return score

    def score_authority(self, text: str) -> float:
        """
        Score Authority element (0.0-1.0)

        Detects: Numbers, proof, mechanisms
        Examples: "I've helped 127 founders...", "Here's how...", "The data shows..."

        Args:
            text: Content to score

        Returns:
            Score from 0.0 to 1.0
        """
        if not text:
            return 0.0

        # Count pattern matches
        matches = sum(1 for regex in self.authority_regexes if regex.search(text))

        # Additional heuristics
        has_numbers = bool(re.search(r'\b\d+\b', text))
        has_mechanism = bool(re.search(r'\b(how|mechanism|pattern|process|system)\b', text, re.IGNORECASE))
        has_proof_words = bool(re.search(r'\b(proof|data|research|tested|measured|found)\b', text, re.IGNORECASE))

        # Combine signals
        base_score = min(matches * 0.35, 0.65)  # Each match adds 0.35, cap at 0.65
        numbers_bonus = 0.27 if has_numbers else 0.0
        mechanism_bonus = 0.22 if has_mechanism else 0.0
        proof_bonus = 0.18 if has_proof_words else 0.0

        score = min(base_score + numbers_bonus + mechanism_bonus + proof_bonus, 1.0)

        logger.debug(f"Authority score: {score:.2f} (matches={matches}, numbers={has_numbers}, mechanism={has_mechanism})")
        return score

    def score_tribe(self, text: str) -> float:
        """
        Score Tribe element (0.0-1.0)

        Detects: Identity markers, us-vs-them
        Examples: "If you're a bootstrapper...", "People like us...", "We founders know..."

        Args:
            text: Content to score

        Returns:
            Score from 0.0 to 1.0
        """
        if not text:
            return 0.0

        # Count pattern matches
        matches = sum(1 for regex in self.tribe_regexes if regex.search(text))

        # Additional heuristics
        has_second_person = bool(re.search(r'\byou(\'re|r)?\b', text, re.IGNORECASE))
        has_first_person_plural = bool(re.search(r'\b(we|us|our)\b', text, re.IGNORECASE))
        has_identity = bool(re.search(r'\b(founder|builder|creator|entrepreneur|bootstrapper)\b', text, re.IGNORECASE))

        # Combine signals
        base_score = min(matches * 0.4, 0.7)  # Each match adds 0.4, cap at 0.7
        second_person_bonus = 0.15 if has_second_person else 0.0
        plural_bonus = 0.22 if has_first_person_plural else 0.0
        identity_bonus = 0.15 if has_identity else 0.0

        score = min(base_score + second_person_bonus + plural_bonus + identity_bonus, 1.0)

        logger.debug(f"Tribe score: {score:.2f} (matches={matches}, identity={has_identity})")
        return score

    def score_emotion(self, text: str) -> float:
        """
        Score Emotion element (0.0-1.0)

        Detects: Story beats, contrast, loss aversion, hope
        Examples: "I was broke, desperate...", "Then everything changed...", "Imagine..."

        Args:
            text: Content to score

        Returns:
            Score from 0.0 to 1.0
        """
        if not text:
            return 0.0

        # Count pattern matches
        matches = sum(1 for regex in self.emotion_regexes if regex.search(text))

        # Additional heuristics
        has_story_markers = bool(re.search(r'\b(I|me|my)\b.*\b(was|felt|remember|never forget)\b', text, re.IGNORECASE))
        has_contrast = bool(re.search(r'\b(but|then|now|before|after)\b', text, re.IGNORECASE))
        has_transformation = bool(re.search(r'\b(changed|transformed|discovered|realized|finally)\b', text, re.IGNORECASE))
        has_vivid_language = bool(re.search(r'\b(imagine|picture|feel|pain|frustration|hope|dream)\b', text, re.IGNORECASE))

        # Combine signals
        base_score = min(matches * 0.4, 0.6)  # Each match adds 0.4, cap at 0.6
        story_bonus = 0.3 if has_story_markers else 0.0
        contrast_bonus = 0.2 if has_contrast else 0.0
        transformation_bonus = 0.25 if has_transformation else 0.0
        vivid_bonus = 0.15 if has_vivid_language else 0.0

        score = min(base_score + story_bonus + contrast_bonus + transformation_bonus + vivid_bonus, 1.0)

        logger.debug(f"Emotion score: {score:.2f} (matches={matches}, story={has_story_markers}, contrast={has_contrast})")
        return score

    def score_all(self, text: str) -> Dict[str, float]:
        """
        Score all FATE elements

        Args:
            text: Content to score

        Returns:
            Dictionary with scores for each element:
            {"F": 0.65, "A": 0.78, "T": 0.52, "E": 0.71}
        """
        scores = {
            "F": self.score_focus(text),
            "A": self.score_authority(text),
            "T": self.score_tribe(text),
            "E": self.score_emotion(text),
        }

        logger.info(f"FATE scores: F={scores['F']:.2f}, A={scores['A']:.2f}, T={scores['T']:.2f}, E={scores['E']:.2f}")
        return scores


# Singleton instance
_scorer_instance = None

def get_fate_scorer() -> FATEScorer:
    """Get singleton FATE scorer instance"""
    global _scorer_instance
    if _scorer_instance is None:
        _scorer_instance = FATEScorer()
    return _scorer_instance
