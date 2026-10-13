"""Unit tests for look-bundle helpers in src/look.py."""
from src.look import _thin_text_link_fallback


class TestThinTextLinkFallback:
    """Real case (2026-07-28): searching Google for certain queries (seen with
    CJK-heavy natural-language queries) renders an image-card-heavy results
    layout where document.body.innerText comes back nearly empty (just
    nav/footer chrome), even though the page has real result links with real
    hrefs in `elements`. Confirmed via a live repro: element_count=29 but
    extracted text was under 150 chars. The fallback surfaces those links
    instead of silently returning what looks like a failed/empty search."""

    def test_leaves_normal_length_text_untouched(self):
        text = "a" * 500
        elements = [{"text": "Result", "href": "https://example.com/page"}]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert result == text

    def test_appends_links_when_text_is_thin(self):
        text = "Skip to main content\nAccessibility help\nSearch Results"
        elements = [
            {"text": "", "href": "https://taobao.com/item/123"},
            {"text": "A review", "href": "https://reddit.com/r/x/comments/1"},
        ]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert text in result
        assert "https://taobao.com/item/123" in result
        assert "https://reddit.com/r/x/comments/1" in result
        assert "A review" in result

    def test_excludes_search_engine_chrome_links(self):
        text = "Skip to main content"
        elements = [
            {"text": "Go to Google Home", "href": "https://www.google.com/webhp?hl=en"},
            {"text": "Sign in", "href": "https://accounts.google.com/signin"},
            {"text": "Real result", "href": "https://zhuanlan.zhihu.com/p/123"},
        ]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert "google.com" not in result.replace("Skip to main content", "")
        assert "https://zhuanlan.zhihu.com/p/123" in result

    def test_no_fallback_note_when_no_real_links_found(self):
        text = "Skip to main content"
        elements = [{"text": "Go to Google Home", "href": "https://www.google.com/webhp"}]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert result == text

    def test_deduplicates_repeated_href(self):
        text = ""
        elements = [
            {"text": f"Result {i}", "href": "https://example.com/page"}
            for i in range(30)
        ]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert result.count("https://example.com/page") == 1

    def test_caps_at_max_links(self):
        """Codex review (2026-07-28): the original version of this test only
        used 3 unique hrefs, so it never actually exercised the 15-link cap."""
        text = ""
        elements = [
            {"text": f"Result {i}", "href": f"https://example{i}.com/page"}
            for i in range(20)  # 20 unique hrefs, cap is 15
        ]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert result.count("- Result") == 15
        assert "https://example19.com/page" not in result  # 16th+ unique href dropped

    def test_skips_elements_without_href(self):
        text = "Skip to main content"
        elements = [{"text": "not a link", "href": ""}]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert result == text

    def test_skips_non_http_schemes(self):
        """javascript:/mailto:/tel: anchors aren't real result links (Codex review, 2026-07-28)."""
        text = "Skip to main content"
        elements = [
            {"text": "Email us", "href": "mailto:test@example.com"},
            {"text": "Call us", "href": "tel:+1234567890"},
            {"text": "Run JS", "href": "javascript:void(0)"},
            {"text": "Real result", "href": "https://example.com/page"},
        ]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert "mailto:" not in result
        assert "tel:" not in result
        assert "javascript:" not in result
        assert "https://example.com/page" in result

    def test_host_match_not_substring_match(self):
        """Codex review (2026-07-28): a naive `domain in href` substring check
        would wrongly exclude a real result whose URL merely contains
        "google.com" in a query string, or a lookalike domain that isn't
        actually google.com."""
        text = ""
        elements = [
            {"text": "Real result with google.com in query", "href": "https://example.com/?ref=google.com"},
            {"text": "Lookalike domain, not really Google", "href": "https://google.com.evil.example/phish"},
        ]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert "https://example.com/?ref=google.com" in result
        assert "https://google.com.evil.example/phish" in result

    def test_host_match_is_case_insensitive_and_covers_subdomains(self):
        text = "Skip to main content"
        elements = [
            {"text": "Go to Google Home", "href": "https://WWW.GOOGLE.COM/webhp"},
            {"text": "Sign in", "href": "https://accounts.google.com/signin"},
            {"text": "Real result", "href": "https://example.com/page"},
        ]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert "GOOGLE.COM" not in result
        assert "accounts.google.com" not in result
        assert "https://example.com/page" in result

    def test_regional_tld_variant_of_com_domain_is_excluded(self):
        """Live re-test finding (2026-07-28): searching Google (navigated to
        www.google.com) still rendered a "Google apps" chrome link pointing to
        google.com.au — Google's own navigation isn't pinned to whichever TLD
        you're currently on. A ".com" exclude entry must also catch regional
        variants (google.com.au, google.co.uk) of the same name."""
        text = "Skip to main content"
        elements = [
            {"text": "Google apps", "href": "https://www.google.com.au/intl/en/about/products"},
            {"text": "Google UK", "href": "https://www.google.co.uk/webhp"},
            {"text": "Real result", "href": "https://example.com/page"},
        ]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert "google.com.au" not in result
        assert "google.co.uk" not in result
        assert "https://example.com/page" in result

    def test_regional_tld_pattern_still_rejects_lookalike_domain(self):
        """The regional-TLD regex added above must not reopen the lookalike-domain
        gap the host-based match was built to close in the first place."""
        text = "Skip to main content"
        elements = [
            {"text": "Phishing lookalike", "href": "https://google.com.evil.example/phish"},
        ]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert "google.com.evil.example" in result

    def test_boundary_at_threshold_length(self):
        """Text at exactly the threshold is left alone; one char under triggers the fallback."""
        elements = [{"text": "Real result", "href": "https://example.com/page"}]
        at_threshold = "a" * 400
        assert _thin_text_link_fallback(at_threshold, elements, exclude_domains={"google.com"}) == at_threshold

        under_threshold = "a" * 399
        result = _thin_text_link_fallback(under_threshold, elements, exclude_domains={"google.com"})
        assert "https://example.com/page" in result

    def test_empty_text_with_no_elements(self):
        result = _thin_text_link_fallback("", [], exclude_domains={"google.com"})
        assert result == ""

    def test_result_never_exceeds_max_length(self):
        """Codex review (2026-07-28): the fallback must not push a (never-truncated,
        since it's thin) text past the same budget _capture_look() enforces on the
        original text — otherwise text_truncated would go stale."""
        text = ""
        elements = [
            {"text": f"Result {i}", "href": f"https://example{i}.com/" + "x" * 300}
            for i in range(15)
        ]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"}, max_length=500)
        assert len(result) <= 500

    def test_href_display_length_is_capped(self):
        """A single absurdly long query-string URL shouldn't eat the whole budget."""
        text = ""
        long_href = "https://example.com/page?" + "x" * 1000
        elements = [{"text": "Result", "href": long_href}]
        result = _thin_text_link_fallback(text, elements, exclude_domains={"google.com"})
        assert len(result) < len(long_href)
