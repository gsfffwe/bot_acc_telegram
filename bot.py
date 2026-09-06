"""Bot Telegram bán tài khoản độc lập, dùng danh mục sản phẩm của website.

Khách chỉ cần Telegram, không cần đăng nhập website:
    /start -> chọn sản phẩm -> nạp tiền -> bot tự động xử lý và gửi tài khoản.

Bot gọi backend website bằng shared secret server-to-server. Không đặt bot token,
Firebase secret hoặc API key nhà cung cấp vào mã nguồn.
"""

from __future__ import annotations

import asyncio
import html
import hmac
import logging
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    URLInputFile,
)
from dotenv import load_dotenv
from fastapi import FastAPI, Request


# ---------------------------------------------------------------------------
# Cấu hình
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEB_API_BASE_URL = os.getenv("WEB_API_BASE_URL", "").strip().rstrip("/")
TELEGRAM_BOT_SHARED_SECRET = os.getenv("TELEGRAM_BOT_SHARED_SECRET", "").strip()


def parse_admin_ids() -> set[int]:
    raw_values = [os.getenv("ADMIN_TELEGRAM_ID", ""), os.getenv("ADMIN_TELEGRAM_IDS", "")]
    admin_ids: set[int] = set()
    for raw_value in raw_values:
        for value in re.split(r"[,;\s]+", raw_value or ""):
            value = value.strip().strip("'\"")
            if value.isdigit():
                admin_ids.add(int(value))
    return admin_ids


ADMIN_TELEGRAM_IDS = parse_admin_ids()
ADMIN_TELEGRAM_ID = min(ADMIN_TELEGRAM_IDS) if ADMIN_TELEGRAM_IDS else 0
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", str(BASE_DIR)))
DB_PATH = DATA_DIR / "telegram_shop.sqlite3"
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_TOKEN = os.getenv("SEPAY_WEBHOOK_TOKEN", "").strip()
SEPAY_FORWARD_URL = os.getenv("SEPAY_FORWARD_URL", "").strip().rstrip("/")

CATALOG_PAGE_SIZE = max(1, min(20, int(os.getenv("CATALOG_PAGE_SIZE", "8"))))
MAX_QUANTITY = max(1, min(100, int(os.getenv("MAX_QUANTITY", "100"))))
MIN_DEPOSIT = max(0, int(os.getenv("MIN_DEPOSIT", "0")))
DEPOSIT_WATCH_SECONDS = max(60, int(os.getenv("DEPOSIT_WATCH_SECONDS", "900")))
DEPOSIT_POLL_SECONDS = max(3, int(os.getenv("DEPOSIT_POLL_SECONDS", "5")))
SUPPORT_HANDLE = os.getenv("SUPPORT_TELEGRAM", "@tai_khoan_xin").strip() or "@tai_khoan_xin"
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("telegram-account-shop")


class DepositStates(StatesGroup):
    amount = State()


class QuantityStates(StatesGroup):
    quantity = State()


class AdminStates(StatesGroup):
    broadcast = State()
    confirm_broadcast = State()


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class WebApi:
    """Client gọi các endpoint API đã thêm vào web_test."""

    def __init__(self, base_url: str, shared_secret: str):
        self.base_url = base_url.rstrip("/")
        self.shared_secret = shared_secret
        self.client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "User-Agent": "TaiKhoanXin-TelegramBot/2.0",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    def headers(self, telegram_id: int | str | None = None) -> dict[str, str]:
        headers = {
            "X-Telegram-Bot-Secret": self.shared_secret,
        }
        if telegram_id is not None:
            headers["X-Telegram-User-Id"] = str(telegram_id)
        return headers

    async def request(
        self,
        method: str,
        path: str,
        telegram_id: int | str | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise ApiError(500, "BOT_CONFIG_MISSING", "Chưa cấu hình WEB_API_BASE_URL.")
        if not self.shared_secret:
            raise ApiError(500, "BOT_CONFIG_MISSING", "Chưa cấu hình TELEGRAM_BOT_SHARED_SECRET.")

        try:
            response = await self.client.request(
                method,
                f"{self.base_url}/{path.lstrip('/')}",
                headers=self.headers(telegram_id),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ApiError(503, "WEB_UNAVAILABLE", "Không kết nối được website, vui lòng thử lại.") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise ApiError(502, "WEB_INVALID_RESPONSE", "Website trả về dữ liệu không hợp lệ.") from exc
        if not isinstance(body, dict):
            raise ApiError(502, "WEB_INVALID_RESPONSE", "Website trả về dữ liệu không hợp lệ.")

        if response.status_code < 200 or response.status_code >= 300 or body.get("success") is False:
            raise ApiError(
                response.status_code,
                str(body.get("code") or "WEB_REQUEST_FAILED"),
                str(body.get("error") or "Website không thể xử lý yêu cầu."),
            )
        data = body.get("data", body)
        return data if isinstance(data, dict) else {"value": data}

    async def catalog(self, telegram_id: int) -> dict[str, Any]:
        return await self.request("GET", "/user/catalog", telegram_id)

    async def balance(self, telegram_id: int) -> dict[str, Any]:
        return await self.request("GET", "/user/balance", telegram_id)

    async def orders(self, telegram_id: int) -> dict[str, Any]:
        return await self.request("GET", "/user/orders", telegram_id)

    async def checkout(self, telegram_id: int, product_id: str, order_id: str, quantity: int) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/provider/checkout",
            telegram_id,
            payload={"productId": product_id, "orderId": order_id, "quantity": quantity},
        )

    async def order_detail(self, telegram_id: int, order_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/provider/order/{order_id}", telegram_id)

    async def admin_stats(self, telegram_id: int) -> dict[str, Any]:
        return await self.request("GET", "/telegram/admin/stats", telegram_id)

    async def create_deposit(self, telegram_id: int, amount: int) -> dict[str, Any]:
        return await self.request("POST", "/telegram/deposit", telegram_id, payload={"amount": amount})

    async def deposit_status(self, telegram_id: int, memo: str) -> dict[str, Any]:
        return await self.request("GET", f"/telegram/deposit/{memo}", telegram_id)

    async def confirm_deposit(self, telegram_id: int, memo: str, amount: int, transaction_id: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/telegram/deposit/confirm",
            telegram_id,
            payload={"memo": memo, "amount": amount, "transactionId": transaction_id},
        )

    async def confirm_deposit_by_memo(self, memo: str, amount: int, transaction_id: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/telegram/deposit/confirm-by-memo",
            None,
            payload={"memo": memo, "amount": amount, "transactionId": transaction_id},
        )


@dataclass
class CatalogMenu:
    key: str
    products: list[dict[str, Any]]
    discount_percent: int
    created_at: float


@dataclass
class PendingPurchase:
    product_id: str
    product_name: str
    quantity: int
    order_id: str
    expected_total: int


api = WebApi(WEB_API_BASE_URL, TELEGRAM_BOT_SHARED_SECRET)
bot: Bot | None = None
dp = Dispatcher()
app = FastAPI(title="Telegram account shop webhook")

menus: dict[int, CatalogMenu] = {}
purchase_attempts: dict[tuple[int, str, int, int], str] = {}
pending_purchases: dict[int, PendingPurchase] = {}
purchase_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
deposit_watch_tasks: dict[tuple[int, str], asyncio.Task[Any]] = {}
deposit_notified: set[tuple[int, str]] = set()
admin_notified_orders: set[str] = set()


# ---------------------------------------------------------------------------
# SQLite: chỉ lưu trạng thái/menu không nhạy cảm; ví nằm ở Firebase backend
# ---------------------------------------------------------------------------

def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_users(
                telegram_id INTEGER PRIMARY KEY,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL
            )
            """
        )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(telegram_users)").fetchall()}
        if "display_name" not in columns:
            conn.execute("ALTER TABLE telegram_users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        if "username" not in columns:
            conn.execute("ALTER TABLE telegram_users ADD COLUMN username TEXT NOT NULL DEFAULT ''")


def touch_user(telegram_id: int, display_name: str = "", username: str = "") -> None:
    now = int(time.time())
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute(
            """
            INSERT INTO telegram_users(telegram_id, first_seen_at, last_seen_at, display_name, username)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                display_name = CASE WHEN excluded.display_name <> '' THEN excluded.display_name ELSE telegram_users.display_name END,
                username = CASE WHEN excluded.username <> '' THEN excluded.username ELSE telegram_users.username END
            """,
            (telegram_id, now, now, str(display_name or "").strip(), str(username or "").strip()),
        )


def remember_user(user: Any) -> None:
    """Lưu tên hiển thị tối thiểu để admin dễ nhận diện khách trong bot."""
    if not user or getattr(user, "id", None) is None:
        return
    touch_user(
        int(user.id),
        str(getattr(user, "full_name", "") or ""),
        str(getattr(user, "username", "") or ""),
    )


def local_users() -> dict[int, dict[str, Any]]:
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        rows = conn.execute(
            "SELECT telegram_id, display_name, username, last_seen_at FROM telegram_users ORDER BY last_seen_at DESC"
        ).fetchall()
    return {
        int(row[0]): {
            "displayName": str(row[1] or ""),
            "username": str(row[2] or ""),
            "lastSeenAt": row[3],
        }
        for row in rows
    }


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

UI_DIVIDER = "━━━━━━━━━━━━━━"

def money(value: Any) -> str:
    try:
        amount = int(round(float(value or 0)))
    except (TypeError, ValueError):
        amount = 0
    return f"{amount:,}".replace(",", ".") + "đ"


def deposit_requirement_text() -> str:
    if MIN_DEPOSIT <= 0:
        return "không giới hạn tối thiểu, số tiền phải lớn hơn 0"
    return f"tối thiểu <b>{money(MIN_DEPOSIT)}</b>"


def new_order_id() -> str:
    """Tạo mã đơn ngắn, không làm lộ Telegram ID của khách."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "DH-" + "".join(secrets.choice(alphabet) for _ in range(8))


def support_url() -> str:
    return f"https://t.me/{SUPPORT_HANDLE.lstrip('@')}"


def support_link() -> str:
    """Liên kết mở thẳng cuộc trò chuyện hỗ trợ, không dùng dạng mã sao chép."""
    return (
        f'<a href="{html.escape(support_url(), quote=True)}">'
        f"{html.escape(SUPPORT_HANDLE)}"
        "</a>"
    )


def support_line() -> str:
    return f"🆘 Hỗ trợ: {support_link()}"


def order_status_icon(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    if any(value in normalized for value in ("hoàn thành", "thành công", "completed", "success")):
        return "✅"
    if any(value in normalized for value in ("hủy", "huỷ", "lỗi", "failed", "cancel")):
        return "❌"
    return "⏳"


def short_text(value: Any, length: int = 30) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= length else text[: max(1, length - 1)] + "…"


def product_warranty(product: dict[str, Any]) -> str:
    """Chuẩn hóa bảo hành theo cùng cách hiển thị của website."""
    if not product:
        return "Không bảo hành"

    raw_warranty = str(product.get("warranty") or "").strip()
    normalized = raw_warranty.casefold()
    if raw_warranty and normalized not in {"không bảo hành", "khong bao hanh", "none", "no", "-"}:
        if re.match(r"^bảo hành\b", raw_warranty, re.IGNORECASE):
            return raw_warranty
        return f"Bảo hành {raw_warranty}"

    try:
        warranty_days = float(product.get("warrantyDays") or 0)
    except (TypeError, ValueError):
        warranty_days = 0
    if warranty_days > 0:
        days = int(warranty_days) if warranty_days.is_integer() else warranty_days
        return f"Bảo hành {days:g} ngày"

    desc = str(product.get("desc") or "")
    match = re.search(r"bảo hành\s*[:\-]?\s*([^.,;\n]+)", desc, re.IGNORECASE)
    if match:
        value = match.group(0).strip()
        return value if re.match(r"^bảo hành\b", value, re.IGNORECASE) else f"Bảo hành {value}"
    return "Không bảo hành"


def vi_time(timestamp: Any) -> str:
    try:
        from datetime import datetime, timedelta

        dt = datetime.utcfromtimestamp(float(timestamp) / 1000) + timedelta(hours=7)
        return dt.strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "Không rõ thời gian"


def timestamp_value(timestamp: Any) -> float:
    try:
        return float(timestamp or 0)
    except (TypeError, ValueError):
        return 0.0


def is_admin(telegram_id: int | str) -> bool:
    try:
        return int(telegram_id) in ADMIN_TELEGRAM_IDS
    except (TypeError, ValueError):
        return False


def main_keyboard(telegram_id: int | None = None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📦 Sản phẩm"), KeyboardButton(text="💰 Số dư")],
        [KeyboardButton(text="💳 Nạp tiền"), KeyboardButton(text="🧾 Đơn hàng")],
        [KeyboardButton(text="ℹ️ Trợ giúp"), KeyboardButton(text="🆘 Hỗ trợ")],
    ]
    if telegram_id is not None and is_admin(telegram_id):
        rows.append([KeyboardButton(text="🛠 Quản trị")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Chọn chức năng…",
    )


def back_to_products_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Xem sản phẩm", callback_data="products")],
            [InlineKeyboardButton(text="🏠 Trang chủ", callback_data="home")],
        ]
    )


def home_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Xem sản phẩm", callback_data="products")],
            [
                InlineKeyboardButton(text="💳 Nạp tiền", callback_data="deposit"),
                InlineKeyboardButton(text="💰 Số dư", callback_data="balance"),
            ],
            [
                InlineKeyboardButton(text="🧾 Đơn hàng", callback_data="orders"),
                InlineKeyboardButton(text="ℹ️ Hướng dẫn", callback_data="help"),
            ],
            [InlineKeyboardButton(text="🆘 Liên hệ hỗ trợ", url=support_url())],
        ]
    )


def utility_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Sản phẩm", callback_data="products"),
                InlineKeyboardButton(text="💳 Nạp tiền", callback_data="deposit"),
            ],
            [
                InlineKeyboardButton(text="🧾 Đơn hàng", callback_data="orders"),
                InlineKeyboardButton(text="🏠 Trang chủ", callback_data="home"),
            ],
        ]
    )


def after_purchase_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Mua tiếp", callback_data="products"),
                InlineKeyboardButton(text="💰 Số dư", callback_data="balance"),
            ],
            [
                InlineKeyboardButton(text="🧾 Đơn hàng", callback_data="orders"),
                InlineKeyboardButton(text="🏠 Trang chủ", callback_data="home"),
            ],
            [InlineKeyboardButton(text="🆘 Hỗ trợ", url=support_url())],
        ]
    )


def menu_for_user(user_id: int) -> CatalogMenu | None:
    menu = menus.get(user_id)
    if not menu or time.time() - menu.created_at > 30 * 60:
        return None
    return menu


def catalog_keyboard(menu: CatalogMenu, page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(menu.products) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * CATALOG_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    for index, product in enumerate(menu.products[start : start + CATALOG_PAGE_SIZE], start=start):
        stock = int(product.get("quantity") or 0)
        stock_text = f"🟢 {stock}" if stock > 0 else "🔴 hết"
        duration = short_text(product.get("duration") or "Dùng ngay", 14)
        label = f"{short_text(product.get('name'), 20)} · {money(product.get('finalPrice', product.get('price')))} · {stock_text}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"product|{menu.key}|{index}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹ Trước", callback_data=f"catalog|{menu.key}|{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="Sau ›", callback_data=f"catalog|{menu.key}|{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(text="🔄 Làm mới", callback_data="products"),
            InlineKeyboardButton(text="🏠 Trang chủ", callback_data="home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_text(product: dict[str, Any], discount_percent: int) -> str:
    name = html.escape(str(product.get("name") or "Sản phẩm"))
    desc = html.escape(str(product.get("desc") or "").strip())
    duration = html.escape(str(product.get("duration") or "Dùng ngay"))
    fmt = html.escape(str(product.get("format") or "Theo mô tả sản phẩm"))
    warranty = html.escape(product_warranty(product))
    stock = int(product.get("quantity") or 0)
    price = money(product.get("finalPrice", product.get("price")))
    old_price = money(product.get("price"))
    price_line = f"💵 Giá: <b>{price}</b>"
    if discount_percent > 0 and int(product.get("finalPrice") or 0) != int(product.get("price") or 0):
        price_line += f" <s>{old_price}</s> (-{discount_percent}%)"
    lines = [
        f"📦 <b>{name}</b>",
        UI_DIVIDER,
        "",
        price_line,
        f"📦 Tình trạng: <b>{stock} tài khoản</b>" if stock > 0 else "📦 Tình trạng: <b>Hết hàng</b>",
        f"⏳ Thời hạn: {duration}",
        f"🧾 Định dạng: <code>{fmt}</code>",
        f"🛡 Bảo hành: {warranty}",
    ]
    if desc:
        lines.extend(["", desc])
    return "\n".join(lines)


def product_detail_keyboard(menu: CatalogMenu, index: int, product: dict[str, Any]) -> InlineKeyboardMarkup:
    stock = int(product.get("quantity") or 0)
    rows: list[list[InlineKeyboardButton]] = []
    if stock > 0:
        rows.append(
            [
                InlineKeyboardButton(text="🛒 Mua ngay", callback_data=f"payment|{menu.key}|{index}|1"),
                InlineKeyboardButton(text="🔢 Số lượng", callback_data=f"quantity|{menu.key}|{index}"),
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="⚠️ Hết hàng", callback_data=f"catalog|{menu.key}|{index // CATALOG_PAGE_SIZE}")])
    rows.append(
        [
            InlineKeyboardButton(text="💳 Nạp tiền", callback_data="deposit"),
            InlineKeyboardButton(text="🏠 Trang chủ", callback_data="home"),
        ]
    )
    rows.append([InlineKeyboardButton(text="‹ Quay lại danh sách", callback_data=f"catalog|{menu.key}|{index // CATALOG_PAGE_SIZE}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_method_keyboard(menu: CatalogMenu, index: int, quantity: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 Thanh toán bằng số dư", callback_data=f"pay_balance|{menu.key}|{index}|{quantity}")],
            [InlineKeyboardButton(text="📲 Thanh toán QR nhanh", callback_data=f"pay_qr|{menu.key}|{index}|{quantity}")],
            [InlineKeyboardButton(text="‹ Quay lại sản phẩm", callback_data=f"product|{menu.key}|{index}")],
        ]
    )


async def answer_in_chunks(message: Message, text: str, reply_markup: Any = None) -> None:
    chunks = split_text_lines(text)
    for index, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=reply_markup if index == len(chunks) - 1 else None)


def split_text_lines(text: str, max_length: int = 3900) -> list[str]:
    """Chia văn bản theo dòng để không cắt giữa thẻ HTML của Telegram."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > max_length:
            chunks.append(current.rstrip("\n"))
            current = ""
        if len(line) > max_length:
            for start in range(0, len(line), max_length):
                part = line[start : start + max_length]
                if current:
                    chunks.append(current.rstrip("\n"))
                    current = ""
                chunks.append(part.rstrip("\n"))
        else:
            current += line
    if current:
        chunks.append(current.rstrip("\n"))
    return chunks or [""]


async def handle_api_error(target: Message | CallbackQuery, exc: ApiError) -> None:
    message = f"⚠️ {html.escape(public_error_message(exc))}"
    if isinstance(target, CallbackQuery):
        if target.message:
            await target.message.answer(message)
        await target.answer("Có lỗi, vui lòng thử lại.")
    else:
        await target.answer(message)


async def notify_admin(text: str) -> None:
    global bot
    if not bot or not ADMIN_TELEGRAM_IDS:
        return
    for admin_id in sorted(ADMIN_TELEGRAM_IDS):
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            LOGGER.exception("Could not notify admin %s", admin_id)


def public_error_message(exc: ApiError) -> str:
    """Ẩn chi tiết kỹ thuật nội bộ khỏi tin nhắn gửi cho khách."""
    if exc.code.startswith("PROVIDER_") or exc.code in {
        "PRODUCT_NOT_PROVIDER",
        "PROVIDER_DISABLED",
    }:
        return "Sản phẩm tạm thời chưa thể xử lý. Vui lòng thử lại sau hoặc liên hệ hỗ trợ.\n\n" + support_line()
    return exc.message


def admin_error_message(exc: ApiError) -> str:
    """Thông báo nội bộ đã rút gọn, không đẩy giá vốn/phản hồi thô lên Telegram."""
    if exc.code == "PROVIDER_PURCHASE_UNCERTAIN":
        return "Hệ thống nhận phản hồi không rõ từ bên giao hàng; đơn cần được kiểm tra thủ công."
    if exc.code.startswith("PROVIDER_"):
        return "Đơn không thể hoàn tất tự động; tiền đã được hoàn về số dư khách."
    return "Yêu cầu thất bại; vui lòng kiểm tra log hệ thống để biết chi tiết."


def admin_error_code(exc: ApiError) -> str:
    if exc.code == "PROVIDER_PURCHASE_UNCERTAIN":
        return "ORDER_REVIEW"
    if exc.code.startswith("PROVIDER_"):
        return "DELIVERY_FAILED"
    return "SYSTEM_ERROR"


async def notify_admin_order_with_accounts(
    telegram_id: int,
    order_id: str,
    product_name: str,
    quantity: int,
    total: Any,
    delivery_text: str,
) -> None:
    """Gửi thông tin đơn và toàn bộ tài khoản đã cấp cho admin."""
    await notify_admin(
        "🛒 <b>ĐƠN HÀNG HOÀN TẤT</b>\n\n"
        f"👤 Telegram ID: <code>{telegram_id}</code>\n"
        f"🧾 Mã đơn: <code>{html.escape(order_id)}</code>\n"
        f"📦 Sản phẩm: <b>{html.escape(product_name or 'Sản phẩm')}</b>\n"
        f"🔢 Số lượng: <b>{quantity}</b>\n"
        f"💵 Thanh toán: <b>{money(total)}</b>"
    )
    escaped = html.escape(delivery_text[:12000])
    for chunk_start in range(0, len(escaped), 3700):
        await notify_admin(f"🔐 <b>THÔNG TIN ĐÃ CẤP</b>\n<pre>{escaped[chunk_start : chunk_start + 3700]}</pre>")


async def fetch_order_detail(telegram_id: int, order_id: str) -> dict[str, Any]:
    detail: dict[str, Any] | None = None
    for attempt in range(4):
        try:
            detail = await api.order_detail(telegram_id, order_id)
            if detail.get("status") == "Hoàn thành" or detail.get("deliveredAccounts"):
                break
        except ApiError:
            if attempt == 3:
                raise
            await asyncio.sleep(0.35)
    return detail or {}


async def send_purchase_result(
    telegram_id: int,
    result: dict[str, Any],
    detail: dict[str, Any],
    fallback_product_name: str,
    expected_total: int,
    order_id: str,
    initial_message: Message | None = None,
) -> bool:
    """Gửi kết quả mua hàng, trả về True khi đã có dữ liệu tài khoản."""
    global bot
    status = str(detail.get("status") or result.get("status") or "Đang xử lý")
    accounts = detail.get("deliveredAccounts")
    if not isinstance(accounts, list):
        accounts = []
    account_details = str(detail.get("accountDetails") or "").strip()
    has_delivery = bool(accounts) or (status == "Hoàn thành" and bool(account_details))
    if not has_delivery:
        text = (
            "🧾 <b>ĐƠN ĐÃ GHI NHẬN</b>\n\n"
            f"Mã đơn: <code>{html.escape(order_id)}</code>\n"
            f"Trạng thái: <b>{html.escape(status)}</b>\n\n"
            "Hệ thống chưa trả thông tin tài khoản ngay lúc này. Vui lòng xem lại trong mục Đơn hàng.\n\n"
            f"{support_line()}"
        )
        if initial_message:
            await initial_message.edit_text(text, reply_markup=after_purchase_keyboard())
        elif bot:
            await bot.send_message(telegram_id, text, reply_markup=after_purchase_keyboard())
        return False

    if accounts:
        delivery_text = "\n".join(f"[{i}] {str(value)}" for i, value in enumerate(accounts, start=1))
    else:
        delivery_text = account_details
    delivery_text = delivery_text[:12000]
    escaped_delivery_text = html.escape(delivery_text)
    summary = (
        "✅ <b>MUA TÀI KHOẢN THÀNH CÔNG</b>\n\n"
        f"🧾 Mã đơn: <code>{html.escape(order_id)}</code>\n"
        f"📦 Sản phẩm: <b>{html.escape(str(detail.get('productName') or fallback_product_name or 'Sản phẩm'))}</b>\n"
        f"🔢 Số lượng: <b>{int(detail.get('deliveredQuantity') or len(accounts))}</b>\n"
        f"💵 Thanh toán: <b>{money(detail.get('price') or result.get('totalAmount') or expected_total)}</b>\n\n"
        "🔐 <b>THÔNG TIN TÀI KHOẢN</b>\n"
        f"{support_line()}"
    )
    if initial_message:
        await initial_message.edit_text(summary, reply_markup=after_purchase_keyboard())
        target = initial_message
        for chunk_start in range(0, len(escaped_delivery_text), 3800):
            await target.answer(f"<pre>{escaped_delivery_text[chunk_start : chunk_start + 3800]}</pre>")
    elif bot:
        await bot.send_message(telegram_id, summary, reply_markup=after_purchase_keyboard())
        for chunk_start in range(0, len(escaped_delivery_text), 3800):
            await bot.send_message(telegram_id, f"<pre>{escaped_delivery_text[chunk_start : chunk_start + 3800]}</pre>")
    if order_id not in admin_notified_orders:
        admin_notified_orders.add(order_id)
        await notify_admin_order_with_accounts(
            telegram_id,
            order_id,
            str(detail.get('productName') or fallback_product_name or 'Sản phẩm'),
            int(detail.get('deliveredQuantity') or len(accounts)),
            detail.get('price') or result.get('totalAmount') or expected_total,
            delivery_text,
        )
    return True


async def resume_pending_purchase(telegram_id: int, pending: PendingPurchase) -> None:
    global bot
    try:
        if bot:
            await bot.send_message(
                telegram_id,
                f"⏳ Đã đủ số dư. Bot đang tự động xử lý <b>{html.escape(pending.product_name)}</b>…",
            )
        result = await api.checkout(telegram_id, pending.product_id, pending.order_id, pending.quantity)
        detail = await fetch_order_detail(telegram_id, pending.order_id)
        await send_purchase_result(
            telegram_id,
            result,
            detail,
            pending.product_name,
            pending.expected_total,
            pending.order_id,
        )
    except ApiError as exc:
        LOGGER.warning(
            "Checkout failed for order %s (%s): %s",
            pending.order_id,
            exc.code,
            exc.message,
        )
        await notify_admin(
            "⚠️ <b>ĐƠN HÀNG CẦN KIỂM TRA</b>\n\n"
            f"👤 Telegram ID: <code>{telegram_id}</code>\n"
            f"🧾 Mã đơn: <code>{html.escape(pending.order_id)}</code>\n"
            f"📌 Mã xử lý: <code>{admin_error_code(exc)}</code>\n"
            f"{html.escape(admin_error_message(exc))}"
        )
        if bot:
            if exc.code == "PROVIDER_PURCHASE_UNCERTAIN":
                await bot.send_message(
                    telegram_id,
                    f"⚠️ Đơn <code>{html.escape(pending.order_id)}</code> cần admin kiểm tra. Hệ thống đã khóa mua lại để tránh trừ tiền hai lần.",
                )
            else:
                await bot.send_message(telegram_id, f"❌ Không thể hoàn tất đơn: {html.escape(public_error_message(exc))}")
    except Exception:
        LOGGER.exception("Automatic pending purchase failed")
        await notify_admin(
            "⚠️ <b>XỬ LÝ ĐƠN HÀNG GẶP LỖI</b>\n\n"
            f"👤 Telegram ID: <code>{telegram_id}</code>\n"
            f"🧾 Mã đơn: <code>{html.escape(pending.order_id)}</code>"
        )
        if bot:
            await bot.send_message(telegram_id, "❌ Có lỗi khi xử lý đơn hàng. Vui lòng kiểm tra mục Đơn hàng.")


def parse_amount(text: str) -> int:
    digits = re.sub(r"[^0-9]", "", text or "")
    return int(digits) if digits else 0


# ---------------------------------------------------------------------------
# Catalog / ví / đơn hàng
# ---------------------------------------------------------------------------

def catalog_text(menu: CatalogMenu, page: int) -> str:
    total_pages = max(1, (len(menu.products) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    return (
        "📦 <b>DANH MỤC SẢN PHẨM</b>\n"
        f"{UI_DIVIDER}\n"
        f"🛒 {len(menu.products)} sản phẩm · Trang <b>{page + 1}/{total_pages}</b>\n\n"
        "Chọn sản phẩm để xem thông tin, thời hạn và mua tự động."
    )


async def show_home(
    message: Message,
    *,
    edit: bool = False,
    telegram_id: int | None = None,
    display_name: str | None = None,
) -> None:
    user_id = telegram_id or message.from_user.id
    if telegram_id is None:
        remember_user(message.from_user)
    else:
        touch_user(user_id, display_name or "")
    balance_label = "Đang cập nhật"
    try:
        data = await api.balance(user_id)
        balance_label = money(data.get("balance"))
    except ApiError as exc:
        LOGGER.warning("Could not load balance for home screen: %s", exc.code)

    name = html.escape(short_text(display_name or message.from_user.full_name or "bạn", 32))
    text = (
        "🛍 <b>SHOP TÀI KHOẢN</b>\n"
        f"{UI_DIVIDER}\n"
        f"👋 Xin chào <b>{name}</b>\n\n"
        f"💰 Số dư hiện tại: <b>{balance_label}</b>\n"
        "⚡ Giao thông tin tự động sau khi thanh toán\n"
        "🔒 Đơn hàng và thông tin tài khoản được bảo mật\n\n"
        "Chọn một chức năng bên dưới để bắt đầu.\n\n"
        f"{support_line()}"
    )
    if edit:
        await message.edit_text(text, reply_markup=home_inline_keyboard())
    else:
        await message.answer(text, reply_markup=main_keyboard(user_id))

async def show_catalog(message: Message, *, edit: bool = False, telegram_id: int | None = None) -> None:
    try:
        user_id = telegram_id or message.from_user.id
        if telegram_id is None:
            remember_user(message.from_user)
        else:
            touch_user(user_id)
        data = await api.catalog(user_id)
        products = [item for item in data.get("products", []) if isinstance(item, dict)]
        menu = CatalogMenu(
            key=secrets.token_hex(3),
            products=products,
            discount_percent=int(data.get("discountPercent") or 0),
            created_at=time.time(),
        )
        menus[user_id] = menu
        if not products:
            text = (
                "📦 <b>CHƯA CÓ SẢN PHẨM</b>\n"
                f"{UI_DIVIDER}\n"
                "Hiện chưa có sản phẩm phù hợp để hiển thị. Vui lòng thử làm mới sau."
            )
            if edit:
                await message.edit_text(text, reply_markup=back_to_products_keyboard())
            else:
                await message.answer(text, reply_markup=main_keyboard(user_id))
            return
        text = catalog_text(menu, 0)
        markup = catalog_keyboard(menu, 0)
        if edit:
            await message.edit_text(text, reply_markup=markup)
        else:
            await message.answer(text, reply_markup=markup)
    except ApiError as exc:
        await handle_api_error(message, exc)


@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_home(message)


@dp.message(Command("home"))
async def home_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_home(message)


@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Trợ giúp")
async def help_handler(message: Message) -> None:
    remember_user(message.from_user)
    await message.answer(
        "ℹ️ <b>HƯỚNG DẪN MUA HÀNG</b>\n"
        f"{UI_DIVIDER}\n"
        "1️⃣ Mở 📦 <b>Sản phẩm</b> và chọn gói bạn cần.\n"
        "2️⃣ Bấm <b>Mua ngay</b> hoặc nhập số lượng.\n"
        "3️⃣ Nếu chưa đủ số dư, mở 💳 <b>Nạp tiền</b> và chuyển khoản đúng số tiền/nội dung.\n"
        "4️⃣ Hệ thống tự xác nhận, xử lý đơn và gửi thông tin tài khoản.\n\n"
        "Bạn có thể dùng các nút menu hoặc lệnh /products, /deposit, /balance, /orders.\n\n"
        f"{support_line()}",
        reply_markup=utility_keyboard(),
    )


@dp.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "ℹ️ <b>HƯỚNG DẪN MUA HÀNG</b>\n"
            f"{UI_DIVIDER}\n"
            "1️⃣ Chọn sản phẩm và kiểm tra thời hạn, số lượng còn lại.\n"
            "2️⃣ Bấm <b>Mua ngay</b> hoặc nhập số lượng cần mua.\n"
            "3️⃣ Nạp tiền bằng QR nếu số dư chưa đủ.\n"
            "4️⃣ Sau khi thanh toán, bot tự xử lý và gửi thông tin đơn hàng.\n\n"
            f"{support_line()}",
            reply_markup=home_inline_keyboard(),
        )


@dp.message(Command("support"))
@dp.message(F.text == "🆘 Hỗ trợ")
async def support_handler(message: Message) -> None:
    remember_user(message.from_user)
    await message.answer(
        "🆘 <b>HỖ TRỢ KHÁCH HÀNG</b>\n"
        f"{UI_DIVIDER}\n"
        "Nếu cần kiểm tra đơn, nạp tiền hoặc gặp lỗi khi nhận hàng, hãy liên hệ hỗ trợ và gửi mã đơn cho admin.\n\n"
        f"{support_line()}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💬 Mở chat hỗ trợ", url=support_url())],
                [InlineKeyboardButton(text="🏠 Trang chủ", callback_data="home")],
            ]
        ),
    )


@dp.message(Command("products"))
@dp.message(F.text == "📦 Sản phẩm")
async def products_handler(message: Message) -> None:
    await show_catalog(message)


@dp.message(Command("balance"))
@dp.message(F.text == "💰 Số dư")
async def balance_handler(message: Message, telegram_id: int | None = None) -> None:
    try:
        user_id = telegram_id or message.from_user.id
        if telegram_id is None:
            remember_user(message.from_user)
        else:
            touch_user(user_id)
        data = await api.balance(user_id)
        await message.answer(
            "💰 <b>SỐ DƯ CỦA BẠN</b>\n"
            f"{UI_DIVIDER}\n"
            f"Ví hiện tại: <b>{money(data.get('balance'))}</b>\n\n"
            "Bạn có thể dùng số dư này để mua sản phẩm trong shop.",
            reply_markup=utility_keyboard(),
        )
    except ApiError as exc:
        await handle_api_error(message, exc)


@dp.message(Command("orders"))
@dp.message(F.text == "🧾 Đơn hàng")
async def orders_handler(message: Message, telegram_id: int | None = None) -> None:
    try:
        user_id = telegram_id or message.from_user.id
        if telegram_id is None:
            remember_user(message.from_user)
        else:
            touch_user(user_id)
        data = await api.orders(user_id)
        orders = [item for item in data.get("orders", []) if isinstance(item, dict)]
        if not orders:
            await message.answer(
                "🧾 <b>LỊCH SỬ ĐƠN HÀNG</b>\n"
                f"{UI_DIVIDER}\n"
                "Bạn chưa có đơn hàng nào.",
                reply_markup=utility_keyboard(),
            )
            return
        lines = ["🧾 <b>LỊCH SỬ ĐƠN HÀNG</b>", UI_DIVIDER, ""]
        for index, order in enumerate(orders[:10], start=1):
            status = str(order.get("status") or "Đang xử lý")
            lines.append(
                f"<b>{index}.</b> <code>{html.escape(str(order.get('orderId') or ''))}</code>\n"
                f"📦 {html.escape(str(order.get('productName') or 'Sản phẩm'))}\n"
                f"💵 {money(order.get('price'))} · SL {int(order.get('quantity') or 1)}\n"
                f"{order_status_icon(status)} {html.escape(status)} · {vi_time(order.get('timestamp'))}\n"
            )
        await answer_in_chunks(message, "\n".join(lines), main_keyboard(user_id))
    except ApiError as exc:
        await handle_api_error(message, exc)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Làm mới thống kê", callback_data="admin_dashboard")],
            [InlineKeyboardButton(text="👥 Danh sách khách hàng", callback_data="admin_users")],
            [InlineKeyboardButton(text="📣 Gửi thông báo", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🏠 Trang chủ", callback_data="home")],
        ]
    )


def admin_broadcast_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Gửi đến khách hàng", callback_data="admin_broadcast_confirm"),
                InlineKeyboardButton(text="❌ Hủy", callback_data="admin_broadcast_cancel"),
            ]
        ]
    )


async def begin_admin_broadcast(message: Message, state: FSMContext, admin_id: int) -> None:
    if not is_admin(admin_id):
        await message.answer(
            "Bạn không có quyền sử dụng chức năng gửi thông báo.",
            reply_markup=main_keyboard(admin_id),
        )
        return
    await state.clear()
    await state.set_state(AdminStates.broadcast)
    await message.answer(
        "📣 <b>GỬI THÔNG BÁO CHO KHÁCH</b>\n"
        f"{UI_DIVIDER}\n"
        "Gửi một trong các dạng sau:\n"
        "• Tin nhắn văn bản\n"
        "• Ảnh kèm hoặc không kèm chú thích\n"
        "• Video kèm hoặc không kèm chú thích\n\n"
        "Bot sẽ hiển thị bản xem trước để bạn xác nhận trước khi gửi.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Hủy", callback_data="admin_broadcast_cancel")]]
        ),
    )


async def admin_dashboard_text(admin_id: int) -> str:
    data = await api.admin_stats(admin_id)
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    customers = [item for item in data.get("customers", []) if isinstance(item, dict)]
    lines = [
        "🛠 <b>QUẢN TRỊ SHOP</b>",
        UI_DIVIDER,
        f"👥 Khách hàng: <b>{int(summary.get('customers') or 0)}</b>",
        f"🧾 Tổng đơn: <b>{int(summary.get('orders') or 0)}</b>",
        f"✅ Đơn hoàn tất: <b>{int(summary.get('completedOrders') or 0)}</b>",
        f"💰 Doanh số: <b>{money(summary.get('revenue'))}</b>",
        f"💳 Lượt nạp thành công: <b>{int(summary.get('deposits') or 0)}</b>",
        f"📥 Tổng tiền đã nạp: <b>{money(summary.get('depositedAmount'))}</b>",
        f"💼 Tổng số dư khách: <b>{money(summary.get('balances'))}</b>",
        "",
        "<b>Khách hoạt động gần đây</b>",
    ]
    if not customers:
        lines.append("Chưa có dữ liệu khách hàng.")
    else:
        for index, customer in enumerate(customers[:15], start=1):
            lines.append(
                f"{index}. <code>{html.escape(str(customer.get('telegramId') or ''))}</code> · "
                f"đơn {int(customer.get('completedOrders') or 0)}/{int(customer.get('orders') or 0)} · "
                f"chi {money(customer.get('spent'))} · dư {money(customer.get('balance'))}\n"
                f"   Hoạt động: {vi_time(customer.get('lastSeenAt'))}"
            )
    return "\n".join(lines)


async def admin_users_text(admin_id: int) -> str:
    data = await api.admin_stats(admin_id)
    customers = [item for item in data.get("customers", []) if isinstance(item, dict)]
    local = local_users()
    merged: dict[int, dict[str, Any]] = {}

    for customer in customers:
        try:
            user_id = int(customer.get("telegramId") or 0)
        except (TypeError, ValueError):
            continue
        if user_id > 0:
            merged[user_id] = {**local.get(user_id, {}), **customer}

    for user_id, record in local.items():
        merged.setdefault(user_id, record)

    rows = sorted(
        merged.items(),
        key=lambda item: timestamp_value(item[1].get("lastSeenAt")),
        reverse=True,
    )
    lines = [
        "👥 <b>DANH SÁCH KHÁCH HÀNG</b>",
        UI_DIVIDER,
        f"Tổng số đã ghi nhận: <b>{len(rows)}</b>",
        "",
    ]
    if not rows:
        lines.append("Chưa có khách hàng nào sử dụng bot.")
    else:
        for index, (user_id, record) in enumerate(rows[:50], start=1):
            display_name = str(
                record.get("displayName")
                or record.get("name")
                or record.get("fullName")
                or "Khách chưa cập nhật tên"
            ).strip()
            username = str(record.get("username") or "").strip().lstrip("@")
            if username and username.lower() not in display_name.lower():
                display_name += f" · @{username}"
            balance_value = record.get("balance")
            balance_text = money(balance_value) if balance_value is not None else "Chưa đồng bộ"
            status = str(record.get("status") or "")
            status_line = f" · {html.escape(status)}" if status else ""
            lines.append(
                f"<b>{index}. {html.escape(display_name)}</b>\n"
                f"   ID: <code>{user_id}</code> · Số dư: <b>{balance_text}</b>{status_line}\n"
                f"   Hoạt động: {vi_time(record.get('lastSeenAt'))}"
            )
    if len(rows) > 50:
        lines.extend(["", f"… còn {len(rows) - 50} khách hàng khác."])
    return "\n".join(lines)


@dp.message(Command("admin"))
@dp.message(F.text == "🛠 Quản trị")
async def admin_handler(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer(
            "Bạn không có quyền sử dụng mục này.\n"
            f"ID Telegram của tài khoản này: <code>{message.from_user.id}</code>",
            reply_markup=main_keyboard(message.from_user.id),
        )
        return
    try:
        await message.answer(
            await admin_dashboard_text(message.from_user.id),
            reply_markup=admin_keyboard(),
        )
    except ApiError as exc:
        await handle_api_error(message, exc)


@dp.callback_query(F.data == "admin_dashboard")
async def admin_dashboard_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Bạn không có quyền.", show_alert=True)
        return
    try:
        if callback.message:
            await callback.message.edit_text(
                await admin_dashboard_text(callback.from_user.id),
                reply_markup=admin_keyboard(),
            )
        await callback.answer("Đã cập nhật thống kê.")
    except ApiError as exc:
        await callback.answer(public_error_message(exc)[:180], show_alert=True)


@dp.callback_query(F.data == "admin_users")
async def admin_users_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Bạn không có quyền.", show_alert=True)
        return
    try:
        text = await admin_users_text(callback.from_user.id)
        if callback.message:
            chunks = split_text_lines(text)
            await callback.message.edit_text(chunks[0], reply_markup=admin_keyboard() if len(chunks) == 1 else None)
            for chunk in chunks[1:-1]:
                await callback.message.answer(chunk)
            if len(chunks) > 1:
                await callback.message.answer(chunks[-1], reply_markup=admin_keyboard())
        await callback.answer()
    except ApiError as exc:
        await callback.answer(public_error_message(exc)[:180], show_alert=True)


@dp.message(Command("users"))
async def users_command_handler(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer(
            "Bạn không có quyền xem danh sách khách hàng.",
            reply_markup=main_keyboard(message.from_user.id),
        )
        return
    try:
        await answer_in_chunks(message, await admin_users_text(message.from_user.id), admin_keyboard())
    except ApiError as exc:
        await handle_api_error(message, exc)


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Bạn không có quyền.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await begin_admin_broadcast(callback.message, state, callback.from_user.id)


@dp.message(Command("broadcast"))
async def broadcast_command_handler(message: Message, state: FSMContext) -> None:
    await begin_admin_broadcast(message, state, message.from_user.id)


@dp.message(AdminStates.broadcast)
async def admin_broadcast_message_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    kind = ""
    file_id = ""
    content = ""
    if message.photo:
        kind = "photo"
        file_id = message.photo[-1].file_id
        content = str(message.caption or "").strip()
    elif message.video:
        kind = "video"
        file_id = message.video.file_id
        content = str(message.caption or "").strip()
    elif message.text:
        kind = "text"
        content = message.text
    else:
        await message.answer("Chỉ hỗ trợ tin nhắn văn bản, ảnh hoặc video. Vui lòng gửi lại.")
        return
    if kind in {"photo", "video"} and len(content) > 1024:
        await message.answer("Chú thích ảnh/video tối đa 1024 ký tự. Vui lòng gửi lại nội dung ngắn hơn.")
        return

    await state.update_data(kind=kind, file_id=file_id, content=content)
    await state.set_state(AdminStates.confirm_broadcast)
    type_label = {"text": "Tin nhắn văn bản", "photo": "Ảnh", "video": "Video"}[kind]
    preview = content[:1200] if content else "(Không có nội dung chú thích)"
    await message.answer(
        "📣 <b>XEM TRƯỚC THÔNG BÁO</b>\n"
        f"{UI_DIVIDER}\n"
        f"Loại: <b>{type_label}</b>\n"
        f"Nội dung:\n<pre>{html.escape(preview)}</pre>\n"
        "Nếu đúng, bấm gửi để phát đến toàn bộ khách đã từng dùng bot.",
        reply_markup=admin_broadcast_keyboard(),
    )


async def broadcast_recipient_ids(admin_id: int) -> list[int]:
    recipients = set(local_users())
    try:
        data = await api.admin_stats(admin_id)
        customers = data.get("customers", [])
        for customer in customers if isinstance(customers, list) else []:
            if not isinstance(customer, dict):
                continue
            try:
                user_id = int(customer.get("telegramId") or 0)
            except (TypeError, ValueError):
                continue
            if user_id > 0:
                recipients.add(user_id)
    except ApiError as exc:
        LOGGER.warning("Could not load backend customers for broadcast: %s", exc.code)
    return sorted(user_id for user_id in recipients if not is_admin(user_id))


async def broadcast_payload(payload: dict[str, Any], admin_id: int) -> tuple[int, int]:
    global bot
    if not bot:
        return 0, 0
    sent = 0
    failed = 0
    for user_id in await broadcast_recipient_ids(admin_id):
        try:
            kind = str(payload.get("kind") or "")
            content = str(payload.get("content") or "")
            if kind == "photo":
                await bot.send_photo(
                    user_id,
                    photo=str(payload.get("file_id") or ""),
                    caption=content or None,
                    parse_mode=None,
                )
            elif kind == "video":
                await bot.send_video(
                    user_id,
                    video=str(payload.get("file_id") or ""),
                    caption=content or None,
                    parse_mode=None,
                )
            else:
                await bot.send_message(user_id, content, parse_mode=None)
            sent += 1
        except Exception:
            failed += 1
            LOGGER.warning("Could not broadcast to Telegram user %s", user_id)
        await asyncio.sleep(0.06)
    return sent, failed


@dp.callback_query(F.data == "admin_broadcast_confirm")
async def admin_broadcast_confirm_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Bạn không có quyền.", show_alert=True)
        return
    payload = await state.get_data()
    if not payload.get("kind"):
        await state.clear()
        await callback.answer("Nội dung thông báo đã hết hạn.", show_alert=True)
        return
    await callback.answer("Đang gửi thông báo…")
    if callback.message:
        await callback.message.edit_text("⏳ <b>ĐANG GỬI THÔNG BÁO…</b>\nVui lòng chờ bot hoàn tất.")
    sent, failed = await broadcast_payload(payload, callback.from_user.id)
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "✅ <b>ĐÃ GỬI THÔNG BÁO</b>\n"
            f"{UI_DIVIDER}\n"
            f"Gửi thành công: <b>{sent}</b>\n"
            f"Không gửi được: <b>{failed}</b>",
            reply_markup=admin_keyboard(),
        )


@dp.callback_query(F.data == "admin_broadcast_cancel")
async def admin_broadcast_cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Bạn không có quyền.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Đã hủy.")
    if callback.message:
        await callback.message.edit_text("Đã hủy gửi thông báo.", reply_markup=admin_keyboard())


# ---------------------------------------------------------------------------
# Nạp tiền tự động qua VietQR + SePay
# ---------------------------------------------------------------------------

def deposit_keyboard(memo: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Tôi đã chuyển khoản", callback_data=f"deposit_check|{memo}")],
            [InlineKeyboardButton(text="💰 Kiểm tra số dư", callback_data="balance")],
            [InlineKeyboardButton(text="🆘 Hỗ trợ", url=support_url())],
        ]
    )


def purchase_qr_keyboard(memo: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Tôi đã chuyển khoản", callback_data=f"deposit_check|{memo}")],
            [InlineKeyboardButton(text="🧾 Xem đơn hàng", callback_data="orders")],
            [InlineKeyboardButton(text="🆘 Hỗ trợ", url=support_url())],
        ]
    )


async def create_and_send_deposit_qr(
    message: Message,
    telegram_id: int,
    amount: int,
    *,
    pending: PendingPurchase | None = None,
) -> dict[str, Any]:
    data = await api.create_deposit(telegram_id, amount)
    memo = str(data.get("memo") or "")
    expires_at = int(data.get("expiresAt") or 0)
    bank = data.get("bank") if isinstance(data.get("bank"), dict) else {}
    is_purchase = pending is not None
    if pending:
        pending_purchases[telegram_id] = pending
        caption = (
            "📲 <b>THANH TOÁN ĐƠN HÀNG BẰNG QR</b>\n"
            f"{UI_DIVIDER}\n\n"
            f"Mã đơn: <code>{html.escape(pending.order_id)}</code>\n"
            f"Sản phẩm: <b>{html.escape(pending.product_name)}</b>\n"
            f"Số lượng: <b>{pending.quantity}</b>\n"
            f"Số tiền cần chuyển: <b>{money(data.get('amount') or amount)}</b>\n\n"
        )
    else:
        caption = (
            "💳 <b>QUÉT QR ĐỂ NẠP TIỀN</b>\n"
            f"{UI_DIVIDER}\n\n"
            f"Số tiền: <b>{money(data.get('amount') or amount)}</b>\n"
        )
    caption += (
        f"Ngân hàng: <b>MB Bank</b>\n"
        f"Số tài khoản: <code>{html.escape(str(bank.get('account') or ''))}</code>\n"
        f"Chủ tài khoản: <b>{html.escape(str(bank.get('accountName') or ''))}</b>\n"
        f"Nội dung chuyển khoản: <code>{html.escape(memo)}</code>\n\n"
        "Giữ nguyên số tiền và nội dung chuyển khoản. Hệ thống sẽ tự động xác nhận sau khi nhận được tiền.\n\n"
        f"{support_line()}"
    )
    markup = purchase_qr_keyboard(memo) if is_purchase else deposit_keyboard(memo)
    qr_url = str(data.get("qrUrl") or "")
    if qr_url:
        await message.answer_photo(photo=URLInputFile(qr_url), caption=caption, reply_markup=markup)
    else:
        await message.answer(caption, reply_markup=markup)

    if is_purchase:
        await notify_admin(
            "📲 <b>CÓ YÊU CẦU THANH TOÁN ĐƠN HÀNG QUA QR</b>\n\n"
            f"👤 Telegram ID: <code>{telegram_id}</code>\n"
            f"🧾 Mã đơn: <code>{html.escape(pending.order_id)}</code>\n"
            f"📦 Sản phẩm: <b>{html.escape(pending.product_name)}</b>\n"
            f"💰 Số tiền: <b>{money(data.get('amount') or amount)}</b>\n"
            f"📝 Mã nạp: <code>{html.escape(memo)}</code>"
        )
    else:
        await notify_admin(
            "📥 <b>CÓ YÊU CẦU NẠP TIỀN MỚI</b>\n\n"
            f"👤 Telegram ID: <code>{telegram_id}</code>\n"
            f"💰 Số tiền: <b>{money(data.get('amount') or amount)}</b>\n"
            f"📝 Mã nạp: <code>{html.escape(memo)}</code>"
        )

    key = (telegram_id, memo)
    old_task = deposit_watch_tasks.pop(key, None)
    if old_task:
        old_task.cancel()
    deposit_watch_tasks[key] = asyncio.create_task(watch_deposit(telegram_id, memo, expires_at))
    return data


async def notify_deposit_paid(telegram_id: int, data: dict[str, Any]) -> None:
    global bot
    if not bot:
        return
    memo = str(data.get("memo") or "")
    key = (telegram_id, memo)
    if key in deposit_notified:
        return
    deposit_notified.add(key)
    pending = pending_purchases.pop(telegram_id, None)
    pending_note = "\n\n⏳ Bot sẽ tự động tiếp tục đơn hàng bạn vừa chọn." if pending else ""
    await bot.send_message(
        telegram_id,
        "✅ <b>NẠP TIỀN THÀNH CÔNG</b>\n"
        f"{UI_DIVIDER}\n\n"
        f"Đã cộng: <b>+{money(data.get('amount'))}</b>\n"
        f"Số dư mới: <b>{money(data.get('balance'))}</b>\n"
        f"Mã nạp: <code>{html.escape(memo)}</code>{pending_note}\n\n"
        f"{support_line()}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📦 Mua ngay", callback_data="products"),
                    InlineKeyboardButton(text="💰 Số dư", callback_data="balance"),
                ],
                [InlineKeyboardButton(text="🏠 Trang chủ", callback_data="home")],
            ]
        ),
    )
    await notify_admin(
        "💳 <b>KHÁCH NẠP TIỀN TỰ ĐỘNG</b>\n\n"
        f"👤 Telegram ID: <code>{telegram_id}</code>\n"
        f"💰 Số tiền: <b>{money(data.get('amount'))}</b>\n"
        f"📝 Mã nạp: <code>{html.escape(memo)}</code>"
    )
    if pending:
        asyncio.create_task(resume_pending_purchase(telegram_id, pending))


async def watch_deposit(telegram_id: int, memo: str, expires_at: int) -> None:
    key = (telegram_id, memo)
    deadline = min(time.time() + DEPOSIT_WATCH_SECONDS, max(time.time(), expires_at / 1000))
    try:
        while time.time() < deadline:
            try:
                data = await api.deposit_status(telegram_id, memo)
                status = str(data.get("status") or "")
                if "Auto" in status or status.startswith("Đã duyệt") or int(data.get("creditedAt") or 0) > 0:
                    await notify_deposit_paid(telegram_id, data)
                    return
            except ApiError as exc:
                LOGGER.warning("Deposit watch failed for %s: %s", memo, exc.code)
            await asyncio.sleep(DEPOSIT_POLL_SECONDS)
    finally:
        deposit_watch_tasks.pop(key, None)


@dp.message(Command("deposit"))
@dp.message(F.text == "💳 Nạp tiền")
async def deposit_handler(message: Message, state: FSMContext, telegram_id: int | None = None) -> None:
    user_id = telegram_id or message.from_user.id
    if telegram_id is None:
        remember_user(message.from_user)
    else:
        touch_user(user_id)
    await state.clear()
    await state.set_state(DepositStates.amount)
    await message.answer(
        "💳 <b>NẠP TIỀN TỰ ĐỘNG</b>\n"
        f"{UI_DIVIDER}\n"
        f"Nhập số tiền muốn nạp, {deposit_requirement_text()}.\n"
        "Ví dụ: <code>50000</code>\n\n"
        "Sau đó quét QR và giữ nguyên nội dung chuyển khoản để hệ thống nhận diện nhanh.\n\n"
        f"{support_line()}",
        reply_markup=main_keyboard(user_id),
    )


@dp.message(DepositStates.amount)
async def deposit_amount_handler(message: Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    amount = parse_amount(str(message.text or ""))
    if amount <= 0:
        await message.answer("Số tiền phải lớn hơn 0. Vui lòng nhập lại.")
        return
    if MIN_DEPOSIT > 0 and amount < MIN_DEPOSIT:
        await message.answer(f"Số tiền tối thiểu là {money(MIN_DEPOSIT)}. Vui lòng nhập lại.")
        return
    try:
        await state.clear()
        await create_and_send_deposit_qr(message, message.from_user.id, amount)
    except ApiError as exc:
        await state.clear()
        await handle_api_error(message, exc)


@dp.callback_query(F.data.startswith("deposit_check|"))
async def deposit_check_callback(callback: CallbackQuery) -> None:
    memo = str(callback.data or "").split("|", 1)[-1]
    try:
        data = await api.deposit_status(callback.from_user.id, memo)
        status = str(data.get("status") or "Chờ duyệt")
        if "Auto" in status or status.startswith("Đã duyệt") or int(data.get("creditedAt") or 0) > 0:
            await notify_deposit_paid(callback.from_user.id, data)
            await callback.answer("Đã cộng tiền vào ví.", show_alert=True)
        else:
            await callback.answer("Chưa thấy giao dịch. Hãy kiểm tra đúng số tiền và nội dung.", show_alert=True)
    except ApiError as exc:
        await callback.answer(exc.message[:180], show_alert=True)


# ---------------------------------------------------------------------------
# Sản phẩm và mua hàng
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("catalog|"))
async def catalog_page_callback(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    parts = str(callback.data or "").split("|")
    if len(parts) != 3:
        await callback.answer("Dữ liệu không hợp lệ.", show_alert=True)
        return
    menu = menu_for_user(callback.from_user.id)
    if not menu or menu.key != parts[1]:
        await callback.answer("Danh sách đã cũ, vui lòng làm mới.", show_alert=True)
        return
    try:
        page = int(parts[2])
    except ValueError:
        await callback.answer("Trang không hợp lệ.", show_alert=True)
        return
    total_pages = max(1, (len(menu.products) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    await callback.message.edit_text(
        catalog_text(menu, page),
        reply_markup=catalog_keyboard(menu, page),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("product|"))
async def product_detail_callback(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    parts = str(callback.data or "").split("|")
    if len(parts) != 3:
        await callback.answer("Dữ liệu không hợp lệ.", show_alert=True)
        return
    menu = menu_for_user(callback.from_user.id)
    if not menu or menu.key != parts[1]:
        await callback.answer("Danh sách đã cũ, vui lòng mở lại sản phẩm.", show_alert=True)
        return
    try:
        index = int(parts[2])
        product = menu.products[index]
    except (ValueError, IndexError):
        await callback.answer("Sản phẩm không còn trong danh sách.", show_alert=True)
        return
    await callback.message.edit_text(
        product_text(product, menu.discount_percent),
        reply_markup=product_detail_keyboard(menu, index, product),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("quantity|"))
async def quantity_callback(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data or "").split("|")
    if len(parts) != 3:
        await callback.answer("Dữ liệu không hợp lệ.", show_alert=True)
        return
    menu = menu_for_user(callback.from_user.id)
    if not menu or menu.key != parts[1]:
        await callback.answer("Danh sách đã cũ, vui lòng mở lại sản phẩm.", show_alert=True)
        return
    try:
        index = int(parts[2])
        product = menu.products[index]
    except (ValueError, IndexError):
        await callback.answer("Sản phẩm không còn trong danh sách.", show_alert=True)
        return
    limit = min(MAX_QUANTITY, max(1, int(product.get("quantity") or 0)))
    await state.set_state(QuantityStates.quantity)
    await state.update_data(menu_key=menu.key, index=index)
    if callback.message:
        await callback.message.answer(
            f"🔢 Nhập số lượng cho <b>{html.escape(str(product.get('name') or 'sản phẩm'))}</b>\n"
            f"Tối đa hiện tại: <b>{limit}</b>"
        )
    await callback.answer()


@dp.message(QuantityStates.quantity)
async def quantity_message_handler(message: Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    try:
        quantity = int(str(message.text or "").strip())
    except ValueError:
        await message.answer("Vui lòng nhập số nguyên dương, ví dụ: 2")
        return
    data = await state.get_data()
    menu = menu_for_user(message.from_user.id)
    if not menu or menu.key != data.get("menu_key"):
        await state.clear()
        await message.answer("Danh sách đã hết hạn. Chọn 📦 Sản phẩm để mở lại.", reply_markup=main_keyboard(message.from_user.id))
        return
    try:
        index = int(data.get("index"))
        product = menu.products[index]
    except (ValueError, TypeError, IndexError):
        await state.clear()
        await message.answer("Sản phẩm không còn trong danh sách.", reply_markup=main_keyboard(message.from_user.id))
        return
    limit = min(MAX_QUANTITY, max(1, int(product.get("quantity") or 0)))
    if quantity < 1 or quantity > limit:
        await message.answer(f"Số lượng phải từ 1 đến {limit}. Vui lòng nhập lại.")
        return
    await state.clear()
    total = int(product.get("finalPrice") or product.get("price") or 0) * quantity
    await message.answer(
        "🛒 <b>XÁC NHẬN ĐƠN HÀNG</b>\n"
        f"{UI_DIVIDER}\n\n"
        f"Sản phẩm: <b>{html.escape(str(product.get('name') or 'Sản phẩm'))}</b>\n"
        f"Số lượng: <b>{quantity}</b>\n"
        f"Tạm tính: <b>{money(total)}</b>\n\n"
        "Giá cuối cùng và tồn kho sẽ được website kiểm tra lại khi thanh toán.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Chọn phương thức thanh toán", callback_data=f"payment|{menu.key}|{index}|{quantity}")],
                [InlineKeyboardButton(text="‹ Quay lại", callback_data=f"product|{menu.key}|{index}")],
            ]
        ),
    )


@dp.callback_query(F.data.startswith("payment|"))
async def payment_method_callback(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    parts = str(callback.data or "").split("|")
    if len(parts) != 4:
        await callback.answer("Dữ liệu không hợp lệ.", show_alert=True)
        return
    menu = menu_for_user(callback.from_user.id)
    if not menu or menu.key != parts[1]:
        await callback.answer("Danh sách đã cũ, vui lòng mở lại sản phẩm.", show_alert=True)
        return
    try:
        index = int(parts[2])
        quantity = int(parts[3])
        product = menu.products[index]
    except (ValueError, IndexError):
        await callback.answer("Sản phẩm hoặc số lượng không hợp lệ.", show_alert=True)
        return
    stock = int(product.get("quantity") or 0)
    if quantity < 1 or quantity > MAX_QUANTITY or quantity > stock:
        await callback.answer("Số lượng hiện không còn đủ.", show_alert=True)
        return
    total = int(product.get("finalPrice") or product.get("price") or 0) * quantity
    await callback.message.edit_text(
        "💳 <b>CHỌN PHƯƠNG THỨC THANH TOÁN</b>\n"
        f"{UI_DIVIDER}\n\n"
        f"📦 Sản phẩm: <b>{html.escape(str(product.get('name') or 'Sản phẩm'))}</b>\n"
        f"🔢 Số lượng: <b>{quantity}</b>\n"
        f"💵 Tổng tiền: <b>{money(total)}</b>\n\n"
        "Bạn có thể dùng số dư sẵn có hoặc chuyển khoản QR đúng số tiền trên đơn.",
        reply_markup=payment_method_keyboard(menu, index, quantity),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm|"))
@dp.callback_query(F.data.startswith("pay_balance|"))
async def confirm_purchase_callback(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    parts = str(callback.data or "").split("|")
    if len(parts) != 4:
        await callback.answer("Dữ liệu không hợp lệ.", show_alert=True)
        return
    menu = menu_for_user(callback.from_user.id)
    if not menu or menu.key != parts[1]:
        await callback.answer("Danh sách đã cũ, vui lòng mở lại sản phẩm.", show_alert=True)
        return
    try:
        index = int(parts[2])
        quantity = int(parts[3])
        product = menu.products[index]
    except (ValueError, IndexError):
        await callback.answer("Sản phẩm hoặc số lượng không hợp lệ.", show_alert=True)
        return
    if quantity < 1 or quantity > MAX_QUANTITY:
        await callback.answer("Số lượng không hợp lệ.", show_alert=True)
        return

    attempt_key = (callback.from_user.id, menu.key, index, quantity)
    order_id = purchase_attempts.setdefault(
        attempt_key,
        new_order_id(),
    )
    await callback.answer("Đang kiểm tra số dư và tồn kho…")
    async with purchase_locks[callback.from_user.id]:
        try:
            await callback.message.edit_text("⏳ <b>ĐANG XỬ LÝ ĐƠN HÀNG</b>\n\nHệ thống đang kiểm tra số dư, tồn kho và chuẩn bị tài khoản cho bạn…")
            balance_data = await api.balance(callback.from_user.id)
            expected_total = int(product.get("finalPrice") or product.get("price") or 0) * quantity
            balance = int(float(balance_data.get("balance") or 0))
            if balance < expected_total:
                pending_purchases[callback.from_user.id] = PendingPurchase(
                    product_id=str(product.get("id") or ""),
                    product_name=str(product.get("name") or "Sản phẩm"),
                    quantity=quantity,
                    order_id=order_id,
                    expected_total=expected_total,
                )
                purchase_attempts.pop(attempt_key, None)
                await callback.message.edit_text(
                    "💳 <b>SỐ DƯ CHƯA ĐỦ</b>\n"
                    f"{UI_DIVIDER}\n\n"
                    f"Cần khoảng: <b>{money(expected_total)}</b>\n"
                    f"Đang có: <b>{money(balance)}</b>\n"
                    f"Cần nạp thêm: <b>{money(expected_total - balance)}</b>\n\n"
                    "Chọn QR nhanh để chuyển đúng số tiền của đơn hàng, hoặc nạp một số tiền khác.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="📲 QR đúng số tiền đơn", callback_data=f"pay_qr|{menu.key}|{index}|{quantity}")],
                            [InlineKeyboardButton(text="💳 Nạp số tiền khác", callback_data="deposit")],
                            [
                                InlineKeyboardButton(text="📦 Sản phẩm", callback_data="products"),
                                InlineKeyboardButton(text="🏠 Trang chủ", callback_data="home"),
                            ],
                        ]
                    ),
                )
                return

            result = await api.checkout(callback.from_user.id, str(product.get("id") or ""), order_id, quantity)
            detail = await fetch_order_detail(callback.from_user.id, order_id)
            await send_purchase_result(
                callback.from_user.id,
                result,
                detail,
                str(product.get("name") or "Sản phẩm"),
                expected_total,
                order_id,
                initial_message=callback.message,
            )
        except ApiError as exc:
            LOGGER.warning(
                "Checkout failed for order %s (%s): %s",
                order_id,
                exc.code,
                exc.message,
            )
            if exc.code == "PROVIDER_PURCHASE_UNCERTAIN":
                await notify_admin(
                    "⚠️ <b>ĐƠN HÀNG CẦN KIỂM TRA</b>\n\n"
                    f"👤 Telegram ID: <code>{callback.from_user.id}</code>\n"
                    f"🧾 Mã đơn: <code>{html.escape(order_id)}</code>\n"
                    f"📦 Sản phẩm: <b>{html.escape(str(product.get('name') or 'Sản phẩm'))}</b>"
                )
                await callback.message.edit_text(
                    "⚠️ <b>ĐƠN CẦN KIỂM TRA</b>\n\n"
                    f"Mã đơn: <code>{html.escape(order_id)}</code>\n"
                    "Hệ thống chưa nhận được kết quả đầy đủ. Đơn đã được khóa để tránh trừ tiền hai lần.\n"
                    "Vui lòng liên hệ admin để kiểm tra đơn này.",
                    reply_markup=after_purchase_keyboard(),
                )
            else:
                purchase_attempts.pop(attempt_key, None)
                await callback.message.edit_text(f"❌ {html.escape(public_error_message(exc))}", reply_markup=after_purchase_keyboard())
        except Exception:
            LOGGER.exception("Purchase handler failed")
            await callback.message.edit_text(
                "❌ Có lỗi ngoài dự kiến khi xử lý đơn. Vui lòng kiểm tra mục Đơn hàng trước khi thử lại.",
                reply_markup=after_purchase_keyboard(),
            )


@dp.callback_query(F.data.startswith("pay_qr|"))
async def quick_qr_payment_callback(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    parts = str(callback.data or "").split("|")
    if len(parts) != 4:
        await callback.answer("Dữ liệu không hợp lệ.", show_alert=True)
        return
    menu = menu_for_user(callback.from_user.id)
    if not menu or menu.key != parts[1]:
        await callback.answer("Danh sách đã cũ, vui lòng mở lại sản phẩm.", show_alert=True)
        return
    try:
        index = int(parts[2])
        quantity = int(parts[3])
        product = menu.products[index]
    except (ValueError, IndexError):
        await callback.answer("Sản phẩm hoặc số lượng không hợp lệ.", show_alert=True)
        return

    stock = int(product.get("quantity") or 0)
    if quantity < 1 or quantity > MAX_QUANTITY or quantity > stock:
        await callback.answer("Số lượng hiện không còn đủ.", show_alert=True)
        return
    expected_total = int(product.get("finalPrice") or product.get("price") or 0) * quantity
    attempt_key = (callback.from_user.id, menu.key, index, quantity)
    existing = pending_purchases.get(callback.from_user.id)
    if existing and (
        existing.product_id != str(product.get("id") or "")
        or existing.quantity != quantity
    ):
        await callback.answer("Bạn đang có một đơn khác chờ thanh toán.", show_alert=True)
        return
    if any(user_id == callback.from_user.id for user_id, _memo in deposit_watch_tasks):
        await callback.answer("Bạn đang có một mã QR chờ thanh toán. Hãy dùng mã QR trước đó.", show_alert=True)
        return

    order_id = existing.order_id if existing else purchase_attempts.setdefault(attempt_key, new_order_id())
    pending = existing or PendingPurchase(
        product_id=str(product.get("id") or ""),
        product_name=str(product.get("name") or "Sản phẩm"),
        quantity=quantity,
        order_id=order_id,
        expected_total=expected_total,
    )
    await callback.answer("Đang tạo QR thanh toán…")
    try:
        await callback.message.edit_text(
            "⏳ <b>ĐANG TẠO QR THANH TOÁN</b>\n\n"
            f"Số tiền đơn hàng: <b>{money(expected_total)}</b>"
        )
        await create_and_send_deposit_qr(
            callback.message,
            callback.from_user.id,
            expected_total,
            pending=pending,
        )
    except ApiError as exc:
        await callback.message.edit_text(
            f"❌ {html.escape(public_error_message(exc))}",
            reply_markup=after_purchase_keyboard(),
        )


@dp.callback_query(F.data == "products")
async def products_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await show_catalog(callback.message, edit=True, telegram_id=callback.from_user.id)


@dp.callback_query(F.data == "home")
async def home_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await show_home(
            callback.message,
            edit=True,
            telegram_id=callback.from_user.id,
            display_name=callback.from_user.full_name,
        )


@dp.callback_query(F.data == "balance")
async def balance_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await balance_handler(callback.message, callback.from_user.id)


@dp.callback_query(F.data == "orders")
async def orders_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await orders_handler(callback.message, callback.from_user.id)


@dp.callback_query(F.data == "deposit")
async def deposit_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await deposit_handler(callback.message, state, callback.from_user.id)


# ---------------------------------------------------------------------------
# SePay webhook: /sepay/webhook
# ---------------------------------------------------------------------------

def normalize_payment_text(value: str) -> str:
    return "".join(char.lower() for char in str(value or "") if char.isalnum())


def extract_payment_fields(payload: Any) -> tuple[int, str, str]:
    if not isinstance(payload, dict):
        return 0, "", ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if isinstance(data.get("transfer"), dict):
        data = data["transfer"]
    amount = 0
    content = ""
    transaction_id = ""
    for key in ("transferAmount", "amount", "transfer_amount", "creditAmount", "transactionAmount", "incomingAmount"):
        value = data.get(key)
        if value is None:
            continue
        try:
            amount = int(float(str(value).replace(",", "").strip()))
            if amount > 0:
                break
        except (TypeError, ValueError):
            pass
    content_parts: list[str] = []
    for key in ("content", "description", "transferContent", "transactionContent", "referenceCode"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            content_parts.append(value.strip())
    content = " ".join(dict.fromkeys(content_parts))[:4000]
    for key in ("id", "transaction_id", "transactionId", "reference", "code"):
        value = data.get(key)
        if value is not None and str(value).strip():
            transaction_id = str(value).strip()
            break
    return amount, content, transaction_id


def webhook_authorized(request: Request) -> bool:
    if not WEBHOOK_TOKEN:
        return True
    supplied = request.headers.get("x-webhook-token", "")
    if not supplied:
        auth = request.headers.get("authorization", "")
        supplied = auth.removeprefix("Bearer ").strip()
    return bool(supplied) and hmac.compare_digest(supplied, WEBHOOK_TOKEN)


async def forward_sepay_event(payload: Any, request: Request) -> bool:
    """Chuyển giao dịch chưa khớp sang webhook của bot còn lại."""
    if not SEPAY_FORWARD_URL or request.headers.get("x-sepay-forwarded") == "1":
        return False
    headers = {"X-Sepay-Forwarded": "1"}
    if WEBHOOK_TOKEN:
        headers["X-Webhook-Token"] = WEBHOOK_TOKEN
    target_url = SEPAY_FORWARD_URL.rstrip("/")
    if not target_url.endswith("/sepay/webhook"):
        target_url += "/sepay/webhook"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.post(
                target_url,
                json=payload,
                headers=headers,
            )
        if response.status_code < 200 or response.status_code >= 300:
            LOGGER.warning("Forwarded SePay event failed with HTTP %s", response.status_code)
            return False
        LOGGER.info("Forwarded unmatched SePay event to secondary bot")
        return True
    except httpx.HTTPError:
        LOGGER.exception("Could not forward SePay event to secondary bot")
        return False


@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "service": "telegram-account-shop"}


@app.get("/sepay/webhook")
async def sepay_webhook_get() -> dict[str, Any]:
    return {
        "ok": True,
        "message": "SePay webhook endpoint is alive. Use POST.",
        "memoFormat": "Chuyentien_12345",
        "acceptsLegacyMemo": True,
    }


@app.post("/sepay/webhook")
async def sepay_webhook_post(request: Request) -> dict[str, Any]:
    if not webhook_authorized(request):
        return {"ok": False, "message": "unauthorized"}
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "message": "invalid json"}
    amount, content, transaction_id = extract_payment_fields(payload)
    if amount <= 0 or not content:
        forwarded = await forward_sepay_event(payload, request)
        return {"ok": True, "message": "forwarded" if forwarded else "ignored"}

    # Mã nạp mới có dạng Chuyentien_<5 chữ số>; chấp nhận cả ngân hàng bỏ dấu _ hoặc thêm khoảng trắng.
    normalized_content = normalize_payment_text(content)
    new_memo_match = re.search(r"chuyentien(\d{5})(?!\d)", normalized_content, re.IGNORECASE)
    if new_memo_match:
        memo = f"Chuyentien_{new_memo_match.group(1)}"
        try:
            LOGGER.info("SePay deposit matched Telegram memo %s", memo)
            result = await api.confirm_deposit_by_memo(memo, amount, transaction_id or "sepay")
            if result.get("processed"):
                telegram_id = int(result.get("telegramId") or 0)
                if telegram_id:
                    await notify_deposit_paid(telegram_id, {**result, "memo": memo, "amount": amount})
            return {"ok": True, "message": "processed" if result.get("processed") else "already processed"}
        except ApiError as exc:
            LOGGER.warning("SePay confirmation failed for %s: %s", memo, exc.code)
            return {"ok": False, "message": exc.code}

    legacy_memo_match = re.search(r"(TG(\d{1,30})_[A-Za-z0-9]+_[A-Za-z0-9]+)", content, re.IGNORECASE)
    if not legacy_memo_match:
        forwarded = await forward_sepay_event(payload, request)
        return {"ok": True, "message": "forwarded" if forwarded else "not a telegram deposit"}
    memo = legacy_memo_match.group(1)
    telegram_id = int(legacy_memo_match.group(2))
    try:
        result = await api.confirm_deposit(telegram_id, memo, amount, transaction_id or "sepay")
        if result.get("processed"):
            await notify_deposit_paid(telegram_id, {**result, "memo": memo, "amount": amount})
        return {"ok": True, "message": "processed" if result.get("processed") else "already processed"}
    except ApiError as exc:
        LOGGER.warning("SePay confirmation failed for %s: %s", memo, exc.code)
        return {"ok": False, "message": exc.code}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

async def run_web() -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    global bot
    if not BOT_TOKEN:
        raise RuntimeError("Thiếu BOT_TOKEN trong biến môi trường.")
    if not WEB_API_BASE_URL:
        raise RuntimeError("Thiếu WEB_API_BASE_URL trong biến môi trường.")
    if len(TELEGRAM_BOT_SHARED_SECRET) < 32:
        raise RuntimeError("TELEGRAM_BOT_SHARED_SECRET phải có ít nhất 32 ký tự.")
    if not ADMIN_TELEGRAM_IDS:
        LOGGER.warning("No Telegram admin ID configured; /admin will be unavailable.")
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Mở menu chính"),
            BotCommand(command="home", description="Về trang chủ"),
            BotCommand(command="products", description="Xem tài khoản đang bán"),
            BotCommand(command="deposit", description="Nạp tiền tự động"),
            BotCommand(command="balance", description="Xem số dư"),
            BotCommand(command="orders", description="Xem đơn hàng"),
            BotCommand(command="help", description="Hướng dẫn mua hàng"),
            BotCommand(command="support", description="Liên hệ hỗ trợ"),
            BotCommand(command="admin", description="Quản trị shop"),
            BotCommand(command="users", description="Danh sách khách hàng"),
            BotCommand(command="broadcast", description="Gửi thông báo cho khách"),
        ]
    )
    LOGGER.info("Starting Telegram account shop bot")
    try:
        await asyncio.gather(
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
            run_web(),
        )
    finally:
        for task in deposit_watch_tasks.values():
            task.cancel()
        await api.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
