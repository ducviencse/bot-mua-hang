import asyncio
import base64
import logging
from datetime import datetime
from pathlib import Path

from .agent import Action, VisionAgent
from .browser import BrowserManager
from .excel_handler import update_order_status
from .state import AppState, OrderState

logger = logging.getLogger(__name__)

_DEBUG_DIR = Path("debug_logs")

_CAPTCHA_PATTERNS = ("captcha", "verify/captcha", "verify/bot", "challenge")


def _is_captcha_url(url: str) -> bool:
    """Return True if the current page URL indicates a CAPTCHA/anti-bot screen."""
    url_lower = url.lower()
    return any(p in url_lower for p in _CAPTCHA_PATTERNS)


def _build_objective(order: dict, seller_note: str, invoice: dict) -> str:
    variant = order.get("variant, size,color", "")
    quantity = order.get("quantity", 1)

    parts = [
        f"On Shopee Vietnam (shopee.vn), ADD TO CART {quantity} unit(s) "
        f"of the product at URL: {order.get('product_url')}.",
    ]

    if variant:
        parts.append(f"Select variant/size/color: {variant}.")
    else:
        parts.append("Select the first available option for each variant group.")

    parts.append("After selecting variants and quantity, click 'Thêm Vào Giỏ Hàng' (Add To Cart).")
    parts.append("Goal is ADD TO CART only — do NOT proceed to checkout.")

    return " ".join(parts)


class _OrderLogger:
    """Per-order logger that writes a .log file alongside screenshots."""

    def __init__(self, run_dir: Path, row_index: int):
        self._run_dir = run_dir
        self._row_index = row_index
        self._log_path = run_dir / "trace.log"

    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def write(self, msg: str) -> None:
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(f"[{self._ts()}] {msg}\n")

    def save_screenshot(self, step: int, data: bytes) -> Path:
        path = self._run_dir / f"step{step}.png"
        path.write_bytes(data)
        return path

    def step(
        self,
        step: int,
        url: str,
        action: Action,
        llm_raw: str | None = None,
        elements: list[dict] | None = None,
    ) -> None:
        self.write(f"--- Step {step} ---")
        self.write(f"URL: {url}")
        self.write(f"Screenshot: step{step}.png")
        if elements:
            self.write(f"Detected {len(elements)} elements")
        if llm_raw:
            self.write(f"LLM raw: {llm_raw}")
        if action.observation:
            self.write(f"Observation: {action.observation}")
        self.write(
            f"Action: {action.action} element={action.element_index} "
            f"coords=({action.target_x},{action.target_y}) "
            f"text={action.text_to_type!r} scroll={action.scroll_direction}"
        )
        self.write(f"Reasoning: {action.reasoning}")

    def info(self, msg: str) -> None:
        self.write(msg)

    def error(self, msg: str) -> None:
        self.write(f"ERROR: {msg}")

    def result(self, status: str, detail: str = "") -> None:
        self.write(f"=== RESULT: {status} {detail} ===")


class ShopeeBot:
    def __init__(
        self,
        agent: VisionAgent,
        browser: BrowserManager,
        app_state: AppState,
        excel_path: str,
        seller_note: str = "",
        invoice: dict | None = None,
        max_steps: int = 20,
        wait_ms: int = 2500,
    ):
        self.agent = agent
        self.browser = browser
        self.app_state = app_state
        self.excel_path = excel_path
        self.seller_note = seller_note
        self.invoice = invoice or {}
        self.max_steps = max_steps
        self.wait_ms = wait_ms

    def _make_order_logger(self, row_index: int) -> _OrderLogger:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = _DEBUG_DIR / f"order{row_index}_{timestamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return _OrderLogger(run_dir, row_index)

    async def process_order(self, order: dict, order_state: OrderState) -> None:
        row_index = order["_row_index"]
        objective = _build_objective(order, self.seller_note, self.invoice)
        ol = self._make_order_logger(row_index)

        logger.info("[Order %d] Starting: %s", row_index, order.get("product_name"))
        ol.info(f"Starting order {row_index}: {order.get('product_name')}")
        ol.info(f"Objective: {objective}")

        self.agent.reset_history()
        self._recent_actions: list[str] = []
        order_state.status = "running"
        await self.app_state.broadcast()

        try:
            await self.browser.goto(order["product_url"])
        except Exception as exc:
            ol.error(f"Navigation error: {exc}")
            await self._fail(order_state, row_index, f"Navigation error: {exc}")
            ol.result("FAILED", f"Navigation error: {exc}")
            return

        # Check for CAPTCHA immediately after navigation
        if _is_captcha_url(self.browser.page.url):
            ol.error("CAPTCHA detected")
            await self._fail(order_state, row_index, "CAPTCHA detected — skipping this order")
            ol.result("FAILED", "CAPTCHA detected")
            return

        for step in range(1, self.max_steps + 1):
            url = self.browser.page.url
            logger.info("[Order %d] Step %d — URL: %s", row_index, step, url)
            order_state.current_step = step

            screenshot = await self.browser.screenshot()
            order_state.last_screenshot_b64 = base64.b64encode(screenshot).decode()
            await self.app_state.broadcast()

            # Save screenshot
            ss_path = ol.save_screenshot(step, screenshot)
            logger.info("[Order %d] Screenshot saved: %s", row_index, ss_path)

            # Extract interactive elements via JavaScript
            elements = await self.browser.extract_elements()
            logger.info("[Order %d] Detected %d elements", row_index, len(elements))

            try:
                action: Action = await self.agent.decide(
                    screenshot, objective, elements,
                )
            except Exception as exc:
                logger.error("[Order %d] LLM error: %s", row_index, exc)
                ol.error(f"LLM error: {exc}")
                await self._fail(order_state, row_index, f"LLM error: {exc}")
                ol.result("FAILED", f"LLM error: {exc}")
                return

            # Save annotated screenshot for debugging
            annotated_path = ol._run_dir / f"step{step}_annotated.png"
            annotated_path.write_bytes(self.agent.last_annotated_screenshot)

            # Get the raw LLM response from the last log entry
            llm_raw = self.agent.last_raw_response

            order_state.last_reasoning = action.reasoning
            logger.info(
                "[Order %d] Action=%s element=%d coords=(%d,%d) text=%r scroll=%s | %s",
                row_index, action.action, action.element_index,
                action.target_x, action.target_y,
                action.text_to_type, action.scroll_direction, action.reasoning,
            )
            ol.step(step, url, action, llm_raw=llm_raw, elements=elements)
            await self.app_state.broadcast()

            # --- Stuck detection ---
            # Fingerprint: action type + element index (or scroll direction)
            fp = f"{action.action}:{action.element_index or action.scroll_direction}"
            self._recent_actions.append(fp)
            # Check last 3 actions
            if len(self._recent_actions) >= 3 and len(set(self._recent_actions[-3:])) == 1:
                logger.warning("[Order %d] Stuck detected — pressing Escape and scrolling to top", row_index)
                ol.info("STUCK detected — pressing Escape + scroll to top to recover")
                await self.browser.page.keyboard.press("Escape")
                await self.browser.page.wait_for_timeout(500)
                await self.browser.page.keyboard.press("Escape")
                await self.browser.page.wait_for_timeout(500)
                await self.browser.page.evaluate("window.scrollTo(0, 0)")
                await self.browser.page.wait_for_timeout(1000)
                self._recent_actions.clear()
                continue  # re-screenshot and let LLM try again

            if action.action == "done":
                order_id = await self._extract_order_id()
                order_state.status = "success"
                order_state.order_id = order_id
                update_order_status(self.excel_path, row_index, "success", order_id)
                logger.info("[Order %d] SUCCESS — order_id=%s", row_index, order_id)
                ol.result("SUCCESS", f"order_id={order_id}")
                await self.app_state.broadcast()
                return

            if action.action == "fail":
                reason = action.reasoning or "Agent declared failure"
                await self._fail(order_state, row_index, reason)
                ol.result("FAILED", reason)
                return

            if action.action == "wait_otp":
                ol.info("Waiting for OTP from dashboard…")
                otp = await self._wait_for_otp(order_state)
                if not otp:
                    await self._fail(order_state, row_index, "OTP not received")
                    ol.result("FAILED", "OTP not received")
                    return
                ol.info(f"OTP received: {otp}")
                await self.browser.page.keyboard.type(otp)
                await self.browser.page.keyboard.press("Enter")
                await self.browser.page.wait_for_timeout(self.wait_ms)
                order_state.status = "running"
                await self.app_state.broadcast()
                continue

            try:
                await self.browser.execute(action, wait_ms=self.wait_ms)
            except Exception as exc:
                logger.error("[Order %d] Execute error: %s", row_index, exc)
                ol.error(f"Execute error: {exc}")
                await self._fail(order_state, row_index, f"Execute error: {exc}")
                ol.result("FAILED", f"Execute error: {exc}")
                return

            # Check for CAPTCHA after each action
            if _is_captcha_url(self.browser.page.url):
                ol.error("CAPTCHA detected after action")
                await self._fail(order_state, row_index, "CAPTCHA detected — skipping this order")
                ol.result("FAILED", "CAPTCHA detected")
                return

        await self._fail(order_state, row_index, f"Exceeded max_steps ({self.max_steps})")
        ol.result("FAILED", f"Exceeded max_steps ({self.max_steps})")

    async def _wait_for_otp(self, order_state: OrderState, timeout: int = 300) -> str:
        """Pause and wait for user to submit OTP from the dashboard."""
        logger.info("Waiting for OTP input from dashboard…")
        order_state.status = "waiting_otp"
        self.app_state.otp_requested = True
        self.app_state.otp_event.clear()
        self.app_state.otp_value = ""
        await self.app_state.broadcast()

        loop = asyncio.get_event_loop()
        received = await loop.run_in_executor(
            None, lambda: self.app_state.otp_event.wait(timeout)
        )

        otp = self.app_state.otp_value
        self.app_state.otp_requested = False
        self.app_state.otp_value = ""
        await self.app_state.broadcast()

        return otp if received else ""

    async def _fail(self, order_state: OrderState, row_index: int, note: str) -> None:
        order_state.status = "failed"
        order_state.note = note
        update_order_status(self.excel_path, row_index, "failed", note=note)
        logger.warning("[Order %d] FAILED — %s", row_index, note)
        await self.app_state.broadcast()

    async def _extract_order_id(self) -> str:
        try:
            url = self.browser.page.url
            if "order" in url:
                parts = [p for p in url.split("/") if p.isdigit()]
                if parts:
                    return parts[-1]
        except Exception:
            pass
        return ""
