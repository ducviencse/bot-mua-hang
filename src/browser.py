import logging
import random
from pathlib import Path

logger = logging.getLogger(__name__)

# patchright is a drop-in replacement for playwright that patches Chromium's
# binary-level CDP fingerprint — the main thing Shopee's anti-bot detects.
from patchright.async_api import async_playwright, BrowserContext, Page

from .agent import Action

# JavaScript that finds interactive elements on the page and returns their
# text + bounding-box centres.  Used by extract_elements().
_EXTRACT_JS = """() => {
    const els = [];
    const seen = new Set();
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // Detect if a CSS color string is warm (orange/red) — Shopee's
    // selection indicator uses an orange border.
    function isWarm(color) {
        if (!color) return false;
        var m = color.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
        if (!m) return false;
        var r = parseInt(m[1]), g = parseInt(m[2]), b = parseInt(m[3]);
        return r > 200 && g < 150 && b < 100;
    }

    function add(el) {
        var r = el.getBoundingClientRect();
        if (r.width < 10 || r.height < 10) return;
        if (r.right < 0 || r.bottom < 0 || r.left > vw || r.top > vh) return;
        if (r.width > vw * 0.7) return;

        var s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return;

        var text;
        var tag = el.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA') {
            text = el.value || el.placeholder
                   || el.getAttribute('aria-label') || el.type || '';
        } else {
            text = (el.textContent || '').trim().replace(/\\s+/g, ' ');
        }
        if (!text || text.length > 80) return;

        var cx = Math.round(r.x + r.width / 2);
        var cy = Math.round(r.y + r.height / 2);
        var k = Math.round(cx / 5) * 5 + ',' + Math.round(cy / 5) * 5;
        if (seen.has(k)) return;
        seen.add(k);

        // Detect selected state via border/outline color or aria attribute
        var sel = isWarm(s.borderColor) || isWarm(s.outlineColor)
                  || el.getAttribute('aria-selected') === 'true'
                  || el.getAttribute('aria-pressed') === 'true';

        els.push({
            t: text.substring(0, 60),
            x: cx, y: cy,
            w: Math.round(r.width), h: Math.round(r.height),
            s: !!sel
        });
    }

    // Primary: semantic interactive elements
    document.querySelectorAll(
        'button, [role="button"], input, textarea'
    ).forEach(add);

    // Secondary: small clickable elements (styled variant buttons, links)
    // Only runs if primary scan found few elements.
    if (els.length < 30) {
        document.querySelectorAll('div, span, a, label').forEach(function(el) {
            var r = el.getBoundingClientRect();
            if (r.width < 20 || r.width > 250) return;
            if (r.height < 15 || r.height > 60) return;
            if (r.right < 0 || r.bottom < 0) return;
            if (r.left > vw || r.top > vh) return;
            var s = window.getComputedStyle(el);
            if (s.cursor === 'pointer') add(el);
        });
    }

    els.sort(function(a, b) { return a.y - b.y || a.x - b.x; });
    return els;
}"""


def _bezier_point(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
    """Evaluate a cubic Bézier curve at parameter *t*."""
    u = 1 - t
    return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3


class BrowserManager:
    def __init__(
        self,
        cdp_endpoint: str,
        width: int = 1920,
        height: int = 1080,
    ):
        self.cdp_endpoint = cdp_endpoint
        self.width = width
        self.height = height
        self._playwright = None
        self._browser = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        # Track current mouse position for Bézier movements
        self._mouse_x: float = 0.0
        self._mouse_y: float = 0.0

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(
            self.cdp_endpoint
        )
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else await self._browser.new_context()
        self.page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        # Force viewport to match configured size so screenshots
        # have consistent dimensions for the LLM.
        await self.page.set_viewport_size({"width": self.width, "height": self.height})
        logger.info("CDP connected — viewport set to %dx%d", self.width, self.height)

    # ------------------------------------------------------------------
    # Human-like primitives
    # ------------------------------------------------------------------

    async def _human_move(self, x: float, y: float) -> None:
        """Move mouse from current position to (x, y) along a cubic Bézier curve."""
        steps = random.randint(20, 40)
        sx, sy = self._mouse_x, self._mouse_y

        # Two random control points for the curve
        cx1 = sx + (x - sx) * random.uniform(0.1, 0.4) + random.uniform(-60, 60)
        cy1 = sy + (y - sy) * random.uniform(0.1, 0.4) + random.uniform(-60, 60)
        cx2 = sx + (x - sx) * random.uniform(0.6, 0.9) + random.uniform(-60, 60)
        cy2 = sy + (y - sy) * random.uniform(0.6, 0.9) + random.uniform(-60, 60)

        for i in range(1, steps + 1):
            t = i / steps
            mx = _bezier_point(t, sx, cx1, cx2, x) + random.uniform(-1, 1)
            my = _bezier_point(t, sy, cy1, cy2, y) + random.uniform(-1, 1)
            await self.page.mouse.move(mx, my)
            await self.page.wait_for_timeout(random.randint(5, 15))

        self._mouse_x, self._mouse_y = x, y

    async def _human_click(self, x: float, y: float) -> None:
        """Move mouse naturally to (x, y) then click."""
        await self._human_move(x, y)
        await self.page.wait_for_timeout(random.randint(30, 80))
        # Use down/up at current position instead of click(x,y) which
        # teleports — this fires proper mousedown/mouseup/click events
        # at the position we already moved to.
        await self.page.mouse.down()
        await self.page.wait_for_timeout(random.randint(50, 120))
        await self.page.mouse.up()

    async def _human_type(self, text: str) -> None:
        """Type *text* character by character with natural timing."""
        for i, ch in enumerate(text):
            await self.page.keyboard.type(ch)
            delay = random.randint(50, 150)
            # Occasionally pause longer to simulate thinking
            if random.random() < 0.08:
                delay = random.randint(200, 400)
            await self.page.wait_for_timeout(delay)

    async def _human_scroll(self, delta_y: int) -> None:
        """Scroll by *delta_y* pixels in small, natural increments."""
        remaining = delta_y
        direction = 1 if delta_y > 0 else -1
        while abs(remaining) > 0:
            step = min(abs(remaining), random.randint(40, 120)) * direction
            await self.page.mouse.wheel(0, step)
            remaining -= step
            await self.page.wait_for_timeout(random.randint(15, 50))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def is_logged_in(self) -> bool:
        """Check Shopee login status via the SPC_U cookie."""
        cookies = await self._context.cookies("https://shopee.vn")
        return any(c["name"] == "SPC_U" for c in cookies)

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")
        # Wait a bit for rendering but don't require full networkidle —
        # Shopee pages often never stop network activity.
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            logger.info("networkidle timeout — proceeding anyway")

    async def get_viewport_size(self) -> dict:
        """Return the actual viewport size as {width, height}."""
        return self.page.viewport_size or {"width": self.width, "height": self.height}

    async def extract_elements(self) -> list[dict]:
        """Extract interactive elements from the page via JavaScript.

        Returns a list of dicts with keys: t (text), x, y, w, h.
        """
        try:
            return await self.page.evaluate(_EXTRACT_JS)
        except Exception as exc:
            logger.warning("Failed to extract elements: %s", exc)
            return []

    async def screenshot(self) -> bytes:
        return await self.page.screenshot(full_page=False)

    async def execute(self, action: Action, wait_ms: int = 2500) -> None:
        x, y = action.target_x, action.target_y

        if action.action == "click":
            await self._human_click(x, y)

        elif action.action == "type":
            await self._human_click(x, y)
            await self._human_type(action.text_to_type)
            await self.page.keyboard.press("Enter")

        elif action.action == "scroll":
            delta = 540 if action.scroll_direction != "up" else -540
            await self._human_scroll(delta)

        elif action.action == "press_escape":
            await self.page.keyboard.press("Escape")

        # Add random jitter to the base wait
        jitter = random.randint(-500, 500)
        await self.page.wait_for_timeout(max(500, wait_ms + jitter))

    async def _reset(self) -> None:
        """Stop playwright after a failed connect attempt so we can retry cleanly."""
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None

    async def close(self) -> None:
        # Disconnect from CDP without closing the user's Chrome window
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
