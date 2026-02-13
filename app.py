import os
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, Response
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from supabase import create_client

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
TABLE = os.getenv("SUPABASE_TABLE", "products")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in env")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_KEY is missing in env")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()


def extract_brand(caption: str) -> str:
    # Берём первое слово после #
    if not caption:
        return ""
    if "#" not in caption:
        return ""
    try:
        return caption.split("#", 1)[1].split()[0].strip()
    except Exception:
        return ""


def mgid_for_message(msg) -> str:
    # Если это альбом — media_group_id будет одинаковый у всех фоток
    # Если одиночное фото — делаем стабильный "альбом" из одного элемента
    return str(msg.media_group_id) if msg.media_group_id else f"msg_{msg.message_id}"


def supa_select_one_by_mgid(mgid: str) -> Optional[Dict[str, Any]]:
    r = supabase.table(TABLE).select("*").eq("media_group_id", mgid).limit(1).execute()
    data = getattr(r, "data", None) or []
    return data[0] if data else None


def supa_upsert_album_row(mgid: str, new_file_id: str, msg) -> None:
    caption = msg.caption or ""
    brand = extract_brand(caption)
    ts = int(msg.date.timestamp()) if getattr(msg, "date", None) else int(time.time())

    existing = supa_select_one_by_mgid(mgid)

    if existing:
        # дописываем file_id в массив file_ids, без дублей
        file_ids: List[str] = existing.get("file_ids") or []
        if new_file_id and new_file_id not in file_ids:
            file_ids.append(new_file_id)

        # caption/brand обновляем только если в новом сообщении они есть
        upd: Dict[str, Any] = {
            "media_group_id": mgid,
            "file_ids": file_ids,
            "ts": existing.get("ts") or ts,
        }
        if caption:
            upd["caption"] = caption
        if brand:
            upd["brand"] = brand

        # message_id можно хранить последний
        upd["message_id"] = msg.message_id

        supabase.table(TABLE).upsert(upd).execute()
        return

    # если записи ещё нет — создаём новую
    row = {
        "media_group_id": mgid,
        "file_ids": [new_file_id] if new_file_id else [],
        "caption": caption,
        "brand": brand,
        "message_id": msg.message_id,
        "ts": ts,
    }
    supabase.table(TABLE).insert(row).execute()


# ---------- TELEGRAM HANDLER ----------
async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return

    if not msg.photo:
        return

    # best quality photo
    best = msg.photo[-1]
    file_id = best.file_id

    mgid = mgid_for_message(msg)
    supa_upsert_album_row(mgid, file_id, msg)


telegram_app.add_handler(MessageHandler(filters.ALL, handler))


# ---------- FASTAPI LIFECYCLE ----------
@app.on_event("startup")
async def on_startup():
    # Важно: инициализация PTB для webhook режима
    await telegram_app.initialize()
    await telegram_app.start()


@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


# ---------- WEBHOOK ----------
@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    upd = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(upd)
    return {"ok": True}


# ---------- API: PRODUCTS ----------
@app.get("/api/products")
async def api_products():
    # отдаём последние товары (можно увеличить лимит)
    r = supabase.table(TABLE).select("*").order("ts", desc=True).limit(200).execute()
    data = getattr(r, "data", None)
    if data is None:
        return JSONResponse([], status_code=200)
    return JSONResponse(data, status_code=200)


# ---------- API: TELEGRAM FILE PROXY (best quality) ----------
@app.get("/api/tgfile/{file_id}")
async def api_tgfile(file_id: str):
    try:
        f = await telegram_app.bot.get_file(file_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Telegram get_file failed: {e}")

    if not getattr(f, "file_path", None):
        raise HTTPException(status_code=404, detail="No file_path")

    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Telegram file fetch failed: {resp.status_code}")

    # telegram обычно отдаёт image/jpeg, но бывает image/webp
    ctype = resp.headers.get("content-type", "application/octet-stream")
    return Response(content=resp.content, media_type=ctype)
