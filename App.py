import os
import re
import datetime as dt
import requests
from flask import Flask, request, jsonify, render_template, Response, abort
from supabase import create_client

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
BUY_TELEGRAM = os.environ.get("BUY_TELEGRAM", "").lstrip("@")  # твой username без @

app = Flask(__name__)
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")
PRICE_RE = re.compile(r"(€\s*\d+(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?\s*€)|(EUR\s*\d+(?:[.,]\d{1,2})?)", re.IGNORECASE)

def parse_brand(caption: str) -> str | None:
    if not caption:
        return None
    m = HASHTAG_RE.search(caption)
    return m.group(1).lower() if m else None

def parse_price(caption: str):
    # берём последнюю найденную цену (у тебя итоговая обычно последняя: ...=€562,50)
    if not caption:
        return None, None
    matches = list(PRICE_RE.finditer(caption))
    if not matches:
        return None, None
    raw = matches[-1].group(0).strip()
    num = re.findall(r"\d+(?:[.,]\d{1,2})?", raw)
    if not num:
        return raw, None
    val = num[0].replace(",", ".")
    try:
        return raw, float(val)
    except:
        return raw, None

def telegram_api(method: str, params=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, data=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(str(data))
    return data["result"]

def upsert_product(message_id: int, date_iso: str, caption: str, file_id: str):
    brand = parse_brand(caption or "")
    price_raw, price_value = parse_price(caption or "")
    payload = {
        "message_id": message_id,
        "date": date_iso,
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

@app.get("/api/products")
def api_products():
    q = (request.args.get("q") or "").strip()
    brand = (request.args.get("brand") or "").strip().lower()
    limit = int(request.args.get("limit") or "60")
    offset = int(request.args.get("offset") or "0")

    query = sb.table("products").select("*").order("date", desc=True).range(offset, offset + limit - 1)

    if brand:
        query = query.eq("brand", brand)
    if q:
        query = query.ilike("caption", f"%{q}%")

    res = query.execute()
    return jsonify(res.data)

@app.get("/img/<file_id>")
def img(file_id):
    # прокси фото, чтобы не светить токен в браузере
    info = telegram_api("getFile", params={"file_id": file_id})
    file_path = info["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    r = requests.get(file_url, timeout=30)
    r.raise_for_status()
    return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))

@app.post("/telegram")
def telegram_webhook():
    # простая защита от левых запросов
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Webhook-Secret", "")
        if got != WEBHOOK_SECRET:
            return "forbidden", 403

    update = request.get_json(silent=True) or {}
    post = update.get("channel_post") or update.get("edited_channel_post")
    if not post:
        return "ok", 200

    photos = post.get("photo") or []
    if not photos:
        return "ok", 200

    message_id = post.get("message_id")
    date_unix = post.get("date")
    caption = post.get("caption") or ""
    file_id = photos[-1]["file_id"]  # самое большое фото

    date_iso = dt.datetime.fromtimestamp(date_unix, tz=dt.timezone.utc).isoformat()

    try:
        upsert_product(message_id, date_iso, caption, file_id)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200

    return "ok", 200

@app.get("/api/buy_target")
def buy_target():
    # фронту нужен твой username
    return jsonify({"username": BUY_TELEGRAM})

@app.get("/health")
def health():
    return "ok", 200
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Каталог</title>
  <style>
    body { font-family: system-ui, -apple-system, Arial; margin: 0; background:#fff; }
    header { position: sticky; top: 0; background: #fff; border-bottom: 1px solid #eee; padding: 12px; z-index: 10; }
    .row { display:flex; gap:8px; flex-wrap: wrap; align-items: center; }
    input, button { padding: 10px; border: 1px solid #ddd; border-radius: 10px; }
    button { cursor: pointer; }
    main { padding: 12px; }
    .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
    .card { border: 1px solid #eee; border-radius: 14px; overflow: hidden; background: #fff; }
    .img { width: 100%; height: 280px; object-fit: cover; background: #f5f5f5; }
    .meta { padding: 10px; }
    .brand { font-weight: 800; text-transform: uppercase; font-size: 12px; opacity: .7; }
    .price { font-weight: 900; margin: 6px 0; font-size: 16px; }
    .cap { font-size: 13px; white-space: pre-wrap; opacity: .9; }
    .actions { display:flex; gap:8px; margin-top:10px; }
    .buy { flex:1; background:#111; color:#fff; border-color:#111; }
    .more { margin: 12px 0; display:flex; justify-content:center; }
  </style>
</head>
<body>
  <header>
    <div class="row">
      <input id="q" placeholder="Поиск (например: 38, boots, leather)"/>
      <input id="brand" placeholder="Бренд (например: balmain)"/>
      <button onclick="resetAndLoad()">Найти</button>
    </div>
  </header>

  <main>
    <div class="grid" id="grid"></div>
    <div class="more">
      <button id="moreBtn" onclick="loadMore()">Показать ещё</button>
    </div>
  </main>

<script>
  let offset = 0;
  const limit = 60;
  let tgUser = "";

  async function getBuyTarget(){
    const r = await fetch("/api/buy_target");
    const j = await r.json();
    tgUser = (j.username || "").trim();
  }

  function escapeHtml(str){
    return str.replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
  }

  function buildBuyLink(p){
    const brand = (p.brand || "").toUpperCase();
    const price = p.price_raw || (p.price_value ? ("€" + p.price_value) : "");
    const text = `Здравствуйте! Хочу купить товар:\nID: ${p.id}\nБренд: ${brand}\nЦена: ${price}\n\nТекст:\n${p.caption || ""}`;
    const encoded = encodeURIComponent(text);
    // tg:// работает если Telegram установлен; https://t.me/ работает всегда
    if (tgUser) return `https://t.me/${tgUser}?text=${encoded}`;
    return `https://t.me/share/url?url=&text=${encoded}`;
  }

  function card(p){
    const b = (p.brand || '').toUpperCase();
    const price = p.price_raw || (p.price_value ? ('€' + p.price_value) : '');
    const cap = p.caption || '';
    const buy = buildBuyLink(p);
    return `
      <div class="card">
        <img class="img" loading="lazy" src="/img/${p.file_id}" alt="">
        <div class="meta">
          <div class="brand">${b}</div>
          <div class="price">${escapeHtml(price)}</div>
          <div class="cap">${escapeHtml(cap)}</div>
          <div class="actions">
            <a href="${buy}" target="_blank" style="flex:1; text-decoration:none;">
              <button class="buy" style="width:100%;">Купить в Telegram</button>
            </a>
          </div>
        </div>
      </div>
    `;
  }

  async function fetchProducts(){
    const q = document.getElementById('q').value.trim();
    const brand = document.getElementById('brand').value.trim();
    const url = new URL(location.origin + "/api/products");
    url.searchParams.set("limit", limit);
    url.searchParams.set("offset", offset);
    if (q) url.searchParams.set("q", q);
    if (brand) url.searchParams.set("brand", brand.toLowerCase());
    const r = await fetch(url);
    return await r.json();
  }

  async function loadMore(){
    const data = await fetchProducts();
    const grid = document.getElementById('grid');
    data.forEach(p => grid.insertAdjacentHTML("beforeend", card(p)));
    offset += data.length;
    document.getElementById('moreBtn').style.display = data.length < limit ? "none" : "inline-block";
  }

  async function resetAndLoad(){
    offset = 0;
    document.getElementById('grid').innerHTML = "";
    document.getElementById('moreBtn').style.display = "inline-block";
    await loadMore();
  }

  (async () => {
    await getBuyTarget();
    await resetAndLoad();
  })();
</script>
</body>
</html>
