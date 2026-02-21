import os
import asyncio
import traceback
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
# Boutiques: -1001158220106
# Outlets:   -1002303984060
# Очки:      -1001330891461

CHAT_SOURCE = {
    -1001158220106: "Boutiques",
    -1002303984060: "Outlets",
    -1001330891461: "Очки", 
}

def detect_source(chat_id: int) -> str:
    return CHAT_SOURCE.get(int(chat_id), "Boutiques")

# ================= TOPIC CACHE =================
# Telegram не присылает название топика в каждом сообщении.
# Поэтому мы запоминаем название, когда приходят service-сообщения:
# forum_topic_created / forum_topic_edited
TopicKey = Tuple[int, int]  # (chat_id, thread_id)
topic_title_cache: Dict[TopicKey, str] = {}

# ================= STARTUP / SHUTDOWN =================

@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()
    print("✅ telegram_app started")

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

# ================= SAVE PRODUCT =================

def save_product(msg):
    caption = msg.caption or ""
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
            .eq("chat_id", chat_id)     # ✅ важно: альбом только в рамках этого канала
            .limit(1)
            .execute()
        )

        if existing.data:
            row = existing.data[0]
            new_file_ids = safe_append_file_id(row.get("file_ids"), file_id)

            new_caption = row.get("caption") or caption
            new_brand = row.get("brand") or brand
            new_ts = row.get("ts") or ts
            new_file_id = row.get("file_id") or file_id

            supabase.table(TABLE).update({
                "file_ids": new_file_ids,
                "file_id": new_file_id,
                "caption": new_caption,
                "brand": new_brand,
                "ts": new_ts,
                "source": source,
                "chat_id": chat_id,     # ✅ сохраняем chat_id
            }).eq("id", row["id"]).execute()
            return

        supabase.table(TABLE).insert({
            "chat_id": chat_id,         # ✅ важно
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
    # ✅ ВАЖНО: уникальность в рамках канала -> on_conflict="chat_id,message_id"
    supabase.table(TABLE).upsert({
        "chat_id": chat_id,             # ✅ важно
        "message_id": message_id,
        "brand": brand,
        "caption": caption,
        "file_id": file_id or None,
        "file_ids": [file_id] if file_id else [],
        "ts": ts,
        "source": source,
    }, on_conflict="chat_id,message_id").execute()

# ================= CAPTION FIX (120 sec) =================
# ✅ ВАЖНО: теперь фикс привязан и к chat_id, чтобы текст не прилипал к другому каналу

def attach_text_caption_to_recent_item(msg, window_seconds: int = 120) -> bool:
    text = (msg.text or "").strip()
    if not text:
        return False

    ts = msg_ts(msg)
    if not ts:
        return False

    chat_id = int(msg.chat.id)
    source = detect_source(chat_id)
    brand = pick_brand(msg, text)

    try:
        res = (
            supabase.table(TABLE)
            .select("id,caption,brand,ts,source,chat_id")
            .eq("chat_id", chat_id)         # ✅ важно
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

    # 0) service topic events
    if getattr(msg, "forum_topic_created", None) or getattr(msg, "forum_topic_edited", None):
        remember_topic_title(msg)
        return

    # 1) photo -> save product
    if msg.photo:
        save_product(msg)
        return

    # 2) text -> caption fix
    if msg.text:
        attach_text_caption_to_recent_item(msg, window_seconds=120)
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

# ================= PRODUCTS API (SERVER PAGINATION) =================

@app.get("/api/products")
def get_products(
    offset: int = 0,
    limit: int = 24,
    source: str = "",
    brand: str = "",
    q: str = "",
):
    """
    ✅ серверная пагинация + фильтры.
    """
    try:
        offset = max(0, int(offset))
        limit = max(1, min(200, int(limit)))  # защита

        query = supabase.table(TABLE).select("*", count="exact").order("ts", desc=True)

        s = (source or "").strip()
        if s:
            query = query.eq("source", s)

        b = (brand or "").strip()
        if b:
            safe = b.replace("%", "").replace(",", " ")
            query = query.or_(f"brand.ilike.%{safe}%,caption.ilike.%{safe}%")

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

    headers = {
        "Cache-Control": "public, max-age=604800, immutable",
        "ETag": etag,
    }
    return Response(content=file_resp.content, media_type=content_type, headers=headers)

# ================= ROOT =================

@app.get("/")
def root():
    p = Path(__file__).resolve().parent / "src" / "index.html"
    if not p.exists():
        return JSONResponse({"detail": f"index.html not found at {p}"}, status_code=404)
    return FileResponse(str(p))
