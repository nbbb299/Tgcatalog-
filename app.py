# app.py
import os
import re
import time
import requests
from flask import Flask, request, jsonify, render_template, Response, abort

from supabase import create_client

app = Flask(__name__, template_folder="templates")

# ---------------------------
# ENV
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "products").strip()  # можно не трогать

BUY_TELEGRAM = os.getenv("BUY_TELEGRAM", "mikab16").strip()       # username без @
BUY_WHATSAPP = os.getenv("BUY_WHATSAPP", "393463203783").strip()  # без +

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

sb = None
if SUPABASE_URL and SUPABASE_KEY:
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------------------
# PARSING (brand/price/sizes/caption)
# ---------------------------
SIZE_WORDS_RE = re.compile(r"(?i)\b(XXS|XS|S|M|L|XL|XXL)\b")
SIZE_NUM_RE = re.compile(r"^\s*\d{1,3}(\s*[-,/]\s*\d{1,3})+(\s*(FR|IT|EU|US))?\s*$", re.IGNORECASE)
SIZE_SINGLE_NUM_WITH_SYS_RE = re.compile(r"^\s*\d{1,3}\s*(FR|IT|EU|US)\s*$", re.IGNORECASE)
HASHTAG_BRAND_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,30})")

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
    if "€" not in t:
        return False
    # ты просила чтобы всегда была видна скидка => обычно есть %
    if "%" in t:
        return True
    # иногда скидку пишут через "=€"
    if "=€" in t or "=%" in t:
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
    # тип "50,52" без системы
    if re.match(r"^\s*\d{1,3}\s*[,/]\s*\d{1,3}\s*$", t):
        return True
    return False

def normalize_brand(b: str) -> str:
    b = (b or "").strip()
    if not b:
        return ""
    # убираем # и @
    if b.startswith("#"):
        b = b[1:]
    if b.startswith("@"):
        b = b[1:]
    return b.strip()

def extract_brand(lines):
    # 1) из хештега
    for ln in lines:
        m = HASHTAG_BRAND_RE.search(ln)
        if m:
            return normalize_brand(m.group(1))

    # 2) первая "нормальная" строка, где есть буквы (не цена, не размеры, не сервис)
    for ln in lines:
        t = ln.strip()
        if is_service_line(t):
            continue
        if looks_like_price_line(t):
            continue
        if looks_like_size_line(t):
            continue
        # если строка содержит буквы
        if re.search(r"[A-Za-zА-Яа-яÀ-ÿ]", t):
            # убираем лишние эмодзи по краям, но оставляем внутри
            return normalize_brand(t)

    return ""

def parse_text(raw_text: str):
    raw_text = raw_text or ""
    lines = [l.strip() for l in raw_text.splitlines()]

    # цена (строка со скидкой)
    price_raw = ""
    for ln in lines:
        if looks_like_price_line(ln):
            price_raw = ln.strip()
            break

    brand = extract_brand(lines) or "ITEM"

    # размеры: первая строка, похожая на размеры (которая не цена и не сервис)
    sizes = ""
    for ln in lines:
        t = ln.strip()
        if is_service_line(t):
            continue
        if looks_like_price_line(t):
            continue
        if looks_like_size_line(t):
            sizes = t
            break

    # caption: делаем компактно — размеры + любые эмодзи/строки кроме цены/сервиса/бренда-хештега
    cap_lines = []
    for ln in lines:
        t = ln.strip()
        if not t:
            continue
        if is_service_line(t):
            continue
        if t == price_raw:
            continue
        # уберём строку-хештег если она чисто "#brand"
        if re.fullmatch(r"#\w+", t):
            continue
        # не повторяем бренд если он ровно равен строке
        if normalize_brand(t).lower() == normalize_brand(brand).lower():
            continue
        cap_lines.append(t)

    # если размеры есть — оставим его первым в caption (как ты просила)
    caption = ""
    if sizes:
        # уберём размеры если они уже в cap_lines
        cap_lines = [x for x in cap_lines if x != sizes]
        caption = sizes
        if cap_lines:
            caption += "\n" + "\n".join(cap_lines[:6])
    else:
        caption = "\n".join(cap_lines[:6])

    return brand, price_raw, caption

# ---------------------------
# ROUTES
# ---------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/buy_target")
def buy_target():
    return jsonify({"telegram": BUY_TELEGRAM, "whatsapp": BUY_WHATSAPP})

@app.route("/api/products")
def api_products():
    if sb is None:
        return jsonify({"error": "Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY."}), 500

    limit = int(request.args.get("limit", 30))
    offset = int(request.args.get("offset", 0))
    q = (request.args.get("q", "") or "").strip()
    brand = (request.args.get("brand", "") or "").strip()

    query = sb.table(SUPABASE_TABLE).select("id,brand,price_raw,caption,file_id,raw_text,ts").order("ts", desc=True)

    if brand:
        # ищем по бренду без #
        query = query.ilike("brand", f"%{brand}%")

    if q:
        query = query.ilike("raw_text", f"%{q}%")

    resp = query.range(offset, offset + limit - 1).execute()
    rows = resp.data or []

    # гарантируем поля
    out = []
    for r in rows:
        out.append({
            "id": str(r.get("id", "")),
            "brand": r.get("brand", "") or "ITEM",
            "price_raw": r.get("price_raw", "") or "",
            "caption": r.get("caption", "") or "",
            "file_id": r.get("file_id", "") or "",
            "raw_text": r.get("raw_text", "") or "",
            "ts": r.get("ts", 0) or 0,
        })

    return jsonify(out)

# Telegram image proxy
@app.route("/img/<file_id>")
def telegram_image(file_id):
    if not BOT_TOKEN:
        return abort(500)

    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
        params={"file_id": file_id},
        timeout=20
    )
    data = r.json()
    if not data.get("ok"):
        return abort(404)

    file_path = data["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    img = requests.get(file_url, stream=True, timeout=30)
    if img.status_code != 200:
        return abort(404)

    return Response(img.iter_content(chunk_size=1024), content_type=img.headers.get("Content-Type", "image/jpeg"))

# Telegram webhook
@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    # защита от чужих запросов (если секрет задан)
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if hdr != WEBHOOK_SECRET:
            return ("Forbidden", 403)

    upd = request.get_json(silent=True) or {}
    msg = upd.get("message") or upd.get("channel_post") or {}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return "ok"

    # file_id: берём самый большой фото
    file_id = ""
    if msg.get("photo"):
        file_id = msg["photo"][-1].get("file_id", "")
    elif msg.get("document") and (msg["document"].get("mime_type", "").startswith("image/")):
        file_id = msg["document"].get("file_id", "")

    brand, price_raw, caption = parse_text(text)

    if sb is None:
        return "ok"

    row = {
        "id": str(int(time.time())) + "_" + str(msg.get("message_id", "")),
        "brand": brand,
        "price_raw": price_raw,
        "caption": caption,
        "file_id": file_id,
        "raw_text": text,
        "ts": int(time.time()),
    }

    # upsert чтобы не плодить дубликаты
    sb.table(SUPABASE_TABLE).upsert(row).execute()
    return "ok"
