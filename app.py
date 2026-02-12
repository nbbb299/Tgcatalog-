import os
import re
import time
import json
import requests
from io import BytesIO
from flask import Flask, request, jsonify, send_file, render_template, abort

from supabase import create_client

app = Flask(__name__, template_folder="templates", static_folder=None)

# -------------------------
# ENV
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "products").strip() or "products"

BUY_TELEGRAM = os.getenv("BUY_TELEGRAM", "").strip().lstrip("@")
BUY_WHATSAPP = os.getenv("BUY_WHATSAPP", "").strip()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # опционально

# -------------------------
# Supabase client (lazy)
# -------------------------
_supabase = None

def supabase():
    global _supabase
    if _supabase is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL or SUPABASE_KEY is missing")
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase

# -------------------------
# Parsing helpers
# -------------------------

PRICE_LINE_RE = re.compile(r"€.*%.*=€", re.IGNORECASE)  # "3900€-20%=€3.120,00"

HASHTAG_BRAND_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,40})")

SIZE_WORDS_RE = re.compile(r"(?i)\b(XXS|XS|S|M|L|XL|XXL)\b")
SIZE_NUM_RE = re.compile(r"^\s*\d{1,3}(\s*[-,/]\s*\d{1,3})+(\s*(FR|IT|EU|US))?\s*$", re.IGNORECASE)
SIZE_SINGLE_NUM_WITH_SYS_RE = re.compile(r"^\s*\d{1,3}\s*(FR|IT|EU|US)\s*$", re.IGNORECASE)

def is_service_line(line: str) -> bool:
    t = (line or "").strip()
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
    t = (line or "").strip()
    if not t:
        return False
    # обяз: показываем скидку как ты просила
    return ("€" in t) and ("%" in t) and ("=€" in t or "€=" in t or "= €" in t)

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
    # "50,52" (без пробелов)
    if re.match(r"^\s*\d{1,3}\s*,\s*\d{1,3}\s*$", t):
        return True
    # "50 52"
    if re.match(r"^\s*\d{1,3}\s+\d{1,3}\s*$", t):
        return True
    return False

def guess_brand(lines):
    # 1) ищем #brand
    for ln in lines:
        m = HASHTAG_BRAND_RE.search(ln)
        if m:
            return m.group(1)
    # 2) иначе: первая "нормальная" строка (не сервисная и не цена и не размеры)
    for ln in lines:
        t = (ln or "").strip()
        if not t:
            continue
        if is_service_line(t):
            continue
        if looks_like_price_line(t):
            continue
        if looks_like_size_line(t):
            continue
        # уберём лишние эмодзи вокруг — но оставим если они вместе с брендом
        return t
    return "item"

def parse_post(text: str):
    raw_lines = [l.rstrip() for l in (text or "").split("\n")]
    lines = [l.strip() for l in raw_lines if l.strip()]

    brand = guess_brand(lines)

    price_raw = ""
    for ln in lines:
        if looks_like_price_line(ln):
            price_raw = ln.strip()
            break

    size_line = ""
    for ln in lines:
        if looks_like_size_line(ln):
            size_line = ln.strip()
            break

    # caption = только размеры (как ты хочешь)
    caption = size_line

    return {
        "brand": brand,
        "price_raw": price_raw,
        "caption": caption,
        "raw_text": text or ""
    }

# -------------------------
# Telegram helpers
# -------------------------
def tg_api(method: str, params=None):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def tg_file_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

# simple in-memory cache for images (file_id -> (bytes, mime, ts))
_IMG_CACHE = {}
_CACHE_TTL = 60 * 20  # 20 min

def cache_get(file_id):
    it = _IMG_CACHE.get(file_id)
    if not it:
        return None
    data, mime, ts = it
    if time.time() - ts > _CACHE_TTL:
        _IMG_CACHE.pop(file_id, None)
        return None
    return data, mime

def cache_set(file_id, data, mime):
    _IMG_CACHE[file_id] = (data, mime, time.time())

# -------------------------
# Routes
# -------------------------

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/buy_target")
def buy_target():
    return jsonify({"telegram": BUY_TELEGRAM, "whatsapp": BUY_WHATSAPP})

@app.get("/api/products")
def api_products():
    """
    Returns list of products with:
    id, brand, price_raw, caption, file_id, ts, raw_text
    """
    try:
        limit = int(request.args.get("limit", "30"))
        offset = int(request.args.get("offset", "0"))
        q = (request.args.get("q") or "").strip()
        brand = (request.args.get("brand") or "").strip()

        sb = supabase()
        query = sb.table(SUPABASE_TABLE).select("*").order("ts", desc=True).range(offset, offset + limit - 1)

        if brand:
            # бренд может быть в базе в разном регистре
            query = query.ilike("brand", brand)

        if q:
            # ищем по raw_text (самое надежное)
            query = query.ilike("raw_text", f"%{q}%")

        res = query.execute()
        data = res.data or []

        # нормализуем поля чтобы index не ломался
        out = []
        for p in data:
            out.append({
                "id": p.get("id"),
                "brand": p.get("brand") or "item",
                "price_raw": p.get("price_raw") or "",
                "caption": p.get("caption") or "",
                "file_id": p.get("file_id") or "",
                "ts": p.get("ts") or 0,
                "raw_text": p.get("raw_text") or ""
            })
        return jsonify(out)

    except Exception as e:
        # НЕ роняем сервер — возвращаем понятный ответ
        return jsonify({
            "error": "api_products_failed",
            "details": str(e),
            "hint": "Проверь SUPABASE_URL / SUPABASE_KEY / SUPABASE_TABLE и доступ к таблице (RLS)."
        }), 500

@app.get("/img/<file_id>")
def img(file_id):
    """
    Proxy image from Telegram file_id:
    1) getFile(file_id) -> file_path
    2) download file
    """
    if not BOT_TOKEN:
        abort(404)

    cached = cache_get(file_id)
    if cached:
        data, mime = cached
        return send_file(BytesIO(data), mimetype=mime)

    try:
        info = tg_api("getFile", {"file_id": file_id})
        if not info.get("ok"):
            abort(404)

        file_path = info["result"]["file_path"]
        url = tg_file_url(file_path)

        r = requests.get(url, timeout=30)
        r.raise_for_status()

        mime = r.headers.get("Content-Type", "image/jpeg")
        data = r.content

        cache_set(file_id, data, mime)
        return send_file(BytesIO(data), mimetype=mime)

    except Exception:
        abort(404)

@app.post("/telegram")
def telegram_webhook():
    # опциональная защита
    if WEBHOOK_SECRET:
        sec = request.args.get("secret", "")
        if sec != WEBHOOK_SECRET:
            return "forbidden", 403

    upd = request.get_json(silent=True) or {}

    try:
        message = upd.get("message") or upd.get("channel_post") or {}
        text = message.get("text") or message.get("caption") or ""
        photos = message.get("photo") or []

        if not text:
            return "ok"

        parsed = parse_post(text)

        # фото берём самое большое
        file_id = ""
        if photos:
            file_id = photos[-1].get("file_id") or ""

        parsed["file_id"] = file_id
        parsed["ts"] = int(time.time())
        parsed["id"] = str(message.get("message_id") or parsed["ts"])

        # записываем в supabase
        sb = supabase()
        sb.table(SUPABASE_TABLE).upsert(parsed).execute()

        return "ok"
    except Exception as e:
        # чтобы телега не зацикливалась — отвечаем 200
        print("telegram_webhook_error:", e)
        return "ok"
