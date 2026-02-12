import os
import re
import json
import time
import threading
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, request, jsonify, send_file, abort
from supabase import create_client
from postgrest.exceptions import APIError


# =========================
# ENV
# =========================
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()

# Для чтения (можно anon)
SUPABASE_KEY = (os.environ.get("SUPABASE_KEY") or "").strip()
# Для записи (лучше service_role, иначе при RLS будут проблемы)
SUPABASE_SERVICE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()

SUPABASE_TABLE = (os.environ.get("SUPABASE_TABLE") or "products").strip()

BUY_TELEGRAM = (os.environ.get("BUY_TELEGRAM") or "").strip().lstrip("@")
BUY_WHATSAPP = (os.environ.get("BUY_WHATSAPP") or "393463203783").strip().replace("+", "").replace(" ", "")

WEBHOOK_SECRET = (os.environ.get("WEBHOOK_SECRET") or "").strip()

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

app = Flask(__name__)

sb_read = None
sb_write = None

if SUPABASE_URL and SUPABASE_KEY:
    sb_read = create_client(SUPABASE_URL, SUPABASE_KEY)

if SUPABASE_URL and (SUPABASE_SERVICE_KEY or SUPABASE_KEY):
    sb_write = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY or SUPABASE_KEY)


# =========================
# Parsing helpers
# =========================
HASHTAG_BRAND_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,40})")
PRICE_LINE_RE = re.compile(r"€.*%.*=€", re.IGNORECASE)
SERVICE_LINE_RE = re.compile(r"(?i)^\s*(по вопросам|вопрос|контакт|писать)\b|^\s*@")

def extract_brand(text: str) -> str:
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]

    # 1) #brand
    for l in lines:
        m = HASHTAG_BRAND_RE.search(l)
        if m:
            return m.group(1)

    # 2) короткая строка-бренд без цифр/цен
    for l in lines:
        if SERVICE_LINE_RE.search(l):
            continue
        if "€" in l or "%" in l:
            continue
        cleaned = re.sub(r"[^\wÀ-ž'\- ]+", " ", l).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if 2 <= len(cleaned) <= 35 and not re.search(r"\d", cleaned):
            return cleaned

    return ""

def extract_price_line(text: str) -> str:
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    for l in lines:
        if PRICE_LINE_RE.search(l):
            return l
    for l in lines:
        if ("€" in l) and ("%" in l):
            return l
    return ""

def extract_sizes(text: str) -> str:
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    for l in lines:
        if SERVICE_LINE_RE.search(l):
            continue
        if ("€" in l) and ("%" in l):
            continue
        if HASHTAG_BRAND_RE.search(l):
            continue

        if re.search(r"\b(XXS|XS|S|M|L|XL|XXL)\b", l, re.IGNORECASE):
            return l
        if re.fullmatch(r"[0-9 ,\-\/]+(FR|IT|EU|US)?", l, re.IGNORECASE):
            # отсекаем одиночное "8"
            digits = re.sub(r"\D", "", l)
            if len(digits) >= 2 or re.search(r"(FR|IT|EU|US)", l, re.IGNORECASE):
                return l
        if re.search(r"\b\d{1,2}\s*(FR|IT|EU|US)\b", l, re.IGNORECASE):
            return l

    return ""

def build_caption(text: str) -> str:
    return extract_sizes(text).strip()


# =========================
# Telegram image proxy cache
# =========================
_file_path_cache: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 3600

def tg_get_file_path(file_id: str) -> Optional[str]:
    if not BOT_TOKEN:
        return None

    now = time.time()
    cached = _file_path_cache.get(file_id)
    if cached and (now - cached["ts"] < CACHE_TTL):
        return cached["path"]

    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=20)
    if r.status_code != 200:
        return None
    j = r.json()
    if not j.get("ok"):
        return None

    path = j["result"]["file_path"]
    _file_path_cache[file_id] = {"path": path, "ts": now}
    return path


# =========================
# Album buffer
# =========================
_album_lock = threading.Lock()
_albums: Dict[int, Dict[str, Any]] = {}
_album_timers: Dict[int, threading.Timer] = {}
ALBUM_FLUSH_DELAY = 1.5

def _flush_album(mg: int):
    with _album_lock:
        item = _albums.pop(mg, None)
        t = _album_timers.pop(mg, None)
        if t:
            try: t.cancel()
            except: pass

    if not item:
        return
    upsert_product(item)

def _schedule_flush(mg: int):
    with _album_lock:
        old = _album_timers.get(mg)
        if old:
            try: old.cancel()
            except: pass
        t = threading.Timer(ALBUM_FLUSH_DELAY, _flush_album, args=(mg,))
        t.daemon = True
        _album_timers[mg] = t
        t.start()


# =========================
# Supabase operations (robust)
# =========================
def upsert_product(row: Dict[str, Any]) -> None:
    if not sb_write:
        return
    sb_write.table(SUPABASE_TABLE).upsert(row, on_conflict="message_id").execute()

def safe_select_products(limit: int, offset: int) -> List[Dict[str, Any]]:
    """
    ВАЖНО: select('*') чтобы не падать из-за отсутствующих колонок
    """
    if not sb_read:
        return []
    res = (
        sb_read
        .table(SUPABASE_TABLE)
        .select("*")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return res.data or []


# =========================
# API
# =========================
@app.get("/api/health")
def api_health():
    out = {
        "ok": True,
        "bot_token_set": bool(BOT_TOKEN),
        "supabase_url_set": bool(SUPABASE_URL),
        "supabase_key_set": bool(SUPABASE_KEY),
        "supabase_service_key_set": bool(SUPABASE_SERVICE_KEY),
        "table": SUPABASE_TABLE,
    }
    # пробуем простой запрос
    try:
        if sb_read:
            _ = sb_read.table(SUPABASE_TABLE).select("id").limit(1).execute()
            out["supabase_read_ok"] = True
        else:
            out["supabase_read_ok"] = False
    except Exception as e:
        out["supabase_read_ok"] = False
        out["supabase_error"] = str(e)
    return jsonify(out)

@app.get("/api/buy_target")
def api_buy_target():
    return jsonify({"telegram": BUY_TELEGRAM, "whatsapp": BUY_WHATSAPP})

@app.get("/api/products")
def api_products():
    try:
        limit = int(request.args.get("limit") or 30)
        offset = int(request.args.get("offset") or 0)
        limit = max(1, min(60, limit))
        offset = max(0, offset)

        # СНАЧАЛА — гарантированно отдаём данные (без сложных фильтров),
        # чтобы у тебя НАКОНЕЦ исчезла ошибка.
        data = safe_select_products(limit, offset)

        out = []
        for p in data:
            file_ids = p.get("file_ids")

            # file_ids может быть jsonb (list) или строкой
            if isinstance(file_ids, str):
                try:
                    file_ids = json.loads(file_ids)
                except Exception:
                    file_ids = None

            if not isinstance(file_ids, list):
                file_ids = []

            file_id = p.get("file_id")
            if file_id and file_id not in file_ids:
                file_ids = [file_id] + file_ids

            out.append({
                "id": str(p.get("id") or ""),
                "message_id": p.get("message_id"),
                "media_group_id": p.get("media_group_id"),
                "brand": p.get("brand") or "",
                "price_raw": p.get("price_raw") or "",
                "caption": p.get("caption") or "",
                "file_id": file_id,
                "file_ids": file_ids,
                "created_at": p.get("created_at"),
            })

        return jsonify(out)

    except APIError as e:
        return jsonify({
            "error": "supabase_api_error",
            "details": getattr(e, "message", str(e)),
            "hint": "Обычно это RLS (policy) или неверная таблица/ключ."
        }), 500
    except Exception as e:
        return jsonify({"error": "api_products_failed", "details": str(e)}), 500


@app.get("/img/<file_id>")
def img_proxy(file_id: str):
    fp = tg_get_file_path(file_id)
    if not fp:
        abort(404)
    url = f"{TG_FILE_API}/{fp}"
    r = requests.get(url, stream=True, timeout=30)
    if r.status_code != 200:
        abort(404)

    from io import BytesIO
    buf = BytesIO(r.content)
    buf.seek(0)
    return send_file(buf, mimetype="image/jpeg", max_age=3600)


# =========================
# Telegram webhook
# =========================
def parse_post(post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    message_id = post.get("message_id")
    if not message_id:
        return None

    text = (post.get("caption") or post.get("text") or "").strip()
    date_ts = int(post.get("date") or time.time())
    mg = post.get("media_group_id")

    photos = post.get("photo") or []
    best = photos[-1]["file_id"] if photos else None

    row = {
        "message_id": int(message_id),
        "media_group_id": int(mg) if mg else None,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(date_ts)),
        "brand": extract_brand(text),
        "price_raw": extract_price_line(text),
        "caption": build_caption(text),
        "file_id": best,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(date_ts)),
    }

    # file_ids как список (для альбомов соберём позже)
    if best:
        row["file_ids"] = [best]
    else:
        row["file_ids"] = []

    return row

@app.post("/telegram")
def telegram_webhook():
    if WEBHOOK_SECRET:
        secret = request.args.get("secret", "")
        if secret != WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "forbidden"}), 403

    upd = request.get_json(silent=True) or {}
    post = upd.get("channel_post") or upd.get("edited_channel_post")
    if not post:
        return jsonify({"ok": True})

    row = parse_post(post)
    if not row:
        return jsonify({"ok": True})

    mg = row.get("media_group_id")
    if mg:
        with _album_lock:
            cur = _albums.get(mg)
            if not cur:
                _albums[mg] = row
            else:
                # обновляем поля если пришли
                if row.get("brand"):
                    cur["brand"] = row["brand"]
                if row.get("price_raw"):
                    cur["price_raw"] = row["price_raw"]
                if row.get("caption"):
                    cur["caption"] = row["caption"]
                # добавляем file_id
                for fid in row.get("file_ids") or []:
                    if fid and fid not in cur["file_ids"]:
                        cur["file_ids"].append(fid)
                cur["file_id"] = cur["file_ids"][0] if cur["file_ids"] else cur.get("file_id")

        _schedule_flush(int(mg))
    else:
        upsert_product(row)

    return jsonify({"ok": True})


# =========================
# Front
# =========================
@app.get("/")
def home():
    from flask import render_template
    return render_template("index.html")
