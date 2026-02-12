import os
import re
import datetime as dt
import requests
from flask import Flask, request, jsonify, render_template, Response
from supabase import create_client

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

BUY_TELEGRAM = os.environ.get("BUY_TELEGRAM", "mikab16").strip().lstrip("@")
BUY_WHATSAPP = os.environ.get("BUY_WHATSAPP", "393463203783").strip()

app = Flask(__name__)
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")
DISCOUNT_RE = re.compile(r"-\s*(\d{1,2})\s*%")
EURO_PRICE_RE = re.compile(r"(\d[\d\.\,\s]*)\s*€")


def parse_brand(caption: str):
    if not caption:
        return None
    m = HASHTAG_RE.search(caption)
    return m.group(1).lower() if m else None


def money_to_float(s: str):
    if not s:
        return None
    s = s.strip().replace(" ", "")

    if "." in s and "," in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "")
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        if "," in s:
            s = s.replace(".", "")
            s = s.replace(",", ".")
        if "." in s:
            parts = s.split(".")
            if len(parts[-1]) == 3:
                s = s.replace(".", "")

    try:
        return float(s)
    except:
        return None


def parse_price_and_discount(caption: str):
    if not caption:
        return None, None

    m = EURO_PRICE_RE.search(caption)
    if not m:
        return None, None

    base_raw = m.group(1).strip()
    base_val = money_to_float(base_raw)

    d = None
    md = DISCOUNT_RE.search(caption)
    if md:
        try:
            d = int(md.group(1))
        except:
            pass

    final_val = base_val
    if base_val and d:
        final_val = round(base_val * (1 - d / 100.0), 2)

    price_raw = f"{base_raw}€"

    return price_raw, final_val


def telegram_api(method: str, params: dict):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, data=params, timeout=20)
    data = r.json()
    return data["result"]


def upsert_product(message_id, caption, file_id):

    brand = parse_brand(caption or "")
    price_raw, price_value = parse_price_and_discount(caption or "")

    payload = {
        "message_id": message_id,
        "date": dt.datetime.utcnow().isoformat(),
        "brand": brand,
        "price_raw": price_raw,
        "price_value": price_value,
        "caption": caption,
        "file_id": file_id,
    }

    sb.table("products").upsert(payload, on_conflict="message_id").execute()


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/health")
def health():
    return "ok", 200


@app.get("/api/buy_target")
def buy_target():
    return jsonify({
        "telegram": BUY_TELEGRAM,
        "whatsapp": BUY_WHATSAPP
    })


@app.get("/api/products")
def api_products():
    res = sb.table("products").select("*").order("date", desc=True).execute()
    return jsonify(res.data)


@app.get("/img/<file_id>")
def img(file_id):
    info = telegram_api("getFile", {"file_id": file_id})
    file_path = info["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    r = requests.get(file_url)
    return Response(r.content, mimetype="image/jpeg")


@app.post("/telegram")
def telegram_webhook():

    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != WEBHOOK_SECRET:
            return "forbidden", 403

    update = request.json or {}

    post = update.get("channel_post") or update.get("edited_channel_post")
    if not post:
        return "ok"

    photos = post.get("photo") or []
    if not photos:
        return "ok"

    message_id = post.get("message_id")
    caption = post.get("caption") or ""
    file_id = photos[-1]["file_id"]

    upsert_product(message_id, caption, file_id)

    return "ok"
