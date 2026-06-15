import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..state import app_state, OrderState
from ..excel_handler import load_orders
from .. import database as db

app = FastAPI(title="bot-mua-hang dashboard")

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
