import os
import re
import datetime as dt
import requests
from flask import Flask, request, jsonify, Response

from supabase import create_client

# ========== ENV ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

BUY_TELEGRAM = os.environ.get("BUY_TELEGRAM", "").strip().lstrip("@")
BUY_WHATSAPP = os.environ.get("BUY_WHATSAPP", "").strip().replace("+", "")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

if not BOT_TOKEN:
    print("WARNING: BOT_TOKEN is empty")
if not SUPABASE_URL or not SUPABASE_KEY:
    print("WARNING: SUPABASE_URL or SUPABASE_KEY is empty")

app = Flask(__name__)
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========== PARSERS ==========
HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")

# Примеры:
# "750€-25%=€562,50"
# "750€ -25% = €562,50"
# "€562,50"
# "562,50€"
PRICE_ANY_EURO_RE = re.compile(r"(€\s*\d+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?\s*€)")
PERCENT_RE = re.compile(r"(-?\s*\d{1,2})\s*%")

def parse_brand(caption: str):
    if not caption:
        return None
    m = HASHTAG_RE.search(caption)
    return m.group(1).lower() if m else None

def _to_float_eu(s: str):
    # "€562,50" -> 562.50
    digits = re.findall(r"\d+(?:[.,]\d{1,2})?", s)
    if not digits:
        return None
    v = digits[0].replace(",", ".")
    try:
        return float(v)
    except:
        return None

def parse_price_full(caption: str):
    """
    Возвращает:
    price_raw (как в посте),
    base_price (число или None),
    discount_percent (число или None),
    final_price (число или None)

    Логика:
    - Если есть "€...=€..." → base = первое, final = второе
    - Если есть только одна цена → final = она
    - Если есть base и % но нет final → final = base*(1 - %/100)
    """
    if not caption:
        return None, None, None, None

    # найдем все "цены в евро" в тексте
    all_prices = PRICE_ANY_EURO_RE.findall(caption)
    all_prices = [p.strip() for p in all_prices]

    percent = None
    pm = PERCENT_RE.search(caption)
    if pm:
        try:
            percent = int(pm.group(1).replace(" ", ""))
        except:
            percent = None

    base_price = None
    final_price = None

    if len(all_prices) >= 2 and "=" in caption:
        base_price = _to_float_eu(all_prices[0])
        final_price = _to_float_eu(all_prices[-1])
    elif len(all_prices) >= 1:
        # берем последнюю найденную как финальную цену
        final_price = _to_float_eu(all_prices[-1])
        # а если до этого еще была цена — считаем её базовой
        if len(all_prices) >= 2:
            base_price = _to_float_eu(all_prices[0])

    # если есть base и % но нет final → вычислим
    if base_price is not None and percent is not None and final_price is None:
        final_price = round(base_price * (1 - (abs(percent) / 100.0)), 2)

    # строка для показа: оставим "как в посте" — ищем похожую на твой формат
    # но если не нашли — просто вернем исходный caption
    raw = None
    # попробуем вытащить кусок с евро и процентом
    m = re.search(r"(\d+(?:[.,]\d{1,2})?\s*€\s*-\s*\d{1,2}\s*%\s*=\s*€\s*\d+(?:[.,]\d{1,2})?)", caption)
    if m:
        raw = m.group(1).strip()
    else:
        # иначе последняя цена
        if all_prices:
            raw = all_prices[-1]
        else:
            raw = None

    return raw, base_price, percent, final_price

# ========== TELEGRAM HELPERS ==========
def telegram_api(method: str, params=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, data=params or {}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(str(data))
    return data["result"]

def file_url(file_id: str):
    info = telegram_api("getFile", {"file_id": file_id})
    file_path = info["file_path"]
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

# ========== DB ==========
def upsert_product(message_id: int, date_iso: str, caption: str, file_id: str):
    brand = parse_brand(caption or "")
    price_raw, base_price, discount_percent, final_price = parse_price_full(caption or "")

    payload = {
        "message_id": message_id,
        "date": date_iso,
        "brand": brand,
        "price_raw": price_raw,
        "price_value": final_price,
        "caption": caption,
        "file_id": file_id,
        # доп. поля (если у тебя таблица без них — ничего страшного, supabase может ругнуться)
        "base_price": base_price,
        "discount_percent": discount_percent,
    }

    # Если в таблице нет base_price/discount_percent — удалим эти ключи и попробуем еще раз
    try:
        sb.table("products").upsert(payload, on_conflict="message_id").execute()
    except Exception:
        payload.pop("base_price", None)
        payload.pop("discount_percent", None)
        sb.table("products").upsert(payload, on_conflict="message_id").execute()

# ========== UI (встроенный HTML) ==========
INDEX_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Каталог</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#fff;color:#111}
    header{position:sticky;top:0;background:#fff;border-bottom:1px solid #eee;padding:10px;z-index:10}
    .row{display:flex;gap:8px;flex-wrap:wrap}
    input,button{padding:10px;border:1px solid #ddd;border-radius:10px;font-size:15px}
    button{cursor:pointer}
    main{padding:12px}
    .grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
    @media(min-width:900px){.grid{grid-template-columns:repeat(4,minmax(0,1fr));}}
    .card{border:1px solid #eee;border-radius:14px;overflow:hidden;background:#fff}
    .img{width:100%;height:260px;object-fit:cover;background:#f7f7f7}
    .meta{padding:10px}
    .brand{font-weight:800;text-transform:uppercase;font-size:13px;opacity:.8}
    .price{font-weight:900;font-size:16px;margin-top:4px}
    .cap{font-size:13px;opacity:.85;margin-top:6px;white-space:pre-wrap}
    .actions{display:flex;gap:8px;margin-top:10px}
    .btn{flex:1;display:inline-flex;align-items:center;justify-content:center;
         padding:10px;border-radius:12px;border:1px solid #111;text-decoration:none;color:#111;font-weight:700}
    .btn.primary{background:#111;color:#fff}
    .more{margin:14px 0;display:flex;justify-content:center}
  </style>
</head>
<body>
  <header>
    <div class="row">
      <input id="q" placeholder="Поиск (текст/описание)"/>
      <input id="brand" placeholder="Бренд (например balmain)"/>
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
let buy = {telegram:"", whatsapp:""};

async function getBuyTarget(){
  const r = await fetch("/api/buy_target");
  buy = await r.json();
}

function esc(s){
  return (s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}

function buildTelegramLink(p){
  const brand = (p.brand || "").toUpperCase();
  const price = p.price_raw || (p.price_value ? ("€" + p.price_value) : "");
  const text = `Здравствуйте! Хочу купить: ${brand}\nЦена: ${price}\nID: ${p.message_id}`;
  const encoded = encodeURIComponent(text);
  if (buy.telegram) return `https://t.me/${buy.telegram}?text=${encoded}`;
  return `https://t.me/share/url?url=${encodeURIComponent(location.href)}&text=${encoded}`;
}

function buildWhatsAppLink(p){
  if (!buy.whatsapp) return null;
  const brand = (p.brand || "").toUpperCase();
  const price = p.price_raw || (p.price_value ? ("€" + p.price_value) : "");
  const text = `Здравствуйте! Хочу купить: ${brand}\nЦена: ${price}\nID: ${p.message_id}\n${location.href}`;
  const encoded = encodeURIComponent(text);
  return `https://wa.me/${buy.whatsapp}?text=${encoded}`;
}

function card(p){
  const b = (p.brand || "").toUpperCase();
  const price = p.price_raw || (p.price_value ? ("€" + p.price_value) : "");
  const cap = p.caption || "";
  const tg = buildTelegramLink(p);
  const wa = buildWhatsAppLink(p);
  return `
    <div class="card">
      <img class="img" loading="lazy" src="/img/${p.file_id}" alt=""/>
      <div class="meta">
        <div class="brand">${esc(b)}</div>
        <div class="price">${esc(price || "")}</div>
        <div class="cap">${esc(cap)}</div>
        <div class="actions">
          <a class="btn primary" href="${tg}" target="_blank" rel="noopener">Купить в Telegram</a>
          ${wa ? `<a class="btn" href="${wa}" target="_blank" rel="noopener">WhatsApp</a>` : ``}
        </div>
      </div>
    </div>
  `;
}

async function fetchProducts(){
  const q = document.getElementById("q").value.trim();
  const brand = document.getElementById("brand").value.trim();
  const url = new URL("/api/products", location.origin);
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("offset", String(offset));
  if (q) url.searchParams.set("q", q);
  if (brand) url.searchParams.set("brand", brand);
  const r = await fetch(url);
  return await r.json();
}

async function loadMore(){
  const data = await fetchProducts();
  const grid = document.getElementById("grid");
  data.forEach(p => grid.insertAdjacentHTML("beforeend", card(p)));
  offset += data.length;
  document.getElementById("moreBtn").style.display = (data.length < limit) ? "none" : "inline-block";
}

async function resetAndLoad(){
  offset = 0;
  document.getElementById("grid").innerHTML = "";
  document.getElementById("moreBtn").style.display = "inline-block";
  await loadMore();
}

(async () => {
  await getBuyTarget();
  await resetAndLoad();
})();
</script>
</body>
</html>
"""

# ========== ROUTES ==========
@app.get("/")
def home():
    return Response(INDEX_HTML, mimetype="text/html")

@app.get("/health")
def health():
    return "ok", 200

@app.get("/api/buy_target")
def buy_target():
    return jsonify({"telegram": BUY_TELEGRAM, "whatsapp": BUY_WHATSAPP})

@app.get("/api/products")
def api_products():
    q = (request.args.get("q") or "").strip()
    brand = (request.args.get("brand") or "").strip().lower()
    limit = int(request.args.get("limit") or 60)
    offset = int(request.args.get("offset") or 0)

    query = sb.table("products").select("*").order("date", desc=True).range(offset, offset + limit - 1)

    if brand:
        query = query.eq("brand", brand)
    if q:
        query = query.ilike("caption", f"%{q}%")

    res = query.execute()
    return jsonify(res.data or [])

@app.get("/img/<file_id>")
def img(file_id):
    # прокси фото (чтобы не светить прямую ссылку на Telegram file)
    try:
        url = file_url(file_id)
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))
    except Exception as e:
        return Response(f"img error: {e}", status=404)

@app.post("/telegram")
def telegram_webhook():
    # Telegram secret header:
    # X-Telegram-Bot-Api-Secret-Token должен совпадать с secret_token при setWebhook
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != WEBHOOK_SECRET:
            return "forbidden", 403

    update = request.get_json(silent=True) or {}

    post = update.get("channel_post") or update.get("edited_channel_post")
    if not post:
        return "ok", 200

    photos = post.get("photo") or []
    if not photos:
        return "ok", 200

    message_id = int(post.get("message_id"))
    date_unix = int(post.get("date"))
    caption = post.get("caption") or ""

    # берем самое большое фото (последнее)
    file_id = photos[-1]["file_id"]
    date_iso = dt.datetime.fromtimestamp(date_unix, tz=dt.timezone.utc).isoformat()

    try:
        upsert_product(message_id, date_iso, caption, file_id)
    except Exception as e:
        # важно не падать, иначе Telegram будет спамить
        print("upsert error:", e)

    return "ok", 200
