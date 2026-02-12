import os
import re
import json
import time
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, request, jsonify, send_from_directory, Response, abort
from supabase import create_client, Client


# ----------------- ENV -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "products").strip()

BUY_TELEGRAM = os.getenv("BUY_TELEGRAM", "").strip()  # username without @
BUY_WHATSAPP = os.getenv("BUY_WHATSAPP", "").strip()  # digits only, no +
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # e.g. https://<service>.onrender.com/telegram

if not BOT_TOKEN:
    print("⚠️ BOT_TOKEN is empty")
if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠️ SUPABASE_URL/SUPABASE_KEY is empty")

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"


# ----------------- PARSING RULES -----------------
HASHTAG_BRAND_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,40})")
PRICE_RE = re.compile(r"€\s*\d")  # very loose trigger
DISCOUNT_RE = re.compile(r"%")
EUR_OR_EQ_RE = re.compile(r"(€|=€)")

SIZE_WORDS_RE = re.compile(r"(?i)\b(XXS|XS|S|M|L|XL|XXL)\b")
SIZE_NUM_RE = re.compile(r"^\s*\d{1,3}(\s*[-,/]\s*\d{1,3})+(\s*(FR|IT|EU|US))?\s*$", re.IGNORECASE)
SIZE_SINGLE_NUM_WITH_SYS_RE = re.compile(r"^\s*\d{1,3}\s*(FR|IT|EU|US)\s*$", re.IGNORECASE)

SERVICE_LINE_RE = re.compile(r"(?i)^\s*(по вопросам|вопрос|inquiries|dm)\b")

KNOWN_BRANDS = {
    # Add more if you want. Bot still works without this list.
    "dior", "chloé", "chloe", "jilsander", "entirestudios", "balmain",
}


def is_service_line(line: str) -> bool:
    t = (line or "").strip()
    if not t:
        return True
    if SERVICE_LINE_RE.search(t):
        return True
    if t.strip().startswith("@"):
        return True
    return False


def looks_like_price_line(line: str) -> bool:
    t = (line or "").strip()
    # Need discount visible: contains € AND (%) usually
    if "€" in t and ("%" in t or "=€" in t or "=%" in t):
        return True
    # fallback: € and %
    if PRICE_RE.search(t) and DISCOUNT_RE.search(t):
        return True
    return False


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
    # Common "40,41,42,43" format
    if re.match(r"^\s*\d{1,3}(\s*,\s*\d{1,3}){1,10}\s*(FR|IT|EU|US)?\s*$", t, re.IGNORECASE):
        return True
    return False


def normalize_brand(b: str) -> str:
    b = (b or "").strip()
    if not b:
        return ""
    # remove leading # and punctuation around
    b = b.lstrip("#").strip()
    return b


def find_brand(lines: List[str]) -> str:
    """
    Brand can be anywhere. Priority:
    1) hashtag #brand
    2) exact known brand word in line
    3) first non-service line that looks like a brand (short-ish, letters)
    """
    # 1) hashtag
    for ln in lines:
        m = HASHTAG_BRAND_RE.search(ln or "")
        if m:
            return normalize_brand(m.group(1))

    # 2) known brands (case-insensitive)
    low_lines = [ (ln or "").lower() for ln in lines ]
    for b in KNOWN_BRANDS:
        for ln in low_lines:
            if b in ln:
                return normalize_brand(b)

    # 3) heuristic: first line with letters, not price/size/service
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
        # allow emojis around
        pure = re.sub(r"[^\wÀ-ÿ\s'-]", "", t, flags=re.UNICODE).strip()
        if pure and len(pure) <= 30 and re.search(r"[A-Za-zÀ-ÿ]", pure):
            return normalize_brand(pure)
    return ""


def parse_text(raw_text: str) -> Dict[str, str]:
    """
    Output:
    brand (first line in catalog)
    price_raw (second line: keep exactly like "185€-25%=€138,75")
    caption (third line: sizes if present, else empty)
    """
    raw_text = (raw_text or "").strip()
    lines = [ln.strip() for ln in raw_text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]

    brand = find_brand(lines)

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

    # Caption should be sizes only (as you wanted), emojis can stay in raw_text but sizes ok with emojis too.
    caption = size_line

    return {
        "brand": brand,
        "price_raw": price_raw,
        "caption": caption,
    }


# ----------------- SUPABASE HELPERS -----------------
def upsert_product(row: Dict[str, Any]) -> None:
    # Ensure file_ids is jsonb array
    if "file_ids" in row and isinstance(row["file_ids"], list):
        row["file_ids"] = row["file_ids"]  # supabase-py will serialize
    sb.table(SUPABASE_TABLE).upsert(row, on_conflict="message_id").execute()


def get_product_by_message_id(message_id: int) -> Optional[Dict[str, Any]]:
    res = sb.table(SUPABASE_TABLE).select("*").eq("message_id", message_id).limit(1).execute()
    data = getattr(res, "data", None) or []
    return data[0] if data else None


def get_product_by_media_group(media_group_id: str) -> Optional[Dict[str, Any]]:
    res = sb.table(SUPABASE_TABLE).select("*").eq("media_group_id", media_group_id).order("id", desc=True).limit(1).execute()
    data = getattr(res, "data", None) or []
    return data[0] if data else None


# ----------------- TELEGRAM HELPERS -----------------
def tg_get_file_path(file_id: str) -> str:
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"getFile failed: {j}")
    return j["result"]["file_path"]


def pick_best_photo_file_id(message: Dict[str, Any]) -> Optional[str]:
    photos = message.get("photo") or []
    if not photos:
        return None
    # Telegram gives multiple sizes; take the biggest (last usually)
    best = photos[-1]
    return best.get("file_id")


# ----------------- FLASK APP -----------------
app = Flask(__name__, static_folder="templates", static_url_path="")

@app.get("/")
def index():
    return send_from_directory("templates", "index.html")


@app.get("/api/buy_target")
def buy_target():
    return jsonify({
        "telegram": BUY_TELEGRAM,
        "whatsapp": BUY_WHATSAPP,
    })


@app.get("/api/products")
def api_products():
    """
    Returns list of products:
    brand, price_raw, caption, file_ids[], file_id, id
    Supports ?q= and ?brand= and pagination: limit/offset
    """
    q = (request.args.get("q") or "").strip()
    brand = (request.args.get("brand") or "").strip()
    limit = int(request.args.get("limit") or 30)
    offset = int(request.args.get("offset") or 0)

    # IMPORTANT: we sort by ts (if exists), else by date/created_at
    query = sb.table(SUPABASE_TABLE).select("id,brand,price_raw,caption,file_id,file_ids,raw_text,ts,date,created_at")

    if brand:
        b = brand.lstrip("#").strip()
        # case-insensitive contains
        query = query.ilike("brand", f"%{b}%")

    if q:
        # search in raw_text OR brand OR price_raw
        # Supabase filters: use "or" syntax
        q_esc = q.replace(",", " ")
        query = query.or_(f"raw_text.ilike.%{q_esc}%,brand.ilike.%{q_esc}%,price_raw.ilike.%{q_esc}%")

    # order: ts desc if present, else date desc, else created_at desc
    # Supabase needs a column. We'll do ts desc first; if null it will go last, acceptable.
    query = query.order("ts", desc=True).order("date", desc=True).order("created_at", desc=True)

    res = query.range(offset, offset + limit - 1).execute()
    data = getattr(res, "data", None) or []

    # Normalize output: ensure file_ids is list
    out = []
    for p in data:
        fids = p.get("file_ids")
        if isinstance(fids, str):
            try:
                fids = json.loads(fids)
            except:
                fids = None
        if not isinstance(fids, list):
            fids = []
            if p.get("file_id"):
                fids = [p["file_id"]]

        out.append({
            "id": p.get("id"),
            "brand": p.get("brand") or "",
            "price_raw": p.get("price_raw") or "",
            "caption": p.get("caption") or "",
            "file_id": p.get("file_id") or (fids[0] if fids else ""),
            "file_ids": fids,
            "ts": p.get("ts"),
        })

    return jsonify(out)


@app.get("/img/<path:file_id>")
def img_proxy(file_id: str):
    """
    Proxy Telegram file by file_id.
    Browser loads /img/<file_id> and we stream the file from Telegram.
    """
    if not BOT_TOKEN:
        abort(500, "BOT_TOKEN not set")

    try:
        file_path = tg_get_file_path(file_id)
        url = f"{TG_FILE}/{file_path}"
        rr = requests.get(url, stream=True, timeout=60)
        rr.raise_for_status()

        # Telegram usually returns image/jpeg
        ctype = rr.headers.get("Content-Type", "image/jpeg")
        return Response(rr.iter_content(chunk_size=1024 * 64), content_type=ctype)
    except Exception as e:
        return Response("not found", status=404)


@app.post("/telegram")
def telegram_webhook():
    # Optional security: header check (Telegram supports secret token)
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != WEBHOOK_SECRET:
            return jsonify({"ok": True})  # silently ignore

    upd = request.get_json(silent=True) or {}
    msg = upd.get("message") or upd.get("channel_post") or upd.get("edited_channel_post") or upd.get("edited_message")
    if not msg:
        return jsonify({"ok": True})

    message_id = msg.get("message_id")
    if not message_id:
        return jsonify({"ok": True})

    media_group_id = msg.get("media_group_id")  # albums share this
    caption = msg.get("caption") or msg.get("text") or ""
    # combine text pieces if needed
    raw_text = caption.strip()

    # best photo
    file_id = pick_best_photo_file_id(msg)

    # Parse brand/price/sizes
    parsed = parse_text(raw_text)

    # timestamp
    dt = msg.get("date")  # unix seconds
    ts = int(dt) if isinstance(dt, int) else int(time.time())

    # If it's an album: store as ONE product by media_group_id,
    # but we still need a numeric message_id unique key in schema. We'll keep:
    # - message_id = first message_id we saw for the album (stored row)
    # - media_group_id = album id
    if media_group_id:
        existing = get_product_by_media_group(media_group_id)
        if existing:
            # merge file_ids
            fids = existing.get("file_ids")
            if isinstance(fids, str):
                try:
                    fids = json.loads(fids)
                except:
                    fids = []
            if not isinstance(fids, list):
                fids = []
            if file_id and file_id not in fids:
                fids.append(file_id)

            # keep the richest text/brand/price
            brand = existing.get("brand") or parsed["brand"]
            price_raw = existing.get("price_raw") or parsed["price_raw"]
            cap = existing.get("caption") or parsed["caption"]
            raw = existing.get("raw_text") or raw_text

            row = {
                "id": existing.get("id"),
                "message_id": existing.get("message_id"),
                "media_group_id": media_group_id,
                "brand": brand or "",
                "price_raw": price_raw or "",
                "caption": cap or "",
                "raw_text": raw or "",
                "file_id": existing.get("file_id") or file_id or "",
                "file_ids": fids,
                "ts": existing.get("ts") or ts,
            }
            sb.table(SUPABASE_TABLE).upsert(row, on_conflict="message_id").execute()
            return jsonify({"ok": True})

        # new album row (first photo in album)
        row = {
            "message_id": int(message_id),
            "media_group_id": media_group_id,
            "date": None,
            "brand": parsed["brand"] or "",
            "price_raw": parsed["price_raw"] or "",
            "caption": parsed["caption"] or "",
            "raw_text": raw_text or "",
            "file_id": file_id or "",
            "file_ids": [file_id] if file_id else [],
            "ts": ts,
        }
        upsert_product(row)
        return jsonify({"ok": True})

    # Not album: one message = one product
    row = {
        "message_id": int(message_id),
        "media_group_id": None,
        "date": None,
        "brand": parsed["brand"] or "",
        "price_raw": parsed["price_raw"] or "",
        "caption": parsed["caption"] or "",
        "raw_text": raw_text or "",
        "file_id": file_id or "",
        "file_ids": [file_id] if file_id else [],
        "ts": ts,
    }
    upsert_product(row)
    return jsonify({"ok": True})


@app.get("/health")
def health():
    return "ok"


@app.get("/setup_webhook")
def setup_webhook():
    """
    One-time helper. Open:
    https://<service>.onrender.com/setup_webhook
    """
    if not WEBHOOK_URL:
        return "WEBHOOK_URL env is empty", 400
    payload = {"url": WEBHOOK_URL}
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET
    r = requests.get(f"{TG_API}/setWebhook", params=payload, timeout=20)
    return jsonify(r.json())
