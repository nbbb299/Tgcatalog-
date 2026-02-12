import os
import re
import datetime as dt
import requests
from flask import Flask, request, jsonify, render_template, Response
from supabase import create_client

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BUY_TELEGRAM = os.environ.get("BUY_TELEGRAM","").lstrip("@")
BUY_WHATSAPP = os.environ.get("BUY_WHATSAPP","")

app = Flask(__name__)
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")

def parse_brand(caption):
    if not caption:
        return None
    m = HASHTAG_RE.search(caption)
    return m.group(1).lower() if m else None

def extract_price_line(caption):
    if not caption:
        return None
    for line in caption.split("\n"):
        if "€" in line:
            return line.strip()
    return None

def telegram_api(method, params=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, data=params)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(str(data))
    return data["result"]

def upsert_product(message_id, date_iso, caption, file_id):
    payload = {
        "message_id": message_id,
        "date": date_iso,
        "brand": parse_brand(caption),
        "price_raw": extract_price_line(caption),
        "caption": caption,
        "file_id": file_id,
    }
    sb.table("products").upsert(payload, on_conflict="message_id").execute()

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/buy_targets")
def buy_targets():
    return jsonify({
        "telegram": BUY_TELEGRAM,
        "whatsapp": BUY_WHATSAPP
    })

@app.get("/api/products")
def api_products():
    q = (request.args.get("q") or "").strip()
    brand = (request.args.get("brand") or "").strip().lower()

    query = sb.table("products").select("*").order("date", desc=True)

    if brand:
        query = query.eq("brand", brand)
    if q:
        query = query.ilike("caption", f"%{q}%")

    res = query.execute()
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
        got = request.headers.get("X-Webhook-Secret","")
        if got != WEBHOOK_SECRET:
            return "forbidden",403

    update = request.get_json(silent=True) or {}
    post = update.get("channel_post")

    if not post:
        return "ok",200

    photos = post.get("photo") or []
    if not photos:
        return "ok",200

    message_id = post["message_id"]
    caption = post.get("caption","")
    file_id = photos[-1]["file_id"]

    date_iso = dt.datetime.fromtimestamp(post["date"], tz=dt.timezone.utc).isoformat()

    upsert_product(message_id, date_iso, caption, file_id)

    return "ok",200

@app.get("/health")
def health():
    return "ok",200
