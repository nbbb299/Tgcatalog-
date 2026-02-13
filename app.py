import os
import time
from pathlib import Path

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response, FileResponse

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

from supabase import create_client


# ================= ENV =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TABLE = os.getenv("SUPABASE_TABLE", "products")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing")
if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_KEY is missing")


# ================= INIT =================
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()

telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()


# ================= STARTUP/SHUTDOWN (важно для webhook) =================
@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()

@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


# ================= HELPERS =================
def extract_brand(caption: str) -> str:
    if not caption:
        return ""
    if "#" in caption:
        try:
            return caption.split("#", 1)[1].split()[0].strip()
        except Exception:
            return ""
    return ""

def msg_ts(msg) -> int:
    # msg.date обычно datetime
    try:
        return int(msg.date.timestamp())
    except Exception:
        return int(time.time())

def upsert_album(media_group_id: str, file_id: str, brand: str, caption: str, message_id: int, ts: int):
    # 1) пробуем найти существующую запись по media_group_id
    existing = supabase.table(TABLE).select("*").eq("media_group_id", media_group_id).limit(1).execute().data
    if existing:
        row = existing[0]
        current = row.get("file_ids") or []
        if not isinstance(current, list):
            current = []

        if file_id and file_id not in current:
            current.append(file_id)

        # не затираем caption пустым
        final_caption = caption if caption else (row.get("caption") or "")
        final_brand = brand if brand else (row.get("brand") or "")

        data = {
            "media_group_id": media_group_id,
            "file_ids": current,
            "caption": final_caption,
            "brand": final_brand,
            "message_id": message_id,
            "ts": ts,
        }
        supabase.table(TABLE).upsert(data, on_conflict="media_group_id").execute()
        return

    # 2) если нет — создаём новую запись
    data = {
        "media_group_id": media_group_id,
        "file_ids": [file_id] if file_id else [],
        "caption": caption or "",
        "brand": brand or "",
        "message_id": message_id,
        "ts": ts,
    }
    supabase.table(TABLE).insert(data).execute()

def insert_single(file_id: str, brand: str, caption: str, message_id: int, ts: int):
    data = {
        "media_group_id": None,
        "file_ids": [file_id] if file_id else [],
        "caption": caption or "",
        "brand": brand or "",
        "message_id": message_id,
        "ts": ts,
    }
    supabase.table(TABLE).insert(data).execute()


# ================= TELEGRAM HANDLER =================
async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return

    if not msg.photo:
        return

    best = msg.photo[-1]  # самое большое качество
    file_id = best.file_id

    caption = msg.caption or ""
    brand = extract_brand(caption)
    ts = msg_ts(msg)

    if msg.media_group_id:
        upsert_album(
            media_group_id=str(msg.media_group_id),
            file_id=file_id,
            brand=brand,
            caption=caption,
            message_id=msg.message_id,
            ts=ts
        )
    else:
        insert_single(
            file_id=file_id,
            brand=brand,
            caption=caption,
            message_id=msg.message_id,
            ts=ts
        )

telegram_app.add_handler(MessageHandler(filters.ALL, handler))


# ================= WEBHOOK =================
@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


# ================= PRODUCTS API =================
@app.get("/api/products")
def get_products():
    res = supabase.table(TABLE).select("*").order("ts", desc=True).limit(300).execute()
    return res.data or []


# ================= TELEGRAM FILE PROXY =================
@app.get("/api/tgfile/{file_id}")
def tgfile(file_id: str):
    # 1) getFile
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
        params={"file_id": file_id},
        timeout=20
    )
    data = r.json()

    if not data.get("ok"):
        # чаще всего тут “Invalid file_id” если в file_id попали пробелы/кириллица/переносы
        raise HTTPException(status_code=400, detail=f"Telegram get_file failed: {data.get('description','Unknown error')}")

    file_path = data["result"]["file_path"]

    # 2) download file
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    file_resp = requests.get(file_url, timeout=30)

    if file_resp.status_code != 200:
        raise HTTPException(status_code=404, detail="Telegram file fetch failed: 404")

    content_type = file_resp.headers.get("content-type", "image/jpeg")
    return Response(content=file_resp.content, media_type=content_type)


# ================= ROOT (serve index.html) =================
@app.get("/")
def root():
    index_path = Path(__file__).resolve().parent / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail=f"index.html not found at {index_path}")
    return FileResponse(str(index_path))
