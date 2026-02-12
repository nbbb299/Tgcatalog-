import os
import re
import json
import time
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, request, jsonify, render_template, Response, abort

from supabase import create_client

app = Flask(__name__, template_folder="templates")
logging.basicConfig(level=logging.INFO)

# ----------------------------
# ENV
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret12345").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "products").strip()

BUY_TELEGRAM = os.getenv("BUY_TELEGRAM", "mikab16").strip()
BUY_WHATSAPP = os.getenv("BUY_WHATSAPP", "393463203783").strip()

# ----------------------------
# Helpers: Supabase (lazy init)
# ----------------------------
_sb = None

def sb():
    global _sb
    if _sb is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY")
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb

def table():
    return sb().table(SUPABASE_TABLE)

# ----------------------------
# Telegram API helpers
# ----------------------------
TG_API = "https://api.telegram.org"

def tg_get_file(file_id: str) -> Optional[str]:
    if not BOT_TOKEN:
        return None
    url = f"{TG_API}/bot{BOT_TOKEN}/getFile"
    r = requests.get(url, params={"file_id": file_id}, timeout=20)
    if not r.ok:
        return None
    j = r.json()
    if not j.get("ok"):
        return None
    return j["result"]["file_path"]

def tg_file_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

# ----------------------------
# Parsing (brand / price / sizes)
# ----------------------------
HASHTAG_BRAND_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,40})")
PRICE_RE = re.compile(r"€.*%.*=€", re.IGNORECASE)  # "3900€-20%=€3.120,00" (примерно)

SIZE_WORDS_RE = re.compile(r"(?i)\b(XXS|XS|S|M|L|XL|XXL)\b")
SIZE_NUM_RE = re.compile(r"^\s*\d{1,3}(\s*[,/ -]\s*\d{1,3})+(\s*(FR|IT|EU|US))?\s*$", re.IGNORECASE)
SIZE_SINGLE_NUM_WITH_SYS_RE = re.compile(r"^\s*\d{1,3}\s*(FR|IT|EU|US)\s*$", re.IGNORECASE)

def is_service_line(line: str) -> bool:
    t = line.strip()
    if not t:
        return True
    low = t.lower()
    if low.startswith("по вопросам"):
        return True
    if low.startswith("вопрос"):
        return True
    if t.startswith("@"):
        return True
    return False

def looks_like_price_line(line: str) -> bool:
    t = line.strip()
    if "€" not in t:
        return False
    # хотим видеть скидку
    return ("%" in t) or ("=€" in t) or ("= €" in t)

def looks_like_size_line(line: str) -> bool:
    t = line.strip()
    if not t:
        return False
    if SIZE_WORDS_RE.search(t):
        return True
    if SIZE_NUM_RE.match(t):
        return True
    if SIZE_SINGLE_NUM_WITH_SYS_RE.match(t):
        return True
    return False

def clean_line(line: str) -> str:
    return line.replace("\u200b", "").strip()

def extract_brand(lines: List[str]) -> str:
    # 1) #brand
    for ln in lines:
        m = HASHTAG_BRAND_RE.search(ln)
        if m:
            b = m.group(1)
            if b.lower() not in ("orders", "order", "item"):
                return b
    # 2) любая строка с буквами, не цена, не сервис, не размер
    for ln in lines:
        t = clean_line(ln)
        if not t:
            continue
        if is_service_line(t):
            continue
        if looks_like_price_line(t):
            continue
        if looks_like_size_line(t):
            continue
        if re.search(r"[A-Za-zА-Яа-я]", t):
            # убираем лишние символы, но оставляем эмодзи рядом — ок
            if t.lower() in ("item", "orders"):
                continue
            return t
    return "ITEM"

def extract_price_raw(lines: List[str]) -> str:
    for ln in lines:
        t = clean_line(ln)
        if looks_like_price_line(t):
            return t
    # fallback: первая строка с €
    for ln in lines:
        t = clean_line(ln)
        if "€" in t:
            return t
    return ""

def extract_sizes_caption(lines: List[str]) -> str:
    for ln in lines:
        t = clean_line(ln)
        if looks_like_size_line(t):
            return t
    return ""

# ----------------------------
# DB write: one product per media_group (album)
# ----------------------------
def upsert_product(
    message_id: int,
    media_group_id: Optional[str],
    date_iso: Optional[str],
    brand: str,
    price_raw: str,
    caption: str,
    file_ids: List[str],
):
    # Берём главный файл = первый
    file_id = file_ids[0] if file_ids else None
    ts = int(time.time())

    payload = {
        "message_id": message_id,
        "date": date_iso,
        "brand": brand,
        "price_raw": price_raw,
        "caption": caption,
        "file_id": file_id,
        "file_ids": file_ids,
        "media_group_id": media_group_id,
        "ts": ts,
    }

    # Если есть media_group_id — делаем “альбом = 1 товар” через upsert по media_group_id.
    # Но в таблице unique на message_id, поэтому:
    # - сначала ищем запись по media_group_id
    # - если есть → update (append file_ids)
    # - если нет → insert
    if media_group_id:
        try:
            existing = (
                table()
                .select("id,file_ids,brand,price_raw,caption,file_id,message_id")
                .eq("media_group_id", media_group_id)
                .limit(1)
                .execute()
            )
            rows = existing.data or []
            if rows:
                row = rows[0]
                merged = []
                old = row.get("file_ids") or []
                if isinstance(old, list):
                    merged.extend(old)
                for fid in file_ids:
                    if fid not in merged:
                        merged.append(fid)

                upd = {
                    "ts": ts,
                    "file_ids": merged,
                    "file_id": merged[0] if merged else row.get("file_id"),
                    # не затираем, если уже норм
                    "brand": brand or row.get("brand"),
                    "price_raw": price_raw or row.get("price_raw"),
                    "caption": caption or row.get("caption"),
                }
                table().update(upd).eq("id", row["id"]).execute()
                return

        except Exception as e:
            app.logger.exception("media_group lookup failed: %s", e)

    # иначе обычный insert
    try:
        table().upsert(payload, on_conflict="message_id").execute()
    except Exception as e:
        app.logger.exception("upsert failed: %s", e)
        raise

# ----------------------------
# Routes
# ----------------------------
@app.get("/")
def index():
    return render_template("index.html")

@app.get("/api/buy_target")
def buy_target():
    return jsonify({"telegram": BUY_TELEGRAM, "whatsapp": BUY_WHATSAPP})

@app.get("/api/health")
def health():
    ok = True
    err = None
    try:
        _ = sb()  # init
    except Exception as e:
        ok = False
        err = str(e)
    return jsonify({"ok": ok, "error": err})

@app.get("/api/products")
def api_products():
    """
    Returns list of products:
    - supports limit/offset/q/brand
    - sorted newest first (ts if exists, else created_at/date)
    """
    limit = int(request.args.get("limit", "30"))
    offset = int(request.args.get("offset", "0"))
    q = (request.args.get("q") or "").strip()
    brand = (request.args.get("brand") or "").strip()
    if brand.startswith("#"):
        brand = brand[1:]

    try:
        sel = "id,brand,price_raw,caption,file_id,file_ids,media_group_id,ts,created_at,date"
        qb = table().select(sel)

        if brand:
            qb = qb.ilike("brand", f"%{brand}%")

        if q:
            # ищем по brand/price_raw/caption
            qb = qb.or_(f"brand.ilike.%{q}%,price_raw.ilike.%{q}%,caption.ilike.%{q}%")

        # order: try ts, else created_at, else date
        try:
            qb2 = qb.order("ts", desc=True).range(offset, offset + limit - 1)
            data = qb2.execute().data or []
        except Exception:
            try:
                qb2 = qb.order("created_at", desc=True).range(offset, offset + limit - 1)
                data = qb2.execute().data or []
            except Exception:
                qb2 = qb.order("date", desc=True).range(offset, offset + limit - 1)
                data = qb2.execute().data or []

        # normalize output
        out = []
        for r in data:
            out.append({
                "id": str(r.get("id") or ""),
                "brand": r.get("brand") or "ITEM",
                "price_raw": r.get("price_raw") or "",
                "caption": r.get("caption") or "",
                "file_id": r.get("file_id"),
                "file_ids": r.get("file_ids") or ([r.get("file_id")] if r.get("file_id") else []),
                "media_group_id": r.get("media_group_id"),
                "ts": r.get("ts"),
            })
        return jsonify(out)

    except Exception as e:
        app.logger.exception("api_products_failed: %s", e)
        return jsonify({
            "error": "api_products_failed",
            "hint": "Проверь SUPABASE_URL / SUPABASE_KEY / RLS policy / таблицу products",
            "details": str(e)
        }), 500

@app.get("/img/<file_id>")
def proxy_img(file_id: str):
    """
    Proxies telegram images by file_id
    """
    if not BOT_TOKEN:
        abort(500, "BOT_TOKEN missing")
    file_path = tg_get_file(file_id)
    if not file_path:
        abort(404)
    url = tg_file_url(file_path)
    r = requests.get(url, timeout=25)
    if not r.ok:
        abort(404)
    # Telegram returns image/jpeg usually
    ct = r.headers.get("content-type", "image/jpeg")
    return Response(r.content, mimetype=ct, headers={"Cache-Control": "public, max-age=86400"})

@app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    """
    Telegram webhook receiver.
    Takes photos + caption, parses brand/price/sizes, writes to Supabase.
    """
    update = request.get_json(silent=True) or {}
    msg = update.get("message") or update.get("channel_post") or {}
    if not msg:
        return jsonify({"ok": True})

    message_id = msg.get("message_id")
    date = msg.get("date")  # unix
    date_iso = None
    if isinstance(date, int):
        date_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(date))

    media_group_id = msg.get("media_group_id")

    text = msg.get("caption") or msg.get("text") or ""
    lines = [clean_line(x) for x in str(text).splitlines()]
    lines = [x for x in lines if x is not None]

    brand = extract_brand(lines)
    price_raw = extract_price_raw(lines)
    sizes = extract_sizes_caption(lines)

    # фото
    file_ids: List[str] = []
    photos = msg.get("photo") or []
    if photos:
        # самое большое фото - последний элемент
        best = photos[-1]
        fid = best.get("file_id")
        if fid:
            file_ids.append(fid)

    # иногда альбом прилетает как document? (на всякий)
    if not file_ids and msg.get("document"):
        fid = msg["document"].get("file_id")
        if fid:
            file_ids.append(fid)

    # caption в каталоге = только размеры (как ты просила)
    caption = sizes

    # анти-мусор
    if brand.lower() in ("orders", "order", "item") and not price_raw and not caption and not file_ids:
        return jsonify({"ok": True})

    try:
        upsert_product(
            message_id=int(message_id),
            media_group_id=str(media_group_id) if media_group_id else None,
            date_iso=date_iso,
            brand=brand,
            price_raw=price_raw,
            caption=caption,
            file_ids=file_ids,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})

# Export for gunicorn
# gunicorn app:app
