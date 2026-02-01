"""
Tests for real service implementations in content-intelligence.
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestFATEScorer:
    """Test the real FATEScorer implementation."""
    
    def test_import(self):
        """Test that FATEScorer can be imported."""
        from services.analysis.fate_scorer import FATEScorer
        scorer = FATEScorer()
        assert scorer is not None
    
    def test_score_focus(self):
        """Test Focus scoring."""
        from services.analysis.fate_scorer import FATEScorer
        scorer = FATEScorer()
        
        # High focus text
        text = "Most founders fail because they don't understand this. Here's why you're doing it wrong."
        score = scorer.score_focus(text)
        assert 0 <= score <= 1
        assert score > 0.3  # Should have moderate focus
    
    def test_score_authority(self):
        """Test Authority scoring."""
        from services.analysis.fate_scorer import FATEScorer
        scorer = FATEScorer()
        
        # High authority text
        text = "I've helped 127 founders grow their businesses. After 10 years of research, here's how the mechanism works."
        score = scorer.score_authority(text)
        assert 0 <= score <= 1
        assert score > 0.3  # Should have authority signals
    
    def test_score_tribe(self):
        """Test Tribe scoring."""
        from services.analysis.fate_scorer import FATEScorer
        scorer = FATEScorer()
        
        # High tribe text
        text = "If you're a founder like me, you've felt this frustration. People like us understand what bootstrappers go through."
        score = scorer.score_tribe(text)
        assert 0 <= score <= 1
        assert score > 0.2  # Should have tribe signals
    
    def test_score_emotion(self):
        """Test Emotion scoring."""
        from services.analysis.fate_scorer import FATEScorer
        scorer = FATEScorer()
        
        # High emotion text
        text = "I was broke and desperate. I wasted 5 years on the wrong approach. Then everything changed when I finally discovered the truth."
        score = scorer.score_emotion(text)
        assert 0 <= score <= 1
        assert score > 0.3  # Should have emotion signals
    
    def test_empty_text(self):
        """Test scoring with empty text."""
        from services.analysis.fate_scorer import FATEScorer
        scorer = FATEScorer()
        
        assert scorer.score_focus("") == 0.0
        assert scorer.score_authority("") == 0.0
        assert scorer.score_tribe("") == 0.0
        assert scorer.score_emotion("") == 0.0


class TestAwarenessClassifier:
    """Test the real AwarenessClassifier implementation."""
    
    def test_import(self):
        """Test that AwarenessClassifier can be imported."""
        from services.analysis.awareness_classifier import AwarenessClassifier
        classifier = AwarenessClassifier.get_instance()
        assert classifier is not None
    
    def test_classify_problem_aware(self):
        """Test problem-aware classification."""
        from services.analysis.awareness_classifier import AwarenessClassifier
        classifier = AwarenessClassifier.get_instance()
        
        text = "Are you struggling with getting views on your content? Tired of posting every day with no results?"
        result = classifier.classify(text)
        
        assert result.level.value in ["unaware", "problem_aware", "solution_aware", "product_aware", "most_aware"]
        assert 0 <= result.confidence <= 1
    
    def test_classify_solution_aware(self):
        """Test solution-aware classification."""
        from services.analysis.awareness_classifier import AwarenessClassifier
        classifier = AwarenessClassifier.get_instance()
        
        text = "There are 3 ways to grow your following. Here's how successful creators do it step by step."
        result = classifier.classify(text)
        
        assert result.level.value in ["unaware", "problem_aware", "solution_aware", "product_aware", "most_aware"]
        assert 0 <= result.confidence <= 1


class TestSentimentAnalyzer:
    """Test the real SentimentAnalyzer implementation."""
    
    def test_import(self):
        """Test that SentimentAnalyzer can be imported."""
        from services.analysis.sentiment_analyzer_standalone import get_sentiment_analyzer
        analyzer = get_sentiment_analyzer()
        assert analyzer is not None
    
    def test_positive_sentiment(self):
        """Test positive sentiment detection."""
        from services.analysis.sentiment_analyzer_standalone import get_sentiment_analyzer
        analyzer = get_sentiment_analyzer()
        
        text = "This is amazing! I absolutely love how easy it is to use. Best tool ever!"
        result = analyzer.analyze(text)
        
        assert result.label == "positive"
        assert result.score > 0
        assert 0 <= result.confidence <= 1
    
    def test_negative_sentiment(self):
        """Test negative sentiment detection."""
        from services.analysis.sentiment_analyzer_standalone import get_sentiment_analyzer
        analyzer = get_sentiment_analyzer()
        
        text = "This is terrible. I hate how broken it is. Worst experience ever."
        result = analyzer.analyze(text)
        
        assert result.label == "negative"
        assert result.score < 0
    
    def test_neutral_sentiment(self):
        """Test neutral sentiment detection."""
        from services.analysis.sentiment_analyzer_standalone import get_sentiment_analyzer
        analyzer = get_sentiment_analyzer()
        
        text = "The product comes in a box. It has a screen and buttons."
        result = analyzer.analyze(text)
        
        assert result.label == "neutral"
        assert -0.3 <= result.score <= 0.3
    
    def test_emotion_detection(self):
        """Test emotion detection."""
        from services.analysis.sentiment_analyzer_standalone import get_sentiment_analyzer
        analyzer = get_sentiment_analyzer()
        
        text = "I'm so happy and excited about this wonderful news!"
        result = analyzer.analyze(text)
        
        assert "joy" in result.emotions or result.score > 0
    
    def test_empty_text(self):
        """Test empty text handling."""
        from services.analysis.sentiment_analyzer_standalone import get_sentiment_analyzer
        analyzer = get_sentiment_analyzer()
        
        result = analyzer.analyze("")
        assert result.label == "neutral"
        assert result.score == 0.0
        assert result.confidence == 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
