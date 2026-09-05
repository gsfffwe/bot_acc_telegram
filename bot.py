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
try:
    ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0").strip() or "0")
except ValueError:
    ADMIN_TELEGRAM_ID = 0
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", str(BASE_DIR)))
DB_PATH = DATA_DIR / "telegram_shop.sqlite3"
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_TOKEN = os.getenv("SEPAY_WEBHOOK_TOKEN", "").strip()

CATALOG_PAGE_SIZE = max(1, min(20, int(os.getenv("CATALOG_PAGE_SIZE", "8"))))
MAX_QUANTITY = max(1, min(100, int(os.getenv("MAX_QUANTITY", "100"))))
MIN_DEPOSIT = max(1, int(os.getenv("MIN_DEPOSIT", "10000")))
DEPOSIT_WATCH_SECONDS = max(60, int(os.getenv("DEPOSIT_WATCH_SECONDS", "900")))
DEPOSIT_POLL_SECONDS = max(3, int(os.getenv("DEPOSIT_POLL_SECONDS", "5")))
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


def touch_user(telegram_id: int) -> None:
    now = int(time.time())
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute(
            """
            INSERT INTO telegram_users(telegram_id, first_seen_at, last_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (telegram_id, now, now),
        )


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def money(value: Any) -> str:
    try:
        amount = int(round(float(value or 0)))
    except (TypeError, ValueError):
        amount = 0
    return f"{amount:,}".replace(",", ".") + "đ"


def short_text(value: Any, length: int = 30) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= length else text[: max(1, length - 1)] + "…"


def vi_time(timestamp: Any) -> str:
    try:
        from datetime import datetime, timedelta

        dt = datetime.utcfromtimestamp(float(timestamp) / 1000) + timedelta(hours=7)
        return dt.strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "Không rõ thời gian"


def is_admin(telegram_id: int | str) -> bool:
    try:
        return ADMIN_TELEGRAM_ID > 0 and int(telegram_id) == ADMIN_TELEGRAM_ID
    except (TypeError, ValueError):
        return False


def main_keyboard(telegram_id: int | None = None) -> ReplyKeyboardMarkup:
    rows = [
            [KeyboardButton(text="📦 Sản phẩm"), KeyboardButton(text="💰 Số dư")],
            [KeyboardButton(text="💳 Nạp tiền"), KeyboardButton(text="🧾 Đơn hàng")],
            [KeyboardButton(text="ℹ️ Trợ giúp")],
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
        inline_keyboard=[[InlineKeyboardButton(text="📦 Xem sản phẩm", callback_data="products")]]
    )


def after_purchase_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Mua sản phẩm khác", callback_data="products")],
            [InlineKeyboardButton(text="💰 Xem số dư", callback_data="balance")],
            [InlineKeyboardButton(text="🧾 Xem đơn hàng", callback_data="orders")],
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
        stock_text = f"còn {stock}" if stock > 0 else "hết hàng"
        duration = short_text(product.get("duration") or "Dùng ngay", 14)
        label = f"{short_text(product.get('name'), 22)} · {duration} · {money(product.get('finalPrice', product.get('price')))} · {stock_text}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"product|{menu.key}|{index}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹ Trước", callback_data=f"catalog|{menu.key}|{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="Sau ›", callback_data=f"catalog|{menu.key}|{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔄 Làm mới", callback_data="products")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_text(product: dict[str, Any], discount_percent: int) -> str:
    name = html.escape(str(product.get("name") or "Sản phẩm"))
    desc = html.escape(str(product.get("desc") or "").strip())
    duration = html.escape(str(product.get("duration") or "Dùng ngay"))
    fmt = html.escape(str(product.get("format") or "Theo mô tả sản phẩm"))
    warranty = html.escape(str(product.get("warranty") or "Không bảo hành"))
    stock = int(product.get("quantity") or 0)
    price = money(product.get("finalPrice", product.get("price")))
    old_price = money(product.get("price"))
    price_line = f"💵 Giá: <b>{price}</b>"
    if discount_percent > 0 and int(product.get("finalPrice") or 0) != int(product.get("price") or 0):
        price_line += f" <s>{old_price}</s> (-{discount_percent}%)"
    lines = [
        f"📦 <b>{name}</b>",
        "",
        price_line,
        f"📊 Kho: <b>{stock}</b>",
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
        rows.append([InlineKeyboardButton(text="🛒 Mua 1 tài khoản", callback_data=f"confirm|{menu.key}|{index}|1")])
        rows.append([InlineKeyboardButton(text="🔢 Chọn số lượng", callback_data=f"quantity|{menu.key}|{index}")])
    else:
        rows.append([InlineKeyboardButton(text="⚠️ Hết hàng", callback_data=f"catalog|{menu.key}|{index // CATALOG_PAGE_SIZE}")])
    rows.append([InlineKeyboardButton(text="‹ Quay lại danh sách", callback_data=f"catalog|{menu.key}|{index // CATALOG_PAGE_SIZE}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def answer_in_chunks(message: Message, text: str, reply_markup: Any = None) -> None:
    chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)] or [""]
    for index, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=reply_markup if index == len(chunks) - 1 else None)


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
    if not bot or not ADMIN_TELEGRAM_ID:
        return
    try:
        await bot.send_message(ADMIN_TELEGRAM_ID, text)
    except Exception:
        LOGGER.exception("Could not notify admin")


def public_error_message(exc: ApiError) -> str:
    """Ẩn chi tiết kỹ thuật/nhà cung cấp khỏi tin nhắn gửi cho khách."""
    if exc.code.startswith("PROVIDER_") or exc.code in {
        "PRODUCT_NOT_PROVIDER",
        "PROVIDER_DISABLED",
    }:
        return "Sản phẩm tạm thời chưa thể xử lý. Vui lòng thử lại sau hoặc liên hệ hỗ trợ."
    return exc.message


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
            "Website chưa trả thông tin tài khoản ngay lúc này. Vui lòng xem lại trong mục Đơn hàng."
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
        "🔐 <b>THÔNG TIN TÀI KHOẢN</b>"
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
        await notify_admin(
            "⚠️ <b>ĐƠN HÀNG CẦN KIỂM TRA</b>\n\n"
            f"👤 Telegram ID: <code>{telegram_id}</code>\n"
            f"🧾 Mã đơn: <code>{html.escape(pending.order_id)}</code>\n"
            f"📌 Mã lỗi: <code>{html.escape(exc.code)}</code>\n"
            f"{html.escape(exc.message)}"
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

async def show_catalog(message: Message, *, edit: bool = False) -> None:
    try:
        touch_user(message.from_user.id)
        data = await api.catalog(message.from_user.id)
        products = [item for item in data.get("products", []) if isinstance(item, dict)]
        menu = CatalogMenu(
            key=secrets.token_hex(3),
            products=products,
            discount_percent=int(data.get("discountPercent") or 0),
            created_at=time.time(),
        )
        menus[message.from_user.id] = menu
        if not products:
            text = "📦 Hiện chưa có sản phẩm nào đang mở bán."
            if edit:
                await message.edit_text(text, reply_markup=back_to_products_keyboard())
            else:
                await message.answer(text, reply_markup=main_keyboard(message.from_user.id))
            return
        total_pages = max(1, (len(products) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
        text = (
            f"📦 <b>DANH SÁCH SẢN PHẨM</b> · {len(products)} sản phẩm\n"
            f"Trang 1/{total_pages}\n\nChọn sản phẩm để xem chi tiết và mua tự động."
        )
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
    touch_user(message.from_user.id)
    await message.answer(
        "🛍 <b>SHOP TÀI KHOẢN</b>\n\n"
        "Bạn chỉ cần Telegram để mua hàng. Chọn sản phẩm, nạp tiền bằng QR và bot sẽ tự động xử lý rồi gửi tài khoản.\n\n"
        "Chỉ sử dụng các tài khoản bạn có quyền phân phối và tuân thủ điều khoản của nhà cung cấp.",
        reply_markup=main_keyboard(message.from_user.id),
    )


@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Trợ giúp")
async def help_handler(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>Hướng dẫn</b>\n\n"
        "1. Chọn 📦 Sản phẩm.\n"
        "2. Bấm sản phẩm cần mua.\n"
        "3. Nếu chưa đủ số dư, chọn 💳 Nạp tiền và chuyển khoản đúng nội dung.\n"
        "4. SePay tự động xác nhận; sau đó bot xử lý và gửi tài khoản cho bạn.\n\n"
        "Lệnh: /products, /deposit, /balance, /orders",
        reply_markup=main_keyboard(message.from_user.id),
    )


@dp.message(Command("products"))
@dp.message(F.text == "📦 Sản phẩm")
async def products_handler(message: Message) -> None:
    await show_catalog(message)


@dp.message(Command("balance"))
@dp.message(F.text == "💰 Số dư")
async def balance_handler(message: Message) -> None:
    try:
        data = await api.balance(message.from_user.id)
        await message.answer(
            "💰 <b>SỐ DƯ CỦA BẠN</b>\n\n"
            f"Số dư: <b>{money(data.get('balance'))}</b>",
            reply_markup=main_keyboard(message.from_user.id),
        )
    except ApiError as exc:
        await handle_api_error(message, exc)


@dp.message(Command("orders"))
@dp.message(F.text == "🧾 Đơn hàng")
async def orders_handler(message: Message) -> None:
    try:
        data = await api.orders(message.from_user.id)
        orders = [item for item in data.get("orders", []) if isinstance(item, dict)]
        if not orders:
            await message.answer("🧾 Bạn chưa có đơn hàng nào.", reply_markup=main_keyboard(message.from_user.id))
            return
        lines = ["🧾 <b>LỊCH SỬ ĐƠN HÀNG</b>", ""]
        for index, order in enumerate(orders[:10], start=1):
            lines.append(
                f"<b>{index}.</b> <code>{html.escape(str(order.get('orderId') or ''))}</code>\n"
                f"📦 {html.escape(str(order.get('productName') or 'Sản phẩm'))}\n"
                f"💵 {money(order.get('price'))} · SL {int(order.get('quantity') or 1)}\n"
                f"📌 {html.escape(str(order.get('status') or 'Đang xử lý'))} · {vi_time(order.get('timestamp'))}\n"
            )
        await answer_in_chunks(message, "\n".join(lines), main_keyboard(message.from_user.id))
    except ApiError as exc:
        await handle_api_error(message, exc)


@dp.message(Command("admin"))
@dp.message(F.text == "🛠 Quản trị")
async def admin_handler(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Bạn không có quyền sử dụng mục này.", reply_markup=main_keyboard(message.from_user.id))
        return
    try:
        data = await api.admin_stats(message.from_user.id)
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        customers = [item for item in data.get("customers", []) if isinstance(item, dict)]
        lines = [
            "🛠 <b>QUẢN TRỊ SHOP</b>",
            "",
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
        await answer_in_chunks(message, "\n".join(lines), main_keyboard(message.from_user.id))
    except ApiError as exc:
        await handle_api_error(message, exc)


# ---------------------------------------------------------------------------
# Nạp tiền tự động qua VietQR + SePay
# ---------------------------------------------------------------------------

def deposit_keyboard(memo: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Tôi đã chuyển khoản", callback_data=f"deposit_check|{memo}")],
            [InlineKeyboardButton(text="💰 Kiểm tra số dư", callback_data="balance")],
        ]
    )


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
        "✅ <b>NẠP TIỀN THÀNH CÔNG</b>\n\n"
        f"Đã cộng: <b>+{money(data.get('amount'))}</b>\n"
        f"Số dư mới: <b>{money(data.get('balance'))}</b>\n"
        f"Mã nạp: <code>{html.escape(memo)}</code>{pending_note}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📦 Mua tài khoản ngay", callback_data="products")]]
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
async def deposit_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(DepositStates.amount)
    await message.answer(
        f"💳 <b>NẠP TIỀN TỰ ĐỘNG</b>\n\nNhập số tiền muốn nạp, tối thiểu <b>{money(MIN_DEPOSIT)}</b>.\n"
        "Ví dụ: <code>50000</code>",
        reply_markup=main_keyboard(message.from_user.id),
    )


@dp.message(DepositStates.amount)
async def deposit_amount_handler(message: Message, state: FSMContext) -> None:
    amount = parse_amount(str(message.text or ""))
    if amount < MIN_DEPOSIT:
        await message.answer(f"Số tiền tối thiểu là {money(MIN_DEPOSIT)}. Vui lòng nhập lại.")
        return
    try:
        data = await api.create_deposit(message.from_user.id, amount)
        await state.clear()
        memo = str(data.get("memo") or "")
        expires_at = int(data.get("expiresAt") or 0)
        bank = data.get("bank") if isinstance(data.get("bank"), dict) else {}
        caption = (
            "💳 <b>QUÉT QR ĐỂ NẠP TIỀN</b>\n\n"
            f"Số tiền: <b>{money(data.get('amount') or amount)}</b>\n"
            f"Ngân hàng: <b>MB Bank</b>\n"
            f"Số tài khoản: <code>{html.escape(str(bank.get('account') or ''))}</code>\n"
            f"Chủ tài khoản: <b>{html.escape(str(bank.get('accountName') or ''))}</b>\n"
            f"Nội dung chuyển khoản: <code>{html.escape(memo)}</code>\n\n"
            "Giữ nguyên số tiền và nội dung chuyển khoản. Bot sẽ tự động cộng tiền sau khi SePay xác nhận."
        )
        qr_url = str(data.get("qrUrl") or "")
        if qr_url:
            await message.answer_photo(
                photo=URLInputFile(qr_url),
                caption=caption,
                reply_markup=deposit_keyboard(memo),
            )
        else:
            await message.answer(caption, reply_markup=deposit_keyboard(memo))
        await notify_admin(
            "📥 <b>CÓ YÊU CẦU NẠP TIỀN MỚI</b>\n\n"
            f"👤 Telegram ID: <code>{message.from_user.id}</code>\n"
            f"💰 Số tiền: <b>{money(data.get('amount') or amount)}</b>\n"
            f"📝 Mã nạp: <code>{html.escape(memo)}</code>"
        )
        key = (message.from_user.id, memo)
        old_task = deposit_watch_tasks.pop(key, None)
        if old_task:
            old_task.cancel()
        deposit_watch_tasks[key] = asyncio.create_task(
            watch_deposit(message.from_user.id, memo, expires_at)
        )
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
        f"📦 <b>DANH SÁCH SẢN PHẨM</b> · {len(menu.products)} sản phẩm\n"
        f"Trang {page + 1}/{total_pages}\n\nChọn sản phẩm để xem chi tiết và mua tự động.",
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
        "🛒 <b>XÁC NHẬN ĐƠN HÀNG</b>\n\n"
        f"Sản phẩm: <b>{html.escape(str(product.get('name') or 'Sản phẩm'))}</b>\n"
        f"Số lượng: <b>{quantity}</b>\n"
        f"Tạm tính: <b>{money(total)}</b>\n\n"
        "Giá cuối cùng và tồn kho sẽ được website kiểm tra lại khi thanh toán.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Xác nhận mua", callback_data=f"confirm|{menu.key}|{index}|{quantity}")],
                [InlineKeyboardButton(text="‹ Quay lại", callback_data=f"product|{menu.key}|{index}")],
            ]
        ),
    )


@dp.callback_query(F.data.startswith("confirm|"))
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
        f"tg_{callback.from_user.id}_{secrets.token_hex(10)}",
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
                    "💳 <b>SỐ DƯ CHƯA ĐỦ</b>\n\n"
                    f"Cần khoảng: <b>{money(expected_total)}</b>\n"
                    f"Đang có: <b>{money(balance)}</b>\n"
                    f"Cần nạp thêm: <b>{money(expected_total - balance)}</b>",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="💳 Nạp tiền", callback_data="deposit")],
                            [InlineKeyboardButton(text="📦 Quay lại sản phẩm", callback_data="products")],
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
            if exc.code == "PROVIDER_PURCHASE_UNCERTAIN":
                await notify_admin(
                    "⚠️ <b>ĐƠN NGUỒN API CẦN KIỂM TRA</b>\n\n"
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


@dp.callback_query(F.data == "products")
async def products_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await show_catalog(callback.message, edit=True)


@dp.callback_query(F.data == "balance")
async def balance_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await balance_handler(callback.message)


@dp.callback_query(F.data == "orders")
async def orders_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await orders_handler(callback.message)


@dp.callback_query(F.data == "deposit")
async def deposit_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await deposit_handler(callback.message, state)


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
    for key in ("content", "description", "transferContent", "transactionContent", "referenceCode"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            content = value.strip()
            break
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


@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "service": "telegram-account-shop"}


@app.get("/sepay/webhook")
async def sepay_webhook_get() -> dict[str, Any]:
    return {"ok": True, "message": "SePay webhook endpoint is alive. Use POST."}


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
        return {"ok": True, "message": "ignored"}

    # Mã nạp mới có dạng Chuyentien_<5 chữ số>; vẫn nhận mã TG cũ cho giao dịch đang chờ.
    new_memo_match = re.search(r"(Chuyentien_?\d{5})", content, re.IGNORECASE)
    if new_memo_match:
        suffix_match = re.search(r"(\d{5})$", new_memo_match.group(1))
        memo = f"Chuyentien_{suffix_match.group(1)}" if suffix_match else ""
        try:
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
        return {"ok": True, "message": "not a telegram deposit"}
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
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Mở menu chính"),
            BotCommand(command="products", description="Xem tài khoản đang bán"),
            BotCommand(command="deposit", description="Nạp tiền tự động"),
            BotCommand(command="balance", description="Xem số dư"),
            BotCommand(command="orders", description="Xem đơn hàng"),
            BotCommand(command="admin", description="Quản trị shop"),
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
