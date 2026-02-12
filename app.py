import os
import re
import time
import requests
from flask import Flask, request, jsonify, render_template, Response, abort

app = Flask(__name__)

# =======================
# ENV (Render -> Environment)
# =======================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BUY_TELEGRAM = os.environ.get("BUY_TELEGRAM", "mikab16").strip().lstrip("@")
BUY_WHATSAPP = os.environ.get("BUY_WHATSAPP", "393463203783").strip().replace("+", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()  # optional

# =======================
# STORAGE (простая память, можно потом заменить на базу)
# =======================
PRODUCTS = []
MAX_PRODUCTS = 500  # чтобы не разрасталось бесконечно


# =======================
# REGEX
# =======================

# Цена со скидкой: должно быть "€" + "%" + "=€"
# Примеры: 245€-25%=€183,75  |  3900€-20%=€3.120,00
PRICE_DISCOUNT_RE = re.compile(r"€.*%.*=€", re.IGNORECASE)

# Хэштег бренда: #entirestudios #Dior #jilsander
HASHTAG_BRAND_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,40})")

# Размеры буквенные: XXS XS S M L XL XXL (в любой строке)
SIZE_WORDS_RE = re.compile(r"(?i)\b(XXS|XS|S|M|L|XL|XXL)\b")

# Размеры числовые диапазоны/наборы: 50,52 / 50 52 / 50-52 / 50/52
SIZE_MULTI_NUM_RE = re.compile(r"^\s*\d{1,3}\s*([,/\-\s]\s*\d{1,3})+\s*$")

# Одиночный размер + система: 38FR / 40 IT / 42EU
SIZE_SYS_RE = re.compile(r"^\s*\d{1,3}\s*(FR|IT|EU|US)\s*$", re.IGNORECASE)

# Линии сервиса
SERVICE_RE = re.compile(r"(?i)^\s*(по вопросам|вопрос|инфо|info)\b")
AT_RE = re.compile(r"^\s*@")

# "брендовая" строка: латиница/кириллица с буквами, без € и без очевидного мусора
# (чтобы находить Chloé / DIOR / entirestudios / Jil Sander)
LIKELY_BRAND_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё0-9&'.\- ]{2,40}$")


# =======================
# HELPERS
# =======================

def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def is_service_line(line: str) -> bool:
    t = (line or "").strip()
    if not t:
        return True
    if SERVICE_RE.search(t):
        return True
    if AT_RE.search(t):
        return True
    return False

def looks_like_price(line: str) -> bool:
    t = (line or "").strip()
    if "€" not in t:
        return False
    return bool(PRICE_DISCOUNT_RE.search(t))

def extract_brand(lines):
    """
    1) сначала ищем #brand
    2) затем ищем строку похожую на бренд (без €, не service)
    """
    # 1) hashtag
    for l in lines:
        m = HASHTAG_BRAND_RE.search(l)
        if m:
            return m.group(1)

    # 2) "похоже на бренд" — первая подходящая строка
    for l in lines:
        t = normalize_spaces(l)
        if is_service_line(t):
            continue
        if "€" in t:
            continue
        if t.startswith("#"):
            continue
        # иногда бывают просто эмодзи строкой — пропускаем
        if len(re.sub(r"\W", "", t)) == 0:
            continue
        if LIKELY_BRAND_RE.match(t):
            return t

    return ""

def extract_price(lines):
    for l in lines:
        t = normalize_spaces(l)
        if looks_like_price(t):
            return t
    return ""

def extract_sizes(lines):
    """
    Берем первую строку, которая выглядит как размеры.
    Важное: размеры могут быть как буквенные "L, M, XS(на мне)" так и числовые "50,52" и "38FR".
    """
    for l in lines:
        t = (l or "").strip()
        if is_service_line(t):
            continue
        # буквенные размеры
        if SIZE_WORDS_RE.search(t):
            return t
        # числовые пачкой
        if SIZE_MULTI_NUM_RE.match(t):
            return t
        # одиночный размер с системой
        if SIZE_SYS_RE.match(t):
            return t

    return ""

def build_clean_payload(text: str):
    """
    Твое требование:
    1 строка: бренд
    2 строка: цена (со скидкой, без отдельного итого)
    3 строка: размеры (если есть)
    Эмодзи можно как угодно — оставляем в "raw_lines" для отображения в карточке (если надо).
    """
    raw_lines = [l.rstrip() for l in (text or "").split("\n")]
    lines = [l.strip() for l in raw_lines if l.strip()]

    brand = extract_brand(lines)
    price_raw = extract_price(lines)
    sizes = extract_sizes(lines)

    # Если бренд пустой — не пишем ITEM, ставим —
    if not brand:
        brand = "—"

    return {
        "brand": brand,
        "price_raw": price_raw,
        "sizes": sizes,
        "raw_text": "\n".join(lines)[:2500],
        "ts": int(time.time())
    }

def telegram_api(method: str, params=None):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, json=params or {}, timeout=25)
    r.raise_for_status()
    return r.json()

def telegram_get_file_path(file_id: str) -> str:
    j = telegram_api("getFile", {"file_id": file_id})
    return j["result"]["file_path"]


# =======================
# ROUTES
# =======================

@app.route("/")
def index():
    # твой index.html лежит в templates/index.html
    return render_template("index.html")

@app.route("/api/buy_target")
def buy_target():
    return jsonify({"telegram": BUY_TELEGRAM, "whatsapp": BUY_WHATSAPP})

@app.route("/api/products")
def api_products():
    limit = int(request.args.get("limit", 30))
    offset = int(request.args.get("offset", 0))
    q = (request.args.get("q") or "").strip().lower()
    brand_q = (request.args.get("brand") or "").strip().lower()
    brand_q = brand_q.lstrip("#")

    items = PRODUCTS

    if brand_q:
        items = [p for p in items if (p.get("brand") or "").lower().lstrip("#") == brand_q]

    if q:
        def hit(p):
            hay = " ".join([
                (p.get("brand") or ""),
                (p.get("price_raw") or ""),
                (p.get("sizes") or ""),
                (p.get("raw_text") or ""),
            ]).lower()
            return q in hay
        items = [p for p in items if hit(p)]

    return jsonify(items[offset:offset + limit])

@app.route("/img/<file_id>")
def img(file_id):
    if not BOT_TOKEN:
        abort(500, "BOT_TOKEN missing")

    # получаем file_path
    try:
        file_path = telegram_get_file_path(file_id)
    except Exception:
        abort(404)

    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    rr = requests.get(file_url, stream=True, timeout=25)
    if rr.status_code != 200:
        abort(404)

    content_type = rr.headers.get("Content-Type", "image/jpeg")
    return Response(rr.content, status=200, content_type=content_type)

@app.route("/health")
def health():
    return "ok"

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    # Optional: проверка секрета (если включишь в setWebhook secret_token)
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != WEBHOOK_SECRET:
            return ("forbidden", 403)

    update = request.get_json(silent=True) or {}

    # Telegram может прислать message / channel_post / edited_*
    msg = (
        update.get("channel_post")
        or update.get("message")
        or update.get("edited_channel_post")
        or update.get("edited_message")
        or {}
    )

    if not msg:
        return "ok"

    # текст может быть в caption (если фото) или text (если просто текст)
    text = msg.get("caption") or msg.get("text") or ""
    if not text.strip():
        return "ok"

    # фото берем самое большое
    file_id = None
    if isinstance(msg.get("photo"), list) and msg["photo"]:
        file_id = msg["photo"][-1].get("file_id")

    parsed = build_clean_payload(text)

    item = {
        "id": f"{parsed['ts']}_{len(PRODUCTS)+1}",
        "brand": parsed["brand"],
        "price_raw": parsed["price_raw"],
        "caption": parsed["sizes"],   # 👈 ВАЖНО: в каталоге "caption" = 3-я строка (размеры)
        "raw_text": parsed["raw_text"],
        "file_id": file_id,
        "ts": parsed["ts"],
    }

    PRODUCTS.insert(0, item)
    if len(PRODUCTS) > MAX_PRODUCTS:
        del PRODUCTS[MAX_PRODUCTS:]

    return "ok"
