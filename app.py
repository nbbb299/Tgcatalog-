import os
import re
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, jsonify, request, send_from_directory, Response
from supabase import create_client


# -----------------------------
# Config
# -----------------------------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or "").strip()
SUPABASE_TABLE = (os.getenv("SUPABASE_TABLE") or "products").strip()

BUY_TELEGRAM = (os.getenv("BUY_TELEGRAM") or "").strip().lstrip("@")
BUY_WHATSAPP = (os.getenv("BUY_WHATSAPP") or "").strip().lstrip("+").replace(" ", "")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()

app = Flask(__name__, template_folder="templates")

sb = None
if SUPABASE_URL and SUPABASE_KEY:
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# -----------------------------
# Parsing helpers
# -----------------------------
HASHTAG_BRAND_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,30})")
PRICE_LINE_RE = re.compile(r"€.*%|%.*€|=€", re.IGNORECASE)

SIZE_WORDS_RE = re.compile(r"(?i)\b(XXS|XS|S|M|L|XL|XXL)\b")
SIZE_NUM_RE = re.compile(r"^\s*\d{1,3}(\s*[-,/]\s*\d{1,3})+(\s*(FR|IT|EU|US))?\s*$", re.IGNORECASE)
SIZE_SINGLE_NUM_WITH_SYS_RE = re.compile(r"^\s*\d{1,3}\s*(FR|IT|EU|US)\s*$", re.IGNORECASE)


def is_service_line(line: str) -> bool:
    t = (line or "").strip()
    if not t:
        return True
    low = t.lower()
    if low.startswith("по вопросам") or low.startswith("вопрос"):
        return True
    if t.startswith("@"):
        return True
    return False


def looks_like_price_line(line: str) -> bool:
    t = (line or "").strip()
    if "€" not in t:
        return False
    return bool(PRICE_LINE_RE.search(t))


def looks_like_size_line(line: str) -> bool:
    t = (line or "").strip()
    if not t:
        return False
    if SIZE_WORDS_RE.search(t):
        return True
    if SIZE_NUM_RE.match(t):
        return True
    if SIZE_SINGLE_NUM_WITH_SYS_RE.match(t):
        return True
    if re.match(r"^\s*\d{1,3}\s*,\s*\d{1,3}\s*$", t):
        return True
    return False


def extract_brand(lines: List[str]) -> str:
    # 1) hashtag
    for line in lines:
        m = HASHTAG_BRAND_RE.search(line or "")
        if m:
            return m.group(1)

    # 2) any “brand-ish” line (not price/size/service)
    for line in lines:
        t = (line or "").strip()
        if not t or is_service_line(t):
            continue
        if looks_like_price_line(t) or looks_like_size_line(t):
            continue
        if re.search(r"[A-Za-zÀ-ÿА-Яа-я]", t):
            low = t.lower()
            if low in ("item", "orders"):
                continue
            return t

    return "ITEM"


def extract_price_raw(lines: List[str]) -> str:
    for line in lines:
        t = (line or "").strip()
        if looks_like_price_line(t):
            return t
    for line in lines:
        t = (line or "").strip()
        if "€" in t:
            return t
    return ""


def extract_sizes(lines: List[str]) -> str:
    for line in lines:
        t = (line or "").strip()
        if looks_like_size_line(t):
            return t
    return ""


# -----------------------------
# Telegram image proxy
# -----------------------------
def telegram_get_file_path(file_id: str) -> Optional[str]:
    if not BOT_TOKEN:
        return None
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
        params={"file_id": file_id},
        timeout=20,
    )
    if not r.ok:
        return None
    j = r.json()
    if not j.get("ok"):
        return None
    return (j.get("result") or {}).get("file_path")


@app.get("/img/<file_id>")
def img(file_id: str):
    if not BOT_TOKEN:
        return ("BOT_TOKEN missing", 500)

    file_path = telegram_get_file_path(file_id)
    if not file_path:
        return ("Not found", 404)

    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    rr = requests.get(url, stream=True, timeout=30)
    if not rr.ok:
        return ("Upstream error", 502)

    content_type = rr.headers.get("Content-Type") or "image/jpeg"
    return Response(rr.iter_content(chunk_size=64 * 1024), content_type=content_type)


# -----------------------------
# Frontend + API
# -----------------------------
@app.get("/")
def home():
    return send_from_directory("templates", "index.html")


@app.get("/api/buy_target")
def buy_target():
    return jsonify({"telegram": BUY_TELEGRAM, "whatsapp": BUY_WHATSAPP})


@app.get("/api/products")
def api_products():
    if not sb:
        return jsonify({"error": "SUPABASE_URL / SUPABASE_KEY not set"}), 500

    limit = int(request.args.get("limit", "30"))
    offset = int(request.args.get("offset", "0"))
    q = (request.args.get("q") or "").strip()
    brand = (request.args.get("brand") or "").strip()

    # ВАЖНО: выбираем file_ids + file_id (для совместимости)
    select_cols = "id,message_id,media_group_id,ts,created_at,brand,price_raw,caption,file_id,file_ids"
    query = sb.table(SUPABASE_TABLE).select(select_cols)

    if brand:
        query = query.ilike("brand", f"%{brand}%")

    if q:
        # поиск по caption
        query = query.ilike("caption", f"%{q}%")

    # сортировка по новым
    try:
        query = query.order("ts", desc=True, nulls_last=True)
    except Exception:
        query = query.order("created_at", desc=True)

    res = query.range(offset, offset + limit - 1).execute()
    data = res.data or []

    out = []
    for p in data:
        item = dict(p)

        # нормализуем массив фото
        fids = item.get("file_ids")
        if not isinstance(fids, list):
            fids = []
        if item.get("file_id") and item["file_id"] not in fids:
            fids = [item["file_id"]] + fids
        item["file_ids"] = fids

        item["brand"] = (item.get("brand") or "ITEM")
        item["price_raw"] = item.get("price_raw") or ""
        item["caption"] = item.get("caption") or ""

        out.append(item)

    return jsonify(out)


# -----------------------------
# Supabase helpers (album-aware)
# -----------------------------
def sb_select_by_media_group(media_group_id: str) -> Optional[Dict[str, Any]]:
    res = (
        sb.table(SUPABASE_TABLE)
        .select("id,media_group_id,file_ids")
        .eq("media_group_id", media_group_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def sb_upsert_album_row(row: Dict[str, Any]) -> None:
    # on_conflict требует уникальный индекс по media_group_id (мы создали)
    sb.table(SUPABASE_TABLE).upsert(row, on_conflict="media_group_id").execute()


def sb_upsert_single_row(row: Dict[str, Any]) -> None:
    sb.table(SUPABASE_TABLE).upsert(row, on_conflict="message_id").execute()


# -----------------------------
# Telegram webhook (album -> 1 product)
# -----------------------------
@app.post("/telegram")
def telegram_webhook():
    if WEBHOOK_SECRET:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header != WEBHOOK_SECRET:
            return ("Forbidden", 403)

    if not sb:
        return ("Supabase not configured", 500)

    upd = request.get_json(force=True, silent=True) or {}
    msg = upd.get("channel_post") or upd.get("message") or {}
    if not msg:
        return ("ok", 200)

    message_id = msg.get("message_id")
    date_ts = msg.get("date")  # unix seconds
    media_group_id = msg.get("media_group_id")  # album id

    text = (msg.get("text") or msg.get("caption") or "").strip()

    # Photo file_id (largest)
    file_id = ""
    photo_arr = msg.get("photo") or []
    if photo_arr:
        file_id = (photo_arr[-1] or {}).get("file_id") or ""

    # If nothing useful
    if not text and not file_id:
        return ("ok", 200)

    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not is_service_line(ln)]

    brand = extract_brand(lines) if lines else "ITEM"
    price_raw = extract_price_raw(lines) if lines else ""
    sizes = extract_sizes(lines) if lines else ""

    # ---------- Album case ----------
    if media_group_id:
        existing = sb_select_by_media_group(media_group_id)
        existing_fids = []
        if existing and isinstance(existing.get("file_ids"), list):
            existing_fids = existing["file_ids"]

        # add new fid (dedupe)
        new_fids = list(existing_fids)
        if file_id and file_id not in new_fids:
            new_fids.append(file_id)

        row = {
            "media_group_id": media_group_id,
            "brand": brand,
            "price_raw": price_raw,
            "caption": sizes,
            "file_id": (new_fids[0] if new_fids else None),  # cover
            "file_ids": new_fids,
            "ts": int(date_ts) if date_ts else int(time.time()),
        }

        # if existing already has brand/price/caption and this message has empty text — keep old values
        if existing:
            # we don't fetch brand/price here to keep minimal queries; simplest behavior is okay:
            # latest message overwrites brand/price if it's non-empty
            if not price_raw:
                row.pop("price_raw", None)
            if not sizes:
                row.pop("caption", None)
            if not brand or brand == "ITEM":
                row.pop("brand", None)

        try:
            sb_upsert_album_row(row)
        except Exception:
            pass

        return ("ok", 200)

    # ---------- Single photo/text case ----------
    row = {
        "message_id": int(message_id) if message_id is not None else None,
        "brand": brand,
        "price_raw": price_raw,
        "caption": sizes,
        "file_id": file_id or None,
        "file_ids": [file_id] if file_id else [],
        "ts": int(date_ts) if date_ts else int(time.time()),
    }
    row = {k: v for k, v in row.items() if v is not None}

    try:
        sb_upsert_single_row(row)
    except Exception:
        pass

    return ("ok", 200)
