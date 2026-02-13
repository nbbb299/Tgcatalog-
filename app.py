import os
import requests
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import Response, FileResponse, JSONResponse
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from supabase import create_client

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TABLE = os.getenv("SUPABASE_TABLE", "products")

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing env: BOT_TOKEN / SUPABASE_URL / SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

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

def save_product(msg):
    caption = msg.caption or ""
    brand = extract_brand_from_caption(caption)
    media_group_id = msg.media_group_id  # album id or None
    message_id = msg.message_id

    file_id = ""
    if msg.photo:
        file_id = msg.photo[-1].file_id

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

            # caption/brand/ts берем первое непустое (чтобы было как в телеге)
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

async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return
    if msg.photo:
        save_product(msg)

telegram_app.add_handler(MessageHandler(filters.ALL, handler))

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.get("/api/products")
def get_products():
    res = (
        supabase.table(TABLE)
        .select("*")
        .order("ts", desc=True)
        .execute()
    )
    return res.data or []

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

@app.get("/")
def root():
    p = Path(__file__).resolve().parent / "src" / "index.html"
    if not p.exists():
        return JSONResponse({"detail": f"index.html not found at {p}"}, status_code=404)
    return FileResponse(str(p))
