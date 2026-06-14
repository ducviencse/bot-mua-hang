#!/usr/bin/env python3
"""bot-mua-hang — Shopee automated shopping bot.

Usage:
    python main.py                          # open dashboard, import Excel from UI
    python main.py --file orders.xlsx       # preload Excel, then use dashboard
    python main.py --file orders.xlsx --no-headless --port 9090
"""
import asyncio
import logging
import threading
from pathlib import Path

import click
import uvicorn
import yaml
from dotenv import load_dotenv

load_dotenv()

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATEFMT = "%H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    datefmt=_LOG_DATEFMT,
)
logger = logging.getLogger(__name__)


def _install_dashboard_log_handler() -> None:
    """Attach a handler that mirrors all logs into app_state for the dashboard."""
    from src.state import DashboardLogHandler, app_state
    handler = DashboardLogHandler(app_state)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    logging.getLogger().addHandler(handler)


def _load_config(path: str = "config.yaml") -> dict:
    config_file = Path(path)
    if config_file.exists():
        with open(config_file) as f:
            return yaml.safe_load(f) or {}
    return {}


def _start_dashboard(port: int) -> None:
    from src.dashboard.server import app
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


@click.command()
@click.option("--file", "-f", default=None, type=click.Path(exists=True), help="Optional: preload an orders Excel file")
@click.option("--headless/--no-headless", default=None, help="Override headless mode from config")
@click.option("--port", default=None, type=int, help="Dashboard port (default: 8081)")
@click.option("--config", default="config.yaml", show_default=True, help="Path to config.yaml")
def main(file, headless, port, config):
    """Shopee shopping bot — import orders from the dashboard or via --file."""
    cfg = _load_config(config)

    effective_headless = headless if headless is not None else cfg.get("headless", False)
    effective_port = port if port is not None else cfg.get("dashboard_port", 8081)
    model = cfg.get("model", "google/gemma-4-31b-it")
    viewport = cfg.get("viewport", {"width": 1920, "height": 1080})
    max_steps = cfg.get("max_steps", 20)
    wait_ms = cfg.get("wait_timeout_ms", 2500)
    session_dir = cfg.get("session_dir", "./session")
    browser_channel = cfg.get("browser_channel", "chrome")
    cdp_endpoint = cfg.get("cdp_endpoint", None)
    seller_note = cfg.get("seller_note", "Quà tặng từ Cửa hàng quét QR chuyển khoản Zalopay")
    invoice = cfg.get("invoice", {})

    _install_dashboard_log_handler()

    dash_thread = threading.Thread(
        target=_start_dashboard,
        args=(effective_port,),
        daemon=True,
    )
    dash_thread.start()
    logger.info("Dashboard running at http://localhost:%d", effective_port)

    asyncio.run(
        _run_bot(
            excel_path=file,
            model=model,
            headless=effective_headless,
            viewport=viewport,
            max_steps=max_steps,
            wait_ms=wait_ms,
            dashboard_port=effective_port,
            session_dir=session_dir,
            browser_channel=browser_channel,
            cdp_endpoint=cdp_endpoint,
            seller_note=seller_note,
            invoice=invoice,
        )
    )


async def _login_watcher(browser, app_state) -> None:
    """Background task: polls Shopee login status and handles login button requests."""
    try:
        while True:
            if app_state.login_event.is_set():
                app_state.login_event.clear()
                logger.info("Opening Shopee login page…")
                await browser.goto("https://shopee.vn/buyer/login")

            logged_in = await browser.is_logged_in()
            new_status = "logged_in" if logged_in else "logged_out"
            if new_status != app_state.login_status:
                app_state.login_status = new_status
                await app_state.broadcast()
                if logged_in:
                    logger.info("Shopee login detected — session saved.")

            await asyncio.sleep(3)
    except asyncio.CancelledError:
        pass


async def _run_bot(
    excel_path: str | None,
    model: str,
    headless: bool,
    viewport: dict,
    max_steps: int,
    wait_ms: int,
    dashboard_port: int = 8081,
    session_dir: str = "./session",
    browser_channel: str | None = "chrome",
    cdp_endpoint: str | None = None,
    seller_note: str = "",
    invoice: dict | None = None,
) -> None:
    from src.agent import VisionAgent
    from src.browser import BrowserManager
    from src.excel_handler import load_orders
    from src.shopee_bot import ShopeeBot
    from src.state import OrderState, app_state

    # Start browser early so login works before orders are imported
    browser = BrowserManager(
        headless=headless,
        width=viewport.get("width", 1920),
        height=viewport.get("height", 1080),
        session_dir=session_dir,
        channel=browser_channel,
        cdp_endpoint=cdp_endpoint,
    )
    await browser.start()

    # Check initial login status and start polling
    app_state.login_status = "logged_in" if await browser.is_logged_in() else "logged_out"
    login_task = asyncio.create_task(_login_watcher(browser, app_state))

    # Preload Excel if --file was provided
    if excel_path:
        _populate_state(app_state, excel_path, load_orders)
        logger.info("Preloaded %d order(s) from %s", len(app_state.orders), excel_path)

    await app_state.broadcast()
    logger.info("Open dashboard at http://localhost:%d", dashboard_port)

    # Wait for Start button
    await asyncio.get_event_loop().run_in_executor(None, app_state.start_event.wait)
    login_task.cancel()
    logger.info("Start received — processing orders.")

    active_path = app_state.excel_path
    if not active_path:
        logger.error("No Excel file loaded. Upload one from the dashboard first.")
        await browser.close()
        return

    agent = VisionAgent(model=model)
    bot = ShopeeBot(
        agent=agent,
        browser=browser,
        app_state=app_state,
        excel_path=active_path,
        seller_note=seller_note,
        invoice=invoice or {},
        max_steps=max_steps,
        wait_ms=wait_ms,
    )

    pending_states = [o for o in app_state.orders if o.status != "success"]
    raw_orders = load_orders(active_path)

    try:
        for order_state in pending_states:
            raw = next((o for o in raw_orders if o["_row_index"] == order_state.row_index), None)
            if raw:
                await bot.process_order(raw, order_state)

        success = sum(1 for s in app_state.orders if s.status == "success")
        failed = sum(1 for s in app_state.orders if s.status == "failed")
        logger.info("Done. Success: %d | Failed: %d", success, failed)

        # Stay alive for re-buy requests (browser remains open)
        logger.info("Waiting for re-buy requests…")
        loop = asyncio.get_event_loop()
        while True:
            row_index = await loop.run_in_executor(None, app_state.rebuy_queue.get)
            order_state = next((s for s in app_state.orders if s.row_index == row_index), None)
            if order_state is None:
                continue
            fresh = load_orders(app_state.excel_path)
            raw = next((o for o in fresh if o["_row_index"] == row_index), None)
            if raw:
                await bot.process_order(raw, order_state)

    except asyncio.CancelledError:
        pass
    finally:
        await browser.close()


def _populate_state(app_state, excel_path: str, load_orders_fn) -> None:
    from src.state import OrderState
    app_state.orders.clear()
    app_state.excel_path = excel_path
    for o in load_orders_fn(excel_path):
        app_state.orders.append(
            OrderState(
                row_index=o["_row_index"],
                product_name=o.get("product_name") or "",
                product_url=o.get("product_url") or "",
                product_id=str(o.get("product_id") or ""),
                receiver_name=o.get("receiver_name") or "",
                phone_number=str(o.get("phone_number") or ""),
                address=o.get("address") or "",
                quantity=int(o.get("quantity") or 1),
                status=o.get("status") or "pending",
                order_id=o.get("order_id") or "",
                note=o.get("note") or "",
            )
        )


if __name__ == "__main__":
    main()
