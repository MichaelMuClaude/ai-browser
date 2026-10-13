"""Unit tests for anti-bot block detection."""
import pytest

from src.core.block_detection import detect_block, is_blocked


class TestDetectBlock:
    """Tests for detect_block()."""

    def test_empty_input_not_blocked(self):
        result = detect_block()
        # body_empty signal fires but alone (0.45) is below threshold
        assert result.blocked is False
        assert result.confidence < 0.5

    def test_cloudflare_title_just_a_moment(self):
        result = detect_block(title="Just a moment...")
        assert result.blocked is True
        assert result.confidence > 0
        assert any(s.name == "cf_title_just_a_moment" for s in result.signals)

    def test_cloudflare_body_verification(self):
        result = detect_block(body_text='<div id="cf-browser-verification">checking</div>')
        assert result.blocked is True
        assert any(s.name == "cf_browser_verification" for s in result.signals)

    def test_datadome_body_hit(self):
        result = detect_block(body_text="Please verify you are human by clicking below verify_you_are_human")
        assert result.blocked is True
        assert any(s.name == "verify_you_are_human" for s in result.signals)

    def test_perimeterx_body_hit(self):
        result = detect_block(body_text='<div class="px-captcha"></div>')
        assert result.blocked is True
        assert any(s.name == "perimeterx" for s in result.signals)

    def test_normal_page_with_captcha_word_not_blocked(self):
        """A single 'captcha' keyword on an educational page must not be flagged."""
        body = (
            "This article explains how to implement a captcha in Python. "
            "Captcha stands for Completely Automated Public Turing test. "
            "We will cover the basics of implementing a captcha system, "
            "including image-based challenges, audio alternatives, and "
            "accessibility considerations for users with disabilities. "
            "The implementation uses Flask and Pillow for image generation."
        )
        result = detect_block(title="How to build a captcha", body_text=body)
        # Only the body 'captcha' signal should fire (body is now > 200 chars).
        assert len(result.signals) == 1
        assert result.signals[0].name == "captcha"
        # A single weak keyword must not cross the block threshold.
        assert result.blocked is False
        assert result.confidence < 0.5

    def test_url_challenge_boosts_confidence(self):
        result = detect_block(
            title="Example Domain",
            url="https://example.com/challenge?token=abc",
        )
        assert any(s.name == "url_challenge" for s in result.signals)

    def test_multiple_signals_aggregate_confidence(self):
        result = detect_block(
            title="Just a moment...",
            body_text="cf-browser-verification " + ("x" * 200),
        )
        assert result.blocked is True
        assert result.confidence > 0.75
        assert len(result.signals) == 2  # cf_title + cf_body (body now > 200 chars)

    def test_no_false_positive_for_normal_page(self):
        result = detect_block(
            title="Python FastAPI Tutorial",
            body_text="Learn how to build APIs with FastAPI and Pydantic. " * 5,
            url="https://example.com/tutorial",
        )
        assert result.blocked is False
        assert result.confidence == 0.0

    # --- Task 3: new signal tests ---

    def test_datadome_empty_body(self):
        """DataDome pages have empty body + domain-only title."""
        r = detect_block(title="g2.com", body_text="", url="https://www.g2.com")
        assert r.blocked is True
        # body_empty(0.45) + title_is_domain(0.40) = 0.85
        assert r.confidence >= 0.8

    def test_datadome_script_in_html(self):
        """DataDome challenge script fingerprint should be detected."""
        r = detect_block(
            title="g2.com",
            body_text="",
            html="<script data-cfasync=\"false\">var dd={'rt':'i','cid':'AHrlqAAAAAMA2PAe0SG3rQEAc0Y6zg=='}</script>",
        )
        assert r.blocked is True
        assert r.confidence >= 0.6
        assert any(s.name == "datadome_script" for s in r.signals)

    def test_title_is_domain(self):
        """Title that is just a domain name should trigger title_is_domain signal."""
        r = detect_block(title="yelp.com", body_text="")
        assert any(s.name == "title_is_domain" for s in r.signals)
        assert r.blocked is True

    def test_title_is_domain_not_triggered_for_normal_title(self):
        """Normal titles with spaces should not trigger domain signal."""
        r = detect_block(title="Welcome to Yelp", body_text="a" * 5000)
        assert not any(s.name == "title_is_domain" for s in r.signals)
        assert r.blocked is False

    def test_body_too_short(self):
        """Body under 200 chars should trigger body_too_short signal."""
        r = detect_block(title="Some Page", body_text="short content")
        assert any(s.name == "body_too_short" for s in r.signals)
        # body_too_short alone (0.30) is below threshold
        assert r.blocked is False

    def test_body_empty_signal(self):
        """Empty body should trigger body_empty signal."""
        r = detect_block(title="Some Page", body_text="")
        assert any(s.name == "body_empty" for s in r.signals)
        # body_empty alone (0.45) is below threshold, needs companion signal
        assert r.blocked is False

    def test_normal_page_with_html_param_not_blocked(self):
        """Normal page with html parameter should not be blocked."""
        r = detect_block(
            title="Stack Overflow",
            body_text="a" * 5000,
            url="https://stackoverflow.com",
            html="<html><body>normal content</body></html>",
        )
        assert r.blocked is False

    def test_url_checkpoint_signal(self):
        """URL with /checkpoint should trigger url_checkpoint signal."""
        r = detect_block(url="https://example.com/checkpoint?next=/home")
        assert any(s.name == "url_checkpoint" for s in r.signals)

    def test_url_blocked_signal(self):
        """URL with /blocked should trigger url_blocked signal."""
        r = detect_block(url="https://example.com/blocked")
        assert any(s.name == "url_blocked" for s in r.signals)

    def test_perimeterx_script_in_html(self):
        """PerimeterX script fingerprint in HTML should be detected."""
        r = detect_block(
            title="example.com",
            body_text="",
            html="<script>var _pxAppId='PXsomeId';</script>",
        )
        assert r.blocked is True
        assert any(s.name == "perimeterx_script" for s in r.signals)

    def test_multiple_new_signals_aggregate(self):
        """DataDome: empty body + domain title + datadome script should aggregate high."""
        r = detect_block(
            title="g2.com",
            body_text="",
            url="https://www.g2.com/products/slack/reviews",
            html="<script data-cfasync=\"false\">var dd={'rt':'i'}</script>",
        )
        assert r.blocked is True
        # body_empty(0.45) + title_is_domain(0.40) + datadome_script(0.60) + datadome_cfasync(0.40) = 1.85 clamped to 1.0
        assert r.confidence == 1.0
        assert len(r.signals) >= 4

    # --- Phase 3 Task 1: native blank page signals ---

    def test_untitled_chrome_detected(self):
        """Title 'Untitled - Google Chrome' should trigger native_blank_page signal."""
        r = detect_block(title="Untitled - Google Chrome", body_text="")
        assert any(s.name == "native_blank_page" for s in r.signals)
        # native_blank_page(0.40) + body_empty(0.45) = 0.85
        assert r.blocked is True

    def test_new_tab_chrome_detected(self):
        """Title 'New Tab - Google Chrome' should trigger native_new_tab signal."""
        r = detect_block(title="New Tab - Google Chrome", body_text="")
        assert any(s.name == "native_new_tab" for s in r.signals)
        # native_new_tab(0.40) + body_empty(0.45) = 0.85
        assert r.blocked is True

    # --- Multi-search-engine support: Google rate-limit signals ---

    def test_google_sorry_index_url_signal(self):
        """Google rate-limit URL /sorry/index should trigger url_google_sorry signal."""
        r = detect_block(url="https://www.google.com/sorry/index?continue=...")
        assert any(s.name == "url_google_sorry" for s in r.signals)
        # 0.50 alone crosses the block threshold
        assert r.blocked is True

    def test_google_sorry_query_url_signal(self):
        """Google rate-limit URL /sorry/?... should trigger url_google_sorry signal."""
        r = detect_block(url="https://www.google.com/sorry/?continue=...")
        assert any(s.name == "url_google_sorry" for s in r.signals)
        assert r.blocked is True

    def test_google_unusual_traffic_body_signal(self):
        """Google 'unusual traffic' body text should trigger google_unusual_traffic signal."""
        r = detect_block(
            title="Sorry...",
            body_text="Our systems have detected unusual traffic from your computer network.",
        )
        assert any(s.name == "google_unusual_traffic" for s in r.signals)
        # google_unusual_traffic(0.55) alone crosses the block threshold
        assert r.blocked is True

    def test_google_unusual_traffic_case_insensitive(self):
        """Google unusual traffic detection should be case-insensitive."""
        r = detect_block(body_text="OUR SYSTEMS HAVE DETECTED UNUSUAL TRAFFIC")
        assert any(s.name == "google_unusual_traffic" for s in r.signals)
        assert r.blocked is True

    def test_bare_unusual_traffic_in_article_not_blocked(self):
        """A normal article that happens to contain 'unusual traffic' must not be flagged."""
        body = (
            "A recent study found that unusual traffic patterns on the interstate "
            "were caused by roadwork and holiday travel. Drivers experienced delays "
            "during peak hours, but the overall congestion was lower than expected."
        )
        r = detect_block(title="Traffic patterns shift as roadwork expands", body_text=body)
        assert not any(s.name == "google_unusual_traffic" for s in r.signals)
        assert r.blocked is False
        assert r.confidence < 0.5


class TestIsBlocked:
    """Tests for the is_blocked() convenience wrapper."""

    def test_is_blocked_true(self):
        assert is_blocked(title="Just a moment...") is True

    def test_is_blocked_false(self):
        assert is_blocked(title="Welcome", body_text="a" * 500) is False
