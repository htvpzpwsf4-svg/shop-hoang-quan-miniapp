import os
import re
import json
import html
import time
import hmac
import hashlib
import queue
import sqlite3
import threading
import urllib.parse
from io import BytesIO
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

import requests
from flask import Flask, render_template_string, request, redirect, url_for, jsonify, flash, session, send_from_directory, Response, send_file

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG / CONSTANTS
# =========================================================
APP_TITLE = "SHOP HOANG QUAN CHEAT - TELEGRAM BOT SYSTEM"
DB_FILE = os.environ.get("SHOP_DB_FILE", "tele_shop.sqlite3")
CONFIG_FILE = os.environ.get("SHOP_CONFIG_FILE", "tele_shop_config.json")

DEFAULT_VIETQR_TEMPLATE = "compact2"

# Bank IDs used by the VietQR image endpoint.
VIETQR_BANKS = [
    ("MB Bank", "MB"),
    ("Vietcombank", "VCB"),
    ("Techcombank", "TCB"),
    ("ACB", "ACB"),
    ("VietinBank", "ICB"),
    ("BIDV", "BIDV"),
    ("Sacombank", "STB"),
    ("TPBank", "TPB"),
]

CODERENT_V2_CODES = {
    "MB Bank": "MB",
    "Vietcombank": "VCB",
    "BIDV": "BIDV",
    "Techcombank": "TCB",
    "ACB": "ACB",
    "TPBank": "TPB",
    "VPBank": "VPB",
    "OCB": "OCB",
    "MSB": "MSB",
    "ViettelMoney": "VTM",
    "SeABank": "SEA",
    "TheSieuRe": "TSR",
}

NAPID_RE = re.compile(r"\bNapid\s+(\d+)\b", re.IGNORECASE)

ADMIN_MODE = set()
LOG_QUEUE = queue.Queue()

def log_msg(msg: str):
    formatted = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        LOG_QUEUE.put(formatted)
    except Exception:
        print(formatted)

# =========================================================
# HELPERS
# =========================================================
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def safe_int(x, default=0) -> int:
    try:
        return int(str(x).replace(",", "").strip())
    except Exception:
        return default

def vietqr_image_url(
    bank_id: str,
    account_no: str,
    template: str,
    amount: int,
    add_info: str,
    account_name: str,
) -> str:
    base = f"https://img.vietqr.io/image/{bank_id}-{account_no}-{template}.png"
    qs = {
        "amount": str(max(0, int(amount))),
        "addInfo": add_info,
        "accountName": account_name or "",
    }
    return base + "?" + urllib.parse.urlencode(qs, safe="")

def download_vietqr_image(url: str) -> BytesIO:
    """Download a VietQR PNG; if the image endpoint returns SVG, ask VietQR's
    generate API for a PNG data URL. This avoids CairoSVG/Pillow dependencies.
    """
    response = requests.get(
        url, timeout=(8, 20),
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; SHOP-HOANG-QUAN/1.0)",
            "Accept": "image/png,image/jpeg,image/webp,*/*",
        },
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    data = response.content or b""

    # Trust the file signature (magic bytes) over Content-Type: some VietQR
    # gateways incorrectly label a real PNG as image/svg+xml.
    png_sig = b"\x89PNG\r\n\x1a\n"
    jpeg_sig = b"\xff\xd8\xff"
    webp_sig = b"RIFF"
    if data.startswith(b"\xef\xbf\xbdPNG\r\n\x1a\n"):
        # Recover a common UTF-8 replacement-character corruption of PNG's
        # first byte, if a proxy decoded/re-encoded the binary response.
        data = b"\x89" + data[3:]

    # Some wrappers prepend a short BOM/whitespace prefix before image bytes.
    for sig in (png_sig, jpeg_sig, webp_sig):
        pos = data.find(sig, 0, 32)
        if pos > 0:
            data = data[pos:]
            break
    has_image_magic = (
        data.startswith(png_sig)
        or data.startswith(jpeg_sig)
        or (data.startswith(webp_sig) and data[8:12] == b"WEBP")
    )
    sample = data[:4096].lstrip().lower()
    looks_like_svg = (not has_image_magic) and (
        "image/svg+xml" in content_type
        or sample.startswith(b"<svg")
        or (sample.startswith(b"<?xml") and b"<svg" in sample)
        or (b"<svg" in sample and b"</svg>" in sample)
    )

    # Some environments/endpoints return SVG even when .png is requested.
    # Use VietQR's own PNG generator instead of requiring native image libraries.
    if looks_like_svg:
        parsed = urllib.parse.urlparse(url)
        parts = parsed.path.rstrip("/").split("/")[-1].split("-")
        if len(parts) < 3:
            raise ValueError("Không đọc được ngân hàng/tài khoản từ URL VietQR.")
        bank_id, account_no = parts[0], parts[1]
        params = urllib.parse.parse_qs(parsed.query)
        amount = safe_int(params.get("amount", ["0"])[0], 0)
        add_info = params.get("addInfo", [""])[0]
        account_name = params.get("accountName", [""])[0]
        bank_bins = {
            "MB": "970422", "VCB": "970436", "TCB": "970407",
            "ACB": "970416", "ICB": "970415", "BIDV": "970418",
            "STB": "970403", "TPB": "970423", "VPB": "970432",
            "OCB": "970448", "MSB": "970426", "SEA": "970440",
        }
        acq_id = bank_bins.get(bank_id.upper())
        if not acq_id:
            raise ValueError(
                f"VietQR trả SVG và chưa có mã BIN cho ngân hàng {bank_id!r}."
            )
        generated = requests.post(
            "https://api.vietqr.io/v2/generate",
            json={
                "accountNo": account_no,
                "accountName": account_name,
                "acqId": acq_id,
                "amount": amount,
                "addInfo": add_info,
                "format": "text",
                "template": "compact",
            },
            timeout=(8, 25),
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; SHOP-HOANG-QUAN/1.0)",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        generated.raise_for_status()
        payload = generated.json()
        qr_data = payload.get("data", {}).get("qrDataURL", "")
        if not isinstance(qr_data, str) or "base64," not in qr_data:
            raise ValueError(
                "VietQR generate API không trả qrDataURL base64 hợp lệ: "
                + str(payload.get("desc") or payload.get("message") or payload.get("code") or "không rõ lỗi")[:160]
            )
        import base64
        data = base64.b64decode(qr_data.split("base64,", 1)[1])

    signatures = (
        data.startswith(png_sig),
        data.startswith(jpeg_sig),
        data.startswith(webp_sig) and data[8:12] == b"WEBP",
    )
    if not data or not any(signatures):
        preview = re.sub(r"\s+", " ", data[:100].decode("utf-8", "replace"))
        raise ValueError(
            "VietQR không trả về ảnh PNG/JPEG/WEBP hợp lệ "
            f"(Content-Type={content_type or 'missing'}, preview={preview!r})."
        )
    image = BytesIO(data)
    image.name = "payment-qr.png" if data.startswith(png_sig) else "payment-qr-image"
    image.seek(0)
    return image

def build_coderent_v2_url(bank_name: str, token: str) -> str:
    code = CODERENT_V2_CODES.get(bank_name)
    if not code:
        raise ValueError(f"Bank V2 không hỗ trợ: {bank_name}")
    return f"https://api.coderent.one/api/{code}/historyv2/{token}"

# =========================================================
# DB
# =========================================================
def db_connect():
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db_connect()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance INTEGER DEFAULT 0,
        ref_by INTEGER,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS categories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        is_active INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER,
        name TEXT NOT NULL,
        price INTEGER NOT NULL,
        stock INTEGER DEFAULT 0,
        deliver_text TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        product_id INTEGER,
        price INTEGER,
        discount INTEGER DEFAULT 0,
        final_price INTEGER,
        coupon_code TEXT,
        status TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS deposits(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        description TEXT,
        trans_id TEXT,
        status TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS coupons(
        code TEXT PRIMARY KEY,
        discount_amount INTEGER DEFAULT 0,
        discount_percent INTEGER DEFAULT 0,
        apply_product_id INTEGER DEFAULT NULL,
        min_order INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        max_uses INTEGER DEFAULT 0,
        used_count INTEGER DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS affiliate(
        user_id INTEGER PRIMARY KEY,
        percent INTEGER DEFAULT 5
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS affiliate_earnings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_user_id INTEGER,
        order_id INTEGER,
        amount INTEGER,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS bank_seen(
        transaction_id TEXT PRIMARY KEY,
        seen_at TEXT
    )
    """)

    con.commit()
    con.close()

# ---------- user helpers ----------
def upsert_user(tg_user):
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (tg_user.id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users(user_id, username, first_name, balance, ref_by, created_at) VALUES(?,?,?,?,?,?)",
            (tg_user.id, tg_user.username or "", tg_user.first_name or "", 0, None, now_str()),
        )
    else:
        cur.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (tg_user.username or "", tg_user.first_name or "", tg_user.id),
        )
    con.commit()
    con.close()

def set_ref_if_empty(user_id: int, ref_by: int):
    if user_id == ref_by:
        return
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT ref_by FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    if r and r["ref_by"] is None:
        cur.execute("UPDATE users SET ref_by=? WHERE user_id=?", (ref_by, user_id))
    con.commit()
    con.close()

def get_user(user_id: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    con.close()
    return r

def get_balance(user_id: int) -> int:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    con.close()
    return int(r["balance"] or 0) if r else 0

def add_balance(user_id: int, amount: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO users(user_id, username, first_name, balance, ref_by, created_at)
        VALUES(?, '', '', 0, NULL, ?)
    """, (int(user_id), now_str()))
    cur.execute("""
        UPDATE users
        SET balance = COALESCE(balance,0) + ?
        WHERE user_id=?
    """, (int(amount), int(user_id)))
    con.commit()
    con.close()

def subtract_balance(user_id: int, amount: int) -> bool:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    if not r:
        con.close()
        return False
    bal = int(r["balance"] or 0)
    if bal < amount:
        con.close()
        return False
    cur.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (int(amount), user_id))
    con.commit()
    con.close()
    return True

# ---------- categories/products ----------
def add_category(name: str) -> int:
    con = db_connect()
    cur = con.cursor()
    cur.execute("INSERT INTO categories(name, is_active) VALUES(?,1)", (name,))
    con.commit()
    cid = cur.lastrowid
    con.close()
    return cid

def edit_category(cid: int, name: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute("UPDATE categories SET name=? WHERE id=?", (name, cid))
    con.commit()
    con.close()

def delete_category(cid: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute("UPDATE categories SET is_active=0 WHERE id=?", (cid,))
    con.commit()
    con.close()

def list_categories():
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT id, name FROM categories WHERE is_active=1 ORDER BY id DESC")
    rows = cur.fetchall()
    con.close()
    return rows

def add_product(category_id: int, name: str, price: int, stock: int, deliver_text: str) -> int:
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO products(category_id, name, price, stock, deliver_text, is_active) VALUES(?,?,?,?,?,1)",
        (category_id, name, int(price), int(stock), deliver_text or ""),
    )
    con.commit()
    pid = cur.lastrowid
    con.close()
    return pid

def edit_product(pid: int, name: str, price: int, stock: int, deliver_text: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "UPDATE products SET name=?, price=?, stock=?, deliver_text=? WHERE id=?",
        (name, int(price), int(stock), deliver_text or "", pid),
    )
    con.commit()
    con.close()

def delete_product(pid: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute("UPDATE products SET is_active=0 WHERE id=?", (pid,))
    con.commit()
    con.close()

def list_all_products():
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        SELECT p.id, p.category_id, p.name, p.price, p.stock, p.deliver_text, c.name AS category_name
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.is_active=1
        ORDER BY p.id DESC
    """)
    rows = cur.fetchall()
    con.close()
    return rows

def get_product(pid: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM products WHERE id=? AND is_active=1", (pid,))
    r = cur.fetchone()
    con.close()
    return r

def decrement_stock(pid: int) -> bool:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT stock FROM products WHERE id=?", (pid,))
    r = cur.fetchone()
    if not r:
        con.close()
        return False
    stock = int(r["stock"] or 0)
    if stock <= 0:
        con.close()
        return False
    cur.execute("UPDATE products SET stock = stock - 1 WHERE id=?", (pid,))
    con.commit()
    con.close()
    return True

# ---------- coupons ----------
def upsert_coupon(
    code: str,
    discount_amount: int,
    discount_percent: int,
    apply_product_id: Optional[int],
    min_order: int,
    max_uses: int,
):
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO coupons(code, discount_amount, discount_percent, apply_product_id, min_order, is_active, max_uses, used_count)
        VALUES(?,?,?,?,?,1,?,0)
        ON CONFLICT(code) DO UPDATE SET
            discount_amount=excluded.discount_amount,
            discount_percent=excluded.discount_percent,
            apply_product_id=excluded.apply_product_id,
            min_order=excluded.min_order,
            max_uses=excluded.max_uses,
            is_active=1
    """, (code.upper(), int(discount_amount), int(discount_percent), apply_product_id, int(min_order), int(max_uses)))
    con.commit()
    con.close()

def disable_coupon(code: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute("UPDATE coupons SET is_active=0 WHERE code=?", (code.upper(),))
    con.commit()
    con.close()

def list_coupons():
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM coupons ORDER BY code DESC")
    rows = cur.fetchall()
    con.close()
    return rows

def get_coupon(code: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM coupons WHERE code=? AND is_active=1", (code.upper(),))
    r = cur.fetchone()
    con.close()
    return r

def mark_coupon_used(code: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute("UPDATE coupons SET used_count = used_count + 1 WHERE code=?", (code.upper(),))
    con.commit()
    con.close()

# ---------- affiliate ----------
def get_aff_percent(user_id: int) -> int:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT percent FROM affiliate WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    if not r:
        cur.execute("INSERT OR IGNORE INTO affiliate(user_id, percent) VALUES(?,5)", (user_id,))
        con.commit()
        con.close()
        return 5
    con.close()
    return int(r["percent"] or 5)

def set_aff_percent(user_id: int, percent: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO affiliate(user_id, percent) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET percent=excluded.percent",
        (user_id, int(percent)),
    )
    con.commit()
    con.close()

def add_aff_earning(referrer_id: int, referred_user_id: int, order_id: int, amount: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO affiliate_earnings(referrer_id, referred_user_id, order_id, amount, created_at) VALUES(?,?,?,?,?)",
        (referrer_id, referred_user_id, order_id, int(amount), now_str()),
    )
    con.commit()
    con.close()

# ---------- orders/deposits ----------
def create_order(
    user_id: int,
    product_id: int,
    price: int,
    discount: int,
    final_price: int,
    coupon_code: Optional[str],
    status: str,
) -> int:
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO orders(user_id, product_id, price, discount, final_price, coupon_code, status, created_at)
        VALUES(?,?,?,?,?,?,?,?)
    """, (user_id, product_id, int(price), int(discount), int(final_price), coupon_code, status, now_str()))
    con.commit()
    oid = cur.lastrowid
    con.close()
    return oid

def list_orders(user_id: Optional[int] = None, limit: int = 50):
    con = db_connect()
    cur = con.cursor()
    if user_id:
        cur.execute("""
            SELECT o.id, o.user_id, o.final_price, o.status, o.created_at, p.name AS product_name
            FROM orders o
            LEFT JOIN products p ON p.id=o.product_id
            WHERE o.user_id=?
            ORDER BY o.id DESC
            LIMIT ?
        """, (user_id, int(limit)))
    else:
        cur.execute("""
            SELECT o.id, o.user_id, o.final_price, o.status, o.created_at, p.name AS product_name
            FROM orders o
            LEFT JOIN products p ON p.id=o.product_id
            ORDER BY o.id DESC
            LIMIT ?
        """, (int(limit),))
    rows = cur.fetchall()
    con.close()
    return rows

def create_deposit(user_id: int, amount: int, description: str, trans_id: str, status: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO deposits(user_id, amount, description, trans_id, status, created_at)
        VALUES(?,?,?,?,?,?)
    """, (user_id, int(amount), description or "", trans_id or "", status, now_str()))
    con.commit()
    con.close()

def list_deposits(limit: int = 50):
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM deposits ORDER BY id DESC LIMIT ?", (int(limit),))
    rows = cur.fetchall()
    con.close()
    return rows

# ---------- bank seen ----------
def bank_tx_seen(transaction_id: str) -> bool:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT transaction_id FROM bank_seen WHERE transaction_id=?", (transaction_id,))
    r = cur.fetchone()
    con.close()
    return r is not None

def mark_bank_tx_seen(transaction_id: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO bank_seen(transaction_id, seen_at) VALUES(?,?)", (transaction_id, now_str()))
    con.commit()
    con.close()

def credit_bank_deposit_once(user_id: int, amount: int, description: str, transaction_id: str) -> bool:
    """Atomically de-duplicate a transaction, credit balance and save its receipt."""
    con = db_connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        cur = con.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO bank_seen(transaction_id, seen_at) VALUES(?,?)",
            (transaction_id, now_str()),
        )
        if cur.rowcount == 0:
            con.rollback()
            return False
        cur.execute(
            """INSERT OR IGNORE INTO users
               (user_id, username, first_name, balance, ref_by, created_at)
               VALUES(?, '', '', 0, NULL, ?)""",
            (int(user_id), now_str()),
        )
        cur.execute(
            "UPDATE users SET balance=COALESCE(balance,0)+? WHERE user_id=?",
            (int(amount), int(user_id)),
        )
        cur.execute(
            """INSERT INTO deposits(user_id, amount, description, trans_id, status, created_at)
               VALUES(?,?,?,?,?,?)""",
            (int(user_id), int(amount), description or "", transaction_id, "SUCCESS", now_str()),
        )
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

# =========================================================
# CONFIG DATACLASS
# =========================================================
@dataclass
class BotConfig:
    admin_id: int = 0
    bot_token: str = ""
    log_group_id: int = 0
    start_video_path: str = ""
    vietqr_bank_name: str = "MB Bank"
    vietqr_bank_id: str = "mbbank"
    vietqr_stk: str = ""
    vietqr_ctk: str = ""
    vietqr_template: str = DEFAULT_VIETQR_TEMPLATE
    bank_v3_name: str = "MB Bank"
    bank_v3_token: str = ""
    poll_interval: int = 15
    auto_bank_api_text: str = ""

def _apply_environment_config(cfg: BotConfig) -> BotConfig:
    env_map = {
        "BOT_TOKEN": ("bot_token", str),
        "ADMIN_ID": ("admin_id", lambda v: safe_int(v, cfg.admin_id)),
        "LOG_GROUP_ID": ("log_group_id", lambda v: safe_int(v, cfg.log_group_id)),
        "VIETQR_BANK_NAME": ("vietqr_bank_name", str),
        "VIETQR_BANK_ID": ("vietqr_bank_id", str),
        "VIETQR_STK": ("vietqr_stk", str),
        "VIETQR_CTK": ("vietqr_ctk", str),
        "VIETQR_TEMPLATE": ("vietqr_template", str),
        "BANK_V3_NAME": ("bank_v3_name", str),
        "BANK_V3_TOKEN": ("bank_v3_token", str),
        "BANK_POLL_INTERVAL": ("poll_interval", lambda v: max(5, safe_int(v, cfg.poll_interval))),
    }
    for env_key, (attr, converter) in env_map.items():
        value = os.environ.get(env_key)
        if value is not None and value != "":
            setattr(cfg, attr, converter(value.strip()))
    legacy = {"mbbank": "MB", "vietcombank": "VCB", "techcombank": "TCB", "vietinbank": "ICB", "sacombank": "STB", "tpbank": "TPB"}
    cfg.vietqr_bank_id = legacy.get(str(cfg.vietqr_bank_id).lower(), cfg.vietqr_bank_id)
    return cfg


def load_config() -> BotConfig:
    if not os.path.exists(CONFIG_FILE):
        return _apply_environment_config(BotConfig())
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            j = json.load(f)
        cfg = BotConfig(**j)
        return _apply_environment_config(cfg)
    except Exception:
        return _apply_environment_config(BotConfig())

def save_config(cfg: BotConfig):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg.__dict__, f, ensure_ascii=False, indent=2)

# =========================================================
# BOT RUNNER & BANK POLL THREAD
# =========================================================
class BotRunner:
    def __init__(self):
        self.cfg: BotConfig = load_config()
        self._app: Optional[Application] = None
        self._bot_thread: Optional[threading.Thread] = None
        self._bank_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._bot_loop: Optional[asyncio.AbstractEventLoop] = None
        self._bot_username: Optional[str] = None
        self._buy_cache: Dict[int, Dict[str, Any]] = {}

    def user_main_keyboard(self, user_id: int) -> InlineKeyboardMarkup:
        mini_url = os.environ.get("SHOP_MINIAPP_URL", "").strip()
        if not mini_url.startswith("https://"):
            mini_url = ""
        open_button = (
            InlineKeyboardButton("🔴 MỞ SHOP (MINI APP)", web_app=WebAppInfo(url=mini_url))
            if mini_url else
            InlineKeyboardButton("🔴 MỞ SHOP (MINI APP)", callback_data="u:miniapp_setup")
        )
        rows = [
            [open_button],
            [InlineKeyboardButton("🛒 Mua Hàng", callback_data="u:cats"),
             InlineKeyboardButton("🛡 Tài Khoản", callback_data="u:account")],
            [InlineKeyboardButton("🇻🇳 Nạp VNĐ", callback_data="u:deposit")],
            [InlineKeyboardButton("⏰ Lịch Sử", callback_data="u:history"),
             InlineKeyboardButton("🔗 Top Nạp", callback_data="u:top")],
        ]
        if user_id in ADMIN_MODE or user_id == int(self.cfg.admin_id or 0):
            rows.append([InlineKeyboardButton("🛠 ADMIN PANEL", callback_data="a:panel")])
        return InlineKeyboardMarkup(rows)

    def admin_keyboard(self) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton("➕ Thêm chuyên mục", callback_data="a:addcat"),
             InlineKeyboardButton("✏️ Sửa chuyên mục", callback_data="a:editcat")],
            [InlineKeyboardButton("🗑 Xóa chuyên mục", callback_data="a:delcat")],
            [InlineKeyboardButton("➕ Thêm sản phẩm", callback_data="a:addprod"),
             InlineKeyboardButton("✏️ Sửa sản phẩm", callback_data="a:editprod")],
            [InlineKeyboardButton("🗑 Xóa sản phẩm", callback_data="a:delprod")],
            [InlineKeyboardButton("🏷 Mã giảm giá (Coupon)", callback_data="a:addcoupon")],
            [InlineKeyboardButton("📢 Gửi thông báo toàn bộ (Broadcast)", callback_data="a:broadcast")],
            [InlineKeyboardButton("⬅️ VỀ MENU CHÍNH", callback_data="u:home")],
        ]
        return InlineKeyboardMarkup(rows)

    async def _send_log_group(self, bot, text: str):
        gid = int(self.cfg.log_group_id or 0)
        if gid == 0:
            return
        try:
            await bot.send_message(chat_id=gid, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            log_msg(f"Send log group error: {e}")

    async def _notify_purchase_log(self, bot, user_id: int, order_id: int, prod_name: str, price: int, discount: int, final_price: int, coupon_code: Optional[str]):
        u = get_user(user_id)
        uname = u["username"] if u and u["username"] else ""
        fname = u["first_name"] if u and u["first_name"] else ""
        user_line = f"<code>{user_id}</code>"
        if uname: user_line += f" • @{uname}"
        elif fname: user_line += f" • {fname}"

        text = (
            "🚀 <b>ĐƠN HÀNG MỚI - SHOP HOANG QUAN CHEAT</b>\n"
            f"🧾 Mã đơn: <b>#{order_id}</b>\n"
            f"👤 Khách hàng: {user_line}\n"
            f"📦 Sản phẩm: <b>{prod_name}</b>\n"
            f"💵 Giá gốc: <b>{price:,}đ</b>\n"
            f"🏷 Giảm giá: <b>{discount:,}đ</b>\n"
            f"✅ Thực thu: <b>{final_price:,}đ</b>\n"
            f"🎟 Mã giảm giá: <code>{coupon_code or 'Không dùng'}</code>\n"
            f"⏰ Thời gian: <code>{now_str()}</code>"
        )
        await self._send_log_group(bot, text)

    async def _notify_deposit_log(self, bot, user_id: int, amount: int, tid: str, desc: str):
        u = get_user(user_id)
        uname = u["username"] if u and u["username"] else ""
        fname = u["first_name"] if u and u["first_name"] else ""
        user_line = f"<code>{user_id}</code>"
        if uname: user_line += f" • @{uname}"
        elif fname: user_line += f" • {fname}"

        text = (
            "💳 <b>BIẾN ĐỘNG SỐ DƯ (NẠP TIỀN TỰ ĐỘNG)</b>\n"
            f"👤 Khách hàng: {user_line}\n"
            f"💰 Số tiền nạp: <b>+{amount:,}đ</b>\n"
            f"🧾 Mã giao dịch: <code>{tid}</code>\n"
            f"📝 Nội dung CK: <code>{(desc or '')[:300]}</code>\n"
            f"⏰ Thời gian: <code>{now_str()}</code>"
        )
        await self._send_log_group(bot, text)

    def _deposit_totals(self, user_id: int) -> Tuple[int, int]:
        con = db_connect()
        try:
            cur = con.cursor()
            month_prefix = datetime.now().strftime("%Y-%m")
            cur.execute(
                """SELECT COALESCE(SUM(amount), 0) AS total,
                          COALESCE(SUM(CASE WHEN substr(created_at,1,7)=? THEN amount ELSE 0 END),0) AS monthly
                   FROM deposits
                   WHERE user_id=? AND UPPER(status)='SUCCESS'""",
                (month_prefix, int(user_id)),
            )
            row = cur.fetchone()
            return int(row["total"] or 0), int(row["monthly"] or 0)
        finally:
            con.close()

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        upsert_user(user)
        if context.args:
            arg0 = context.args[0].strip()
            if arg0.lower().startswith("ref_"):
                ref_id = safe_int(arg0.split("_", 1)[1], 0)
                if ref_id > 0:
                    set_ref_if_empty(user.id, ref_id)

        try:
            if self.cfg.start_video_path and os.path.exists(self.cfg.start_video_path):
                with open(self.cfg.start_video_path, "rb") as video_file:
                    await update.message.reply_video(video=video_file, caption="🔥 CHÀO MỪNG ĐẾN VỚI SHOP HOANG QUAN CHEAT!")
        except Exception as e:
            log_msg(f"Send start video error: {e}")

        bal = get_balance(user.id)
        total_deposit, monthly_deposit = self._deposit_totals(user.id)
        first_name = (user.first_name or "bạn").replace("<", "&lt;").replace(">", "&gt;")
        txt = (
            f"👋 <b>HỆ THỐNG DỊCH VỤ TỰ ĐỘNG</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Chào <b>{first_name}</b>, hệ thống đã sẵn sàng.\n\n"
            f"🚀 Nhấn <b>Mở Shop (Mini App)</b> để mua hàng trực tiếp trong Telegram.\n\n"
            f"📦 <code>/ds</code> : Xem danh mục & sản phẩm\n"
            f"💳 <code>/nap</code> : Nạp tiền qua ngân hàng\n"
            f"🛒 <code>/buy</code> : Mua sản phẩm bằng số dư\n"
            f"🛡 <code>/me</code> : Xem tài khoản & số dư\n"
            f"🕘 <code>/history</code> : Xem lịch sử mua hàng\n"
            f"🏆 <code>/top</code> : Bảng xếp hạng top nạp\n\n"
            f"💰 Số dư hiện tại: <b>{bal:,}đ</b>"
        )
        await update.message.reply_text(
            txt,
            reply_markup=self.user_main_keyboard(user.id),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    async def cmd_ds(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cats = list_categories()
        if not cats:
            await update.message.reply_text("Shop hiện chưa có danh mục sản phẩm.", reply_markup=self.user_main_keyboard(update.effective_user.id))
            return
        rows = [[InlineKeyboardButton(f"📁 {c['name']}", callback_data=f"u:cat:{c['id']}")] for c in cats]
        await update.message.reply_text("📦 <b>DANH MỤC SẢN PHẨM</b>", reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)

    async def cmd_nap(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data["waiting_deposit_amount"] = True
        await update.message.reply_text("💳 Nhập số tiền muốn nạp (VNĐ):")

    async def cmd_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.cmd_ds(update, context)

    async def cmd_me(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        upsert_user(user)
        total, monthly = self._deposit_totals(user.id)
        await update.message.reply_text(
            f"🛡 <b>TÀI KHOẢN</b>\n🆔 <code>{user.id}</code>\n💰 Số dư: <b>{get_balance(user.id):,}đ</b>\n🏅 Tổng nạp: <b>{total:,}đ</b>\n📈 Tháng này: <b>{monthly:,}đ</b>",
            parse_mode=ParseMode.HTML, reply_markup=self.user_main_keyboard(user.id))

    async def cmd_top(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        con = db_connect()
        cur = con.cursor()
        cur.execute("""SELECT u.first_name, SUM(d.amount) AS total FROM deposits d
                       LEFT JOIN users u ON u.user_id=d.user_id WHERE UPPER(d.status)='SUCCESS'
                       GROUP BY d.user_id ORDER BY total DESC LIMIT 10""")
        rows = cur.fetchall()
        con.close()
        lines = ["🏆 <b>TOP NẠP TIỀN</b>"]
        lines.extend(f"{i}. {r['first_name'] or 'Khách hàng'} — <b>{int(r['total'] or 0):,}đ</b>" for i, r in enumerate(rows, 1))
        if not rows:
            lines.append("Chưa có giao dịch nạp thành công.")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=self.user_main_keyboard(update.effective_user.id))

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        orders = list_orders(update.effective_user.id, limit=15)
        if not orders:
            await update.message.reply_text("Bạn chưa có đơn hàng nào.", reply_markup=self.user_main_keyboard(update.effective_user.id))
            return
        lines = ["🧾 <b>LỊCH SỬ MUA HÀNG</b>"]
        lines.extend(f"#{o['id']} · {o['product_name'] or 'Sản phẩm'} · {int(o['final_price'] or 0):,}đ · {o['created_at']}" for o in orders)
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=self.user_main_keyboard(update.effective_user.id))

    async def cmd_checkadmin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        upsert_user(user)
        if user.id == int(self.cfg.admin_id or 0):
            ADMIN_MODE.add(user.id)
            await update.message.reply_text("✅ Đã bật chế độ Admin.", reply_markup=self.user_main_keyboard(user.id))
        else:
            await update.message.reply_text("❌ Bạn không phải admin hệ thống.")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        txt = (
            "📌 <b>HƯỚNG DẪN SỬ DỤNG BOT:</b>\n"
            "• /start - Khởi động lại Bot & Bảng điều khiển\n"
            "• /getchatid - Lấy Chat ID hiện tại\n"
            "• Quản lý chi tiết, thêm sản phẩm qua Web Dashboard Dashboard local."
        )
        await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

    async def cmd_getchatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        await update.message.reply_text(f"🆔 Chat ID của bạn: <code>{chat.id}</code>\n📝 Loại: <b>{chat.type}</b>", parse_mode=ParseMode.HTML)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        user = q.from_user
        upsert_user(user)
        data = q.data or ""

        if data == "u:home":
            bal = get_balance(user.id)
            txt = (
                f"🔥 <b>SHOP HOANG QUAN CHEAT - MENU CHÍNH</b> 🔥\n\n"
                f"👤 Tài khoản: <b>{user.first_name}</b> (<code>{user.id}</code>)\n"
                f"💰 Số dư: <b>{bal:,}đ</b>\n\n"
                f"⚡ Chọn mục bạn muốn thao tác bên dưới:"
            )
            await q.edit_message_text(txt, reply_markup=self.user_main_keyboard(user.id), parse_mode=ParseMode.HTML)
            return

        if data == "u:miniapp_setup":
            await q.message.reply_text("🔗 Mini App chưa có URL HTTPS. Sau khi triển khai hosting, hãy đặt biến SHOP_MINIAPP_URL rồi khởi động lại bot.")
            return

        if data == "u:account":
            balance = get_balance(user.id)
            total_deposit, monthly_deposit = self._deposit_totals(user.id)
            txt = (
                "🛡 <b>TÀI KHOẢN CỦA BẠN</b>\n"
                f"👤 Tên: <b>{user.first_name or 'Khách hàng'}</b>\n"
                f"🆔 ID: <code>{user.id}</code>\n"
                f"💰 Số dư: <b>{balance:,}đ</b>\n"
                f"🏅 Tổng nạp: <b>{total_deposit:,}đ</b>\n"
                f"📈 Tổng nạp tháng: <b>{monthly_deposit:,}đ</b>"
            )
            await q.edit_message_text(txt, reply_markup=self.user_main_keyboard(user.id), parse_mode=ParseMode.HTML)
            return

        if data == "u:top":
            con = db_connect()
            cur = con.cursor()
            cur.execute("""SELECT u.first_name, u.username, SUM(d.amount) AS total
                           FROM deposits d LEFT JOIN users u ON u.user_id=d.user_id
                           WHERE UPPER(d.status)='SUCCESS'
                           GROUP BY d.user_id ORDER BY total DESC LIMIT 10""")
            top_rows = cur.fetchall()
            con.close()
            lines = ["🏆 <b>TOP NẠP TIỀN</b>\n"]
            for i, row in enumerate(top_rows, 1):
                name = html.escape(row["first_name"] or ("@" + row["username"] if row["username"] else "Khách hàng"))
                lines.append(f"{i}. {name} — <b>{int(row['total'] or 0):,}đ</b>")
            if not top_rows:
                lines.append("Chưa có giao dịch nạp thành công.")
            await q.edit_message_text("\n".join(lines), reply_markup=self.user_main_keyboard(user.id), parse_mode=ParseMode.HTML)
            return

        if data == "u:cats":
            cats = list_categories()
            if not cats:
                await q.edit_message_text("❌ Hệ thống chưa có chuyên mục nào. Vui lòng quay lại sau!", reply_markup=self.user_main_keyboard(user.id))
                return
            rows = [[InlineKeyboardButton(f"📁 {c['name']}", callback_data=f"u:cat:{c['id']}")] for c in cats]
            rows.append([InlineKeyboardButton("⬅️ TRỞ VỀ MENU CHÍNH", callback_data="u:home")])
            await q.edit_message_text("📦 <b>DANH SÁCH CHUYÊN MỤC HÀNG:</b>\n\nVui lòng chọn chuyên mục bạn muốn mua:", reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
            return

        if data.startswith("u:cat:"):
            cid = safe_int(data.split(":")[2], 0)
            con = db_connect()
            cur = con.cursor()
            cur.execute("SELECT id, name, price, stock FROM products WHERE is_active=1 AND category_id=?", (cid,))
            prods = cur.fetchall()
            con.close()
            if not prods:
                await q.edit_message_text("❌ Chuyên mục này hiện tại đang hết hàng!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ QUAY LẠI CHUYÊN MỤC", callback_data="u:cats")]]))
                return
            rows = [[InlineKeyboardButton(f"🛒 {p['name']} — {p['price']:,}đ (Kho: {p['stock']})", callback_data=f"u:prod:{p['id']}")] for p in prods]
            rows.append([InlineKeyboardButton("⬅️ QUAY LẠI CHUYÊN MỤC", callback_data="u:cats")])
            await q.edit_message_text("🛍 <b>DANH SÁCH SẢN PHẨM:</b>\n\nChọn sản phẩm bạn muốn mua:", reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
            return

        if data.startswith("u:prod:"):
            pid = safe_int(data.split(":")[2], 0)
            prod = get_product(pid)
            if not prod:
                await q.edit_message_text("❌ Sản phẩm không tồn tại hoặc đã bị gỡ!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ QUAY LẠI", callback_data="u:cats")]]))
                return
            txt = (
                f"💎 <b>CHI TIẾT SẢN PHẨM</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 Tên sản phẩm: <b>{prod['name']}</b>\n"
                f"💵 Giá bán: <b>{int(prod['price']):,} VNĐ</b>\n"
                f"📦 Tình trạng kho: <b>{int(prod['stock']):,} sản phẩm</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <i>Nội dung/File giao tự động ngay lập tức sau khi hoàn tất mua hàng.</i>"
            )
            rows = [
                [InlineKeyboardButton("✅ THANH TOÁN VÀ MUA NGAY", callback_data=f"u:buy:{pid}")],
                [InlineKeyboardButton("⬅️ QUAY LẠI SẢN PHẨM", callback_data="u:cats")]
            ]
            await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
            return

        if data.startswith("u:buy:"):
            pid = safe_int(data.split(":")[2], 0)
            prod = get_product(pid)
            if not prod or int(prod["stock"] or 0) <= 0:
                await q.edit_message_text("❌ Sản phẩm này đã hết hàng trong kho!", reply_markup=self.user_main_keyboard(user.id))
                return
            self._buy_cache[user.id] = {"product_id": pid}
            rows = [
                [InlineKeyboardButton("🎟 CÓ MÃ GIẢM GIÁ", callback_data="u:coupon_yes"), InlineKeyboardButton("❌ KHÔNG CÓ MÃ", callback_data="u:coupon_no")],
                [InlineKeyboardButton("⬅️ HUỶ GIAO DỊCH", callback_data="u:home")]
            ]
            await q.edit_message_text("🏷 <b>MÃ GIẢM GIÁ (COUPON)</b>\n\nBạn có mã giảm giá áp dụng cho đơn hàng này không?", reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
            return

        if data == "u:coupon_no":
            await self._finalize_purchase(user.id, coupon_code=None, message=q.message, context=context)
            return

        if data == "u:coupon_yes":
            await q.edit_message_text("📩 <b>NHẬP MÃ GIẢM GIÁ:</b>\n\nHãy gửi tin nhắn chứa mã giảm giá của bạn (Ví dụ: <code>SALE10</code>):", parse_mode=ParseMode.HTML)
            context.user_data["waiting_coupon"] = True
            return

        if data == "u:deposit":
            await q.edit_message_text("💳 <b>NẠP TIỀN TỰ ĐỘNG 24/7</b>\n\nNhập số tiền bạn muốn nạp vào tài khoản (Ví dụ: <code>50000</code>):", parse_mode=ParseMode.HTML)
            context.user_data["waiting_deposit_amount"] = True
            return

        if data == "u:history":
            orders = list_orders(user.id, limit=15)
            if not orders:
                await q.edit_message_text("🧾 Bạn chưa thực hiện đơn hàng nào trên hệ thống.", reply_markup=self.user_main_keyboard(user.id))
                return
            lines = ["🧾 <b>LỊCH SỬ MUA HÀNG (15 ĐƠN GẦN NHẤT):</b>\n"]
            for o in orders:
                lines.append(f"• <b>#{o['id']}</b> | {o['product_name'] or 'Sản phẩm'} | <b>{int(o['final_price']):,}đ</b>\n  └ <i>{o['created_at']}</i>")
            await q.edit_message_text("\n\n".join(lines), reply_markup=self.user_main_keyboard(user.id), parse_mode=ParseMode.HTML)
            return

        if data == "u:aff":
            if not self._bot_username:
                try:
                    me = await context.bot.get_me()
                    self._bot_username = me.username
                except Exception:
                    self._bot_username = "YourBot"
            link = f"https://t.me/{self._bot_username}?start=ref_{user.id}"
            percent = get_aff_percent(user.id)
            con = db_connect()
            cur = con.cursor()
            cur.execute("SELECT COALESCE(SUM(amount),0) AS s FROM affiliate_earnings WHERE referrer_id=?", (user.id,))
            total = int(cur.fetchone()["s"] or 0)
            con.close()
            txt = (
                f"🤝 <b>CHƯƠNG TRÌNH TIẾP THỊ LIÊN KẾT (AFFILIATE)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔗 Link giới thiệu của bạn:\n<code>{link}</code>\n\n"
                f"💸 Hoa hồng nhận được: <b>{percent}% / mỗi đơn hàng thành công</b>\n"
                f"🏦 Tổng tiền hoa hồng đã tích lũy: <b>{total:,} VNĐ</b>\n\n"
                f"<i>Chia sẻ link cho bạn bè, khi họ nhấn vào link và phát sinh đơn hàng, bạn sẽ nhận được % hoa hồng trực tiếp vào số dư!</i>"
            )
            await q.edit_message_text(txt, reply_markup=self.user_main_keyboard(user.id), parse_mode=ParseMode.HTML)
            return

        if data == "a:panel":
            if user.id not in ADMIN_MODE and user.id != int(self.cfg.admin_id or 0):
                await q.edit_message_text("❌ Bạn không có quyền truy cập Admin.", reply_markup=self.user_main_keyboard(user.id))
                return
            await q.edit_message_text("🛠 <b>HỆ THỐNG QUẢN TRỊ ADMIN PANEL</b>\n\nQuản lý chuyên mục, sản phẩm, coupon và cài đặt:", reply_markup=self.admin_keyboard(), parse_mode=ParseMode.HTML)
            return

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        upsert_user(user)
        text = (update.message.text or "").strip()

        if context.user_data.get("waiting_deposit_amount"):
            context.user_data["waiting_deposit_amount"] = False
            amt = safe_int(text, 0)
            if amt <= 0:
                await update.message.reply_text("❌ Số tiền nạp không hợp lệ. Vui lòng thực hiện lại!", reply_markup=self.user_main_keyboard(user.id))
                return
            if not self.cfg.vietqr_stk or not self.cfg.vietqr_bank_id:
                await update.message.reply_text("❌ Admin chưa cài đặt thông tin ngân hàng VietQR.", reply_markup=self.user_main_keyboard(user.id))
                return
            
            add_info = f"Napid {user.id}"
            qr_url = vietqr_image_url(
                self.cfg.vietqr_bank_id, 
                self.cfg.vietqr_stk, 
                self.cfg.vietqr_template or DEFAULT_VIETQR_TEMPLATE, 
                amt, 
                add_info, 
                self.cfg.vietqr_ctk
            )
            
            caption_text = (
                f"💳 <b>NẠP TIỀN TỰ ĐỘNG CHUYỂN KHOẢN</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Nội dung chuyển khoản: <code>{add_info}</code>\n"
                f"💰 Số tiền: <b>{amt:,}đ</b>\n"
                f"🏦 Ngân hàng: <b>{self.cfg.vietqr_bank_name}</b>\n"
                f"💳 Số tài khoản: <code>{self.cfg.vietqr_stk}</code>\n"
                f"👤 Chủ tài khoản: <b>{self.cfg.vietqr_ctk}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Quét mã QR trên để chuyển khoản tự động duyệt trong 10-30 giây!</b>"
            )
            
            qr_sent = False
            qr_errors = []
            # Attempt 1: download, convert SVG to PNG if needed, then upload actual bytes.
            # Avoid optional timeout kwargs for compatibility with older PTB releases.
            try:
                qr_image = await asyncio.to_thread(download_vietqr_image, qr_url)
                await update.message.reply_photo(
                    photo=qr_image,
                    caption=caption_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=self.user_main_keyboard(user.id),
                )
                qr_sent = True
                log_msg(f"QR image uploaded successfully user={user.id} amount={amt}")
            except Exception as e:
                detail = f"Tải/gửi ảnh: {type(e).__name__}: {str(e)[:220]}"
                qr_errors.append(detail)
                log_msg(f"QR byte-upload attempt failed user={user.id}: {type(e).__name__}: {e}")

            # Attempt 2: let Telegram fetch the VietQR image URL directly.
            # This provides a fallback if local image downloading or BytesIO upload fails.
            if not qr_sent:
                try:
                    await update.message.reply_photo(
                        photo=qr_url,
                        caption=caption_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=self.user_main_keyboard(user.id),
                    )
                    qr_sent = True
                    log_msg(f"QR URL sent successfully user={user.id} amount={amt}")
                except Exception as e:
                    detail = f"Gửi URL ảnh: {type(e).__name__}: {str(e)[:220]}"
                    qr_errors.append(detail)
                    log_msg(f"QR URL-send attempt failed user={user.id}: {type(e).__name__}: {e}")

            if not qr_sent:
                fallback_text = (
                    caption_text
                    + "\n\n⚠️ Bot không gửi được ảnh QR tự động. "
                    + "Bạn có thể mở liên kết VietQR dự phòng bên dưới:"
                    + f"\n{qr_url}"
                    + ("\n\n🔎 Lỗi kỹ thuật: " + " | ".join(qr_errors) if qr_errors else "")
                )
                await update.message.reply_text(
                    fallback_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                    reply_markup=self.user_main_keyboard(user.id),
                )
            return

        if context.user_data.get("waiting_coupon"):
            context.user_data["waiting_coupon"] = False
            await self._finalize_purchase(user.id, coupon_code=text.upper(), message=update.message, context=context)
            return

        await update.message.reply_text("🔥 <b>SHOP HOANG QUAN CHEAT</b>\n\nVui lòng chọn các chức năng trong menu bấm bên dưới!", reply_markup=self.user_main_keyboard(user.id), parse_mode=ParseMode.HTML)

    async def _finalize_purchase(self, user_id: int, coupon_code: Optional[str], message, context: ContextTypes.DEFAULT_TYPE):
        cache = self._buy_cache.get(user_id)
        if not cache:
            await message.reply_text("❌ Không có giao dịch mua hàng nào đang chờ.", reply_markup=self.user_main_keyboard(user_id))
            return
        pid = cache.get("product_id")
        prod = get_product(pid)
        if not prod or int(prod["stock"] or 0) <= 0:
            await message.reply_text("❌ Sản phẩm đã hết hàng trong kho.", reply_markup=self.user_main_keyboard(user_id))
            return

        price = int(prod["price"])
        discount = 0
        applied_code = None

        if coupon_code:
            cp = get_coupon(coupon_code)
            if cp:
                max_uses = int(cp["max_uses"] or 0)
                used = int(cp["used_count"] or 0)
                if max_uses == 0 or used < max_uses:
                    if price >= int(cp["min_order"] or 0):
                        ap_pid = cp["apply_product_id"]
                        if ap_pid is None or int(ap_pid) == int(pid):
                            dpct = int(cp["discount_percent"] or 0)
                            damt = int(cp["discount_amount"] or 0)
                            discount = (price * dpct // 100) if dpct > 0 else damt
                            discount = max(0, min(discount, price))
                            applied_code = coupon_code

        final_price = max(0, price - discount)
        bal = get_balance(user_id)
        if bal < final_price:
            await message.reply_text(
                f"❌ <b>SỐ DƯ KHÔNG ĐỦ THANH TOÁN!</b>\n\n"
                f"💰 Số dư hiện tại: <b>{bal:,}đ</b>\n"
                f"💵 Số tiền cần thanh toán: <b>{final_price:,}đ</b>\n\n"
                f"👉 Vui lòng chọn <b>Nạp tiền tự động</b> để nạp thêm vào tài khoản!",
                reply_markup=self.user_main_keyboard(user_id),
                parse_mode=ParseMode.HTML
            )
            return

        if not subtract_balance(user_id, final_price):
            await message.reply_text(
                "❌ Số dư đã thay đổi, giao dịch chưa được thực hiện. Vui lòng thử lại.",
                reply_markup=self.user_main_keyboard(user_id),
            )
            return
        if not decrement_stock(pid):
            add_balance(user_id, final_price)
            await message.reply_text(
                "❌ Sản phẩm vừa hết hàng. Số dư đã được hoàn lại.",
                reply_markup=self.user_main_keyboard(user_id),
            )
            return

        order_id = create_order(user_id, pid, price, discount, final_price, applied_code, "PAID")
        if applied_code:
            mark_coupon_used(applied_code)

        u = get_user(user_id)
        ref_by = u["ref_by"] if u else None
        if ref_by:
            percent = get_aff_percent(int(ref_by))
            earn = (final_price * percent) // 100
            if earn > 0:
                add_balance(int(ref_by), earn)
                add_aff_earning(int(ref_by), user_id, order_id, earn)
                try:
                    await context.bot.send_message(chat_id=int(ref_by), text=f"🎉 Bạn nhận được <b>+{earn:,}đ</b> tiền hoa hồng tiếp thị từ đơn hàng #{order_id}!", parse_mode=ParseMode.HTML)
                except Exception:
                    pass

        deliver_text = (prod["deliver_text"] or "").strip() or "✅ Đơn hàng thanh toán thành công!"
        await message.reply_text(
            f"🎉 <b>MUA HÀNG THÀNH CÔNG!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧾 Mã đơn hàng: <b>#{order_id}</b>\n"
            f"🛒 Sản phẩm: <b>{prod['name']}</b>\n"
            f"💰 Giá thanh toán: <b>{final_price:,} VNĐ</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>NỘI DUNG / FILE GIAO HÀNG:</b>\n\n"
            f"{deliver_text}",
            parse_mode=ParseMode.HTML,
            reply_markup=self.user_main_keyboard(user_id)
        )

        try:
            await self._notify_purchase_log(context.bot, user_id, order_id, prod["name"], price, discount, final_price, applied_code)
        except Exception:
            pass

        self._buy_cache.pop(user_id, None)

    def poll_bank_and_credit_sync(self):
        if not self.cfg.bank_v3_token:
            return
        try:
            url = build_coderent_v2_url(self.cfg.bank_v3_name, self.cfg.bank_v3_token)
            r = requests.get(url, timeout=25)
            data = r.json()
            txs = data.get("transactions", []) or []
            for tx in txs:
                if str(tx.get("type", "")).upper().strip() != "IN":
                    continue
                tid = str(tx.get("transactionID", "")).strip()
                amount = safe_int(tx.get("amount", 0), 0)
                desc = str(tx.get("description", "") or "")
                if not tid or amount <= 0:
                    continue
                m = re.search(r"napid\s*(\d+)", desc, re.IGNORECASE)
                if not m:
                    continue
                user_id = safe_int(m.group(1), 0)
                if user_id <= 0:
                    continue
                # Deduplication and credit are committed atomically.
                if not credit_bank_deposit_once(user_id, amount, desc, tid):
                    continue
                log_msg(f"Deposit credited: user={user_id} amount={amount} tid={tid}")

                if self._app and self._bot_loop:
                    bot = self._app.bot
                    asyncio.run_coroutine_threadsafe(bot.send_message(chat_id=user_id, text=f"✅ <b>NẠP TIỀN THÀNH CÔNG!</b>\n\n💰 Số tiền: <b>+{amount:,} VNĐ</b>\n🧾 Mã GD: <code>{tid}</code>", parse_mode=ParseMode.HTML), self._bot_loop)
                    if int(self.cfg.admin_id or 0) > 0:
                        asyncio.run_coroutine_threadsafe(bot.send_message(chat_id=int(self.cfg.admin_id), text=f"💳 <b>NẠP TIỀN MỚI</b>\nUser: <code>{user_id}</code>\nSố tiền: +{amount:,}đ\nMã GD: {tid}"), self._bot_loop)
                    asyncio.run_coroutine_threadsafe(self._notify_deposit_log(bot, user_id, amount, tid, desc), self._bot_loop)
        except Exception as e:
            log_msg(f"Bank poll error: {e}")

    def bank_loop(self):
        log_msg("Bank poll thread started.")
        while not self._stop_event.is_set():
            try:
                self.poll_bank_and_credit_sync()
            except Exception as e:
                log_msg(f"Bank loop error: {e}")
            self._stop_event.wait(max(5, int(self.cfg.poll_interval or 15)))

    def _bot_thread_main(self):
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
            loop = asyncio.get_event_loop()
            self._bot_loop = loop

            self._app = Application.builder().token(self.cfg.bot_token).build()
            self._app.add_handler(CommandHandler("start", self.cmd_start))
            self._app.add_handler(CommandHandler("help", self.cmd_help))
            self._app.add_handler(CommandHandler("checkadmin", self.cmd_checkadmin))
            self._app.add_handler(CommandHandler("getchatid", self.cmd_getchatid))
            self._app.add_handler(CommandHandler("ds", self.cmd_ds))
            self._app.add_handler(CommandHandler("nap", self.cmd_nap))
            self._app.add_handler(CommandHandler("buy", self.cmd_buy))
            self._app.add_handler(CommandHandler("me", self.cmd_me))
            self._app.add_handler(CommandHandler("history", self.cmd_history))
            self._app.add_handler(CommandHandler("top", self.cmd_top))
            self._app.add_handler(CallbackQueryHandler(self.on_callback))
            self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message))

            async def runner():
                await self._app.initialize()
                await self._app.start()
                try:
                    me = await self._app.bot.get_me()
                    self._bot_username = me.username
                    log_msg(f"Bot username: @{self._bot_username}")
                    mini_url = os.environ.get("SHOP_MINIAPP_URL", "").strip()
                    if mini_url.startswith("https://"):
                        try:
                            await self._app.bot.set_chat_menu_button(
                                menu_button=MenuButtonWebApp(text="Mua Key", web_app=WebAppInfo(url=mini_url))
                            )
                            log_msg("Telegram Mini App menu button configured.")
                        except Exception as menu_error:
                            log_msg(f"Mini App menu button setup failed: {menu_error}")
                except Exception:
                    pass
                await self._app.updater.start_polling(drop_pending_updates=True)
                while not self._stop_event.is_set():
                    await asyncio.sleep(0.5)
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()

            loop.run_until_complete(runner())
        except Exception as e:
            log_msg(f"Bot thread error: {e}")

    def start(self, cfg: BotConfig):
        self.cfg = cfg
        save_config(self.cfg)
        if not self.cfg.bot_token:
            raise RuntimeError("Chưa nhập BOT TOKEN")

        self._stop_event.clear()
        self._bot_thread = threading.Thread(target=self._bot_thread_main, daemon=True)
        self._bot_thread.start()

        self._bank_thread = threading.Thread(target=self.bank_loop, daemon=True)
        self._bank_thread.start()
        log_msg("✅ Started Bot + Bank Poll.")

    def stop(self):
        self._stop_event.set()
        log_msg("⛔ Stopped Bot.")

BOT_RUNNER = BotRunner()

# =========================================================
# FLASK WEB APP
# =========================================================
app = Flask(__name__)
# Set SHOP_FLASK_SECRET in the environment for a persistent secret across restarts.
app.secret_key = os.environ.get("SHOP_FLASK_SECRET") or os.urandom(32)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#080b12">
    <meta name="description" content="SHOP HOANG QUAN — quản trị bán hàng, đơn hàng và thanh toán.">
    <title>{{ title }}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {
            color-scheme: dark;
            --bg-main: #080b12;
            --card-bg: #151b23;
            --card-sec: #1c242f;
            --primary: #8b5cf6;
            --action: #22d3ee;
            --success: #26d07c;
            --danger: #ff3b3b;
            --text-p: #ffffff;
            --text-s: #8b949e;
            --border: #30363d;
        }
        body {
            background-color: var(--bg-main);
            color: var(--text-p);
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        .navbar, .card, .modal-content {
            background-color: var(--card-bg) !important;
            border-color: var(--border) !important;
            color: var(--text-p);
        }
        .card { border-radius: 20px; box-shadow: 0 12px 40px rgba(0,0,0,0.28); margin-bottom: 20px; border: 1px solid rgba(148,163,184,.12) !important; }
        .btn { border-radius: 12px; transition: transform .18s ease, box-shadow .18s ease; }
        .btn:hover { transform: translateY(-1px); box-shadow: 0 8px 24px rgba(139,92,246,.18); }
        .nav-pills .nav-link { border-radius: 12px; color: #aab4c5; }
        .nav-pills .nav-link.active { background: linear-gradient(135deg,#7c3aed,#2563eb); color: white; }
        .table { --bs-table-bg: transparent; --bs-table-color: #e8edf6; --bs-table-hover-bg: rgba(139,92,246,.08); }
        .form-control, .form-select {
            background-color: var(--bg-main);
            border-color: var(--border);
            color: var(--text-p);
            border-radius: 10px;
        }
        .form-control:focus, .form-select:focus {
            background-color: var(--bg-main);
            border-color: var(--primary);
            color: var(--text-p);
            box-shadow: 0 0 0 0.25rem rgba(0, 163, 255, 0.25);
        }
        .table { color: var(--text-p); border-color: var(--border); }
        .nav-pills .nav-link { color: var(--text-s); border-radius: 10px; margin-right: 5px; font-weight: 600; }
        .nav-pills .nav-link.active { background-color: var(--primary); color: #fff; }
        .btn-primary { background-color: var(--primary); border: none; border-radius: 10px; }
        .btn-success { background-color: var(--success); border: none; border-radius: 10px; }
        .btn-danger { background-color: var(--danger); border: none; border-radius: 10px; }
        .btn-warning { background-color: var(--action); border: none; color: #fff; border-radius: 10px; }
        pre { background: var(--bg-main); color: var(--success); padding: 15px; border-radius: 10px; border: 1px solid var(--border); max-height: 350px; overflow-y: auto; }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark border-bottom mb-4 py-3">
        <div class="container-fluid">
            <a class="navbar-brand fw-bold text-info" href="/"><i class="fa-solid fa-fire me-2"></i> SHOP HOANG QUAN CHEAT - Web Dashboard</a>
            <div class="d-flex align-items-center">
                <form action="/toggle_bot" method="POST" class="d-inline">
                    {% if bot_running %}
                        <button type="submit" class="btn btn-danger btn-sm px-3 py-2 fw-bold"><i class="fa-solid fa-stop me-1"></i> DỪNG BOT</button>
                    {% else %}
                        <button type="submit" class="btn btn-success btn-sm px-3 py-2 fw-bold"><i class="fa-solid fa-play me-1"></i> CHẠY BOT</button>
                    {% endif %}
                </form>
            </div>
        </div>
    </nav>

    <div class="container-fluid px-4">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for cat, msg in messages %}
                    <div class="alert alert-{{ 'danger' if cat=='error' else 'success' }} alert-dismissible fade show" role="alert">
                        {{ msg }}
                        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
                    </div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <ul class="nav nav-pills mb-4" id="pills-tab" role="tablist">
            <li class="nav-item"><button class="nav-link active" data-bs-toggle="pill" data-bs-target="#tab-config" type="button"><i class="fa-solid fa-gears me-1"></i> Cấu hình</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-categories" type="button"><i class="fa-solid fa-folder me-1"></i> Chuyên mục</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-products" type="button"><i class="fa-solid fa-box me-1"></i> Sản phẩm</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-coupons" type="button"><i class="fa-solid fa-ticket me-1"></i> Mã giảm giá</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-orders" type="button"><i class="fa-solid fa-receipt me-1"></i> Đơn hàng & Nạp</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-logs" type="button"><i class="fa-solid fa-terminal me-1"></i> Log hệ thống</button></li>
        </ul>

        <div class="tab-content">
            <!-- CONFIG TAB -->
            <div class="tab-pane fade show active" id="tab-config">
                <div class="row">
                    <div class="col-lg-8 mx-auto">
                        <div class="card p-4">
                            <h4 class="mb-4 text-primary"><i class="fa-solid fa-sliders me-2"></i> Cấu hình Hệ thống & VietQR & AutoBank</h4>
                            <form action="/save_config" method="POST">
                                <div class="row g-3">
                                    <div class="col-md-6">
                                        <label class="form-label fw-bold">Admin ID</label>
                                        <input type="number" class="form-control" name="admin_id" value="{{ cfg.admin_id }}">
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label fw-bold">Log Group ID</label>
                                        <input type="number" class="form-control" name="log_group_id" value="{{ cfg.log_group_id }}">
                                    </div>
                                    <div class="col-12">
                                        <label class="form-label fw-bold">Bot Token</label>
                                        <input type="text" class="form-control" name="bot_token" value="{{ cfg.bot_token }}">
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label fw-bold">VietQR Ngân hàng</label>
                                        <select class="form-select" name="vietqr_bank_name">
                                            {% for name, bid in vietqr_banks %}
                                                <option value="{{ name }}" {% if cfg.vietqr_bank_name == name %}selected{% endif %}>{{ name }}</option>
                                            {% endfor %}
                                        </select>
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label fw-bold">Số tài khoản</label>
                                        <input type="text" class="form-control" name="vietqr_stk" value="{{ cfg.vietqr_stk }}">
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label fw-bold">Chủ tài khoản</label>
                                        <input type="text" class="form-control" name="vietqr_ctk" value="{{ cfg.vietqr_ctk }}">
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label fw-bold">VietQR Template</label>
                                        <input type="text" class="form-control" name="vietqr_template" value="{{ cfg.vietqr_template }}">
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label fw-bold">AutoBank V3 (Ngân hàng API)</label>
                                        <select class="form-select" name="bank_v3_name">
                                            {% for bname in coderent_banks %}
                                                <option value="{{ bname }}" {% if cfg.bank_v3_name == bname %}selected{% endif %}>{{ bname }}</option>
                                            {% endfor %}
                                        </select>
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label fw-bold">Token API AutoBank</label>
                                        <input type="text" class="form-control" name="bank_v3_token" value="{{ cfg.bank_v3_token }}">
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label fw-bold">Thời gian quét (giây)</label>
                                        <input type="number" class="form-control" name="poll_interval" value="{{ cfg.poll_interval }}">
                                    </div>
                                    <div class="col-12 mt-4 text-end">
                                        <button type="submit" class="btn btn-primary px-5 py-2 fw-bold"><i class="fa-solid fa-floppy-disk me-2"></i> LƯU CẤU HÌNH</button>
                                    </div>
                                </div>
                            </form>
                        </div>
                    </div>
                </div>
            </div>

            <!-- CATEGORIES TAB -->
            <div class="tab-pane fade" id="tab-categories">
                <div class="row">
                    <div class="col-md-4">
                        <div class="card p-4">
                            <h5>➕ Thêm chuyên mục</h5>
                            <form action="/add_category" method="POST" class="mt-3">
                                <div class="mb-3">
                                    <label class="form-label">Tên chuyên mục</label>
                                    <input type="text" class="form-control" name="name" required placeholder="VD: File Aim Free Fire">
                                </div>
                                <button type="submit" class="btn btn-primary w-100 fw-bold">Thêm Chuyên Mục</button>
                            </form>
                        </div>
                    </div>
                    <div class="col-md-8">
                        <div class="card p-4">
                            <h5>📁 Danh sách chuyên mục</h5>
                            <div class="table-responsive mt-3">
                                <table class="table table-hover align-middle">
                                    <thead><tr><th>ID</th><th>Tên</th><th>Thao tác</th></tr></thead>
                                    <tbody>
                                        {% for c in categories %}
                                        <tr>
                                            <td>#{{ c.id }}</td>
                                            <td>{{ c.name }}</td>
                                            <td>
                                                <form action="/delete_category/{{ c.id }}" method="POST" class="d-inline">
                                                    <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('Xóa chuyên mục này?')"><i class="fa-solid fa-trash"></i></button>
                                                </form>
                                            </td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- PRODUCTS TAB -->
            <div class="tab-pane fade" id="tab-products">
                <div class="row">
                    <div class="col-md-4">
                        <div class="card p-4">
                            <h5>➕ Thêm sản phẩm</h5>
                            <form action="/add_product" method="POST" class="mt-3">
                                <div class="mb-3">
                                    <label class="form-label">Chuyên mục</label>
                                    <select class="form-select" name="category_id" required>
                                        {% for c in categories %}
                                            <option value="{{ c.id }}">{{ c.name }}</option>
                                        {% endfor %}
                                    </select>
                                </div>
                                <div class="mb-3">
                                    <label class="form-label">Tên sản phẩm</label>
                                    <input type="text" class="form-control" name="name" required placeholder="VD: Mod Headshot FF VIP">
                                </div>
                                <div class="mb-3">
                                    <label class="form-label">Giá (đ)</label>
                                    <input type="number" class="form-control" name="price" required placeholder="VD: 50000">
                                </div>
                                <div class="mb-3">
                                    <label class="form-label">Kho hàng</label>
                                    <input type="number" class="form-control" name="stock" value="10" required>
                                </div>
                                <div class="mb-3">
                                    <label class="form-label">Nội dung / Link File giao khi mua</label>
                                    <textarea class="form-control" name="deliver_text" rows="3" placeholder="Link tải file hoặc mã kích hoạt..."></textarea>
                                </div>
                                <button type="submit" class="btn btn-primary w-100 fw-bold">Thêm Sản Phẩm</button>
                            </form>
                        </div>
                    </div>
                    <div class="col-md-8">
                        <div class="card p-4">
                            <h5>📦 Danh sách sản phẩm</h5>
                            <div class="table-responsive mt-3">
                                <table class="table table-hover align-middle">
                                    <thead><tr><th>ID</th><th>Tên</th><th>Giá</th><th>Kho</th><th>Chuyên mục</th><th>Thao tác</th></tr></thead>
                                    <tbody>
                                        {% for p in products %}
                                        <tr>
                                            <td>#{{ p.id }}</td>
                                            <td>{{ p.name }}</td>
                                            <td>{{ "{:,}".format(p.price) }}đ</td>
                                            <td>{{ p.stock }}</td>
                                            <td>{{ p.category_name }}</td>
                                            <td>
                                                <form action="/delete_product/{{ p.id }}" method="POST" class="d-inline">
                                                    <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('Xóa sản phẩm này?')"><i class="fa-solid fa-trash"></i></button>
                                                </form>
                                            </td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- COUPONS TAB -->
            <div class="tab-pane fade" id="tab-coupons">
                <div class="row">
                    <div class="col-md-4">
                        <div class="card p-4">
                            <h5>🏷 Tạo / Sửa Coupon</h5>
                            <form action="/save_coupon" method="POST" class="mt-3">
                                <div class="mb-3">
                                    <label class="form-label">Mã Code (VD: HOANGQUAN10)</label>
                                    <input type="text" class="form-control text-uppercase" name="code" required>
                                </div>
                                <div class="mb-3">
                                    <label class="form-label">Giảm tiền (đ)</label>
                                    <input type="number" class="form-control" name="discount_amount" value="0">
                                </div>
                                <div class="mb-3">
                                    <label class="form-label">Giảm % (0-100)</label>
                                    <input type="number" class="form-control" name="discount_percent" value="0">
                                </div>
                                <div class="mb-3">
                                    <label class="form-label">Áp dụng cho SP ID (0 = Tất cả)</label>
                                    <input type="number" class="form-control" name="apply_product_id" value="0">
                                </div>
                                <div class="mb-3">
                                    <label class="form-label">Đơn tối thiểu (đ)</label>
                                    <input type="number" class="form-control" name="min_order" value="0">
                                </div>
                                <div class="mb-3">
                                    <label class="form-label">Lượt dùng tối đa (0 = Không giới hạn)</label>
                                    <input type="number" class="form-control" name="max_uses" value="0">
                                </div>
                                <button type="submit" class="btn btn-primary w-100 fw-bold">Lưu Coupon</button>
                            </form>
                        </div>
                    </div>
                    <div class="col-md-8">
                        <div class="card p-4">
                            <h5>🎫 Danh sách mã giảm giá</h5>
                            <div class="table-responsive mt-3">
                                <table class="table table-hover align-middle">
                                    <thead><tr><th>Mã</th><th>Giảm</th><th>Đã dùng</th><th>Trạng thái</th><th>Thao tác</th></tr></thead>
                                    <tbody>
                                        {% for cp in coupons %}
                                        <tr>
                                            <td class="fw-bold text-info">{{ cp.code }}</td>
                                            <td>{% if cp.discount_percent > 0 %}{{ cp.discount_percent }}%{% else %}{{ "{:,}".format(cp.discount_amount) }}đ{% endif %}</td>
                                            <td>{{ cp.used_count }} / {% if cp.max_uses == 0 %}∞{% else %}{{ cp.max_uses }}{% endif %}</td>
                                            <td>{% if cp.is_active %}<span class="badge bg-success">Hoạt động</span>{% else %}<span class="badge bg-danger">Đã tắt</span>{% endif %}</td>
                                            <td>
                                                <form action="/disable_coupon/{{ cp.code }}" method="POST" class="d-inline">
                                                    <button type="submit" class="btn btn-warning btn-sm">Tắt</button>
                                                </form>
                                            </td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ORDERS & DEPOSITS TAB -->
            <div class="tab-pane fade" id="tab-orders">
                <div class="row">
                    <div class="col-md-6">
                        <div class="card p-4">
                            <h5>🧾 Đơn hàng gần đây</h5>
                            <div class="table-responsive mt-3" style="max-height: 400px; overflow-y: auto;">
                                <table class="table table-sm table-hover align-middle">
                                    <thead><tr><th>#ID</th><th>User</th><th>SP</th><th>Giá</th><th>Thời gian</th></tr></thead>
                                    <tbody>
                                        {% for o in orders %}
                                        <tr>
                                            <td>#{{ o.id }}</td>
                                            <td>{{ o.user_id }}</td>
                                            <td>{{ o.product_name }}</td>
                                            <td>{{ "{:,}".format(o.final_price) }}đ</td>
                                            <td><small>{{ o.created_at }}</small></td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-6">
                        <div class="card p-4">
                            <h5>💳 Lịch sử nạp tiền tự động</h5>
                            <div class="table-responsive mt-3" style="max-height: 400px; overflow-y: auto;">
                                <table class="table table-sm table-hover align-middle">
                                    <thead><tr><th>#ID</th><th>User</th><th>Số tiền</th><th>Mã GD</th><th>Thời gian</th></tr></thead>
                                    <tbody>
                                        {% for d in deposits %}
                                        <tr>
                                            <td>#{{ d.id }}</td>
                                            <td>{{ d.user_id }}</td>
                                            <td class="text-success fw-bold">+{{ "{:,}".format(d.amount) }}đ</td>
                                            <td><code>{{ d.trans_id }}</code></td>
                                            <td><small>{{ d.created_at }}</small></td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- LOGS TAB -->
            <div class="tab-pane fade" id="tab-logs">
                <div class="card p-4">
                    <h5><i class="fa-solid fa-terminal me-2"></i> Log hệ thống trực tiếp</h5>
                    <div class="mt-3">
                        <pre id="log-box">Đang tải log hệ thống...</pre>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        function fetchLogs() {
            fetch('/get_logs')
                .then(res => res.json())
                .then(data => {
                    const box = document.getElementById('log-box');
                    if(data.logs && data.logs.length > 0) {
                        box.textContent = data.logs.join('\n');
                        box.scrollTop = box.scrollHeight;
                    }
                }).catch(err => console.log(err));
        }
        setInterval(fetchLogs, 2000);
    </script>
</body>
</html>
"""

@app.before_request
def _protect_admin_dashboard():
    # Publicly reachable endpoints are limited to the Mini App and health check.
    if request.path == "/miniapp" or request.path.startswith("/api/miniapp/") or request.path == "/health":
        return None
    expected_user = os.environ.get("DASHBOARD_USER", "").strip()
    expected_password = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not expected_user or not expected_password:
        return Response("Dashboard is locked. Set DASHBOARD_USER and DASHBOARD_PASSWORD.", status=503, mimetype="text/plain")
    auth = request.authorization
    if not auth or not hmac.compare_digest(auth.username or "", expected_user) or not hmac.compare_digest(auth.password or "", expected_password):
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Shop Admin"'})
    return None


@app.route("/health")
def health_check():
    return jsonify({"ok": True, "service": "shop-miniapp"})


def _miniapp_authorized_user():
    """Validate Telegram Mini App initData and return its parsed user, or None."""
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    token = (BOT_RUNNER.cfg.bot_token or os.environ.get("BOT_TOKEN", "")).strip()
    if not raw or not token:
        return None
    try:
        pairs = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True, strict_parsing=False))
        received_hash = pairs.pop("hash", "")
        # `signature` belongs to Telegram's third-party Ed25519 verification flow.
        pairs.pop("signature", None)
        if not received_hash:
            return None
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        auth_date = safe_int(pairs.get("auth_date"), 0)
        now_ts = int(time.time())
        if auth_date <= 0 or now_ts - auth_date > 86400 or auth_date > now_ts + 60:
            return None
        user = json.loads(pairs.get("user", "{}"))
        if not isinstance(user, dict) or safe_int(user.get("id"), 0) <= 0:
            return None
        return user
    except Exception as exc:
        log_msg(f"Mini App auth parse error: {type(exc).__name__}: {exc}")
        return None


def _miniapp_require_user():
    user = _miniapp_authorized_user()
    if not user:
        return None, (jsonify({"ok": False, "error": "Không xác thực được Telegram. Hãy mở Mini App từ nút trong bot."}), 401)
    uid = safe_int(user.get("id"), 0)
    con = db_connect()
    try:
        con.execute(
            """INSERT INTO users(user_id, username, first_name, balance, ref_by, created_at)
               VALUES(?,?,?,?,NULL,?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
            (uid, str(user.get("username") or ""), str(user.get("first_name") or ""), 0, now_str()),
        )
        con.commit()
    finally:
        con.close()
    return (uid, user), None


def _miniapp_deposit_totals(user_id: int):
    con = db_connect()
    try:
        month_prefix = datetime.now().strftime("%Y-%m")
        row = con.execute(
            """SELECT COALESCE(SUM(amount),0) AS total,
                      COALESCE(SUM(CASE WHEN substr(created_at,1,7)=? THEN amount ELSE 0 END),0) AS monthly
               FROM deposits WHERE user_id=? AND UPPER(status)='SUCCESS'""",
            (month_prefix, int(user_id)),
        ).fetchone()
        return int(row["total"] or 0), int(row["monthly"] or 0)
    finally:
        con.close()


@app.route("/miniapp")
def miniapp_page():
    return send_from_directory(os.path.join(os.path.dirname(__file__), "static"), "miniapp.html")


@app.route("/api/miniapp/bootstrap")
def miniapp_bootstrap():
    auth, error = _miniapp_require_user()
    if error:
        return error
    (uid, tg_user) = auth
    con = db_connect()
    try:
        user_row = con.execute("SELECT balance, first_name, username, ref_by FROM users WHERE user_id=?", (uid,)).fetchone()
        categories = con.execute("SELECT id,name FROM categories WHERE is_active=1 ORDER BY id DESC").fetchall()
        products = con.execute(
            """SELECT p.id,p.category_id,p.name,p.price,p.stock,c.name AS category_name
               FROM products p LEFT JOIN categories c ON c.id=p.category_id
               WHERE p.is_active=1 ORDER BY p.id DESC"""
        ).fetchall()
        orders = con.execute(
            """SELECT o.id,o.final_price,o.status,o.created_at,p.name AS product_name
               FROM orders o LEFT JOIN products p ON p.id=o.product_id
               WHERE o.user_id=? ORDER BY o.id DESC LIMIT 20""", (uid,)
        ).fetchall()
        deposits = con.execute(
            "SELECT id,amount,description,status,created_at FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,)
        ).fetchall()
        latest = con.execute(
            """SELECT COALESCE(NULLIF(u.first_name,''),NULLIF(u.username,''),'Khách hàng') AS display_name,
                      d.amount,d.created_at
               FROM deposits d LEFT JOIN users u ON u.user_id=d.user_id
               WHERE UPPER(d.status)='SUCCESS' ORDER BY d.id DESC LIMIT 5"""
        ).fetchall()
    finally:
        con.close()
    total, monthly = _miniapp_deposit_totals(uid)
    return jsonify({
        "ok": True,
        "user": {
            "id": uid,
            "name": str(tg_user.get("first_name") or user_row["first_name"] or "Khách hàng"),
            "username": str(tg_user.get("username") or user_row["username"] or ""),
            "photo_url": str(tg_user.get("photo_url") or ""),
            "balance": int(user_row["balance"] or 0),
            "total_deposit": total,
            "monthly_deposit": monthly,
            "role": "ADMIN" if uid == int(BOT_RUNNER.cfg.admin_id or 0) else "CUSTOMER",
        },
        "categories": [dict(r) for r in categories],
        "products": [dict(r) for r in products],
        "orders": [dict(r) for r in orders],
        "deposits": [dict(r) for r in deposits],
        "recent": [dict(r) for r in latest],
    })


@app.route("/api/miniapp/top")
def miniapp_top():
    auth, error = _miniapp_require_user()
    if error:
        return error
    con = db_connect()
    try:
        rows = con.execute(
            """SELECT COALESCE(NULLIF(u.first_name,''),NULLIF(u.username,''),'Khách hàng') AS name,
                      SUM(d.amount) AS total
               FROM deposits d LEFT JOIN users u ON u.user_id=d.user_id
               WHERE UPPER(d.status)='SUCCESS' GROUP BY d.user_id
               ORDER BY total DESC LIMIT 20"""
        ).fetchall()
        return jsonify({"ok": True, "items": [dict(r) for r in rows]})
    finally:
        con.close()


@app.route("/api/miniapp/deposit", methods=["POST"])
def miniapp_deposit():
    auth, error = _miniapp_require_user()
    if error:
        return error
    (uid, tg_user) = auth
    body = request.get_json(silent=True) or {}
    amount = safe_int(body.get("amount"), 0)
    if amount < 1000 or amount > 50000000:
        return jsonify({"ok": False, "error": "Số tiền nạp phải từ 1.000đ đến 50.000.000đ."}), 400
    cfg = BOT_RUNNER.cfg
    if not cfg.vietqr_stk or not cfg.vietqr_bank_id:
        return jsonify({"ok": False, "error": "Chủ shop chưa cấu hình ngân hàng nhận tiền."}), 409
    add_info = f"Napid {uid}"
    qr_url = vietqr_image_url(
        str(cfg.vietqr_bank_id), str(cfg.vietqr_stk), cfg.vietqr_template or "compact2",
        amount, add_info, cfg.vietqr_ctk or "",
    )
    return jsonify({
        "ok": True, "amount": amount, "description": add_info,
        "bank_name": cfg.vietqr_bank_name, "bank_id": cfg.vietqr_bank_id,
        "account_no": cfg.vietqr_stk, "account_name": cfg.vietqr_ctk,
        "qr_url": qr_url,
        "note": "Số dư chỉ được cộng sau khi hệ thống ngân hàng xác nhận giao dịch.",
    })


@app.route("/api/miniapp/qr", methods=["POST"])
def miniapp_qr():
    auth, error = _miniapp_require_user()
    if error:
        return error
    (uid, tg_user) = auth
    body = request.get_json(silent=True) or {}
    amount = safe_int(body.get("amount"), 0)
    if amount < 1000 or amount > 50000000:
        return jsonify({"ok": False, "error": "Số tiền nạp không hợp lệ."}), 400
    cfg = BOT_RUNNER.cfg
    if not cfg.vietqr_stk or not cfg.vietqr_bank_id:
        return jsonify({"ok": False, "error": "Chủ shop chưa cấu hình ngân hàng."}), 409
    url = vietqr_image_url(str(cfg.vietqr_bank_id), str(cfg.vietqr_stk), cfg.vietqr_template or "compact2",
                           amount, f"Napid {uid}", cfg.vietqr_ctk or "")
    try:
        image = download_vietqr_image(url)
        raw = image.getvalue()
        mime = "image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else ("image/jpeg" if raw.startswith(b"\xff\xd8\xff") else "image/webp")
        image.seek(0)
        return send_file(image, mimetype=mime, download_name="vietqr-image", max_age=0)
    except Exception as exc:
        log_msg(f"Mini App QR generation failed uid={uid}: {type(exc).__name__}: {exc}")
        return jsonify({"ok": False, "error": f"Không tạo được ảnh QR: {str(exc)[:180]}"}), 502


@app.route("/api/miniapp/buy", methods=["POST"])
def miniapp_buy():
    auth, error = _miniapp_require_user()
    if error:
        return error
    (uid, tg_user) = auth
    body = request.get_json(silent=True) or {}
    pid = safe_int(body.get("product_id"), 0)
    if pid <= 0:
        return jsonify({"ok": False, "error": "Sản phẩm không hợp lệ."}), 400
    con = db_connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        product = con.execute(
            "SELECT id,name,price,stock,deliver_text FROM products WHERE id=? AND is_active=1", (pid,)
        ).fetchone()
        if not product or int(product["stock"] or 0) <= 0:
            con.rollback()
            return jsonify({"ok": False, "error": "Sản phẩm đã hết hàng."}), 409
        price = int(product["price"] or 0)
        updated = con.execute(
            "UPDATE users SET balance=COALESCE(balance,0)-? WHERE user_id=? AND COALESCE(balance,0)>=?",
            (price, uid, price),
        )
        if updated.rowcount != 1:
            con.rollback()
            row = con.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
            balance = int(row["balance"] or 0) if row else 0
            return jsonify({"ok": False, "error": "Số dư không đủ.", "balance": balance, "price": price}), 409
        changed = con.execute(
            "UPDATE products SET stock=stock-1 WHERE id=? AND is_active=1 AND stock>0", (pid,)
        )
        if changed.rowcount != 1:
            con.rollback()
            return jsonify({"ok": False, "error": "Kho vừa hết hàng, vui lòng tải lại."}), 409
        cur = con.execute(
            """INSERT INTO orders(user_id,product_id,price,discount,final_price,coupon_code,status,created_at)
               VALUES(?,?,?,0,?,NULL,'PAID',?)""",
            (uid, pid, price, price, now_str()),
        )
        order_id = cur.lastrowid
        ref_row = con.execute("SELECT ref_by FROM users WHERE user_id=?", (uid,)).fetchone()
        ref_id = safe_int(ref_row["ref_by"], 0) if ref_row else 0
        if ref_id > 0:
            aff_row = con.execute("SELECT percent FROM affiliate WHERE user_id=?", (ref_id,)).fetchone()
            percent = int(aff_row["percent"] or 5) if aff_row else 5
            earn = price * max(0, min(percent, 100)) // 100
            if earn > 0:
                con.execute("UPDATE users SET balance=COALESCE(balance,0)+? WHERE user_id=?", (earn, ref_id))
                con.execute(
                    "INSERT INTO affiliate_earnings(referrer_id,referred_user_id,order_id,amount,created_at) VALUES(?,?,?,?,?)",
                    (ref_id, uid, order_id, earn, now_str()),
                )
        balance_row = con.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        new_balance = int(balance_row["balance"] or 0)
        con.commit()
        return jsonify({
            "ok": True, "order_id": order_id, "product": product["name"],
            "price": price, "balance": new_balance,
            "delivery": (product["deliver_text"] or "").strip() or "Đơn hàng đã thanh toán thành công. Vui lòng liên hệ admin nếu cần hỗ trợ.",
        })
    except Exception as exc:
        con.rollback()
        log_msg(f"Mini App purchase error user={uid} product={pid}: {type(exc).__name__}: {exc}")
        return jsonify({"ok": False, "error": "Không thể hoàn tất đơn hàng. Vui lòng thử lại."}), 500
    finally:
        con.close()


@app.route("/")
def index():
    cfg = load_config()
    categories = list_categories()
    products = list_all_products()
    coupons = list_coupons()
    orders = list_orders(limit=30)
    deposits = list_deposits(limit=30)
    bot_running = BOT_RUNNER._bot_thread is not None and BOT_RUNNER._bot_thread.is_alive()
    return render_template_string(
        HTML_TEMPLATE,
        title=APP_TITLE,
        cfg=cfg,
        vietqr_banks=VIETQR_BANKS,
        coderent_banks=list(CODERENT_V2_CODES.keys()),
        categories=categories,
        products=products,
        coupons=coupons,
        orders=orders,
        deposits=deposits,
        bot_running=bot_running
    )

@app.route("/toggle_bot", methods=["POST"])
def toggle_bot():
    cfg = load_config()
    running = BOT_RUNNER._bot_thread is not None and BOT_RUNNER._bot_thread.is_alive()
    if running:
        BOT_RUNNER.stop()
        flash("Đã dừng Bot Telegram thành công.", "success")
    else:
        try:
            BOT_RUNNER.start(cfg)
            flash("Đã khởi động Bot Telegram và Bank Poll thành công!", "success")
        except Exception as e:
            flash(f"Lỗi khởi động Bot: {e}", "error")
    return redirect(url_for("index"))

@app.route("/save_config", methods=["POST"])
def save_config_route():
    cfg = load_config()
    cfg.admin_id = safe_int(request.form.get("admin_id"), 0)
    cfg.log_group_id = safe_int(request.form.get("log_group_id"), 0)
    cfg.bot_token = request.form.get("bot_token", "").strip()
    cfg.vietqr_bank_name = request.form.get("vietqr_bank_name", "MB Bank")
    cfg.vietqr_bank_id = dict(VIETQR_BANKS).get(cfg.vietqr_bank_name, "mbbank")
    cfg.vietqr_stk = request.form.get("vietqr_stk", "").strip()
    cfg.vietqr_ctk = request.form.get("vietqr_ctk", "").strip()
    cfg.vietqr_template = request.form.get("vietqr_template", DEFAULT_VIETQR_TEMPLATE).strip()
    cfg.bank_v3_name = request.form.get("bank_v3_name", "MB Bank")
    cfg.bank_v3_token = request.form.get("bank_v3_token", "").strip()
    cfg.poll_interval = max(5, safe_int(request.form.get("poll_interval"), 15))
    save_config(cfg)
    BOT_RUNNER.cfg = cfg
    flash("Đã lưu cấu hình hệ thống thành công!", "success")
    return redirect(url_for("index"))

@app.route("/add_category", methods=["POST"])
def route_add_category():
    name = request.form.get("name", "").strip()
    if name:
        add_category(name)
        flash("Thêm chuyên mục thành công!", "success")
    return redirect(url_for("index"))

@app.route("/delete_category/<int:cid>", methods=["POST"])
def route_delete_category(cid):
    delete_category(cid)
    flash("Đã xóa chuyên mục!", "success")
    return redirect(url_for("index"))

@app.route("/add_product", methods=["POST"])
def route_add_product():
    cid = safe_int(request.form.get("category_id"), 0)
    name = request.form.get("name", "").strip()
    price = safe_int(request.form.get("price"), 0)
    stock = safe_int(request.form.get("stock"), 0)
    deliver_text = request.form.get("deliver_text", "").strip()
    if cid > 0 and name and price > 0:
        add_product(cid, name, price, stock, deliver_text)
        flash("Thêm sản phẩm thành công!", "success")
    else:
        flash("Dữ liệu sản phẩm không hợp lệ!", "error")
    return redirect(url_for("index"))

@app.route("/delete_product/<int:pid>", methods=["POST"])
def route_delete_product(pid):
    delete_product(pid)
    flash("Đã xóa sản phẩm!", "success")
    return redirect(url_for("index"))

@app.route("/save_coupon", methods=["POST"])
def route_save_coupon():
    code = request.form.get("code", "").strip().upper()
    damt = safe_int(request.form.get("discount_amount"), 0)
    dpct = safe_int(request.form.get("discount_percent"), 0)
    ap_pid = safe_int(request.form.get("apply_product_id"), 0)
    min_order = safe_int(request.form.get("min_order"), 0)
    max_uses = safe_int(request.form.get("max_uses"), 0)
    if code:
        apply_pid = None if ap_pid <= 0 else ap_pid
        upsert_coupon(code, damt, dpct, apply_pid, min_order, max_uses)
        flash(f"Đã lưu coupon {code} thành công!", "success")
    return redirect(url_for("index"))

@app.route("/disable_coupon/<code>", methods=["POST"])
def route_disable_coupon(code):
    disable_coupon(code)
    flash(f"Đã tắt coupon {code}!", "success")
    return redirect(url_for("index"))

@app.route("/get_logs")
def get_logs():
    logs = []
    while not LOG_QUEUE.empty():
        try:
            logs.append(LOG_QUEUE.get_nowait())
        except queue.Empty:
            break
    return jsonify({"logs": logs})

def initialize_runtime():
    init_db()
    if os.environ.get("SHOP_AUTOSTART_BOT", "1").strip() == "1" and BOT_RUNNER.cfg.bot_token:
        running = BOT_RUNNER._bot_thread is not None and BOT_RUNNER._bot_thread.is_alive()
        if not running:
            try:
                BOT_RUNNER.start(BOT_RUNNER.cfg)
            except Exception as exc:
                log_msg(f"Auto-start bot failed: {type(exc).__name__}: {exc}")


if os.environ.get("SHOP_INIT_ON_IMPORT", "1").strip() == "1":
    initialize_runtime()


if __name__ == "__main__":
    init_db()
    log_msg("🚀 Web App started. Dashboard mặc định chỉ mở trên máy cục bộ.")
    web_host = os.environ.get("SHOP_WEB_HOST", "0.0.0.0")
    web_port = safe_int(os.environ.get("SHOP_WEB_PORT", "10000"), 10000)
    app.run(host=web_host, port=web_port, debug=False)
