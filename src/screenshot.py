"""AI-friendly screenshot — annotates interactive elements with coordinate labels.

Takes a raw screenshot + page DOM info, produces an annotated image where each
clickable element has a numbered label with its coordinates. This makes it much
easier for AI to know exactly where to click.
"""
import base64
import io
from PIL import Image, ImageDraw, ImageFont
from typing import Optional


def recommended_click_point(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    """Return a click coordinate inside an element that avoids adjacent icons.

    Live testing showed that clicking the exact centre of a bounding box can
    hit a neighbouring 3-dot menu / icon instead of the intended link.  We
    target a point inset 25% from the left edge (capped at 10 px) and vertically
    centred, while keeping at least 2 px inside the element on all sides.
    """
    if width <= 0 or height <= 0:
        return x, y
    offset_x = min(int(width * 0.25), 10)
    offset_y = height // 2
    click_x = max(x + 2, min(x + offset_x, x + width - 2))
    click_y = max(y + 2, min(y + offset_y, y + height - 2))
    return click_x, click_y


def annotate_screenshot(screenshot_bytes: bytes, elements: list) -> bytes:
    """Draw coordinate labels on a screenshot.

    Args:
        screenshot_bytes: Raw PNG bytes of the page.
        elements: List of dicts with keys: x, y, width, height, tag, text, role,
                  and optionally click_x, click_y.
                  e.g. {"x": 100, "y": 200, "width": 80, "height": 30,
                        "tag": "button", "text": "Search", "role": "button"}

    Returns:
        Annotated PNG bytes.
    """
    img = Image.open(io.BytesIO(screenshot_bytes))
    draw = ImageDraw.Draw(img, "RGBA")

    # Try to load a font, fall back to default
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except Exception:
        font = ImageFont.load_default()

    for i, el in enumerate(elements):
        x = el.get("x", 0)
        y = el.get("y", 0)
        w = el.get("width", 0)
        h = el.get("height", 0)

        # Skip tiny or invisible elements
        if w < 5 or h < 5:
            continue

        # Draw a semi-transparent box around the element
        draw.rectangle(
            [x, y, x + w, y + h],
            outline=(255, 0, 0, 180),
            width=2,
        )

        # Draw a small dot at the recommended click point so the AI can see
        # where the safe target is without computing it from the box.
        click_x, click_y = recommended_click_point(x, y, w, h)
        draw.ellipse(
            [click_x - 2, click_y - 2, click_x + 2, click_y + 2],
            fill=(0, 255, 0, 220),
        )

        # Draw a numbered label
        label = str(i + 1)
        text_x = x + 2
        text_y = y - 18 if y > 20 else y + 2

        # Background for the label
        bbox = draw.textbbox((text_x, text_y), label, font=font)
        draw.rectangle(
            [bbox[0] - 2, bbox[1] - 2, bbox[2] + 4, bbox[3] + 2],
            fill=(255, 0, 0, 220),
        )
        draw.text((text_x, text_y), label, fill=(255, 255, 255, 255), font=font)

    # Save to bytes
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def extract_elements_js() -> str:
    """Return JS that extracts all interactive elements with their bounding rects.

    This JS is injected into the page via CDP evaluate(). It returns a JSON array
    of elements with coordinates, sizes, tag names, text, and roles.
    """
    return """
    (function() {
        const selector = 'a, button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [onclick], [contenteditable]';
        const elements = document.querySelectorAll(selector);
        const results = [];
        const seen = new Set();
        
        for (const el of elements) {
            const rect = el.getBoundingClientRect();
            if (rect.width < 5 || rect.height < 5) continue;
            if (rect.bottom < 0 || rect.right < 0) continue;
            if (rect.top > window.innerHeight || rect.left > window.innerWidth) continue;
            
            const key = `${rect.x},${rect.y},${rect.width},${rect.height}`;
            if (seen.has(key)) continue;
            seen.add(key);
            
            // Skip if hidden
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (parseFloat(style.opacity) < 0.1) continue;
            
            const clickX = Math.round(rect.left + Math.min(rect.width * 0.25, 10));
            const clickY = Math.round(rect.top + rect.height / 2);
            results.push({
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                click_x: Math.min(Math.max(clickX, Math.round(rect.left) + 2), Math.round(rect.right) - 2),
                click_y: Math.min(Math.max(clickY, Math.round(rect.top) + 2), Math.round(rect.bottom) - 2),
                tag: el.tagName.toLowerCase(),
                text: (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').substring(0, 50).trim(),
                role: el.getAttribute('role') || el.tagName.toLowerCase(),
                href: el.href || '',
                type: el.type || '',
            });
        }
        return results;
    })()
    """


def format_elements_text(elements: list) -> str:
    """Format the elements list as readable text for AI consumption.

    Example output:
        1. button "Search" at (400, 150) 80x30
        2. link "Images" at (500, 50) 60x20
        ...
    """
    lines = []
    for i, el in enumerate(elements):
        label = el.get("text", "")
        tag = el.get("tag", "")
        x = el.get("x", 0)
        y = el.get("y", 0)
        w = el.get("width", 0)
        h = el.get("height", 0)
        href = el.get("href", "")

        click_x = el.get("click_x", x)
        click_y = el.get("click_y", y)
        parts = [f'{i + 1}. {tag}']
        if label:
            parts.append(f'"{label}"')
        parts.append(f'at ({x}, {y}) {w}x{h}')
        parts.append(f'click ({click_x}, {click_y})')
        if href:
            parts.append(f'→ {href[:60]}')
        
        lines.append(" ".join(parts))
    
    return "\n".join(lines)
