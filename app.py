import os
import re
import time
import json
import requests
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify, render_template, Response, abort

from supabase import create_client


# -----------------------------
# CONFIG
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

# необязательно, но если задан — будем проверять секрет
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# куда “купить”
BUY_TELEGRAM = (os.getenv("BUY_TELEGRAM") or os.getenv("BUY_TEL") or "").strip().lstrip("@")
BUY_WHATSAPP = (os.getenv("BUY_WHATSAPP") or os.getenv("BUY_WHA") or "393463203783").strip().replace("+", "")

TABLE = os.getenv("SUPABASE_TABLE", "products").strip()

app = Flask(__name__)

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_KEY are required")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_FILE = "https://api.telegram.org/file/bot{token}/{path}"


# -----------------------------
# PARSING RULES (под твои требования)
# 1) price_raw показываем КАК ЕСТЬ: "245€-25%=€183,75"
# 2) бренд берём из ЛЮБОЙ строки (чаще из #hashtag или из слова типа DIOR/Chloé)
# 3) размеры: "L, M, XS(на мне)" / "50,52" / "38FR" / "50-52" / "50 52"
# -----------------------------
SIZE_WORDS_RE = re.compile(r"(?i)\b(XXS|XS|S|M|L|XL|XXL)\b")
SIZE_NUM_RE = re.compile(r"^\s*\d{1,3}(\s*[-,/ ]\s*\d{1,3})+(\s*(FR|IT|EU|US))?\s*$", re.IGNORECASE)
SIZE_SINGLE_NUM_WITH_SYS_RE = re.compile(r"^\s*\d{1,3}\s*(FR|IT|EU|US)\s*$", re.IGNORECASE)

HASHTAG_BRAND_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,30})")

def is_service_line(line: str) -> bool:
    t = line.strip()
    if not t:
        return True
    low = t.lower()
    if low.startswith("по вопросам"):
        return True
    if t.startswith("@"):
        return True
    return False

def looks_like_price_line(line: str) -> bool:
    t = line.strip()
    # главное: есть евро и скидка/итог
    if "€" not in t:
        return False
    # обычно в твоём формате есть "-" и "%" и "="
    if "%" in t and "=" in t:
        return True
    # запасной вариант
    if "=%" in t or "=€" in t:
        return True
    return False

def clean_brand(s: str) -> str:
    s = s.strip()
    s = s.replace("#", "")
    # убираем лишние символы по краям, но сохраняем dior/chloé и т.п.
    s = re.sub(r"^[^\wÀ-ÿ]+|[^\wÀ-ÿ]+$", "", s, flags=re.UNICODE)
    return s

def extract_brand(lines: List[str]) -> Optional[str]:
    # 1) hashtag бренд (#entirestudios)
    for line in lines:
        m = HASHTAG_BRAND_RE.search(line)
        if m:
            b = clean_brand(m.group(1))
            if b:
                return b

    # 2) строка, которая выглядит как бренд (не цена, не сервис)
    # берём первую “сильную” строку
    for line in lines:
        t = line.strip()
        if not t:
            continue
        if is_service_line(t):
            continue
        if looks_like_price_line(t):
            continue
        # если строка очень длинная — не бренд
        if len(t) > 35:
            continue
        # если содержит цифры — скорее не бренд (но "3.1 Phillip Lim" нам не надо)
        if re.search(r"\d", t):
            continue
        # если почти всё — эмодзи/символы
        letters = re.findall(r"[A-Za-zÀ-ÿ]", t)
        if len(letters) < 2:
            continue
        return clean_brand(t)

    return None

def extract_price_raw(lines: List[str]) -> str:
    for line in lines:
        if looks_like_price_line(line):
            return line.strip()
    return ""

def extract_sizes(lines: List[str]) -> str:
    # размеры могут быть отдельной строкой (50,52) или строкой с L/M/XS
    for line in lines:
        t = line.strip()
        if not t or is_service_line(t):
            continue
        if looks_like_price_line(t):
            continue

        if SIZE_WORDS_RE.search(t):
            return t
        if SIZE_NUM_RE.match(t):
            return t
        if SIZE_SINGLE_NUM_WITH_SYS_RE.match(t):
            return t

    return ""

def parse_text_to_product(text: str) -> Dict[str, Any]:
    raw_text = (text or "").strip()
    lines = [ln.rstrip() for ln in raw_text.splitlines()]
    brand = extract_brand(lines) or "ITEM"
    price_raw = extract_price_raw(lines)
    sizes = extract_sizes(lines)

    return {
        "brand": brand,
        "price_raw": price_raw,
        "caption": sizes,        # в каталоге это 3 строка (размеры)
        "raw_text": raw_text,    # для поиска
    }


# -----------------------------
# TELEGRAM HELPERS
# -----------------------------
def tg_get_file_path(file_id: str) -> Optional[str]:
    if not BOT_TOKEN:
        return None
    url = TG_API.format(token=BOT_TOKEN, method="getFile")
    r = requests.get(url, params={"file_id": file_id}, timeout=20)
    if not r.ok:
        return None
    j = r.json()
    if not j.get("ok"):
        return None
    return j["result"].get("file_path")

def tg_download_file(file_path: str) -> Response:
    file_url = TG_FILE.format(token=BOT_TOKEN, path=file_path)
    r = requests.get(file_url, stream=True, timeout=30)
    if not r.ok:
        abort(404)
    content_type = r.headers.get("Content-Type", "image/jpeg")

    def gen():
        for chunk in r.iter_content(chunk_size=1024 * 64):
            if chunk:
                yield chunk

    return Response(gen(), content_type=content_type)


# -----------------------------
# ROUTES
# -----------------------------
@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/buy_target")
def api_buy_target():
    return jsonify({"telegram": BUY_TELEGRAM, "whatsapp": BUY_WHATSAPP})

@app.get("/api/products")
def api_products():
    q = (request.args.get("q") or "").strip()
    brand = (request.args.get("brand") or "").strip().lstrip("#")
    limit = int(request.args.get("limit") or 30)
    offset = int(request.args.get("offset") or 0)

    query = supabase.table(TABLE).select("*").order("ts", desc=True)

    if brand:
        # точнее: по brand
        query = query.ilike("brand", f"%{brand}%")

    if q:
        # поиск по raw_text (описание/эмодзи/всё)
        query = query.ilike("raw_text", f"%{q}%")

    res = query.range(offset, offset + limit - 1).execute()
    data = res.data or []
    return jsonify(data)

@app.get("/img/<file_id>")
def img(file_id: str):
    # прокси фото по file_id, чтобы токен не светился
    file_path = tg_get_file_path(file_id)
    if not file_path:
        abort(404)
    return tg_download_file(file_path)

@app.post("/telegram")
def telegram_webhook():
    # чтобы не было вечных 403:
    # если WEBHOOK_SECRET задан — проверяем,
    # если не задан — принимаем всегда
    if WEBHOOK_SECRET:
        hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        qs = request.args.get("secret", "")
        if hdr != WEBHOOK_SECRET and qs != WEBHOOK_SECRET:
            return ("Forbidden", 403)

    update = request.get_json(silent=True) or {}
    try:
        handle_update(update)
    except Exception as e:
        # важное: телеграму всё равно возвращаем 200, чтобы не блокировал webhook
        print("handle_update error:", str(e))

    return ("OK", 200)


# -----------------------------
# UPDATE HANDLER
# -----------------------------
def handle_update(update: Dict[str, Any]) -> None:
    msg = update.get("message") or update.get("channel_post") or {}
    if not msg:
        return

    text = msg.get("text") or msg.get("caption") or ""
    if not text:
        # если фото без подписи — пропускаем
        return

    prod = parse_text_to_product(text)

    # photo может быть в message/photo или message/caption с фото
    file_id = None
    photos = msg.get("photo") or []
    if photos and isinstance(photos, list):
        # берём самое большое фото
        file_id = photos[-1].get("file_id")

    # ID записи — по message_id + chat_id (чтобы не было коллизий)
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = msg.get("message_id")
    ts = int(msg.get("date") or int(time.time()))
    row_id = f"{ts}_{chat_id}_{message_id}"

    record = {
        "id": row_id,
        "ts": ts,
        "brand": prod["brand"],
        "price_raw": prod["price_raw"],
        "caption": prod["caption"],
        "raw_text": prod["raw_text"],
        "file_id": file_id or "",
    }

    # фильтр: если это просто "#orders" и нет цены/контента — можно оставить, но ты видела “ORDERS” карточку
    # я убираю такие пустые
    if record["brand"].lower() == "orders" and not record["price_raw"] and not record["caption"]:
        return

    supabase.table(TABLE).upsert(record).execute()


# -----------------------------
# LOCAL RUN (не влияет на Render)
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=True)
