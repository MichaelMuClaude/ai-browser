"""Anti-bot / blocking page detection.

Provides a standalone, testable module for deciding whether the current page
is showing anti-bot content (Cloudflare, DataDome, PerimeterX, CAPTCHA, etc.).
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class BlockSignal:
    """A single signal that contributed to the block detection result."""
    name: str
    value: str
    confidence: float


@dataclass
class BlockResult:
    """Result of a block detection check."""
    blocked: bool
    confidence: float
    signals: List[BlockSignal] = field(default_factory=list)


# Known anti-bot / challenge indicators.
# Each entry maps a signal name to one or more textual patterns and a confidence
# contribution. Confidences are summed and clamped to [0.0, 1.0].
TITLE_INDICATORS = {
    "cf_title_just_a_moment": (["just a moment..."], 0.75),
    "cf_title_checking_browser": (["checking your browser"], 0.75),
    "generic_access_denied": (["access denied"], 0.70),
    "generic_security_check": (["security check"], 0.70),
    "generic_blocked": (["blocked"], 0.60),
    "ddos_protection": (["ddos protection"], 0.75),
    "native_blank_page": (["untitled - google chrome"], 0.40),
    "native_new_tab": (["new tab - google chrome"], 0.40),
}

BODY_INDICATORS = {
    "cf_browser_verification": (["cf-browser-verification"], 0.75),
    "cf_challenge_running": (["cf-challenge-running"], 0.75),
    "challenge_platform": (["challenge-platform"], 0.70),
    "cf_checking_browser": (["checking your browser"], 0.65),
    "are_you_a_robot": (["are you a robot"], 0.80),
    "verify_you_are_human": (["verify you are human", "verify_you_are_human"], 0.80),
    "datadome": (["datadome"], 0.75),
    "perimeterx": (["px-captcha"], 0.75),
    "captcha": (["captcha"], 0.45),  # weak signal, must combine with others to reach threshold
    "google_unusual_traffic": ([
        "our systems have detected unusual traffic",
        "unusual traffic from your computer network",
    ], 0.55),
}

URL_INDICATORS = {
    "url_challenge": (["/challenge", "challenge?"], 0.35),
    "url_captcha": (["/captcha", "captcha?"], 0.35),
    "url_verify": (["/verify", "verify?"], 0.30),
    "url_checkpoint": (["/checkpoint"], 0.30),
    "url_blocked": (["/blocked"], 0.40),
    "url_google_sorry": (["/sorry/index", "/sorry/?"], 0.50),  # Google rate-limit / CAPTCHA page
}

# Anti-bot script fingerprints in raw HTML.
# Each tuple: (pattern, confidence)
SCRIPT_INDICATORS = [
    ("datadome_script", "var dd={'rt'", 0.60),
    ("datadome_cfasync", 'data-cfasync="false"', 0.40),
    ("perimeterx_script", "var _pxAppId", 0.60),
    ("hcaptcha_script", "hcaptcha.com/1/api.js", 0.50),
]

# Body length thresholds for detecting challenge / block pages.
# Confidences are kept below BLOCK_THRESHOLD so these signals need companions
# (e.g. title_is_domain, datadome_script) to push the total over the threshold.
BODY_EMPTY_THRESHOLD = 0      # exactly empty
BODY_SHORT_THRESHOLD = 200    # suspiciously short

# Title patterns that look like a bare domain (e.g. "g2.com", "yelp.com").
def _title_looks_like_domain(title: str) -> bool:
    """Check if title is just a domain name: short, contains a dot, no spaces."""
    if not title:
        return False
    t = title.strip()
    return len(t) < 20 and "." in t and " " not in t

# Confidence threshold above which we consider the page blocked.
BLOCK_THRESHOLD = 0.50


def _check_indicators(text: str, indicators: dict) -> List[BlockSignal]:
    """Scan text for indicator patterns and return matching signals."""
    signals = []
    text_lower = text.lower()
    for signal_name, (patterns, confidence) in indicators.items():
        for pattern in patterns:
            if pattern.lower() in text_lower:
                signals.append(BlockSignal(
                    name=signal_name,
                    value=pattern,
                    confidence=confidence,
                ))
                # Only count the strongest pattern per signal category.
                break
    return signals


def detect_block(title: str = "", body_text: str = "", url: str = "", html: str = "") -> BlockResult:
    """Detect whether a page appears to be blocked by anti-bot protection.

    Args:
        title: Page title text.
        body_text: Visible page body text (or a snippet of it).
        url: Final URL of the page.
        html: Raw HTML snippet (first ~5000 chars) for script fingerprint detection.

    Returns:
        BlockResult with blocked flag, aggregated confidence, and hit signals.
    """
    signals: List[BlockSignal] = []

    # --- Existing keyword-based signals ---
    signals.extend(_check_indicators(title, TITLE_INDICATORS))
    signals.extend(_check_indicators(body_text, BODY_INDICATORS))
    signals.extend(_check_indicators(url, URL_INDICATORS))

    # --- New: body length signals ---
    stripped_body = body_text.strip()
    if len(stripped_body) == BODY_EMPTY_THRESHOLD:
        signals.append(BlockSignal(name="body_empty", value="0", confidence=0.45))
    elif len(stripped_body) < BODY_SHORT_THRESHOLD:
        signals.append(BlockSignal(
            name="body_too_short",
            value=str(len(stripped_body)),
            confidence=0.30,
        ))

    # --- New: title looks like bare domain ---
    if _title_looks_like_domain(title):
        signals.append(BlockSignal(
            name="title_is_domain",
            value=title,
            confidence=0.40,
        ))

    # --- New: anti-bot script fingerprints in HTML ---
    if html:
        for signal_name, pattern, conf in SCRIPT_INDICATORS:
            if pattern in html:
                signals.append(BlockSignal(
                    name=signal_name,
                    value=pattern,
                    confidence=conf,
                ))

    confidence = min(1.0, sum(s.confidence for s in signals))
    blocked = confidence >= BLOCK_THRESHOLD

    return BlockResult(
        blocked=blocked,
        confidence=round(confidence, 2),
        signals=signals,
    )


def is_blocked(title: str = "", body_text: str = "", url: str = "", html: str = "") -> bool:
    """Convenience wrapper returning only the boolean blocked status."""
    return detect_block(title=title, body_text=body_text, url=url, html=html).blocked
