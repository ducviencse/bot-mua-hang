# bot-mua-hang

Shopee Vietnam shopping bot — reads orders from an Excel file and autonomously completes the checkout flow using a Vision LLM. Includes a real-time web dashboard and a first-run setup wizard.

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

The bot connects to your **real Chrome or Brave browser** via remote debugging — no headless Chromium is needed. This is intentional: using your real browser avoids Shopee's anti-bot detection.

- Chrome: https://www.google.com/chrome/
- Brave: https://brave.com/

### 3. Run the install script

**macOS / Linux:**
```bash
chmod +x install.sh
./install.sh
```

**Windows:**
```
install.bat
```

The script installs all Python dependencies and downloads the Chromium browser used for dependency checks. When it finishes it starts the bot and opens the setup wizard automatically.

### 4. Manual install (alternative to the script)

If you prefer to run steps individually:
```bash
pip install -r requirements.txt
python main.py
```
Then open **http://localhost:8081** in your browser.

---

## Setup Wizard (first run)

When the bot starts without a configured API key it redirects you to the setup wizard at **http://localhost:8081/setup**.

| Step | What happens |
|------|-------------|
| 1 — Dependencies | Verifies all packages are installed; shows copy-pasteable fix commands if anything is missing |
| 2 — API Key | Paste your VNG Cloud AI Platform key; wizard validates it with a live test call |
| 3 — Browser | Run the shown command to launch Chrome with remote debugging; wizard detects it automatically |
| 4 — Shopee Login | Click "Open Shopee Login", log in; wizard detects your session cookie automatically |
| 5 — Config | Set seller note, batch size, retries, VAT invoice fields; click Save |

After saving, the wizard writes `.env` and `config.yaml` and redirects you to the dashboard.

---

## Daily Use

1. Start Chrome with remote debugging (the command from Step 3 above)
2. Run `python main.py`
3. Open **http://localhost:8081**
4. Upload your orders Excel file, click **Start**

---

## Configuration

Settings live in `config.yaml`. Fields marked **hot-reload** take effect within 5 seconds without restarting:

| Field | Default | Hot-reload |
|-------|---------|------------|
| `seller_note` | `""` | yes |
| `max_steps` | `40` | yes |
| `wait_timeout_ms` | `3000` | yes |
| `batch_size` | `50` | yes |
| `max_retries` | `2` | yes |
| `cdp_endpoint` | `"http://localhost:9222"` | no — restart required |
| `dashboard_port` | `8081` | no — restart required |

`cdp_endpoint` is **required**. The bot will not start without it.

The API key is stored in `.env` as `AI_PLATFORM_API_KEY`.

---

## Excel File Format

| Column | Description |
|--------|-------------|
| `product_url` | Shopee product URL |
| `product_name` | Display name |
| `product_id` | Shopee product ID |
| `receiver_name` | Recipient full name |
| `phone_number` | Recipient phone |
| `address` | Delivery address |
| `quantity` | Number of items |
| `status` | `pending` / `running` / `success` / `failed` |
| `order_id` | Filled in by the bot after success |
| `note` | Custom note or failure reason |

---

## Re-running the Setup Wizard

To reset and go through the wizard again:
```bash
rm .env
python main.py
```
Then open **http://localhost:8081** — it redirects to the wizard automatically.
