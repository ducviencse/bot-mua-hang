import asyncio
import logging
import queue
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_MAX_LOGS = 500  # max log entries kept in memory

_PERMANENT_PATTERNS = (
    "captcha",
    "out of stock",
    "unavailable",
    "access denied",
    "login page",
    "het hang",
    "không còn hàng",
)


def classify_failure(note: str) -> str:
    """Classify a failure note as 'permanent' or 'retryable'."""
    if not note:
        return ""
    note_lower = note.lower()
    for pattern in _PERMANENT_PATTERNS:
        if pattern in note_lower:
            return "permanent"
    return "retryable"


def _normalize_failure_note(note: str) -> str:
    """Strip variable suffixes for grouping. E.g. 'Navigation error: timeout' -> 'Navigation error'."""
    if not note:
        return ""
    # Strip everything after the first colon for known prefixed errors
    for prefix in ("Navigation error", "LLM error", "Execute error"):
        if note.startswith(prefix):
            return prefix
    # Strip trailing details in parentheses
    cleaned = re.sub(r"\s*\(.*?\)\s*$", "", note)
    return cleaned


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
    retry_count: int = 0
    failure_category: str = ""  # "retryable" | "permanent" | ""
    db_order_id: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0


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
        self.active_order_index: int | None = None
        self.batch_size: int = 50
        self.max_retries: int = 2
        self.current_batch: int = 0
        self.total_batches: int = 0
        self.completed: bool = False
        self.session_id: int | None = None
        self.setup_complete: bool = False
        self.seller_note: str = ""
        self.invoice: dict = {}
        self.browser_status: str = "disconnected"  # "disconnected" | "connected"

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
        # Summary stats
        status_counts = Counter(o.status for o in self.orders)
        summary = {
            "total": len(self.orders),
            "success": status_counts.get("success", 0),
            "failed": status_counts.get("failed", 0),
            "running": status_counts.get("running", 0),
            "pending": status_counts.get("pending", 0),
        }

        # Failure groups
        failure_map: dict[str, dict] = {}
        for o in self.orders:
            if o.status == "failed" and o.note:
                key = _normalize_failure_note(o.note)
                if key not in failure_map:
                    failure_map[key] = {
                        "reason": key,
                        "category": o.failure_category,
                        "count": 0,
                        "row_indices": [],
                    }
                failure_map[key]["count"] += 1
                failure_map[key]["row_indices"].append(o.row_index)
        failure_groups = sorted(failure_map.values(), key=lambda g: g["count"], reverse=True)

        orders_data = []
        for i, o in enumerate(self.orders):
            order_dict: dict[str, Any] = {
                "row_index": o.row_index,
                "db_order_id": o.db_order_id,
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
                "retry_count": o.retry_count,
                "failure_category": o.failure_category,
                "input_tokens": o.input_tokens,
                "output_tokens": o.output_tokens,
                "duration_ms": o.duration_ms,
            }
            # Only send screenshot/reasoning for the active order
            if self.active_order_index is not None and i == self.active_order_index:
                order_dict["last_screenshot_b64"] = o.last_screenshot_b64
                order_dict["last_reasoning"] = o.last_reasoning
            else:
                order_dict["last_screenshot_b64"] = ""
                order_dict["last_reasoning"] = ""
            orders_data.append(order_dict)

        total_input_tokens = sum(o.input_tokens for o in self.orders)
        total_output_tokens = sum(o.output_tokens for o in self.orders)
        total_duration_ms = sum(o.duration_ms for o in self.orders)

        return {
            "started": self.started,
            "session_id": self.session_id,
            "browser_status": self.browser_status,
            "login_status": self.login_status,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_duration_ms": total_duration_ms,
            "otp_requested": self.otp_requested,
            "logs": list(self.logs),
            "orders": orders_data,
            "summary": summary,
            "failure_groups": failure_groups,
            "batch_info": {
                "current": self.current_batch,
                "total": self.total_batches,
                "batch_size": self.batch_size,
            },
            "completed": self.completed,
            "max_retries": self.max_retries,
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
