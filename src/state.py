import asyncio
import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any

_MAX_LOGS = 500  # max log entries kept in memory


@dataclass
class OrderState:
    row_index: int
    product_name: str
    product_url: str
    product_id: str = ""
    receiver_name: str = ""
    phone_number: str = ""
    address: str = ""
    quantity: int = 1
    status: str = "pending"
    order_id: str = ""
    note: str = ""
    current_step: int = 0
    last_screenshot_b64: str = ""
    last_reasoning: str = ""


class AppState:
    def __init__(self) -> None:
        self.orders: list[OrderState] = []
        self._subscribers: list[asyncio.Queue] = []
        self.started: bool = False
        self.start_event: threading.Event = threading.Event()
        self.rebuy_queue: queue.Queue = queue.Queue()
        self.excel_path: str = ""
        self.login_status: str = "unknown"   # "unknown" | "logged_in" | "logged_out"
        self.login_event: threading.Event = threading.Event()
        self.otp_requested: bool = False
        self.otp_value: str = ""
        self.otp_event: threading.Event = threading.Event()
        self.logs: list[str] = []  # rolling log buffer for dashboard

    def add_subscriber(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def remove_subscriber(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def add_log(self, message: str) -> None:
        """Append a log line (thread-safe for the daemon dashboard thread)."""
        self.logs.append(message)
        if len(self.logs) > _MAX_LOGS:
            self.logs = self.logs[-_MAX_LOGS:]

    async def broadcast(self) -> None:
        payload = self._serialize()
        for q in list(self._subscribers):
            await q.put(payload)

    def _serialize(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "login_status": self.login_status,
            "otp_requested": self.otp_requested,
            "logs": list(self.logs),
            "orders": [
                {
                    "row_index": o.row_index,
                    "product_name": o.product_name,
                    "product_url": o.product_url,
                    "product_id": o.product_id,
                    "receiver_name": o.receiver_name,
                    "phone_number": o.phone_number,
                    "address": o.address,
                    "quantity": o.quantity,
                    "status": o.status,
                    "order_id": o.order_id,
                    "note": o.note,
                    "current_step": o.current_step,
                    "last_screenshot_b64": o.last_screenshot_b64,
                    "last_reasoning": o.last_reasoning,
                }
                for o in self.orders
            ],
        }


app_state = AppState()


class DashboardLogHandler(logging.Handler):
    """Logging handler that pushes formatted records into AppState.logs."""

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self._state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._state.add_log(msg)
        except Exception:
            self.handleError(record)
