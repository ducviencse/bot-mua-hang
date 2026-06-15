import json
import os
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..state import app_state, OrderState
from ..excel_handler import load_orders
from .. import database as db

app = FastAPI(title="bot-mua-hang dashboard")


# ── First-run redirect middleware ─────────────────────────────────

@app.middleware("http")
async def setup_redirect(request: Request, call_next):
    if not app_state.setup_complete:
        path = request.url.path
        if path == "/" or path.startswith("/sessions"):
            return RedirectResponse(url="/setup")
    return await call_next(request)

_TEMPLATE_DIR = Path(__file__).parent / "templates"

# Serve debug_logs as static files for screenshots
_DEBUG_LOGS_DIR = Path("debug_logs")
_DEBUG_LOGS_DIR.mkdir(exist_ok=True)
app.mount("/debug_logs", StaticFiles(directory=str(_DEBUG_LOGS_DIR)), name="debug_logs")

_UPLOAD_PATH = Path("uploaded_orders.xlsx")


# ── HTML page routes ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def sessions_page():
    return (_TEMPLATE_DIR / "sessions.html").read_text(encoding="utf-8")


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_dashboard(session_id: int):
    return (_TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/orders/{order_db_id}", response_class=HTMLResponse)
async def order_detail_page(order_db_id: int):
    return (_TEMPLATE_DIR / "order_detail.html").read_text(encoding="utf-8")


# ── Sessions API ─────────────────────────────────────────────────

@app.get("/api/sessions")
async def api_list_sessions():
    return JSONResponse(db.list_sessions())


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: int):
    session = db.get_session(session_id)
    if not session:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(session)


@app.post("/api/sessions")
async def api_create_session():
    sid = db.create_session(excel_filename="", total_orders=0)
    return JSONResponse({"id": sid})


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: int):
    db.delete_session(session_id)
    return JSONResponse({"status": "deleted"})


@app.get("/api/sessions/{session_id}/orders")
async def api_session_orders(session_id: int):
    return JSONResponse(db.get_orders_for_session(session_id))


@app.post("/api/sessions/{session_id}/upload")
async def api_upload_to_session(session_id: int, file: UploadFile = File(...)):
    contents = await file.read()
    _UPLOAD_PATH.write_bytes(contents)

    orders = load_orders(str(_UPLOAD_PATH))
    app_state.excel_path = str(_UPLOAD_PATH)
    app_state.orders.clear()
    app_state.started = False
    app_state.start_event.clear()

    for o in orders:
        db_id = db.create_order(
            session_id=session_id,
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
                db_order_id=db_id,
            )
        )

    db.update_session(session_id, total_orders=len(orders), excel_filename=file.filename or "uploaded.xlsx")
    app_state.session_id = session_id
    await app_state.broadcast()
    return JSONResponse({"status": "ok", "count": len(orders)})


@app.post("/api/sessions/{session_id}/start")
async def api_start_session(session_id: int):
    if not app_state.started:
        app_state.started = True
        app_state.start_event.set()
        db.update_session(session_id, status="running")
        await app_state.broadcast()
    return JSONResponse({"status": "started"})


@app.post("/api/sessions/{session_id}/retry-all")
async def api_retry_all(session_id: int):
    count = 0
    for order_state in app_state.orders:
        if (
            order_state.status == "failed"
            and order_state.failure_category == "retryable"
            and order_state.retry_count < app_state.max_retries
        ):
            order_state.retry_count += 1
            order_state.status = "pending"
            order_state.note = ""
            order_state.failure_category = ""
            order_state.last_screenshot_b64 = ""
            order_state.last_reasoning = ""
            order_state.current_step = 0

            if order_state.db_order_id:
                db.update_order(
                    order_state.db_order_id,
                    status="pending", note="", failure_category="",
                    retry_count=order_state.retry_count,
                )
                app_state.rebuy_queue.put(order_state.db_order_id)
            count += 1

    app_state.completed = False
    await app_state.broadcast()
    return JSONResponse({"status": "ok", "retried": count})


# ── Orders API ───────────────────────────────────────────────────

@app.get("/api/orders/{order_db_id}")
async def api_get_order(order_db_id: int):
    order = db.get_order(order_db_id)
    if not order:
        return JSONResponse({"error": "not found"}, status_code=404)
    order["steps"] = db.get_steps_for_order(order_db_id)
    return JSONResponse(order)


class OrderUpdate(BaseModel):
    product_url: Optional[str] = None
    product_name: Optional[str] = None
    product_id: Optional[str] = None
    receiver_name: Optional[str] = None
    phone_number: Optional[str] = None
    address: Optional[str] = None
    quantity: Optional[int] = None


@app.put("/api/orders/{order_db_id}")
async def api_update_order(order_db_id: int, body: OrderUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if fields:
        db.update_order(order_db_id, **fields)

    # Also update in-memory AppState if this order is active
    order_state = next((o for o in app_state.orders if o.db_order_id == order_db_id), None)
    if order_state:
        for k, v in fields.items():
            if hasattr(order_state, k):
                setattr(order_state, k, v)

    await app_state.broadcast()
    return JSONResponse({"status": "updated"})


@app.post("/api/orders/{order_db_id}/rebuy")
async def api_rebuy_order(order_db_id: int):
    order_state = next((o for o in app_state.orders if o.db_order_id == order_db_id), None)
    if order_state:
        order_state.status = "pending"
        order_state.order_id = ""
        order_state.note = ""
        order_state.current_step = 0
        order_state.last_screenshot_b64 = ""
        order_state.last_reasoning = ""
        order_state.retry_count = 0
        order_state.failure_category = ""

    db.update_order(
        order_db_id,
        status="pending", order_id="", note="",
        retry_count=0, failure_category="",
    )

    app_state.rebuy_queue.put(order_db_id)
    await app_state.broadcast()
    return JSONResponse({"status": "queued"})


# ── OTP / Login (not session-scoped) ─────────────────────────────

class OtpSubmit(BaseModel):
    otp: str


@app.post("/otp")
async def submit_otp(body: OtpSubmit):
    app_state.otp_value = body.otp.strip()
    app_state.otp_event.set()
    return JSONResponse({"status": "ok"})


@app.post("/login")
async def login():
    app_state.login_event.set()
    return JSONResponse({"status": "opening"})


# ── Legacy endpoints (redirect to new API) ───────────────────────

@app.post("/upload")
async def legacy_upload(file: UploadFile = File(...)):
    """Legacy upload — creates a session if needed, then delegates."""
    sid = app_state.session_id
    if not sid:
        sid = db.create_session(excel_filename=file.filename or "uploaded.xlsx")
        app_state.session_id = sid
    return await api_upload_to_session(sid, file)


@app.post("/start")
async def legacy_start():
    sid = app_state.session_id or 0
    return await api_start_session(sid)


@app.post("/retry-all")
async def legacy_retry_all():
    sid = app_state.session_id or 0
    return await api_retry_all(sid)


# ── Setup Wizard ─────────────────────────────────────────────────

@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    return (_TEMPLATE_DIR / "setup.html").read_text(encoding="utf-8")


_REQUIRED_PACKAGES = [
    ("patchright", "patchright"),
    ("openai", "openai"),
    ("openpyxl", "openpyxl"),
    ("pandas", "pandas"),
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("yaml", "pyyaml"),
    ("dotenv", "python-dotenv"),
    ("click", "click"),
    ("PIL", "Pillow"),
    ("websockets", "websockets"),
]


@app.get("/api/setup/status")
async def api_setup_status():
    return JSONResponse({
        "setup_complete": app_state.setup_complete,
        "login_status": app_state.login_status,
    })


@app.get("/api/setup/check-deps")
async def api_check_deps():
    import importlib
    missing = []
    results = []
    for import_name, pip_name in _REQUIRED_PACKAGES:
        try:
            importlib.import_module(import_name)
            results.append({"name": pip_name, "ok": True})
        except ImportError:
            results.append({"name": pip_name, "ok": False})
            missing.append(pip_name)

    install_cmd = ""
    if missing:
        install_cmd = "pip install " + " ".join(missing)

    return JSONResponse({
        "ok": len(missing) == 0,
        "results": results,
        "missing": missing,
        "install_cmd": install_cmd,
    })


class ValidateKeyBody(BaseModel):
    api_key: str


@app.post("/api/setup/validate-key")
async def api_validate_key(body: ValidateKeyBody):
    import asyncio
    from openai import AsyncOpenAI, APIError

    client = AsyncOpenAI(
        api_key=body.api_key.strip(),
        base_url="https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1",
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model="google/gemma-4-31b-it",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=5,
            ),
            timeout=15,
        )
        _ = resp.choices[0].message.content
        return JSONResponse({"ok": True})
    except APIError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/setup/check-browser")
async def api_check_browser():
    try:
        with urllib.request.urlopen("http://localhost:9222/json/version", timeout=2) as r:
            data = json.loads(r.read())
        return JSONResponse({"found": True, "browser": data.get("Browser", "")})
    except Exception:
        return JSONResponse({"found": False})


@app.get("/api/setup/check-login")
async def api_check_login():
    # Try CDP-based cookie check via browser WebSocket
    try:
        import asyncio
        import websockets

        with urllib.request.urlopen("http://localhost:9222/json/version", timeout=2) as r:
            data = json.loads(r.read())
        ws_url = data.get("webSocketDebuggerUrl")
        if ws_url:
            async with websockets.connect(ws_url, ping_interval=None) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                cookies = resp.get("result", {}).get("cookies", [])
                logged_in = any(c.get("name") == "SPC_U" for c in cookies)
                if logged_in:
                    app_state.login_status = "logged_in"
                return JSONResponse({"logged_in": logged_in})
    except Exception:
        pass
    # Fallback to app_state
    return JSONResponse({"logged_in": app_state.login_status == "logged_in"})


class SetupSaveBody(BaseModel):
    api_key: str
    seller_note: str = ""
    max_steps: int = 20
    wait_timeout_ms: int = 2500
    batch_size: int = 50
    max_retries: int = 2
    invoice_company: str = ""
    invoice_tax_id: str = ""
    invoice_address: str = ""
    invoice_email: str = ""


@app.post("/api/setup/save")
async def api_setup_save(body: SetupSaveBody):
    import yaml

    # Write .env
    env_path = Path(".env")
    env_lines: list[str] = []
    if env_path.exists():
        existing = env_path.read_text(encoding="utf-8").splitlines()
        env_lines = [l for l in existing if not l.startswith("AI_PLATFORM_API_KEY=")]
    env_lines.append(f"AI_PLATFORM_API_KEY={body.api_key.strip()}")
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    # Load existing config.yaml (preserve non-wizard keys)
    cfg_path = Path("config.yaml")
    existing_cfg: dict = {}
    if cfg_path.exists():
        with open(cfg_path) as f:
            existing_cfg = yaml.safe_load(f) or {}

    existing_cfg["seller_note"] = body.seller_note
    existing_cfg["max_steps"] = body.max_steps
    existing_cfg["wait_timeout_ms"] = body.wait_timeout_ms
    existing_cfg["batch_size"] = body.batch_size
    existing_cfg["max_retries"] = body.max_retries
    existing_cfg["invoice"] = {
        "company_name": body.invoice_company,
        "tax_id": body.invoice_tax_id,
        "address": body.invoice_address,
        "email": body.invoice_email,
    }

    with open(cfg_path, "w") as f:
        yaml.dump(existing_cfg, f, allow_unicode=True, default_flow_style=False)

    # Reload env + update app_state
    from dotenv import load_dotenv
    load_dotenv(override=True)
    app_state.setup_complete = True
    app_state.batch_size = body.batch_size
    app_state.max_retries = body.max_retries
    app_state.seller_note = body.seller_note
    app_state.invoice = existing_cfg["invoice"]

    return JSONResponse({"ok": True})


# ── WebSocket ────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    queue = app_state.add_subscriber()
    try:
        await ws.send_text(json.dumps(app_state._serialize()))
        while True:
            payload = await queue.get()
            await ws.send_text(json.dumps(payload))
    except WebSocketDisconnect:
        pass
    finally:
        app_state.remove_subscriber(queue)
