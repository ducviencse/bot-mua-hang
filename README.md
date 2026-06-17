# bot-mua-hang

AI-powered Shopee Vietnam shopping bot — reads orders from an Excel file and autonomously adds products to cart using a Vision LLM with element-level detection. Includes a real-time web dashboard; order data is persisted in SQLite with multi-session support.

> **Anti-bot requirement:** Run a **real browser (Chrome or Brave) on the same machine** as the Python bot, started with **remote debugging**, and attach via **CDP** (`cdp_endpoint` pointing at that local endpoint). Shopee’s defenses look for a normal consumer device + browser profile; **headless-only automation, remote “browser farms,” or Chrome that never ran locally on your device** are much easier to flag. Treat **local real browser + CDP** as mandatory for this workflow, not an optimization.

## Business context

The marketing team sends gifts to users. Previously, the purchasing team manually bought each gift on Shopee — taking up to 60 days. This bot automates the add-to-cart process by selecting the correct product variants (size, color) and adding items to the shopping cart, reducing manual effort significantly.

## How it works

1. **Import orders** — Upload an Excel file via the dashboard UI. Each row is one gift order with product URL, variant/size/color, and quantity.
2. **Login to Shopee** — With Chrome running and remote debugging enabled (CDP is **required**), sign in to Shopee in that window. You can also use **Login to Shopee** in the dashboard to open the login page in the attached Chrome tab. Session cookies stay in your real Chrome profile.
3. **Click Start** — The bot opens each product URL, extracts interactive elements via JavaScript, and the Vision LLM picks elements by number to select variants and add to cart.
4. **Status persisted to SQLite** — After each order, the status, screenshots, and step-by-step reasoning are saved to `data/bot.db`. All data persists across restarts and is browsable in the dashboard.

---

## Before You Start (Pre-requisites)

Complete these steps **once** on a fresh machine before launching the bot.

### 1. Install Python 3.11 or newer

| OS | Download |
|----|----------|
| Windows | https://www.python.org/downloads/ — check **"Add Python to PATH"** during install |
| macOS | https://www.python.org/downloads/ or `brew install python` |
| Linux | `sudo apt install python3.11 python3-pip` (Ubuntu/Debian) |

Verify:

```bash
python --version   # should print 3.11 or higher
```

### 2. Install Google Chrome or Brave

**Anti-bot:** The shopping session must run in a **real browser on this same computer** (not a remote VM-only browser unless that VM is where you also use Shopee as a human). The bot only attaches over CDP; it does not replace the need for that local, trusted Chrome/Brave instance.

The bot connects to your **real Chrome or Brave browser** via remote debugging — no headless Chromium substitute. That local real browser is what keeps Shopee’s trust signals closer to a normal shopper.

- Chrome: https://www.google.com/chrome/
- Brave: https://brave.com/

### 3. Install dependencies and API key

From the project directory:

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env → set AI_PLATFORM_API_KEY=your_key
```

**API key** — get your key from the VNG Cloud AI Platform at `maas-llm-aiplatform-hcm.api.vngcloud.vn`.

---

## Daily Use

1. Start Chrome with remote debugging (see **CDP mode (required)** below for OS-specific commands), log in to Shopee in that window, and set `cdp_endpoint` in `config.yaml` to match your debugging port (if unset, the app defaults to `http://localhost:9222`).
2. Run `python main.py` (optionally `python main.py --file orders.xlsx`).
3. Open **http://localhost:8081**
4. Upload your orders Excel file if you did not use `--file`, then click **Start**

---

## Configuration

Settings live in `config.yaml`. CLI flags override config values.

Fields commonly tuned in-repo (some support hot-reload within ~5 seconds — see your deployment):

| Field | Default | Description |
|-------|---------|-------------|
| `model` | `google/gemma-4-31b-it` | Vision model served by VNG Cloud |
| `cdp_endpoint` | (see `config.yaml`) | **Required in practice:** HTTP URL of Chrome’s remote debugging endpoint (e.g. `http://localhost:9222`). If omitted or empty, the app defaults to `http://localhost:9222`. Chrome must be running with that port open — the bot only connects via `connect_over_cdp`. |
| `viewport` | `1280×1080` | Browser viewport; screenshots sent to the LLM at this resolution |
| `max_steps` | `40` | Max LLM actions per order before failing |
| `wait_timeout_ms` | `3000` | Base wait after each browser action (ms). Random jitter of ±500ms is added. |
| `dashboard_port` | `8081` | Dashboard HTTP port |
| `batch_size` | `50` | Orders per batch |
| `max_retries` | `2` | Auto-retry for retryable failures |
| `seller_note` | `""` | Text written in the seller note field on every order |
| `invoice.*` | empty | VAT invoice: `company_name`, `tax_id`, `address`, `email` |

The API key is stored in `.env` as `AI_PLATFORM_API_KEY`.

---

## Excel file format

The first row must be a header row with the column names below. Column order does not matter — the bot reads by name.

| Column | Type | Description |
|--------|------|-------------|
| `product_url` | string | Full Shopee product URL (required) |
| `product_name` | string | Display name for the dashboard |
| `product_id` | string | Shopee product ID |
| `variant, size,color` | string | Variant specification, e.g. "Màu đen, size M". If empty, bot selects first available. |
| `receiver_name` | string | Recipient full name |
| `phone_number` | string | Recipient phone number |
| `address` | string | Full shipping address |
| `quantity` | integer | Number of units to add to cart |
| `status` | output | Written by bot: `pending` / `running` / `success` / `failed` |
| `order_id` | output | Extracted after successful add-to-cart (if available) |
| `note` | output | Error message if order failed |

Rows with `status = success` are skipped on re-run. Use **Re-buy** in the dashboard to force re-processing.

---

## CDP mode (required)

**Anti-bot:** CDP is how the bot reaches that **same-machine real browser**. Chrome must be running **locally** with remote debugging; the bot then connects to `localhost` (or your machine’s loopback), not to a generic cloud browser.

The bot **only** drives Chrome through the Chrome DevTools Protocol: you must start **real Chrome** (or another Chromium-based browser that exposes the same debugging protocol) with remote debugging enabled and point `cdp_endpoint` at it. That gives a natural fingerprint, browsing history, and Shopee trust score — and avoids CAPTCHA far more reliably than a freshly launched automation browser.

1. **Start Chrome with debugging**
   - **macOS:** `/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222`
   - **Linux:** `google-chrome --remote-debugging-port=9222`
   - **Windows:** `"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222`
2. **Log in to Shopee** — In that Chrome window, go to `shopee.vn` and log in normally with your account.
3. **Set CDP endpoint** — In `config.yaml`, set `cdp_endpoint: "http://localhost:9222"` (or your port).
4. **Run the bot** — e.g. `python main.py --file orders.xlsx` — the bot attaches to your existing Chrome. No new browser window opens.

**Important** — The bot uses the first tab of your existing Chrome. When the bot exits, it disconnects from CDP and does **not** close your Chrome.

---

## Shopee login & session persistence

CDP is **required**: `BrowserManager` always uses `connect_over_cdp` to attach to your already-running Chrome. Shopee cookies live in that Chrome user profile.

**Typical flow** — Start Chrome with remote debugging, open `shopee.vn` in that window, and sign in. Then start the bot; the dashboard **login** badge reflects whether Shopee sees you as logged in on the attached page.

**Dashboard “Login to Shopee”** — If you are logged out, click **Login to Shopee** in the UI; the bot navigates the **connected** Chrome tab to `shopee.vn/buyer/login` so you can complete login there. The badge updates after a few seconds once cookies are present.

**Switch accounts** — Sign out in Chrome (or use a different Chrome profile with its own debugging port), then sign in again in that browser.

---

## Running the bot

```bash
# Start with dashboard only (import Excel from UI)
python main.py

# Preload an Excel file on startup
python main.py --file orders.xlsx

# Override dashboard port
python main.py --file orders.xlsx --port 9090
```

| Flag | Description |
|------|-------------|
| `--file` / `-f` | Optional path to Excel file. If omitted, upload from dashboard. |
| `--port` | Override dashboard port |
| `--config` | Path to config file (default: `config.yaml`) |

---

## Add to Cart flow

After navigating to the product URL, the bot uses a hybrid JavaScript + Vision LLM pipeline to add the product to cart. A typical successful flow completes in 3–4 steps (~35 seconds):

1. **Select color/variant** — JavaScript extracts all interactive elements with pixel coordinates. The LLM identifies the correct color button by its text label (e.g. "Đen") and clicks it. JavaScript detects the orange border indicating selection.
2. **Select size** — The LLM sees the color is now marked `[SELECTED]` and picks the correct size button (e.g. "M"). JavaScript confirms selection via border color detection.
3. **Add to Cart** — With all variants marked `[SELECTED]`, the LLM clicks **Thêm Vào Giỏ Hàng** (Add To Cart). The bot does **not** click **Mua Ngay** (Buy Now).
4. **Confirm success** — The LLM sees the success toast/modal ("Sản phẩm đã được thêm vào Giỏ Hàng") and returns `action="done"`.

**Add to Cart only** — The bot stops after adding to cart. It does **not** proceed to checkout, payment, or order placement. This is by design for the current workflow.

### Automatic failure cases

The LLM immediately returns `action="fail"` for:

- CAPTCHA / anti-bot screen detected
- Login page (session expired)
- Product out of stock or unavailable
- Access denied

### Stuck detection

If the bot repeats the same action 3 times in a row (same action type + same element), it triggers a recovery: press Escape twice, scroll to top, then re-analyze the page.

---

## OTP handling

When Shopee requires a one-time password (e.g. during login or payment), the LLM returns `action="wait_otp"`. The bot then:

1. **Pauses the loop** — Order status changes to `waiting_otp`.
2. **OTP modal appears** — A full-screen modal in the dashboard with an OTP input field (auto-focused).
3. **User enters OTP** — Type the code from SMS/email; press Enter or Submit. 5-minute timeout.
4. **Bot continues** — The OTP is typed into the browser and processing resumes automatically.

---

## Re-buy

The **Re-buy** button on each order row re-processes orders that already have `success` or `failed` status. It:

- Resets the order status to `pending` in the Excel file
- Reloads the order from Excel (so edits are picked up)
- Queues it for immediate re-processing

The bot stays alive after finishing all orders, waiting for re-buy requests — no restart needed.

---

## Dashboard UI

Open **http://localhost:8081** after starting the bot.

| Element | Description |
|---------|-------------|
| Login badge | **Logged in** / **Not logged in** based on Shopee session cookie. Updated every ~3 seconds. |
| Login to Shopee | Opens Shopee login in the **Chrome window attached via CDP**. Hidden once logged in. |
| Start | Begins processing pending orders. Disabled until orders are loaded. |
| Upload zone | Drag-and-drop or click to upload `.xlsx`. Hidden once orders are loaded. |
| Re-import | Replace the current Excel file; resets started state. |
| Orders table | Live orders, status badges, IDs, actions. Collapsible via **Hide**. |
| Hide / Orders toggle | Collapses Orders panel; Live Preview and Console Logs expand. |
| Edit | Modal to edit order fields; saves to Excel immediately. |
| Re-buy | Re-queues completed/failed orders. |
| Live Preview | Current screenshot + LLM reasoning + step counter. |
| Console Logs | Real-time bot activity (warnings/errors color-coded). |
| OTP modal | Appears when OTP is required; enter code and press Enter. |

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serves the dashboard HTML |
| WS | `/ws` | WebSocket — pushes AppState JSON on state change (orders, screenshots, logs) |
| POST | `/upload` | Upload `.xlsx` (multipart); loads orders; resets started flag |
| POST | `/login` | Signal bot to open `shopee.vn/buyer/login` |
| POST | `/start` | Start processing pending orders |
| POST | `/otp` | Submit OTP: `{"otp": "123456"}` |
| PUT | `/orders/{row_index}` | Edit order fields; writes Excel + in-memory state |
| POST | `/orders/{row_index}/rebuy` | Reset order to pending and queue for re-processing |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                        main.py (CLI)                    │
│  ┌──────────────────┐    ┌──────────────────────────┐   │
│  │  FastAPI dashboard│    │     _run_bot() (async)   │   │
│  │  (daemon thread) │    │                          │   │
│  │  port 8081       │◄──►│  BrowserManager          │   │
│  │                  │    │  (patchright / CDP)      │   │
│  │  WebSocket /ws   │    │  + human-like behavior   │   │
│  │  POST /upload    │    │                          │   │
│  │  POST /login     │    │  VisionAgent             │   │
│  │  POST /start     │    │  (Gemma-4-31B-IT)       │   │
│  │  POST /otp       │    │  + Set-of-Mark markers   │   │
│  │  PUT  /orders/N  │    │                          │   │
│  │  POST /orders/N/ │    │  ShopeeBot               │   │
│  │       rebuy      │    │  (add-to-cart loop)      │   │
│  └──────────────────┘    └──────────────────────────┘   │
│                AppState (shared singleton)               │
│     orders[] · login_status · otp_requested             │
│     start_event · login_event · otp_event               │
│     rebuy_queue · excel_path                            │
└─────────────────────────────────────────────────────────┘
```

The FastAPI server runs in a background daemon thread. The bot loop runs in the main asyncio event loop. They communicate through `AppState` — a shared singleton that uses `threading.Event` objects for cross-thread signalling and `asyncio.Queue` for WebSocket broadcasts.

### Project structure (summary)

| Path | Role |
|------|------|
| `main.py` | CLI entry point (click) |
| `config.yaml` | Runtime settings (CDP, viewport, model) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Documents `AI_PLATFORM_API_KEY` |
| `src/agent.py` | VisionAgent, Set-of-Mark markers, `Action` |
| `src/browser.py` | BrowserManager (`connect_over_cdp`, human-like input) |
| `src/excel_handler.py` | Load/update Excel |
| `src/shopee_bot.py` | Shopee add-to-cart orchestration |
| `src/state.py` | `AppState`, order state |
| `src/dashboard/server.py` | FastAPI app and endpoints |
| `src/dashboard/templates/index.html` | Dashboard UI |
| `debug_logs/` | Per-order screenshots + traces (gitignored) |

---

## Vision pipeline

The bot uses a hybrid JavaScript + LLM approach that separates *what to do* (LLM) from *where things are* (JavaScript):

```
Screenshot (raw PNG)
    │
    ├── JavaScript: extract_elements()
    │     ├── Finds buttons, inputs, clickable elements
    │     ├── Gets bounding box centers (exact pixels)
    │     └── Detects selection state via border color
    │            isWarm(rgb) → R>200, G<150, B<100
    │
    ├── Python: _draw_markers()
    │     ├── Draws numbered badges on screenshot
    │     ├── Red badge = normal element
    │     ├── Orange badge = [SELECTED] element
    │     └── Green/orange outline around element bounds
    │
    └── LLM: Gemma-4-31B-IT
          ├── Sees annotated screenshot + element text list
          ├── Picks element_index by NUMBER (not coordinates)
          ├── Trusts [SELECTED] markers (no visual detection needed)
          └── Returns JSON: {action, element_index, reasoning}
```

**Why this approach?** Earlier iterations had the LLM guess pixel coordinates from screenshots; Gemma-4-31B-IT was often off by 200+ pixels. JavaScript supplies precise locations; the LLM only chooses *which* element by index — accuracy improved from ~30% to nearly 100%.

**Selection state** — Shopee marks selected variants with an orange/red border. JavaScript checks `borderColor` / `outlineColor` with `isWarm()`: if R>200, G<150, B<100, the element is marked `[SELECTED]` in the element list for the LLM.

---

## Anti-bot strategy

**Local real browser is required for anti-bot, not optional.** Shopee scores device and browser trust; this project is built around **Chrome/Brave running on your machine** and controlled via CDP. Skipping that (e.g. only headless pool, or CDP to a browser that is not your normal device context) defeats the main evasion strategy below.

**Standard Playwright is blocked by Shopee.** Shopee detects CDP fingerprints at the **browser binary** level — JS-only patches are not enough.

| Detection vector | Mitigation |
|------------------|------------|
| CDP binary fingerprint | Patchright patches Chromium at compile time |
| Browser fingerprint / trust | **CDP (required):** **local** real Chrome with history, extensions, cookies — much higher trust than fresh or remote-only Chromium |
| `navigator.webdriver` | Removed by patchright (binary level) |
| Robotic mouse | Cubic Bézier movement, 20–40 steps, ±1px jitter |
| Instant clicks | `mouse.down()` / `mouse.up()` with 50–120ms hold after natural move |
| Robotic typing | Per-character 50–150ms delays; occasional 200–400ms pauses |
| Instant scrolling | Small `mouse.wheel()` steps (40–120px) with 15–50ms pauses |
| Predictable timing | ±500ms jitter on waits |
| Sequential orders | One order at a time; ~2.5–3.5s base delays between actions |
| Locale | Browser launched with `locale="vi-VN"` |

---

## Session persistence

Session cookies and storage are **your real Chrome profile** — the bot does not spin up a separate Chromium user-data directory. Connect and disconnect over CDP only; exiting the bot does not close Chrome. To use another Shopee account, sign out or switch Chrome user profiles (and matching remote-debugging setup).

---

## Debugging

Each order can produce `debug_logs/order{N}_{timestamp}/`:

| File | Contents |
|------|----------|
| `trace.log` | URL, element count, raw LLM response, observation, action, reasoning per step |
| `step{N}.png` | Raw screenshot per step |
| `step{N}_annotated.png` | Screenshot with numbered badges (orange = selected, red + green outline = normal) |

Compare raw vs annotated screenshots to verify detection. Use `trace.log` for LLM reasoning; if the wrong element is chosen, inspect detected text labels in the trace.

---

## Module reference (short)

- **`src/agent.py` — VisionAgent** — VNG Cloud OpenAI-compatible API; **Set-of-Mark (SoM)** numbered badges on elements; LLM returns `element_index`. Actions: `click`, `type`, `scroll`, `press_escape`, `wait_otp`, `done`, `fail`. Key pieces: `_draw_markers`, `VisionAgent.decide`, `Action.from_dict`.
- **`src/browser.py` — BrowserManager** — CDP only (`connect_over_cdp`); `_human_move`, `_human_click`, `_human_type`, `_human_scroll`; `extract_elements()` returns `{t, x, y, w, h, s}`; two-pass DOM scan as in the Vision pipeline section.
- **`src/shopee_bot.py` — ShopeeBot** — Per-order loop: navigate → screenshot → LLM → act; stuck detection; CAPTCHA URL checks; debug screenshots.
- **`src/excel_handler.py`** — `load_orders`, `update_order_status`, `update_order_fields`.
- **`src/state.py` — AppState** — Shared singleton; `broadcast()` to WebSocket clients.

---

## Dependencies (purpose)

| Package | Purpose |
|---------|---------|
| `patchright` | Patched Chromium / CDP to reduce Shopee anti-bot triggers |
| `openai` | VNG Cloud AI Platform (OpenAI-compatible) for Gemma-4-31B-IT |
| `Pillow` | Set-of-Mark badges on screenshots |
| `openpyxl` | Read/write `.xlsx` orders |
| `fastapi`, `uvicorn` | Dashboard server |
| `websockets` | Live dashboard updates |
| `pyyaml` | `config.yaml` |
| `python-dotenv` | Load `.env` |
| `click` | CLI |
| `python-multipart` | Excel upload endpoint |
