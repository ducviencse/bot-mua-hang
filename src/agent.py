import base64
import io
import json
import logging
import os
import re
from dataclasses import dataclass

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_BASE_URL = "https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1"

SYSTEM_INSTRUCTION = """\
You are an autonomous gift-purchasing agent for Shopee Vietnam (shopee.vn).

The screenshot has NUMBERED RED BADGES marking interactive elements detected \
on the page. A text list of these elements is provided below the image.

RESPONSE FORMAT — reply with a SINGLE JSON object, nothing else:
{
  "observation": "Describe what you see: page type, any modals/popups, \
which variant buttons exist and which appear SELECTED (orange/red border), \
current state of the flow.",
  "action": "click" | "type" | "scroll" | "press_escape" | "done" | "fail" | "wait_otp",
  "element_index": 5,
  "text_to_type": "",
  "scroll_direction": null,
  "reasoning": "Why this action, referencing your observation."
}

ACTIONS:
- "click": Click the element specified by element_index.
- "type": Click the element, then type text_to_type into it.
- "scroll": Scroll the page. Set scroll_direction to "up" or "down". \
  No element_index needed.
- "press_escape": Press Escape key to close modals/popups/overlays. \
  No element_index needed.
- "done": Task completed successfully (item added to cart).
- "fail": Task cannot be completed (CAPTCHA, login, out of stock, etc.).
- "wait_otp": Waiting for user OTP input.

SELECTION STATE:
- Elements marked [SELECTED] in the list have been detected as selected \
  by their orange/red border color via JavaScript. TRUST this marking.
- If an element is marked [SELECTED], do NOT click it again.
- If ALL required variants are [SELECTED], proceed to Add To Cart.

RULES:
1. ALWAYS start with "observation" describing what you see BEFORE deciding.
2. Use element_index to specify which detected element to interact with. \
   Match the number from the element list / red badges on the screenshot.
3. Close any modal, popup, or overlay first (use press_escape or click a \
   close/X button if one is in the list).
4. Use "scroll" if the target element is not in the detected list — it \
   may be off-screen.
5. If previous actions did not change the page, try a DIFFERENT element \
   or action. Do NOT repeat the same element_index.

ADD TO CART FLOW — follow in order:

STEP 1 — Select variants:
- Look at the element list for variant options (colors, sizes, etc.).
- If the objective specifies a variant/size/color, find the element whose \
  text matches and click it.
- For each variant group: if no button has an orange/red border → click \
  the matching option (or the first available if not specified).
- If a group already shows a selected button (orange border) → skip it.

STEP 2 — Set quantity:
- If more than 1 is needed, click the "+" element to increase.

STEP 3 — Add to cart:
- ONLY after ALL variant groups are selected, click the element whose text \
  is "Thêm Vào Giỏ Hàng" or "Add To Cart".
- Do NOT click "Mua Ngay" / "Buy Now" / "Buy With Voucher".
- After clicking, if you see a success toast/popup ("Sản phẩm đã được \
  thêm vào Giỏ Hàng") or the cart count increases → return action="done".

FAIL immediately (action="fail") for:
- CAPTCHA / anti-bot screen
- Login page
- Product is out of stock or unavailable
- Access denied
Do NOT attempt to solve CAPTCHAs.
"""


def _draw_markers(screenshot_bytes: bytes, elements: list[dict]) -> bytes:
    """Draw numbered badges on the screenshot near each detected element."""
    img = Image.open(io.BytesIO(screenshot_bytes))
    draw = ImageDraw.Draw(img, "RGBA")

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 12)
    except Exception:
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12
            )
        except Exception:
            font = ImageFont.load_default()

    for i, el in enumerate(elements):
        cx, cy = el["x"], el["y"]
        w, h = el.get("w", 40), el.get("h", 20)
        selected = el.get("s", False)
        label = str(i + 1)

        # Thin outline around the element — orange if selected, green if not
        x0 = cx - w // 2
        y0 = cy - h // 2
        x1 = cx + w // 2
        y1 = cy + h // 2
        outline_color = (255, 140, 0, 200) if selected else (0, 200, 0, 140)
        draw.rectangle([x0, y0, x1, y1], outline=outline_color, width=2 if selected else 1)

        # Badge — orange for selected, red for normal
        badge_w = 6 + len(label) * 8
        badge_h = 14
        bx = max(0, x0)
        by = y0 - badge_h - 1
        if by < 0:
            by = y1 + 1

        badge_color = (255, 140, 0, 240) if selected else (220, 30, 30, 220)
        draw.rectangle(
            [bx, by, bx + badge_w, by + badge_h], fill=badge_color
        )
        draw.text((bx + 2, by), label, fill=(255, 255, 255, 255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@dataclass
class Action:
    action: str
    target_x: int = 0
    target_y: int = 0
    element_index: int = 0
    text_to_type: str = ""
    scroll_direction: str | None = None
    reasoning: str = ""
    observation: str = ""

    @classmethod
    def from_dict(
        cls, data: dict, elements: list[dict] | None = None
    ) -> "Action":
        idx = data.get("element_index")
        x, y = 0, 0
        if idx is not None and elements:
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                idx = 0
            if 0 < idx <= len(elements):
                el = elements[idx - 1]
                x, y = el["x"], el["y"]
            else:
                logger.warning(
                    "Invalid element_index: %s (max %d)", idx, len(elements)
                )
        return cls(
            action=data.get("action", "fail"),
            target_x=x,
            target_y=y,
            element_index=idx if idx else 0,
            text_to_type=data.get("text_to_type", ""),
            scroll_direction=data.get("scroll_direction"),
            reasoning=data.get("reasoning", ""),
            observation=data.get("observation", ""),
        )

    def summary(self) -> str:
        """One-line summary for action history."""
        if self.action == "click":
            return f"click(element={self.element_index}): {self.reasoning}"
        if self.action == "type":
            return (
                f"type(element={self.element_index}, "
                f"'{self.text_to_type}'): {self.reasoning}"
            )
        if self.action == "scroll":
            return f"scroll({self.scroll_direction}): {self.reasoning}"
        if self.action == "press_escape":
            return f"press_escape: {self.reasoning}"
        return f"{self.action}: {self.reasoning}"


class VisionAgent:
    def __init__(self, model: str = "google/gemma-4-31b-it"):
        self.model = model
        self.client = OpenAI(
            base_url=_BASE_URL,
            api_key=os.environ.get("AI_PLATFORM_API_KEY", ""),
        )
        self._history: list[str] = []
        self.last_raw_response: str = ""
        self.last_annotated_screenshot: bytes = b""

    def reset_history(self) -> None:
        """Clear action history (call at the start of each order)."""
        self._history = []

    async def decide(
        self,
        screenshot_bytes: bytes,
        objective: str,
        elements: list[dict],
    ) -> Action:
        # Draw numbered markers on screenshot
        annotated_png = _draw_markers(screenshot_bytes, elements)
        self.last_annotated_screenshot = annotated_png

        image_b64 = base64.b64encode(annotated_png).decode()
        data_uri = f"data:image/png;base64,{image_b64}"

        # Build element list text
        if elements:
            lines = ["DETECTED ELEMENTS (numbered badges on screenshot):"]
            for i, el in enumerate(elements):
                sel = " [SELECTED]" if el.get("s") else ""
                lines.append(f'  [{i + 1}] "{el["t"]}"{sel}')
            elements_text = "\n".join(lines)
        else:
            elements_text = (
                "DETECTED ELEMENTS: None found. "
                "Try scrolling or pressing Escape to reveal content."
            )

        # Build history context
        history_text = ""
        if self._history:
            history_text = (
                "\n\nPrevious actions (oldest first):\n"
                + "\n".join(
                    f"  Step {i + 1}: {h}"
                    for i, h in enumerate(self._history)
                )
                + "\n\nIMPORTANT: If previous actions did not change the page, "
                "try a different element or action.\n"
            )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_uri},
                        },
                        {
                            "type": "text",
                            "text": (
                                f"{elements_text}\n\n"
                                f"Objective: {objective}\n"
                                f"{history_text}"
                                "FIRST describe what you see (observation), "
                                "THEN decide the next action. "
                                "Output a single JSON."
                            ),
                        },
                    ],
                },
            ],
            temperature=0.1,
            max_tokens=700,
        )

        raw = response.choices[0].message.content or "{}"
        self.last_raw_response = raw
        logger.info("LLM raw response: %s", raw)

        # Extract the last JSON block
        blocks = re.findall(r"\{[^{}]*\}", raw)
        data = json.loads(blocks[-1] if blocks else raw)
        action = Action.from_dict(data, elements)

        # Append to history for next step
        self._history.append(action.summary())

        return action
