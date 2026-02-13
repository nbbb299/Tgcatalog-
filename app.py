import os
import asyncio
import traceback
import requests
from pathlib import Path
from typing import List, Optional

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

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing env: BOT_TOKEN / SUPABASE_URL / SUPABASE_KEY")

# ================= INIT =================

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

# ================= STARTUP / SHUTDOWN (ВАЖНО) =================
# Без этого process_update() часто падает -> Telegram видит 500 -> новые фото не приходят

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


# ================= SAVE PRODUCT =================

def save_product(msg):
    caption = msg.caption or ""
    brand = extract_brand_from_caption(caption)
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
            .select("id,file_ids,file_id,caption,brand,ts,media_group_id,message_id")
            .eq("media_group_id", str(media_group_id))
            .limit(1)
            .execute()
        )

        if existing.data:
            row = existing.data[0]
            new_file_ids = safe_append_file_id(row.get("file_ids"), file_id)

            # caption/brand/ts берем первое непустое (как в телеге)
            new_caption = row.get("caption") or caption
            new_brand = row.get("brand") or brand
            new_ts = row.get("ts") or ts
            new_file_id = row.get("file_id") or file_id  # первый кадр

            supabase.table(TABLE).update({
                "file_ids": new_file_ids,
                "file_id": new_file_id,
                "caption": new_caption,
                "brand": new_brand,
                "ts": new_ts,
            }).eq("id", row["id"]).execute()
            return

        # если не нашли — создаём
        supabase.table(TABLE).insert({
            "media_group_id": str(media_group_id),
            "message_id": message_id,
            "brand": brand,
            "caption": caption,
            "file_id": file_id or None,
            "file_ids": [file_id] if file_id else [],
            "ts": ts
        }).execute()
        return

    # -------- SINGLE POST --------
    supabase.table(TABLE).upsert({
        "message_id": message_id,
        "brand": brand,
        "caption": caption,
        "file_id": file_id or None,
        "file_ids": [file_id] if file_id else [],
        "ts": ts
    }, on_conflict="message_id").execute()


# ================= CAPTION FIX (НОВОЕ) =================
# Если фото пришло без caption, а текст ты написала отдельным сообщением —
# мы в течение 120 секунд приклеим этот текст к последнему товару без caption.

def attach_text_caption_to_recent_item(msg, window_seconds: int = 120) -> bool:
    text = (msg.text or "").strip()
    if not text:
        return False

    ts = msg_ts(msg)
    if not ts:
        return False

    brand = extract_brand_from_caption(text)

    try:
        # Берем последние записи за окно времени, сверху вниз
        res = (
            supabase.table(TABLE)
            .select("id,caption,brand,ts,message_id,media_group_id")
            .gte("ts", ts - window_seconds)
            .order("ts", desc=True)
            .limit(15)
            .execute()
        )

        rows = res.data or []
        if not rows:
            return False

        # Ищем первую запись БЕЗ caption (пустая/NULL)
        target = None
        for r in rows:
            if is_empty_caption(r.get("caption")):
                target = r
                break

        if not target:
            return False

        upd = {"caption": text}

        # бренд ставим ТОЛЬКО если в записи пустой
        if (not (target.get("brand") or "").strip()) and brand:
            upd["brand"] = brand

        supabase.table(TABLE).update(upd).eq("id", target["id"]).execute()
        print(f"🧩 Caption FIX applied -> row id={target['id']}")
        return True

    except Exception as e:
        print("Caption FIX ERROR ❌", repr(e))
        traceback.print_exc()
        return False


# ================= TELEGRAM HANDLER =================

async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return

    # 1) Фото -> сохраняем товар как обычно
    if msg.photo:
        save_product(msg)
        return

    # 2) Текст без фото -> пытаемся приклеить как caption (120 секунд)
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

        # ✅ НЕ блокируем ответ Telegram — обрабатываем в фоне
        asyncio.create_task(telegram_app.process_update(update))

        # ✅ Telegram должен получить 200 всегда
        return {"ok": True}

    except Exception as e:
        print("WEBHOOK ERROR ❌", repr(e))
        traceback.print_exc()
        # ✅ Все равно отдаём 200, чтобы Telegram не копил очередь и не отключал вебхук
        return {"ok": True}


# ================= PRODUCTS API =================

@app.get("/api/products")
def get_products():
    res = (
        supabase.table(TABLE)
        .select("*")
        .order("ts", desc=True)
        .execute()
    )
    return res.data or []


# ================= TELEGRAM FILE PROXY =================

@app.get("/api/tgfile/{file_id}")
def tgfile(file_id: str):
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
    return Response(content=file_resp.content, media_type=content_type)


# ================= ROOT =================

@app.get("/")
def root():
    p = Path(__file__).resolve().parent / "src" / "index.html"
    if not p.exists():
        return JSONResponse({"detail": f"index.html not found at {p}"}, status_code=404)
    return FileResponse(str(p))
