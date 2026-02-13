import os
import time
import requests
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, Response, JSONResponse
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from supabase import create_client


# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TABLE = os.getenv("SUPABASE_TABLE", "products")

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing env vars: BOT_TOKEN / SUPABASE_URL / SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()
tg_app = ApplicationBuilder().token(BOT_TOKEN).build()


# =========================
# Helpers
# =========================
def extract_brand(caption: str) -> str:
    # brand from #Dior -> dior
    if not caption:
        return ""
    if "#" not in caption:
        return ""
    try:
        b = caption.split("#", 1)[1].split()[0].strip()
        return b.lower()
    except Exception:
        return ""


def now_ts() -> int:
    return int(time.time())


def safe_list(v) -> List:
    return v if isinstance(v, list) else []


# =========================
# Serve INDEX (catalog page)
# =========================
@app.get("/")
def root():
    # ВАЖНО: файл index.html должен лежать РЯДОМ с app.py в репозитории
    return FileResponse("index.html")


# =========================
# API: Products for frontend
# =========================
@app.get("/api/products")
def api_products(limit: int = 200):
    try:
        resp = (
            supabase.table(TABLE)
            .select("*")
            .order("ts", desc=True)
            .limit(limit)
            .execute()
        )
        return JSONResponse(resp.data or [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase error: {e}")


# =========================
# Telegram file proxy
# =========================
@app.get("/api/tgfile/{file_id}")
def tgfile(file_id: str):
    # 1) getFile -> file_path
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
        params={"file_id": file_id},
        timeout=20,
    )
    data = r.json()

    if not data.get("ok"):
        # Telegram вернул ok:false (чаще всего — битый file_id из копипаста)
        raise HTTPException(status_code=400, detail="Telegram get_file failed")

    file_path = data["result"]["file_path"]

    # 2) download file
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    fr = requests.get(file_url, timeout=40)

    if fr.status_code != 200:
        raise HTTPException(status_code=404, detail="Telegram file fetch failed")

    # Telegram обычно отдаёт jpeg/png. Если хочешь — можно определить по headers.
    ct = fr.headers.get("Content-Type") or "image/jpeg"
    return Response(content=fr.content, media_type=ct)


# =========================
# Save / Update product (album aware)
# =========================
def upsert_product_from_message(msg) -> None:
    caption = msg.caption or ""
    brand = extract_brand(caption)
    ts = int(msg.date.timestamp()) if getattr(msg, "date", None) else now_ts()

    media_group_id = msg.media_group_id
    message_id = msg.message_id

    # достаём лучший файл_id из photo (последний = лучший)
    new_file_ids: List[str] = []
    if msg.photo:
        best = msg.photo[-1]
        new_file_ids.append(best.file_id)

    # Если это альбом — собираем в одну запись по media_group_id
    if media_group_id:
        # берём существующую запись
        existing = (
            supabase.table(TABLE)
            .select("id,file_ids,caption,brand,ts,media_group_id")
            .eq("media_group_id", str(media_group_id))
            .limit(1)
            .execute()
        )
        if existing.data:
            row = existing.data[0]
            current = safe_list(row.get("file_ids"))
            merged = current + [x for x in new_file_ids if x not in current]
            merged = merged[:20]  # максимум 20 фото

            # caption/brand берём последний НЕ пустой
            new_caption = caption if caption.strip() else (row.get("caption") or "")
            new_brand = brand if brand.strip() else (row.get("brand") or "")

            update_data = {
                "file_ids": merged,
                "caption": new_caption,
                "brand": new_brand,
                "ts": max(int(row.get("ts") or 0), ts),
                "message_id": message_id,
            }

            supabase.table(TABLE).update(update_data).eq("id", row["id"]).execute()
            return

        # если записи ещё нет — создаём
        insert_data = {
            "media_group_id": str(media_group_id),
            "file_ids": new_file_ids[:20],
            "caption": caption,
            "brand": brand,
            "ts": ts,
            "message_id": message_id,
        }
        supabase.table(TABLE).insert(insert_data).execute()
        return

    # одиночное фото (не альбом)
    insert_data = {
        "media_group_id": None,
        "file_ids": new_file_ids[:20],
        "caption": caption,
        "brand": brand,
        "ts": ts,
        "message_id": message_id,
    }
    supabase.table(TABLE).insert(insert_data).execute()


# =========================
# Telegram handler
# =========================
async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return
    if msg.photo:
        upsert_product_from_message(msg)


tg_app.add_handler(MessageHandler(filters.ALL, handler))


# =========================
# Webhook endpoint
# =========================
@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
