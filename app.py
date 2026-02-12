import os
import re
import json
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, request, jsonify, send_file, abort
from supabase import create_client, Client


# =========================
# ENV
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()  # можно пустым, но лучше поставить
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "products").strip()

BUY_TELEGRAM = os.environ.get("BUY_TELEGRAM", "").strip().lstrip("@")
BUY_WHATSAPP = os.environ.get("BUY_WHATSAPP", "393463203783").strip().replace("+", "").replace(" ", "")

# telegram endpoints
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

# basic checks
if not BOT_TOKEN:
    print("⚠️ BOT_TOKEN is empty. Webhook will not fetch images.")
if not (SUPABASE_URL and SUPABASE_KEY):
    print("⚠️ SUPABASE_URL / SUPABASE_KEY are empty. /api/products will fail.")

sb: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)

# =========================
# Utils: parsing
# =========================

HASHTAG_BRAND_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,40})")
PRICE_LINE_RE = re.compile(r"€.*%.*=€", re.IGNORECASE)  # 3900€-20%=€3.120,00
SERVICE_LINE_RE = re.compile(r"(?i)^\s*(по вопросам|вопрос|контакт|писать)\b|^\s*@")

def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def extract_brand(text: str) -> str:
    """
    Требование:
    - Бренд может быть не в первой строке
    - Может быть в виде #brand
    - ITEM писать нельзя
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # 1) hashtag brand
    for l in lines:
        m = HASHTAG_BRAND_RE.search(l)
        if m:
            return m.group(1)

    # 2) line that looks like a brand name (letters + accents), short
    # e.g. "Chloé ✨ 📸" -> "Chloé"
    for l in lines:
        if SERVICE_LINE_RE.search(l):
            continue
        # remove emojis/symbols at end
        cleaned = re.sub(r"[^\wÀ-ž'\- ]+", " ", l).strip()
        cleaned = normalize_spaces(cleaned)
        if 2 <= len(cleaned) <= 30:
            # if it's not only numbers and not a price
            if not re.search(r"\d", cleaned) and "€" not in cleaned and "%" not in cleaned:
                return cleaned.split(" ")[0] if len(cleaned.split(" ")) == 1 else cleaned

    return ""

def extract_price_line(text: str) -> str:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for l in lines:
        if PRICE_LINE_RE.search(l):
            return l
    # fallback: any line with € and %
    for l in lines:
        if ("€" in l) and ("%" in l):
            return l
    return ""

def extract_sizes(text: str) -> str:
    """
    Размеры:
    - "L, M, XS(на мне) 🪭"
    - "50,52"
    - "38FR"
    - "40,41,42,43,45"
    Берём первую "похожую" строку, исключая бренд/цену/сервис.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for l in lines:
        if SERVICE_LINE_RE.search(l):
            continue
        if PRICE_LINE_RE.search(l) or ("€" in l and "%" in l):
            continue
        if HASHTAG_BRAND_RE.search(l):
            continue
        # sizes patterns
        if re.search(r"\b(XXS|XS|S|M|L|XL|XXL)\b", l, re.IGNORECASE):
            return l
        if re.fullmatch(r"[0-9 ,\-\/]+(FR|IT|EU|US)?", l, re.IGNORECASE):
            # avoid single "8" line if it is just a quantity / ref
            if len(re.sub(r"\D", "", l)) >= 2 or (re.search(r"(FR|IT|EU|US)", l, re.IGNORECASE)):
                return l
        if re.search(r"\b\d{1,2}\s*(FR|IT|EU|US)\b", l, re.IGNORECASE):
            return l
    return ""

def build_caption(text: str) -> str:
    """
    В каталоге:
    1 строка бренд (мы выводим отдельно)
    2 строка цена (выводим отдельно)
    3 строка размеры (если есть)
    Эмодзи можно как угодно — оставляем в sizes или в остальном тексте не нужно.
    """
    sizes = extract_sizes(text)
    return sizes.strip()

# =========================
# Telegram: file proxy + caching
# =========================

_file_path_cache: Dict[str, Tuple[str, float]] = {}  # file_id -> (file_path, ts)
CACHE_TTL = 3600

def tg_get_file_path(file_id: str) -> Optional[str]:
    if not BOT_TOKEN:
        return None
    now = time.time()
    cached = _file_path_cache.get(file_id)
    if cached and (now - cached[1] < CACHE_TTL):
        return cached[0]

    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=20)
    if r.status_code != 200:
        return None
    j = r.json()
    if not j.get("ok"):
        return None
    file_path = j["result"]["file_path"]
    _file_path_cache[file_id] = (file_path, now)
    return file_path

# =========================
# Supabase helpers
# =========================

def sb_upsert_product(p: Dict[str, Any]) -> None:
    if not sb:
        return
    # make sure jsonb is json
    if "file_ids" in p and isinstance(p["file_ids"], list):
        p["file_ids"] = p["file_ids"]

    sb.table(SUPABASE_TABLE).upsert(p, on_conflict="message_id").execute()

def sb_list_products(q: str, brand: str, limit: int, offset: int) -> List[Dict[str, Any]]:
    if not sb:
        return []
    t = sb.table(SUPABASE_TABLE).select(
        "id,message_id,media_group_id,brand,price_raw,caption,file_id,file_ids,created_at,ts"
    )

    if brand:
        t = t.ilike("brand", f"%{brand}%")
    if q:
        # search in brand + price_raw + caption
        # supabase-py doesn't support OR nicely; do two queries? simplest: just caption OR brand:
        t = t.or_(f"brand.ilike.%{q}%,caption.ilike.%{q}%,price_raw.ilike.%{q}%")

    # prefer ts desc if exists, else created_at desc
    t = t.order("ts", desc=True, nulls_last=True).order("created_at", desc=True)
    t = t.range(offset, offset + limit - 1)
    res = t.execute()
    return res.data or []

# =========================
# Album buffering (media_group_id)
# =========================

_album_lock = threading.Lock()
_albums: Dict[int, Dict[str, Any]] = {}
_album_timers: Dict[int, threading.Timer] = {}
ALBUM_FLUSH_DELAY = 1.6  # seconds

def _flush_album(media_group_id: int):
    with _album_lock:
        item = _albums.pop(media_group_id, None)
        timer = _album_timers.pop(media_group_id, None)
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass

    if not item:
        return

    # Save to Supabase
    sb_upsert_product(item)

def _schedule_album_flush(media_group_id: int):
    with _album_lock:
        if media_group_id in _album_timers:
            try:
                _album_timers[media_group_id].cancel()
            except Exception:
                pass
        t = threading.Timer(ALBUM_FLUSH_DELAY, _flush_album, args=(media_group_id,))
        _album_timers[media_group_id] = t
        t.daemon = True
        t.start()

# =========================
# Telegram update handling
# =========================

def parse_channel_post(post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Build product row from Telegram channel_post.
    Supports:
    - single photo + caption
    - album (media_group_id) -> buffered
    """
    message_id = post.get("message_id")
    if not message_id:
        return None

    date_ts = post.get("date")
    media_group_id = post.get("media_group_id")

    text = (post.get("caption") or post.get("text") or "").strip()

    # photo
    photos = post.get("photo") or []
    best_file_id = None
    if photos:
        # choose last (largest) size
        best_file_id = photos[-1].get("file_id")

    # for album: store every best photo per message (use last size)
    file_ids = []
    if photos:
        file_ids = [photos[-1].get("file_id")] if photos[-1].get("file_id") else []

    brand = extract_brand(text)
    price_raw = extract_price_line(text)
    caption = build_caption(text)

    row = {
        "message_id": int(message_id),
        "media_group_id": int(media_group_id) if media_group_id else None,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(date_ts))) if date_ts else None,
        "ts": int(date_ts) if date_ts else int(time.time()),
        "brand": brand,
        "price_raw": price_raw,
        "caption": caption,
        "file_id": best_file_id,
        "file_ids": file_ids,  # will be merged for albums
    }

    return row

@app.post("/telegram")
def telegram_webhook():
    # optional secret check
    if WEBHOOK_SECRET:
        secret = request.args.get("secret", "")
        if secret != WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "forbidden"}), 403

    upd = request.get_json(silent=True) or {}

    post = upd.get("channel_post") or upd.get("edited_channel_post")
    if not post:
        return jsonify({"ok": True})

    row = parse_channel_post(post)
    if not row:
        return jsonify({"ok": True})

    mg = row.get("media_group_id")

    if mg:
        # merge album
        with _album_lock:
            cur = _albums.get(mg)
            if not cur:
                cur = row
                cur["file_ids"] = [fid for fid in (row.get("file_ids") or []) if fid]
                _albums[mg] = cur
            else:
                # keep brand/price/caption if new message has them
                if row.get("brand"):
                    cur["brand"] = row["brand"]
                if row.get("price_raw"):
                    cur["price_raw"] = row["price_raw"]
                if row.get("caption"):
                    cur["caption"] = row["caption"]
                # append file
                for fid in (row.get("file_ids") or []):
                    if fid and fid not in cur["file_ids"]:
                        cur["file_ids"].append(fid)
                # keep latest ts
                cur["ts"] = max(int(cur.get("ts") or 0), int(row.get("ts") or 0))
                cur["date"] = row.get("date") or cur.get("date")

        _schedule_album_flush(int(mg))
    else:
        # single item
        row["file_ids"] = [row["file_id"]] if row.get("file_id") else []
        sb_upsert_product(row)

    return jsonify({"ok": True})

# =========================
# API for frontend
# =========================

@app.get("/api/buy_target")
def api_buy_target():
    return jsonify({"telegram": BUY_TELEGRAM, "whatsapp": BUY_WHATSAPP})

@app.get("/api/products")
def api_products():
    try:
        q = (request.args.get("q") or "").strip()
        brand = (request.args.get("brand") or "").strip()
        limit = int(request.args.get("limit") or 30)
        offset = int(request.args.get("offset") or 0)
        limit = max(1, min(60, limit))
        offset = max(0, offset)

        data = sb_list_products(q=q, brand=brand, limit=limit, offset=offset)

        # normalize output for index.html
        out = []
        for p in data:
            file_ids = p.get("file_ids") or []
            if isinstance(file_ids, str):
                try:
                    file_ids = json.loads(file_ids)
                except Exception:
                    file_ids = []
            if not file_ids and p.get("file_id"):
                file_ids = [p["file_id"]]

            out.append({
                "id": str(p.get("id") or ""),
                "message_id": p.get("message_id"),
                "brand": p.get("brand") or "",
                "price_raw": p.get("price_raw") or "",
                "caption": p.get("caption") or "",
                "file_id": p.get("file_id"),
                "file_ids": file_ids,
                "ts": p.get("ts") or 0,
            })

        return jsonify(out)

    except Exception as e:
        return jsonify({
            "error": "api_products_failed",
            "details": str(e),
            "hint": "Проверь SUPABASE_URL / SUPABASE_KEY / SUPABASE_TABLE и RLS policy (allow read)."
        }), 500

@app.get("/img/<file_id>")
def img_proxy(file_id: str):
    # Streams image from Telegram
    fp = tg_get_file_path(file_id)
    if not fp:
        abort(404)
    url = f"{TG_FILE_API}/{fp}"
    r = requests.get(url, stream=True, timeout=30)
    if r.status_code != 200:
        abort(404)

    # write to temp bytes
    from io import BytesIO
    buf = BytesIO(r.content)
    buf.seek(0)
    return send_file(buf, mimetype="image/jpeg", max_age=3600)

@app.get("/")
def home():
    # serve template
    from flask import render_template
    return render_template("index.html")
