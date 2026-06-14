import io
import json
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..state import app_state, OrderState
from ..excel_handler import load_orders, update_order_fields, update_order_status

app = FastAPI(title="bot-mua-hang dashboard")

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "index.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


_UPLOAD_PATH = Path("uploaded_orders.xlsx")


@app.post("/upload")
async def upload_orders(file: UploadFile = File(...)):
    contents = await file.read()
    _UPLOAD_PATH.write_bytes(contents)

    orders = load_orders(str(_UPLOAD_PATH))
    app_state.excel_path = str(_UPLOAD_PATH)
    app_state.orders.clear()
    # Reset start state so user can start fresh after re-upload
    app_state.started = False
    app_state.start_event.clear()

    for o in orders:
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
    await app_state.broadcast()
    return JSONResponse({"status": "ok", "count": len(orders)})


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


@app.post("/start")
async def start_bot():
    if not app_state.started:
        app_state.started = True
        app_state.start_event.set()
        await app_state.broadcast()
    return JSONResponse({"status": "started"})


class OrderUpdate(BaseModel):
    product_url: Optional[str] = None
    product_name: Optional[str] = None
    product_id: Optional[str] = None
    receiver_name: Optional[str] = None
    phone_number: Optional[str] = None
    address: Optional[str] = None
    quantity: Optional[int] = None


@app.put("/orders/{row_index}")
async def update_order(row_index: int, body: OrderUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if fields and app_state.excel_path:
        update_order_fields(app_state.excel_path, row_index, fields)

    order_state = next((o for o in app_state.orders if o.row_index == row_index), None)
    if order_state:
        for k, v in fields.items():
            if hasattr(order_state, k):
                setattr(order_state, k, v)

    await app_state.broadcast()
    return JSONResponse({"status": "updated"})


@app.post("/orders/{row_index}/rebuy")
async def rebuy_order(row_index: int):
    order_state = next((o for o in app_state.orders if o.row_index == row_index), None)
    if order_state:
        order_state.status = "pending"
        order_state.order_id = ""
        order_state.note = ""
        order_state.current_step = 0
        order_state.last_screenshot_b64 = ""
        order_state.last_reasoning = ""

    if app_state.excel_path:
        update_order_status(app_state.excel_path, row_index, "pending")

    app_state.rebuy_queue.put(row_index)
    await app_state.broadcast()
    return JSONResponse({"status": "queued"})


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
