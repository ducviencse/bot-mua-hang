#!/usr/bin/env python3
"""bot-mua-hang — Shopee automated shopping bot.

Usage:
    python main.py                          # open dashboard, import Excel from UI
    python main.py --file orders.xlsx       # preload Excel, then use dashboard
    python main.py --file orders.xlsx --no-headless --port 9090
"""
import asyncio
import logging
import os
import time
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


def _config_watcher(config_path: str) -> None:
    """Background thread: polls config.yaml every 5s and hot-reloads allowed fields."""
    from src.state import app_state as _state
    p = Path(config_path)
    last_mtime: float | None = p.stat().st_mtime if p.exists() else None
    while True:
        time.sleep(5)
        try:
            if not p.exists():
                continue
            mtime = p.stat().st_mtime
            if last_mtime is None or mtime != last_mtime:
                last_mtime = mtime
                cfg = _load_config(config_path)
                _state.batch_size = cfg.get("batch_size", 50)
                _state.max_retries = cfg.get("max_retries", 2)
                _state.seller_note = cfg.get("seller_note", "")
                _state.invoice = cfg.get("invoice", {})
                logger.info("config.yaml reloaded")
        except Exception:
            pass


def _start_dashboard(port: int) -> None:
    from src.dashboard.server import app
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


@click.command()
@click.option("--file", "-f", default=None, type=click.Path(exists=True), help="Optional: preload an orders Excel file")
@click.option("--port", default=None, type=int, help="Dashboard port (default: 8081)")
@click.option("--config", default="config.yaml", show_default=True, help="Path to config.yaml")
def main(file, port, config):
    """Shopee shopping bot — import orders from the dashboard or via --file."""
    cfg = _load_config(config)

    effective_port = port if port is not None else cfg.get("dashboard_port", 8081)
    model = cfg.get("model", "google/gemma-4-31b-it")
    viewport = cfg.get("viewport", {"width": 1920, "height": 1080})
    max_steps = cfg.get("max_steps", 20)
    wait_ms = cfg.get("wait_timeout_ms", 2500)
    cdp_endpoint = cfg.get("cdp_endpoint", None)
    seller_note = cfg.get("seller_note", "")
    invoice = cfg.get("invoice", {})
    batch_size = cfg.get("batch_size", 50)
    max_retries = cfg.get("max_retries", 2)

    if not cdp_endpoint:
        cdp_endpoint = "http://localhost:9222"
        logger.warning("cdp_endpoint not set in config.yaml — defaulting to %s", cdp_endpoint)

    from src.database import init_db
    from src.state import app_state
    init_db()

    # Determine if first-run setup is needed
    env_file = Path(".env")
    api_key = os.environ.get("AI_PLATFORM_API_KEY", "").strip()
    app_state.setup_complete = env_file.exists() and bool(api_key)

    # Seed hot-reloadable config fields onto app_state
    app_state.batch_size = batch_size
    app_state.max_retries = max_retries
    app_state.seller_note = seller_note
    app_state.invoice = invoice or {}

    _install_dashboard_log_handler()

    dash_thread = threading.Thread(
        target=_start_dashboard,
        args=(effective_port,),
        daemon=True,
    )
    dash_thread.start()
    logger.info("Dashboard running at http://localhost:%d", effective_port)

    # Config file watcher for hot-reload
    watcher_thread = threading.Thread(
        target=_config_watcher,
        args=(config,),
        daemon=True,
    )
    watcher_thread.start()

    asyncio.run(
        _run_bot(
            excel_path=file,
            model=model,
            viewport=viewport,
            max_steps=max_steps,
            wait_ms=wait_ms,
            dashboard_port=effective_port,
            cdp_endpoint=cdp_endpoint,
            seller_note=seller_note,
            invoice=invoice,
            batch_size=batch_size,
            max_retries=max_retries,
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
    viewport: dict,
    max_steps: int,
    wait_ms: int,
    cdp_endpoint: str,
    dashboard_port: int = 8081,
    seller_note: str = "",
    invoice: dict | None = None,
    batch_size: int = 50,
    max_retries: int = 2,
) -> None:
    from src.agent import VisionAgent
    from src.browser import BrowserManager
    from src.database import create_order, create_session, update_order, update_session
    from src.excel_handler import load_orders
    from src.shopee_bot import ShopeeBot
    from src.state import OrderState, app_state

    # Start browser early so login works before orders are imported
    browser = BrowserManager(
        cdp_endpoint=cdp_endpoint,
        width=viewport.get("width", 1920),
        height=viewport.get("height", 1080),
    )
    # Wait for Chrome to be available — poll instead of crashing
    logger.info("Connecting to Chrome at %s…", cdp_endpoint)
    while True:
        try:
            await browser.start()
            app_state.browser_status = "connected"
            await app_state.broadcast()
            logger.info("Chrome connected via CDP")
            break
        except Exception:
            if app_state.browser_status != "disconnected":
                app_state.browser_status = "disconnected"
                await app_state.broadcast()
            logger.warning(
                "Chrome not reachable at %s — start Chrome with "
                "--remote-debugging-port=9222, retrying in 3s…", cdp_endpoint
            )
            await browser._reset()
            await asyncio.sleep(3)

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

    # ── Reuse existing session (created via UI) or create a new one ──
    cfg_snapshot = {
        "model": model, "max_steps": max_steps, "wait_ms": wait_ms,
        "batch_size": batch_size, "max_retries": max_retries,
    }
    if app_state.session_id:
        session_id = app_state.session_id
    else:
        session_id = create_session(
            excel_filename=Path(active_path).name,
            total_orders=len(app_state.orders),
            config=cfg_snapshot,
        )
        app_state.session_id = session_id
    update_session(session_id, status="running")

    raw_orders = load_orders(active_path)
    for order_state in app_state.orders:
        if order_state.db_order_id:
            # Already created by the upload endpoint — skip
            continue
        db_id = create_order(
            session_id=session_id,
            row_index=order_state.row_index,
            product_name=order_state.product_name,
            product_url=order_state.product_url,
            product_id=order_state.product_id,
            receiver_name=order_state.receiver_name,
            phone_number=order_state.phone_number,
            address=order_state.address,
            quantity=order_state.quantity,
            status=order_state.status,
            order_id=order_state.order_id,
            note=order_state.note,
        )
        order_state.db_order_id = db_id

    agent = VisionAgent(model=model)
    bot = ShopeeBot(
        agent=agent,
        browser=browser,
        app_state=app_state,
        seller_note=seller_note,
        invoice=invoice or {},
        max_steps=max_steps,
        wait_ms=wait_ms,
    )

    # Store config on app_state for dashboard visibility
    app_state.batch_size = batch_size
    app_state.max_retries = max_retries

    pending_states = [o for o in app_state.orders if o.status != "success"]

    # Split into batches
    batches = [
        pending_states[i:i + batch_size]
        for i in range(0, len(pending_states), batch_size)
    ]
    app_state.total_batches = len(batches)
    await app_state.broadcast()

    try:
        # --- Main pass: process all batches ---
        for batch_num, batch in enumerate(batches, start=1):
            app_state.current_batch = batch_num
            await app_state.broadcast()
            logger.info("Processing batch %d/%d (%d orders)", batch_num, app_state.total_batches, len(batch))

            for order_state in batch:
                idx = app_state.orders.index(order_state)
                app_state.active_order_index = idx
                await app_state.broadcast()

                raw = next((o for o in raw_orders if o["_row_index"] == order_state.row_index), None)
                if raw:
                    await bot.process_order(raw, order_state)

                app_state.active_order_index = None
                await app_state.broadcast()

        # --- Auto-retry passes ---
        for retry_round in range(1, max_retries + 1):
            retryable = [
                o for o in app_state.orders
                if o.status == "failed"
                and o.failure_category == "retryable"
                and o.retry_count < max_retries
            ]
            if not retryable:
                break

            logger.info("Retry round %d/%d — %d retryable order(s)", retry_round, max_retries, len(retryable))

            for order_state in retryable:
                order_state.retry_count += 1
                order_state.status = "pending"
                order_state.note = ""
                order_state.failure_category = ""
                order_state.last_screenshot_b64 = ""
                order_state.last_reasoning = ""
                order_state.current_step = 0
                if order_state.db_order_id:
                    update_order(order_state.db_order_id, status="pending", note="", failure_category="", retry_count=order_state.retry_count)

                idx = app_state.orders.index(order_state)
                app_state.active_order_index = idx
                await app_state.broadcast()

                raw = next((o for o in raw_orders if o["_row_index"] == order_state.row_index), None)
                if raw:
                    await bot.process_order(raw, order_state)

                app_state.active_order_index = None
                await app_state.broadcast()

        # --- Mark completed ---
        app_state.completed = True
        success = sum(1 for s in app_state.orders if s.status == "success")
        failed = sum(1 for s in app_state.orders if s.status == "failed")
        update_session(session_id, status="completed", success_count=success, failed_count=failed)
        logger.info("Done. Success: %d | Failed: %d", success, failed)
        await app_state.broadcast()

        # Stay alive for re-buy requests (browser remains open)
        logger.info("Waiting for re-buy requests…")
        loop = asyncio.get_event_loop()
        while True:
            db_order_id = await loop.run_in_executor(None, app_state.rebuy_queue.get)
            order_state = next((s for s in app_state.orders if s.db_order_id == db_order_id), None)
            if order_state is None:
                continue

            # Reset completed flag when re-buying
            app_state.completed = False
            idx = app_state.orders.index(order_state)
            app_state.active_order_index = idx
            await app_state.broadcast()

            raw = next((o for o in raw_orders if o["_row_index"] == order_state.row_index), None)
            if raw:
                await bot.process_order(raw, order_state)

            app_state.active_order_index = None
            await app_state.broadcast()

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
