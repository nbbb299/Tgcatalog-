import os
import re
import time
import json
import requests
from flask import Flask, request, jsonify, render_template, Response, abort
from supabase import create_client

app = Flask(__name__, template_folder="templates")

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
TABLE = os.getenv("SUPABASE_TABLE", "products").strip() or "products"

BUY_TELEGRAM = os.getenv("BUY_TELEGRAM", "mikab16").strip().lstrip("@")
BUY_WHATSAPP = os.getenv("BUY_WHATSAPP", "393463203783").strip().replace("+", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # optional

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_KEY missing")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================
# PARSING (brand / price_raw / sizes)
# =========================

HASHTAG_BRAND_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,40})")
SERVICE_LINE_RE = re.compile(r"(?i)^\s*(по вопросам|вопрос)\b")
AT_LINE_RE = re.compile(r"^\s*@")

# price like: 245€-25%=€183,75  | 3900€-20%=€3.120,00
def looks_like_price_line(s: str) -> bool:
    t = (s or "").strip()
    if "€" not in t:
        return False
    # главное: скидка видна
    return ("%" in t) and ("=" in t)

SIZE_WORDS_RE = re.compile(r"(?i)\b(XXS|XS|S|M|L|XL|XXL)\b")
SIZE_MULTI_NUM_RE = re.compile(r"^\s*\d{1,3}\s*([,/\-\s]\s*\d{1,3})+\s*$")
SIZE_SYS_RE = re.compile(r"^\s*\d{1,3}\s*(FR|IT|EU|US)\s*$", re.IGNORECASE)

def is_service_line(s: str) -> bool:
    t = (s or "").strip()
    if not t:
        return True
    if SERVICE_LINE_RE.search(t):
        return True
    if AT_LINE_RE.search(t):
        return True
    return False

def looks_like_size_line(s: str) -> bool:
    t = (s or "").strip()
    if not t:
        return False
    if SIZE_WORDS_RE.search(t):
        return True
    if SIZE_SYS_RE.match(t):
        return True
    if SIZE_MULTI_NUM_RE.match(t):
        return True
    # "50,52" / "50 52"
    if re.match(r"^\s*\d{1,3}\s*,\s*\d{1,3}\s*$", t):
        return True
    if re.match(r"^\s*\d{1,3}\s+\d{1,3}\s*$", t):
        return True
    return False

def extract_brand(lines):
    # 1) first hashtag brand
    for ln in lines:
        m = HASHTAG_BRAND_RE.search(ln)
        if m:
            return m.group(1)

    # 2) otherwise: first “brand-looking” line (not service, not price, not sizes)
    for ln in lines:
        t = (ln or "").strip()
        if not t:
            continue
        if is_service_line(t):
            continue
        if t.startswith("#"):
            continue
        if looks_like_price_line(t):
            continue
        if looks_like_size_line(t):
            continue

        # must contain letters (latin/cyrillic), not only emoji
        if re.search(r"[A-Za-zА-Яа-яÀ-ÿ]", t):
            return t

    # brand always exists in your channel, but fallback just in case
    return "—"

def extract_price_raw(lines):
    for ln in lines:
        if looks_like_price_line(ln):
            return ln.strip()
    # fallback: first line with €
    for ln in lines:
        if "€" in (ln or ""):
            return ln.strip()
    return ""

def extract_sizes(lines):
    for ln in lines:
        t = (ln or "").strip()
        if not t:
            continue
        if is_service_line(t):
            continue
        if looks_like_price_line(t):
            continue
        if looks_like_size_line(t):
            return t
    return ""

def parse_post_text(text: str):
    raw = (text or "").strip()
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    brand = extract_brand(lines)
    price_raw = extract_price_raw(lines)
    sizes = extract_sizes(lines)

    return {
        "brand": brand,
        "price_raw": price_raw,
        # важное: в каталоге 3-я строка = sizes
        "caption": sizes,
        "raw_text": raw,
    }

# =========================
# TELEGRAM FILE PROXY (/img/<file_id>)
# =========================

_TG_FILEPATH_CACHE = {}  # file_id -> (file_path, expires_at)
_TG_CACHE_TTL = 60 * 60  # 1 hour

def tg_get_file_path(file_id: str) -> str:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")

    now = time.time()
    cached = _TG_FILEPATH_CACHE.get(file_id)
    if cached and cached[1] > now:
        return cached[0]

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
    r = requests.get(url, params={"file_id": file_id}, timeout=20)
    r.raise_for_status()
    j = r.json()

    if not j.get("ok"):
        raise RuntimeError(f"Telegram getFile not ok: {j}")

    file_path = j["result"]["file_path"]
    _TG_FILEPATH_CACHE[file_id] = (file_path, now + _TG_CACHE_TTL)
    return file_path

@app.get("/img/<path:file_id>")
def img_proxy(file_id):
    if not BOT_TOKEN:
        abort(404)

    try:
        file_path = tg_get_file_path(file_id)
    except Exception:
        abort(404)

    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    rr = requests.get(file_url, stream=True, timeout=30)

    if rr.status_code != 200:
        abort(404)

    content_type = rr.headers.get("Content-Type", "image/jpeg")

    def gen():
        for chunk in rr.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk

    return Response(gen(), content_type=content_type, headers={"Cache-Control": "public, max-age=3600"})

# =========================
# MEDIA GROUP (ALBUM) COLLECTOR
# =========================

MEDIA_GROUP_CACHE = {}  # media_group_id -> {"ts": last_time, "ids": [], "base": record}
MEDIA_GROUP_FLUSH_AFTER = 2.0  # seconds of silence => flush to DB

def flush_media_groups_if_ready():
    now = time.time()
    ready = []
    for gid, item in list(MEDIA_GROUP_CACHE.items()):
        if now - item["ts"] >= MEDIA_GROUP_FLUSH_AFTER:
            ready.append(gid)

    for gid in ready:
        item = MEDIA_GROUP_CACHE.pop(gid, None)
        if not item:
            continue
        record = item["base"]
        ids = item["ids"]

        record["file_ids"] = ids
        record["file_id"] = ids[0] if ids else ""

        # upsert
        sb.table(TABLE).upsert(record).execute()

# =========================
# ROUTES
# =========================

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/health")
def health():
    return "ok", 200

@app.get("/api/buy_target")
def buy_target():
    return jsonify({"telegram": BUY_TELEGRAM, "whatsapp": BUY_WHATSAPP})

@app.get("/api/products")
def api_products():
    limit = int(request.args.get("limit", 30))
    offset = int(request.args.get("offset", 0))
    q = (request.args.get("q") or "").strip()
    brand = (request.args.get("brand") or "").strip().lstrip("#")

    try:
        query = sb.table(TABLE).select("*").order("ts", desc=True)

        if brand:
            query = query.ilike("brand", f"%{brand}%")

        if q:
            query = query.ilike("raw_text", f"%{q}%")

        res = query.range(offset, offset + limit - 1).execute()
        data = res.data or []

        # нормализуем: чтобы index.html не ломался
        out = []
        for p in data:
            out.append({
                "id": p.get("id", ""),
                "brand": p.get("brand", "") or "—",
                "price_raw": p.get("price_raw", "") or "",
                "caption": p.get("caption", "") or "",
                "file_id": p.get("file_id", "") or "",
                "file_ids": p.get("file_ids", []) or [],
                "raw_text": p.get("raw_text", "") or "",
                "ts": p.get("ts", 0) or 0,
            })
        return jsonify(out)

    except Exception as e:
        return jsonify({
            "error": "api_products_failed",
            "details": str(e),
            "hint": "Проверь SUPABASE_TABLE и колонки (ts, file_ids) + RLS policy SELECT."
        }), 500

# =========================
# TELEGRAM WEBHOOK
# =========================

@app.post("/telegram")
def telegram_webhook():
    # 1) периодически “досохраняем” альбомы
    try:
        flush_media_groups_if_ready()
    except Exception:
        pass

    # 2) optional secret check
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        qs = request.args.get("secret", "")
        if hdr != WEBHOOK_SECRET and qs != WEBHOOK_SECRET:
            # лучше вернуть 403, если ты включила secret
            return ("forbidden", 403)

    update = request.get_json(silent=True) or {}

    msg = (
        update.get("channel_post")
        or update.get("message")
        or update.get("edited_channel_post")
        or update.get("edited_message")
        or {}
    )
    if not msg:
        return "ok"

    text = (msg.get("caption") or msg.get("text") or "").strip()
    if not text:
        return "ok"

    parsed = parse_post_text(text)

    # file_ids: берём самое большое качество фото
    best_file_id = ""
    photos = msg.get("photo") or []
    if isinstance(photos, list) and photos:
        best_file_id = photos[-1].get("file_id", "")

    media_group_id = msg.get("media_group_id")

    chat_id = (msg.get("chat") or {}).get("id", "")
    message_id = msg.get("message_id", "")
    ts = int(msg.get("date") or int(time.time()))

    # базовая запись
    record = {
        "id": f"{ts}_{chat_id}_{message_id}",
        "ts": ts,
        "brand": parsed["brand"],
        "price_raw": parsed["price_raw"],
        "caption": parsed["caption"],     # 3-я строка
        "raw_text": parsed["raw_text"],
        "file_id": best_file_id or "",
        "file_ids": [best_file_id] if best_file_id else [],
    }

    # Альбом: копим несколько фото в 1 запись
    if media_group_id and best_file_id:
        if media_group_id not in MEDIA_GROUP_CACHE:
            MEDIA_GROUP_CACHE[media_group_id] = {"ts": time.time(), "ids": [], "base": record}
        MEDIA_GROUP_CACHE[media_group_id]["ts"] = time.time()

        ids = MEDIA_GROUP_CACHE[media_group_id]["ids"]
        if best_file_id not in ids:
            ids.append(best_file_id)

        # не пишем сразу — ждём пока альбом закончится
        return "ok"

    # Обычный пост (или альбом без фото)
    sb.table(TABLE).upsert(record).execute()
    return "ok"

# =========================
# LOCAL
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=True)
