import os
import asyncio
import traceback
import re
import time
import requests
from pathlib import Path
from typing import List, Optional, Tuple, Dict

from fastapi import FastAPI, Request
from fastapi.responses import Response, FileResponse, JSONResponse

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

from supabase import create_client

# ================= ENV =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TABLE = os.getenv("SUPABASE_TABLE", "products")

# ✅ ADMIN DELETE TOKEN
ADMIN_DELETE_TOKEN = os.getenv("ADMIN_DELETE_TOKEN", "")

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing env: BOT_TOKEN / SUPABASE_URL / SUPABASE_KEY")

# ================= INIT =================

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

# ================= SOURCE MAP =================
# 1) Boutiques:        -1001158220106
# 2) Outlets:          -1002303984060
# 3) Digital-catalog:  -1003494522851   -> ВСЁ из него в Boutiques
# 4) Очки (NEW):       -1003791619052   -> в Очки

CHAT_SOURCE = {
    -1001158220106: "Boutiques",
    -1002303984060: "Outlets",
    -1003494522851: "Boutiques",  # Digital-catalog -> Boutiques
    -1003791619052: "Очки",       # New glasses channel -> Очки
    -1001659927695: "Мужское",
    -1001486422757: "Украшения",
}

ALLOWED_CHATS = set(CHAT_SOURCE.keys())

def detect_source(chat_id: int) -> str:
    return CHAT_SOURCE.get(int(chat_id), "")

# ================= TOPIC CACHE =================

TopicKey = Tuple[int, int]  # (chat_id, thread_id)
topic_title_cache: Dict[TopicKey, str] = {}
boutique_sales_cache = {
    "ts": 0.0,
    "data": [],
}
outlet_brands_cache = {
    "ts": 0.0,
    "data": [],
}

# ================= STARTUP / SHUTDOWN =================

@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()
    print("✅ telegram_app started")
    print("✅ Allowed chats:", sorted(list(ALLOWED_CHATS)))
    print("✅ Chat->Source map:", CHAT_SOURCE)

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await telegram_app.stop()
    finally:
        await telegram_app.shutdown()
    print("🛑 telegram_app stopped")

# ================= HELPERS =================

def extract_brand_from_caption(caption: str) -> str:
    if "#" in caption:
        try:
            return caption.split("#", 1)[1].split()[0].strip()
        except Exception:
            return ""
    return ""

def msg_ts(msg) -> int:
    try:
        return int(msg.date.timestamp())
    except Exception:
        return 0

def safe_append_file_id(existing: Optional[List[str]], file_id: str) -> List[str]:
    arr = existing if isinstance(existing, list) else []
    if file_id and file_id not in arr:
        arr.append(file_id)
    return arr

def is_empty_caption(val) -> bool:
    return val is None or str(val).strip() == ""

def get_topic_title_from_service(msg) -> Optional[str]:
    try:
        if getattr(msg, "forum_topic_created", None):
            return msg.forum_topic_created.name
        if getattr(msg, "forum_topic_edited", None):
            return msg.forum_topic_edited.name
    except Exception:
        pass
    return None

def remember_topic_title(msg):
    try:
        chat_id = int(msg.chat.id)
        thread_id = getattr(msg, "message_thread_id", None)
        title = get_topic_title_from_service(msg)

        if thread_id and title:
            topic_title_cache[(chat_id, int(thread_id))] = str(title).strip()
            print(f"🧠 Topic remembered: chat={chat_id} thread={thread_id} title='{title}'")
    except Exception as e:
        print("remember_topic_title error:", repr(e))

def brand_from_topic_if_known(msg) -> str:
    try:
        chat_id = int(msg.chat.id)
        thread_id = getattr(msg, "message_thread_id", None)
        if not thread_id:
            return ""

        title = topic_title_cache.get((chat_id, int(thread_id)))
        if not title:
            rt = getattr(msg, "reply_to_message", None)
            if rt:
                t2 = get_topic_title_from_service(rt)
                if t2:
                    topic_title_cache[(chat_id, int(thread_id))] = str(t2).strip()
                    title = str(t2).strip()

        return (title or "").strip()
    except Exception:
        return ""

def pick_brand(msg, fallback_text: str) -> str:
    b = brand_from_topic_if_known(msg)
    if b:
        return b
    return extract_brand_from_caption(fallback_text or "")

def extract_best_file_id(msg) -> str:
    """
    Поддержка:
    - photo (обычное фото)
    - document (если прислали как файл, но это картинка)
    """
    if getattr(msg, "photo", None):
        try:
            return msg.photo[-1].file_id
        except Exception:
            pass

    doc = getattr(msg, "document", None)
    if doc:
        mt = (getattr(doc, "mime_type", "") or "").lower()
        if mt.startswith("image/"):
            return doc.file_id

    return ""

def is_media_message(msg) -> bool:
    if getattr(msg, "photo", None):
        return True
    doc = getattr(msg, "document", None)
    if doc:
        mt = (getattr(doc, "mime_type", "") or "").lower()
        return mt.startswith("image/")
    return False

# ================= SAVE PRODUCT =================

def save_product(msg):
    caption = (msg.caption or "").strip()
    chat_id = int(msg.chat.id)
    source = detect_source(chat_id)
    brand = pick_brand(msg, caption)

    media_group_id = msg.media_group_id  # album id or None
    message_id = msg.message_id

    file_id = ""
    if msg.photo:
        file_id = msg.photo[-1].file_id  # best size

    ts = msg_ts(msg)

    # -------- ALBUM --------
    if media_group_id:
        existing = (
            supabase.table(TABLE)
            .select("id,file_ids,file_id,caption,brand,ts,media_group_id,message_id,source,chat_id")
            .eq("media_group_id", str(media_group_id))
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )

        if existing.data:
            row = existing.data[0]
            new_file_ids = safe_append_file_id(row.get("file_ids"), file_id)

            old_caption = (row.get("caption") or "").strip()
            new_caption = old_caption or caption
            if caption:
                new_caption = caption

            old_brand = (row.get("brand") or "").strip()
            new_brand = old_brand or brand
            if brand and not old_brand:
                new_brand = brand

            new_ts = row.get("ts") or ts
            new_file_id = row.get("file_id") or file_id

            supabase.table(TABLE).update({
                "file_ids": new_file_ids,
                "file_id": new_file_id,
                "caption": new_caption,
                "brand": new_brand,
                "ts": new_ts,
                "source": source,
                "chat_id": chat_id,
            }).eq("id", row["id"]).execute()
            return

        supabase.table(TABLE).insert({
            "chat_id": chat_id,
            "media_group_id": str(media_group_id),
            "message_id": message_id,
            "brand": brand,
            "caption": caption,
            "file_id": file_id or None,
            "file_ids": [file_id] if file_id else [],
            "ts": ts,
            "source": source,
        }).execute()
        return

    # -------- SINGLE POST --------
    ex = (
        supabase.table(TABLE)
        .select("id,caption,brand,file_id,file_ids,ts")
        .eq("chat_id", chat_id)
        .eq("message_id", message_id)
        .limit(1)
        .execute()
    )

    if ex.data:
        row = ex.data[0]

        upd = {
            "source": source,
            "chat_id": chat_id,
        }

        if caption:
            upd["caption"] = caption

        old_brand = (row.get("brand") or "").strip()
        if (not old_brand) and brand:
            upd["brand"] = brand

        old_file_ids = row.get("file_ids") if isinstance(row.get("file_ids"), list) else []
        if file_id:
            upd["file_id"] = row.get("file_id") or file_id
            upd["file_ids"] = safe_append_file_id(old_file_ids, file_id)

        if not row.get("ts") and ts:
            upd["ts"] = ts

        supabase.table(TABLE).update(upd).eq("id", row["id"]).execute()
        return

    supabase.table(TABLE).insert({
        "chat_id": chat_id,
        "message_id": message_id,
        "brand": brand,
        "caption": caption,
        "file_id": file_id or None,
        "file_ids": [file_id] if file_id else [],
        "ts": ts,
        "source": source,
    }).execute()

# ================= CAPTION FIX (120 sec) =================

def attach_text_caption_to_recent_item(msg, window_seconds: int = 120) -> bool:
    text = (msg.text or "").strip()
    if not text:
        return False

    chat_id = int(msg.chat.id)

    if chat_id not in ALLOWED_CHATS:
        return False

    source = detect_source(chat_id)
    if not source:
        return False

    ts = msg_ts(msg)
    if not ts:
        return False

    brand = pick_brand(msg, text)

    try:
        res = (
            supabase.table(TABLE)
            .select("id,caption,brand,ts,source,chat_id")
            .eq("chat_id", chat_id)
            .eq("source", source)
            .gte("ts", ts - window_seconds)
            .order("ts", desc=True)
            .limit(20)
            .execute()
        )

        rows = res.data or []
        if not rows:
            return False

        target = None
        for r in rows:
            if is_empty_caption(r.get("caption")):
                target = r
                break

        if not target:
            return False

        upd = {"caption": text}
        if (not (target.get("brand") or "").strip()) and brand:
            upd["brand"] = brand

        supabase.table(TABLE).update(upd).eq("id", target["id"]).execute()
        print(f"🧩 Caption FIX applied -> row id={target['id']} chat_id={chat_id} source={source}")
        return True

    except Exception as e:
        print("Caption FIX ERROR ❌", repr(e))
        traceback.print_exc()
        return False

# ================= TELEGRAM HANDLER =================

async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    try:
        print("IN MSG:", {
            "chat_id": int(msg.chat.id) if msg.chat else None,
            "message_id": getattr(msg, "message_id", None),
            "has_photo": bool(getattr(msg, "photo", None)),
            "has_doc": bool(getattr(msg, "document", None)),
            "media_group_id": getattr(msg, "media_group_id", None),
            "text": (msg.text[:40] + "...") if getattr(msg, "text", None) and len(msg.text) > 40 else getattr(msg, "text", None),
        })
    except Exception:
        pass

    if getattr(msg, "forum_topic_created", None) or getattr(msg, "forum_topic_edited", None):
        remember_topic_title(msg)
        return

    if is_media_message(msg):
        try:
            save_product(msg)
        except Exception as e:
            print("SAVE ERROR ❌", repr(e))
            traceback.print_exc()
        return

    if msg.text:
        try:
            attach_text_caption_to_recent_item(msg, window_seconds=120)
        except Exception as e:
            print("CAPTION FIX ERROR ❌", repr(e))
            traceback.print_exc()
        return

telegram_app.add_handler(MessageHandler(filters.ALL, handler))

# ================= WEBHOOK =================

@app.post("/webhook")
async def webhook(req: Request):
    try:
        data = await req.json()
        upd_id = data.get("update_id", "no_update_id")
        print("WEBHOOK HIT ✅ update_id:", upd_id)

        update = Update.de_json(data, telegram_app.bot)
        asyncio.create_task(telegram_app.process_update(update))
        return {"ok": True}

    except Exception as e:
        print("WEBHOOK ERROR ❌", repr(e))
        traceback.print_exc()
        return {"ok": True}

# ================= HEALTH =================

@app.get("/health")
def health():
    return {"ok": True}

# ================= PRODUCTS API (SERVER PAGINATION) =================

@app.get("/api/products")
def get_products(
    offset: int = 0,
    limit: int = 24,
    source: str = "",
    brand: str = "",
    q: str = "",
):
    try:
        offset = max(0, int(offset))
        limit = max(1, min(200, int(limit)))

        query = supabase.table(TABLE).select("*", count="exact").order("ts", desc=True)

        s = (source or "").strip()
        if s:
            query = query.eq("source", s)

        b = (brand or "").strip()

        if b:
            safe = b.replace("%", "").replace(",", " ")
            compact = safe.replace(" ", "")

            alias_map = {
                "acnestudios": ["acnestudios", "acnestudio", "acne"],
                "alaïa": ["alaia", "alaia_nug"],
                "alevi": ["alevi", "alevi’_nug", "alevi'_nug"],

                "alexandermcquen": [
                    "alexandermcquen",
                    "alexander",
                    "amcqueen",
                    "mcqueen",
                    "alexander mcqueen",
                ],

                "aquazzura": [
                    "aquazzura",
                    "aquazzura_er",
                    "aquazzura_jul",
                    "aquazzura_nug",
                ],

                "balenciaga": ["balenciaga", "balenciaga_ffm"],
                "balmain": ["balmain", "balmain_jul"],

                "bottega veneta": [
                    "bottega veneta",
                    "bottegaveneta",
                    "bottega_ffm",
                ],

                "brunello cucinelli": [
                    "brunello cucinelli",
                    "brunellocucinelli",
                    "brunellocucinelli_fk",
                    "brunellocucinelli_jul",
                    "brunellocucinelli_tb",
                ],

                "burberry": [
                    "burberry",
                    "burberry_ffm",
                    "burberry_jul",
                ],

                "celine": [
                    "celine",
                    "celine_nug",
                    "celine_tb",
                    "celine_tg",
                ],

                "chloe": ["chloe", "chloé", "chloè"],

                "dior": [
                    "dior",
                    "christiandior",
                    "christian dior",
                    "dior_bb",
                    "dior_nug",
                    "dior_tg",
                ],

                "dolce & gabbana": [
                    "dolce&gabbana",
                    "dolce & gabbana",
                    "dolcegabbana",
                    "dg",
                    "dolce_gabbana_er",
                    "dolce_gabbana_jul",
                ],

                "etro": ["etro", "etro_jul"],

                "fendi": [
                    "fendi",
                    "fendi_er",
                    "fendi_ffm",
                    "fendi_jul",
                ],

                "ferragamo": ["ferragamo", "feragamo"],
                "givenchy": ["givenchy", "givenchy_ffm"],

                "golden goose": [
                    "golden goose",
                    "goldengoose",
                    "goldengoose_jul",
                ],

                "gucci": [
                    "gucci",
                    "gucci_er",
                    "gucci_ffm",
                    "gucci_jul",
                    "gucci_nug",
                ],

                "jacquemus": ["jacquemus", "jacquemus_tb"],

                "jilsander": [
                    "jilsander",
                    "jil sander",
                    "jilsander_jul",
                ],

                "jimmychoo": [
                    "jimmychoo",
                    "jimmy choo",
                    "jimmy",
                ],

                "lesilla": [
                    "lesilla",
                    "le silla",
                    "lesilla_jul",
                    "lesilla_nug",
                ],

                "loewe": [
                    "loewe",
                    "loewe_nug",
                    "loewe_tb",
                    "loewe_tg",
                ],

                "magdabutrym": [
                    "magdabutrym",
                    "magda butrym",
                    "magda",
                    "magdabuttym",
                ],

                "maisonmargiela": [
                    "maisonmargiela",
                    "maison margiela",
                    "maisionmargiela",
                    "mm6",
                ],

                "max mara": [
                    "max mara",
                    "maxmara",
                    "maxmara_jul",
                    "maxmara_tb",
                    "maxmarastudio_er",
                    "maxmarathecube_er",
                    "maxmarathecube_tb",
                ],

                "miu miu": [
                    "miumiu",
                    "miu miu",
                    "miu",
                    "miumiu_er",
                    "miumiu_nug",
                ],

                "moncler": ["moncler", "moncler_jul"],

                "paristexas": [
                    "paristexas",
                    "paris texas",
                    "paris",
                    "paristexas_nug",
                    "paristexas_tb",
                ],

                "prada": [
                    "prada",
                    "prada_er",
                    "prada_jul",
                    "prada_ffm",
                ],

               "rené caovilla": [
                   "renècaovilla",
                   "renécaovilla",
                   "renecaovilla",
                   "rene caovilla",
                   "rené caovilla",
                ],
                
                "rogervivier": [
                    "rogervivier",
                    "roger vivier",
                    "roger",
                    "roger_ffm",
                ],

                "rotate": ["rotate", "rotate_jul"],

                "saint laurent": [
                    "saint laurent",
                    "saintlaurent",
                    "ysl",
                    "saint",
                    "saintlaurent_er",
                    "saintlaurent_nug",
                ],

                "theattico": [
                    "theattico",
                    "the attico",
                    "theattico_nug",
                ],

                "the row": [
                    "the row",
                    "therow",
                    "therow_tb",
                ],

                "valentino": [
                    "valentino",
                    "valentino_ffm",
                    "valentino_jul",
                    "valentinogaravani",
                    "valentinogaravani_er",
                    "valentinogaravani_jul",
                    "valentinogaravani_nug",
                ],

                "zimmermann": [
                    "zimmermann",
                    "zimmerman",
                    "zimmermann_jul",
                ],

                "van cleef & arpels": [
                    "van cleef & arpels",
                    "van cleef and arpels",
                    "vancleef&arpels",
                    "vancleefarpels",
                    "van cleef",
                    "vancleef",
                    "vca",
                ],
            }

            normalized_key = safe.lower()

            aliases = alias_map.get(
                normalized_key,
                [safe, compact]
            )

            aliases = list(dict.fromkeys(aliases))

            conditions = []

            for alias in aliases:
                conditions.append(
                    f"brand.ilike.%{alias}%"
                )
                conditions.append(
                    f"caption.ilike.%{alias}%"
                )

            query = query.or_(
                ",".join(conditions)
            )

        qq = (q or "").strip()
        if qq:
            safeq = qq.replace("%", "").replace(",", " ")
            query = query.or_(f"brand.ilike.%{safeq}%,caption.ilike.%{safeq}%")

        query = query.range(offset, offset + limit - 1)
        res = query.execute()

        items = res.data or []
        total = getattr(res, "count", None)

        if total is None:
            has_more = len(items) == limit
        else:
            has_more = (offset + len(items)) < int(total)

        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": has_more,
        }

    except Exception as e:
        print("PRODUCTS API ERROR ❌", repr(e))
        traceback.print_exc()
        return JSONResponse({"detail": "products_fetch_failed"}, status_code=500)

# ================= BRANDS API =================

@app.get("/api/brands")
def get_brands():
    try:
        res = (
            supabase.table(TABLE)
            .select("brand,caption")
            .execute()
        )

        raw = res.data or []
        print("BRANDS RAW COUNT:", len(raw))

        normalize_map = {
            "dior": "Dior",
            "prada": "Prada",
            "gucci": "Gucci",
            "miumiu": "MiuMiu",
            "miu miu": "MiuMiu",
            "ysl": "YSL",
            "lv": "LV",
            "loewe": "Loewe",
            "celine": "Celine",
            "fendi": "Fendi",
            "burberry": "Burberry",
            "valentino": "Valentino",
            "balenciaga": "Balenciaga",
            "bottega veneta": "Bottega Veneta",
            "brunellocucinelli": "BrunelloCucinelli",
            "brunello cucinelli": "BrunelloCucinelli",
            "christiandior": "ChristianDior",
            "christian dior": "ChristianDior",
            "dolce&gabbana": "Dolce&Gabbana",
            "dolce & gabbana": "Dolce&Gabbana",
            "givenchy": "Givenchy",
            "tom ford": "Tom Ford",
            "max mara": "Max Mara",
            "marc jacobs": "Marc Jacobs",
            "golden goose": "Golden Goose",
            "goldengoose": "Golden Goose",
            "off-white": "OFF-WHITE",
            "off white": "OFF-WHITE",
            "saintlaurent": "SaintLaurent",
            "saint laurent": "SaintLaurent",
            "zimmermann": "Zimmermann",
            "schiaparelli": "Schiaparelli",
            "loro piana": "Loro Piana",
            "alexander wang": "Alexander Wang",
            "alexsander wang": "Alexander Wang",
            "tiffany&co": "Tiffany&Co",
            "tiffany & co": "Tiffany&Co",
        }

        skip_exact = {
            "reviews", "review", "new", "outlet", "sale", "brand",
            "по вопросам", "размер", "size", "price"
        }

        def clean_brand_name(s: str) -> str:
            s = (s or "").strip()
            if not s:
                return ""

            bad_parts = ["ð", "ÿ", "œ", "™", "�", "\uFFFD", "Ð", "Ñ"]
            low0 = s.lower()
            for bp in bad_parts:
                if bp.lower() in low0:
                    return ""

            s = s.strip("👜👠👓🕶️✨💼🤍🖤🤎💛💙💚💜❤️🩷🩶🧸🌍•-–—,.;:()[]{}|/\\\"' ")
            if not s:
                return ""

            low = s.lower()

            if low in skip_exact:
                return ""

            if "по вопросам" in low:
                return ""
            if "price" in low:
                return ""
            if "размер" in low:
                return ""
            if "size" in low:
                return ""
            if s.startswith("@"):
                return ""
            if "€" in s or "$" in s:
                return ""

            for suffix in [" outlet", " new", " boutique", " boutiques"]:
                if low.endswith(suffix):
                    s = s[:-len(suffix)].strip()
                    low = s.lower()

            if not s:
                return ""

            if low in normalize_map:
                return normalize_map[low]

            return " ".join(word.capitalize() for word in s.split())

        def extract_brand_from_caption_for_nav(caption: str) -> str:
            caption = (caption or "").strip()
            if not caption:
                return ""

            if "#" in caption:
                try:
                    tag = caption.split("#", 1)[1].split()[0].strip()
                    cleaned = clean_brand_name(tag)
                    if cleaned:
                        return cleaned
                except Exception:
                    pass

            for line in caption.splitlines():
                line = line.strip()
                if not line:
                    continue
                cleaned = clean_brand_name(line)
                if cleaned:
                    return cleaned

            return ""

        found = {}

        for row in raw:
            brand = clean_brand_name(str(row.get("brand", "") or "").strip())

            if not brand:
                brand = extract_brand_from_caption_for_nav(
                    str(row.get("caption", "") or "").strip()
                )

            if brand:
                found[brand.lower()] = brand

        brands = sorted(found.values(), key=lambda s: s.lower())
        print("BRANDS FINAL COUNT:", len(brands))

        return {"brands": brands}

    except Exception as e:
        print("BRANDS API ERROR ❌", repr(e))
        traceback.print_exc()
        return JSONResponse({"brands": []}, status_code=200)
     
# ================= OUTLETS BRAND CARDS API =================

@app.get("/api/outlet-brands")
def get_outlet_brands():
    try:
        now = time.time()
        cached_at = float(outlet_brands_cache.get("ts", 0.0) or 0.0)
        cached_data = outlet_brands_cache.get("data", [])

        if cached_data and now - cached_at < 300:
            return {
                "brands": cached_data,
                "total_brands": len(cached_data),
                "cached": True,
            }

        normalize_map = {
            "dior": "Dior",
            "prada": "Prada",
            "gucci": "Gucci",
            "miumiu": "MiuMiu",
            "miu miu": "MiuMiu",
            "ysl": "YSL",
            "lv": "LV",
            "loewe": "Loewe",
            "celine": "Celine",
            "fendi": "Fendi",
            "burberry": "Burberry",
            "valentino": "Valentino",
            "balenciaga": "Balenciaga",
            "bottega veneta": "Bottega Veneta",
            "brunellocucinelli": "BrunelloCucinelli",
            "brunello cucinelli": "BrunelloCucinelli",
            "christiandior": "ChristianDior",
            "christian dior": "ChristianDior",
            "dolce&gabbana": "Dolce&Gabbana",
            "dolce & gabbana": "Dolce&Gabbana",
            "givenchy": "Givenchy",
            "tom ford": "Tom Ford",
            "max mara": "Max Mara",
            "marc jacobs": "Marc Jacobs",
            "golden goose": "Golden Goose",
            "goldengoose": "Golden Goose",
            "off-white": "OFF-WHITE",
            "off white": "OFF-WHITE",
            "saintlaurent": "SaintLaurent",
            "saint laurent": "SaintLaurent",
            "zimmermann": "Zimmermann",
            "schiaparelli": "Schiaparelli",
            "loro piana": "Loro Piana",
            "alexander wang": "Alexander Wang",
            "alexsander wang": "Alexander Wang",
            "tiffany&co": "Tiffany&Co",
            "tiffany & co": "Tiffany&Co",
        }

        skip_exact = {
            "reviews",
            "review",
            "new",
            "outlet",
            "sale",
            "brand",
            "size",
            "price",
        }

        def clean_name(value: str) -> str:
            value = (value or "").strip()

            if not value:
                return ""

            value = value.strip(".,;:()[]{}|/\\\"' ")

            if not value:
                return ""

            low = value.lower()

            if low in skip_exact:
                return ""

            if "price" in low or "size" in low:
                return ""

            if value.startswith("@"):
                return ""

            if "\u20ac" in value or "$" in value:
                return ""

            for suffix in [
                " outlet",
                " new",
                " boutique",
                " boutiques",
            ]:
                if low.endswith(suffix):
                    value = value[:-len(suffix)].strip()
                    low = value.lower()

            if not value:
                return ""

            if low in normalize_map:
                return normalize_map[low]

            return " ".join(
                word.capitalize()
                for word in value.split()
            )

        def get_row_brand(row: dict) -> str:
            brand = clean_name(
                str(row.get("brand", "") or "")
            )

            if brand:
                return brand

            caption = str(
                row.get("caption", "") or ""
            ).strip()

            if not caption:
                return ""

            if "#" in caption:
                try:
                    tag = (
                        caption
                        .split("#", 1)[1]
                        .split()[0]
                        .strip()
                    )

                    brand = clean_name(tag)

                    if brand:
                        return brand

                except Exception:
                    pass

            return ""

        rows = []
        page_size = 1000
        start = 0

        while True:
            response = (
                supabase.table(TABLE)
                .select("brand,caption")
                .eq("source", "Outlets")
                .range(start, start + page_size - 1)
                .execute()
            )

            chunk = response.data or []
            rows.extend(chunk)

            if len(chunk) < page_size:
                break

            start += page_size

            if start >= 500000:
                break

        grouped = {}

        for row in rows:
            brand = get_row_brand(row)

            if not brand:
                continue

            key = brand.lower()

            if key not in grouped:
                grouped[key] = {
                    "brand": brand,
                    "count": 0,
                }

            grouped[key]["count"] += 1

        brands = sorted(
            grouped.values(),
            key=lambda item: item["brand"].lower(),
        )

        outlet_brands_cache["ts"] = now
        outlet_brands_cache["data"] = brands

        return {
            "brands": brands,
            "total_brands": len(brands),
            "cached": False,
        }

    except Exception as e:
        print("OUTLET BRANDS API ERROR", repr(e))
        traceback.print_exc()

        return JSONResponse(
            {
                "brands": [],
                "total_brands": 0,
                "detail": "outlet_brands_fetch_failed",
            },
            status_code=500,
        )
        # ================= BOUTIQUES SALES API =================

def extract_discount_percent(caption: str) -> Optional[int]:
    text = str(caption or "")

    patterns = [
        r"-\s*(\d{1,3})\s*%",
        r"(\d{1,3})\s*%",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue

        try:
            value = int(match.group(1))
        except Exception:
            continue

        if 0 <= value <= 100:
            return value

    return None


@app.get("/api/boutique-sales")
def get_boutique_sales(
    offset: int = 0,
    limit: int = 24,
    brand: str = "",
    q: str = "",
):
    try:
        offset = max(0, int(offset))
        limit = max(1, min(200, int(limit)))

        now = time.time()
        cached_at = float(boutique_sales_cache.get("ts", 0.0))
        cached_data = boutique_sales_cache.get("data", [])

        if not cached_data or now - cached_at >= 300:
            rows = []
            page_size = 1000
            start = 0

            while True:
                response = (
                    supabase.table(TABLE)
                    .select("*")
                    .eq("source", "Boutiques")
                    .order("ts", desc=True)
                    .range(start, start + page_size - 1)
                    .execute()
                )

                chunk = response.data or []
                rows.extend(chunk)

                if len(chunk) < page_size:
                    break

                start += page_size

                if start >= 500000:
                    break

            filtered_rows = []

            for row in rows:
                discount = extract_discount_percent(
                    str(row.get("caption", "") or "")
                )

                if discount is None:
                    continue

                if 30 <= discount <= 80:
                    item = dict(row)
                    item["discount_percent"] = discount
                    filtered_rows.append(item)

            boutique_sales_cache["ts"] = now
            boutique_sales_cache["data"] = filtered_rows
            cached_data = filtered_rows

        result = list(cached_data)

        brand_value = str(brand or "").strip().lower()
        if brand_value:
            result = [
                row
                for row in result
                if brand_value in str(row.get("brand", "") or "").lower()
                or brand_value in str(row.get("caption", "") or "").lower()
            ]

        search_value = str(q or "").strip().lower()
        if search_value:
            result = [
                row
                for row in result
                if search_value in str(row.get("brand", "") or "").lower()
                or search_value in str(row.get("caption", "") or "").lower()
            ]

        total = len(result)
        items = result[offset:offset + limit]
        has_more = offset + len(items) < total

        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": has_more,
        }

    except Exception as e:
        print("BOUTIQUE SALES API ERROR", repr(e))
        traceback.print_exc()

        return JSONResponse(
            {"detail": "boutique_sales_fetch_failed"},
            status_code=500,
        )

# ================= JEWELRY BRAND CARDS API =================

jewelry_brands_cache = {
    "ts": 0.0,
    "data": [],
}

@app.get("/api/jewelry-brands")
def get_jewelry_brands():
    try:
        now = time.time()
        cached_at = float(jewelry_brands_cache.get("ts", 0.0))
        cached_data = jewelry_brands_cache.get("data", [])

        if cached_data and now - cached_at < 300:
            return {
                "brands": cached_data,
                "total_brands": len(cached_data),
                "cached": True,
            }

        rows = []
        page_size = 1000
        start = 0

        while True:
            response = (
                supabase.table(TABLE)
                .select("brand,caption")
                .eq("source", "Украшения")
                .range(start, start + page_size - 1)
                .execute()
            )

            chunk = response.data or []
            rows.extend(chunk)

            if len(chunk) < page_size:
                break

            start += page_size

            if start >= 500000:
                break

        normalize_map = {
            "cartier": "Cartier",
            "chopard": "Chopard",
            "bvlgari": "Bvlgari",
            "bulgari": "Bvlgari",
            "tiffany": "Tiffany & Co",
            "tiffany&co": "Tiffany & Co",
            "tiffany & co": "Tiffany & Co",
            "messika": "Messika",
            "vancleef": "Van Cleef & Arpels",
            "van cleef": "Van Cleef & Arpels",
            "van cleef & arpels": "Van Cleef & Arpels",
            "vca": "Van Cleef & Arpels",
            "boucheron": "Boucheron",
            "chaumet": "Chaumet",
            "graff": "Graff",
            "piaget": "Piaget",
            "dior": "Dior",
            "hermes": "Hermès",
            "hermès": "Hermès",
            "rolex": "Rolex",
            "omega": "Omega",
            "patek philippe": "Patek Philippe",
            "audemars piguet": "Audemars Piguet",
            "vacheron constantin": "Vacheron Constantin",
            "jaeger-lecoultre": "Jaeger-LeCoultre",
            "jaeger lecoultre": "Jaeger-LeCoultre",
        }

        skip_exact = {
            "reviews", "review", "new", "sale", "brand",
            "size", "price", "по вопросам", "размер"
        }

        def clean_name(value: str) -> str:
            value = (value or "").strip()
            if not value:
                return ""

            value = value.strip(".,;:()[]{}|/\\\"' ")
            if not value:
                return ""

            low = value.lower()

            if low in skip_exact:
                return ""

            if "price" in low or "размер" in low or "по вопросам" in low:
                return ""

            if value.startswith("@"):
                return ""

            if "€" in value or "$" in value:
                return ""

            if low in normalize_map:
                return normalize_map[low]

            return " ".join(word.capitalize() for word in value.split())

        def get_row_brand(row: dict) -> str:
            brand = clean_name(str(row.get("brand", "") or ""))
            if brand:
                return brand

            caption = str(row.get("caption", "") or "").strip()

            if "#" in caption:
                try:
                    tag = caption.split("#", 1)[1].split()[0].strip()
                    brand = clean_name(tag)
                    if brand:
                        return brand
                except Exception:
                    pass

            return ""

        grouped = {}
        no_brand_count = 0

        for row in rows:
            brand = get_row_brand(row)

            if not brand:
                no_brand_count += 1
                continue

            if brand.lower() in {"bvlgulari", "bvulgari", "vancleef&arpels"}:
                continue
           
            key = brand.lower()

            if key not in grouped:
                grouped[key] = {
                    "brand": brand,
                    "count": 0,
                }

            grouped[key]["count"] += 1

        brands = sorted(
            grouped.values(),
            key=lambda item: item["brand"].lower(),
        )

        if no_brand_count > 0:
            brands.insert(0, {
                "brand": "Часы и украшения",
                "count": no_brand_count,
                "special": "no_brand",
            })

        jewelry_brands_cache["ts"] = now
        jewelry_brands_cache["data"] = brands

        return {
            "brands": brands,
            "total_brands": len(brands),
            "cached": False,
        }

    except Exception as e:
        print("JEWELRY BRANDS API ERROR", repr(e))
        traceback.print_exc()

        return JSONResponse(
            {
                "brands": [],
                "total_brands": 0,
                "detail": "jewelry_brands_fetch_failed",
            },
            status_code=500,
        )
        # ================ JEWELRY NO-BRAND PRODUCTS API ================

@app.get("/api/jewelry-no-brand")
def get_jewelry_no_brand(
    offset: int = 0,
    limit: int = 24,
    q: str = "",
):
    try:
        offset = max(0, int(offset))
        limit = max(1, min(200, int(limit)))

        q_value = str(q or "").strip().lower()

        rows = []
        page_size = 1000
        start = 0

        while True:
            response = (
                supabase.table(TABLE)
                .select("*")
                .eq("source", "Украшения")
                .order("ts", desc=True)
                .range(start, start + page_size - 1)
                .execute()
            )

            chunk = response.data or []
            rows.extend(chunk)

            if len(chunk) < page_size:
                break

            start += page_size

            if start >= 500000:
                break

        normalize_map = {
            "cartier": "Cartier",
            "chopard": "Chopard",
            "bvlgari": "Bvlgari",
            "bulgari": "Bvlgari",
            "tiffany": "Tiffany & Co",
            "tiffany&co": "Tiffany & Co",
            "tiffany & co": "Tiffany & Co",
            "messika": "Messika",
            "vancleef": "Van Cleef & Arpels",
            "van cleef": "Van Cleef & Arpels",
            "van cleef & arpels": "Van Cleef & Arpels",
            "vca": "Van Cleef & Arpels",
            "boucheron": "Boucheron",
            "chaumet": "Chaumet",
            "graff": "Graff",
            "piaget": "Piaget",
            "dior": "Dior",
            "hermes": "Hermès",
            "hermès": "Hermès",
            "rolex": "Rolex",
            "omega": "Omega",
            "patek philippe": "Patek Philippe",
            "audemars piguet": "Audemars Piguet",
            "vacheron constantin": "Vacheron Constantin",
            "jaeger-lecoultre": "Jaeger-LeCoultre",
            "jaeger lecoultre": "Jaeger-LeCoultre",
        }

        skip_exact = {
            "reviews",
            "review",
            "new",
            "sale",
            "brand",
            "size",
            "price",
            "по вопросам",
            "размер",
        }

        def clean_name(value: str) -> str:
            value = (value or "").strip()

            if not value:
                return ""

            value = value.strip(".,;:()[]{}|/\\\\\"' ")

            if not value:
                return ""

            low = value.lower()

            if low in skip_exact:
                return ""

            if (
                "price" in low
                or "размер" in low
                or "по вопросам" in low
            ):
                return ""

            if value.startswith("@"):
                return ""

            if "€" in value or "$" in value:
                return ""

            if low in normalize_map:
                return normalize_map[low]

            return " ".join(
                word.capitalize()
                for word in value.split()
            )

        def get_row_brand(row: dict) -> str:
            brand = clean_name(
                str(row.get("brand", "") or "")
            )

            if brand:
                return brand

            caption = str(
                row.get("caption", "") or ""
            ).strip()

            if "#" in caption:
                try:
                    tag = (
                        caption
                        .split("#", 1)[1]
                        .split()[0]
                    )

                    brand = clean_name(tag)

                    if brand:
                        return brand

                except Exception:
                    pass

            return ""

        filtered = []

        for row in rows:
            # Товар относится к "Часы и украшения",
            # только если бренд определить не удалось.
            if get_row_brand(row):
                continue

            if q_value:
                haystack = " ".join([
                    str(row.get("caption", "") or ""),
                    str(row.get("brand", "") or ""),
                ]).lower()

                if q_value not in haystack:
                    continue

            filtered.append(row)

        total = len(filtered)
        items = filtered[offset:offset + limit]

        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + len(items) < total,
        }

    except Exception as e:
        print("JEWELRY NO BRAND API ERROR", repr(e))
        traceback.print_exc()

        return JSONResponse(
            {
                "items": [],
                "offset": 0,
                "limit": limit,
                "total": 0,
                "has_more": False,
                "detail": "jewelry_no_brand_fetch_failed",
            },
            status_code=500,
        )
      # ================= BOUTIQUES CARDS API =================

boutique_cards_cache = {
    "ts": 0.0,
    "data": [],
}


@app.get("/api/boutique-cards")
def get_boutique_cards():
    try:
        now = time.time()
        cached_at = float(boutique_cards_cache.get("ts", 0.0))
        cached_data = boutique_cards_cache.get("data", [])

        if cached_data and now - cached_at < 300:
            return {
                "cards": cached_data,
                "total_cards": len(cached_data),
                "cached": True,
            }

        normalize_map = {
            "acnestudios": "Acne Studios",
            "acnestudio": "Acne Studios",
            "acne": "Acne Studios",

            "alaia": "Alaïa",
            "alaïa": "Alaïa",
            "alaia_nug": "Alaïa",

            "alevi": "Alevi",
            "alevi’_nug": "Alevi",
            "alevi'_nug": "Alevi",

            "alexandermcquen": "Alexander McQueen",
            "alexander": "Alexander McQueen",
            "amcqueen": "Alexander McQueen",
            "mcqueen": "Alexander McQueen",
            "alexander mcqueen": "Alexander McQueen",

            "aquazzura": "Aquazzura",
            "aquazzura_er": "Aquazzura",
            "aquazzura_jul": "Aquazzura",
            "aquazzura_nug": "Aquazzura",

            "balenciaga": "Balenciaga",
            "balenciaga_ffm": "Balenciaga",

            "balmain": "Balmain",
            "balmain_jul": "Balmain",

            "bottega veneta": "Bottega Veneta",
            "bottegaveneta": "Bottega Veneta",
            "bottega_ffm": "Bottega Veneta",

            "brunello cucinelli": "Brunello Cucinelli",
            "brunellocucinelli": "Brunello Cucinelli",
            "brunellocucinelli_fk": "Brunello Cucinelli",
            "brunellocucinelli_jul": "Brunello Cucinelli",
            "brunellocucinelli_tb": "Brunello Cucinelli",

            "burberry": "Burberry",
            "burberry_ffm": "Burberry",
            "burberry_jul": "Burberry",

            "celine": "Celine",
            "celine_nug": "Celine",
            "celine_tb": "Celine",
            "celine_tg": "Celine",

            "chloe": "Chloe",
            "chloé": "Chloe",
            "chloè": "Chloe",

            "dior": "Dior",
            "christiandior": "Dior",
            "christian dior": "Dior",
            "dior_bb": "Dior",
            "dior_nug": "Dior",
            "dior_tg": "Dior",

            "dolce&gabbana": "Dolce & Gabbana",
            "dolce & gabbana": "Dolce & Gabbana",
            "dolcegabbana": "Dolce & Gabbana",
            "dg": "Dolce & Gabbana",
            "dolce_gabbana_er": "Dolce & Gabbana",
            "dolce_gabbana_jul": "Dolce & Gabbana",

            "etro": "Etro",
            "etro_jul": "Etro",

            "fendi": "Fendi",
            "fendi_er": "Fendi",
            "fendi_ffm": "Fendi",
            "fendi_jul": "Fendi",

            "ferragamo": "Ferragamo",
            "feragamo": "Ferragamo",

            "givenchy": "Givenchy",
            "givenchy_ffm": "Givenchy",

            "golden goose": "Golden Goose",
            "goldengoose": "Golden Goose",
            "goldengoose_jul": "Golden Goose",

            "gucci": "Gucci",
            "gucci_er": "Gucci",
            "gucci_ffm": "Gucci",
            "gucci_jul": "Gucci",
            "gucci_nug": "Gucci",

            "jacquemus": "Jacquemus",
            "jacquemus_tb": "Jacquemus",

            "jilsander": "Jil Sander",
            "jil sander": "Jil Sander",
            "jilsander_jul": "Jil Sander",

            "jimmychoo": "Jimmy Choo",
            "jimmy choo": "Jimmy Choo",
            "jimmy": "Jimmy Choo",

            "lesilla": "Le Silla",
            "le silla": "Le Silla",
            "lesilla_jul": "Le Silla",
            "lesilla_nug": "Le Silla",

            "loewe": "Loewe",
            "loewe_nug": "Loewe",
            "loewe_tb": "Loewe",
            "loewe_tg": "Loewe",

            "magdabutrym": "Magda Butrym",
            "magda butrym": "Magda Butrym",
            "magda": "Magda Butrym",
            "magdabuttym": "Magda Butrym",

            "maisonmargiela": "Maison Margiela",
            "maison margiela": "Maison Margiela",
            "maisionmargiela": "Maison Margiela",
            "mm6": "Maison Margiela",

            "max mara": "Max Mara",
            "maxmara": "Max Mara",
            "maxmara_jul": "Max Mara",
            "maxmara_tb": "Max Mara",
            "maxmarastudio_er": "Max Mara",
            "maxmarathecube_er": "Max Mara",
            "maxmarathecube_tb": "Max Mara",

            "miumiu": "Miu Miu",
            "miu miu": "Miu Miu",
            "miu": "Miu Miu",
            "miumiu_er": "Miu Miu",
            "miumiu_nug": "Miu Miu",

            "moncler": "Moncler",
            "moncler_jul": "Moncler",

            "paristexas": "Paris Texas",
            "paris texas": "Paris Texas",
            "paris": "Paris Texas",
            "paristexas_nug": "Paris Texas",
            "paristexas_tb": "Paris Texas",

            "prada": "Prada",
            "prada_er": "Prada",
            "prada_jul": "Prada",
            "prada_ffm": "Prada",

            "renècaovilla": "René Caovilla",
            "renécaovilla": "René Caovilla",
            "renecaovilla": "René Caovilla",
            "rene caovilla": "René Caovilla",
            "rené caovilla": "René Caovilla",

            "rogervivier": "Roger Vivier",
            "roger vivier": "Roger Vivier",
            "roger": "Roger Vivier",
            "roger_ffm": "Roger Vivier",

            "rotate": "Rotate",
            "rotate_jul": "Rotate",

            "saintlaurent": "Saint Laurent",
            "saint laurent": "Saint Laurent",
            "ysl": "Saint Laurent",
            "saint": "Saint Laurent",
            "saintlaurent_er": "Saint Laurent",
            "saintlaurent_nug": "Saint Laurent",

            "theattico": "The Attico",
            "the attico": "The Attico",
            "theattico_nug": "The Attico",

            "the row": "The Row",
            "therow": "The Row",
            "therow_tb": "The Row",

            "valentino": "Valentino",
            "valentino_ffm": "Valentino",
            "valentino_jul": "Valentino",
            "valentinogaravani": "Valentino",
            "valentinogaravani_er": "Valentino",
            "valentinogaravani_jul": "Valentino",
            "valentinogaravani_nug": "Valentino",

            "zimmermann": "Zimmermann",
            "zimmerman": "Zimmermann",
            "zimmermann_jul": "Zimmermann",
        }

        skip_exact = {
            "reviews",
            "review",
            "new",
            "outlet",
            "sale",
            "brand",
            "size",
            "price",
            "по вопросам",
            "размер",
        }

        def clean_brand_name(value: str) -> str:
            value = (value or "").strip()

            if not value:
                return ""

            value = value.strip(
                "👜👠👓🕶️✨💼🤍🖤🤎💛💙💚💜❤️🩷🩶🧸🌍"
                "•-–—,.;:()[]{}|/\\\"' "
            )

            if not value:
                return ""

            low = value.lower()

            if low in skip_exact:
                return ""

            if "по вопросам" in low:
                return ""

            if "price" in low:
                return ""

            if "размер" in low or "size" in low:
                return ""

            if value.startswith("@"):
                return ""

            if "€" in value or "$" in value:
                return ""

            for suffix in [
                " outlet",
                " new",
                " boutique",
                " boutiques",
            ]:
                if low.endswith(suffix):
                    value = value[:-len(suffix)].strip()
                    low = value.lower()

            if not value:
                return ""

            if low in normalize_map:
                return normalize_map[low]

            return " ".join(
                word.capitalize()
                for word in value.split()
            )

        def get_row_brand(row: dict) -> str:
            brand = clean_brand_name(
                str(row.get("brand", "") or "")
            )

            if brand:
                return brand

            caption = str(
                row.get("caption", "") or ""
            ).strip()

            if "#" in caption:
                try:
                    tag = (
                        caption
                        .split("#", 1)[1]
                        .split()[0]
                        .strip()
                    )

                    brand = clean_brand_name(tag)

                    if brand:
                        return brand

                except Exception:
                    pass

            return ""

        grouped = {}
        misc_count = 0

        page_size = 1000
        start = 0

        while True:
            response = (
                supabase.table(TABLE)
                .select("brand,caption")
                .eq("source", "Boutiques")
                .range(start, start + page_size - 1)
                .execute()
            )

            chunk = response.data or []

            for row in chunk:
                brand = get_row_brand(row)

                if brand:
                    key = brand.lower()

                    if key not in grouped:
                        grouped[key] = {
                            "title": brand,
                            "count": 0,
                            "type": "brand",
                            "value": brand,
                        }

                    grouped[key]["count"] += 1
                else:
                    misc_count += 1

            if len(chunk) < page_size:
                break

            start += page_size

            if start >= 500000:
                break

        brand_cards = sorted(
            grouped.values(),
            key=lambda item: item["title"].lower()
        )

        cards = brand_cards

        boutique_cards_cache["ts"] = now
        boutique_cards_cache["data"] = cards

        return {
            "cards": cards,
            "total_cards": len(cards),
            "cached": False,
        }

    except Exception as e:
        print("BOUTIQUE CARDS API ERROR", repr(e))
        traceback.print_exc()

        return JSONResponse(
            {
                "cards": [],
                "total_cards": 0,
                "detail": "boutique_cards_fetch_failed",
            },
            status_code=500,
        )
    # ================= BOUTIQUE MISC PRODUCTS API =================

@app.get("/api/boutique-card-products")
def get_boutique_card_products(
    card: str = "",
    offset: int = 0,
    limit: int = 24,
    q: str = "",
):
    try:
        offset = max(0, int(offset))
        limit = max(1, min(200, int(limit)))

        card_value = str(card or "").strip().lower()
        q_value = str(q or "").strip().lower()

        if card_value != "misc":
            return JSONResponse(
                {
                    "items": [],
                    "offset": offset,
                    "limit": limit,
                    "total": 0,
                    "has_more": False,
                    "detail": "invalid_boutique_card",
                },
                status_code=400,
            )

        filtered = []
        page_size = 1000
        start = 0

        while True:
            response = (
                supabase.table(TABLE)
                .select("*")
                .eq("source", "Boutiques")
                .order("ts", desc=True)
                .range(start, start + page_size - 1)
                .execute()
            )

            chunk = response.data or []

            for row in chunk:
                brand = str(
                    row.get("brand", "") or ""
                ).strip()

                caption = str(
                    row.get("caption", "") or ""
                ).strip()

                if not brand and "#" in caption:
                    try:
                        brand = (
                            caption
                            .split("#", 1)[1]
                            .split()[0]
                            .strip()
                        )
                    except Exception:
                        brand = ""

                if brand:
                    continue

                if q_value and q_value not in caption.lower():
                    continue

                filtered.append(row)

            if len(chunk) < page_size:
                break

            start += page_size

            if start >= 500000:
                break

        total = len(filtered)
        items = filtered[offset:offset + limit]

        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + len(items) < total,
        }

    except Exception as e:
        print(
            "BOUTIQUE MISC PRODUCTS API ERROR",
            repr(e)
        )
        traceback.print_exc()

        return JSONResponse(
            {
                "items": [],
                "offset": offset,
                "limit": limit,
                "total": 0,
                "has_more": False,
                "detail": "boutique_misc_products_fetch_failed",
            },
            status_code=500,
        )
# ================= SINGLE PRODUCT API =================

@app.get("/api/product/{row_id}")
def get_product(row_id: int):
    try:
        res = (
            supabase.table(TABLE)
            .select("*")
            .eq("id", int(row_id))
            .limit(1)
            .execute()
        )

        items = res.data or []
        if not items:
            return JSONResponse({"detail": "not_found"}, status_code=404)

        return {"item": items[0]}

    except Exception as e:
        print("PRODUCT BY ID ERROR ❌", repr(e))
        traceback.print_exc()
        return JSONResponse({"detail": "product_fetch_failed"}, status_code=500)
        
# ================= DELETE PRODUCT (ADMIN) =================

@app.delete("/api/delete/{row_id}")
def delete_product(row_id: int, request: Request):
    try:
        if not ADMIN_DELETE_TOKEN:
            return JSONResponse({"error": "admin_delete_token_not_set"}, status_code=500)

        token = request.headers.get("X-ADMIN-TOKEN", "")
        if token != ADMIN_DELETE_TOKEN:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        resp = supabase.table(TABLE).delete().eq("id", int(row_id)).execute()

        err = getattr(resp, "error", None)
        if err:
            return JSONResponse({"error": "delete_failed", "detail": str(err)}, status_code=500)

        return {"ok": True}

    except Exception as e:
        print("DELETE ERROR ❌", repr(e))
        traceback.print_exc()
        return JSONResponse({"error": "delete_failed"}, status_code=500)

# ================= TELEGRAM FILE PROXY =================

@app.get("/api/tgfile/{file_id}")
def tgfile(file_id: str, request: Request):
    etag = f'W/"{file_id}"'
    inm = request.headers.get("if-none-match")
    if inm and inm.strip() == etag:
        return Response(
            status_code=304,
            headers={
                "Cache-Control": "public, max-age=604800, immutable",
                "ETag": etag,
            },
        )

    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
        params={"file_id": file_id},
        timeout=20
    )
    data = r.json()
    if not data.get("ok"):
        return JSONResponse({"detail": "Telegram get_file failed"}, status_code=400)

    file_path = data["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    file_resp = requests.get(file_url, timeout=40)
    if file_resp.status_code != 200:
        return JSONResponse({"detail": "Telegram file fetch failed"}, status_code=404)

    content_type = file_resp.headers.get("content-type", "image/jpeg")
    headers = {"Cache-Control": "public, max-age=604800, immutable", "ETag": etag}
    return Response(content=file_resp.content, media_type=content_type, headers=headers)

# ================= ROOT =================

@app.get("/")
def root():
    p = Path(__file__).resolve().parent / "src" / "index.html"
    if not p.exists():
        return JSONResponse({"detail": f"index.html not found at {p}"}, status_code=404)
    return FileResponse(str(p))
