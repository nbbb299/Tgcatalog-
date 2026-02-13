import os
import requests
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response, FileResponse, JSONResponse

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

from supabase import create_client


BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

TABLE = "products"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()


# ================= SAVE PRODUCT =================

def save_product(msg):

    new_file_id = msg.photo[-1].file_id
    media_group = msg.media_group_id
    caption = msg.caption or ""

    brand = ""
    if "#" in caption:
        brand = caption.split("#")[1].split()[0]

    # 👉 если это альбом — ищем существующую запись
    if media_group:

        existing = supabase.table(TABLE)\
            .select("*")\
            .eq("media_group_id", media_group)\
            .execute()

        if existing.data:

            row = existing.data[0]
            files = row.get("file_ids") or []

            if new_file_id not in files:
                files.append(new_file_id)

            supabase.table(TABLE)\
                .update({"file_ids": files})\
                .eq("id", row["id"])\
                .execute()

            return

    # новая запись
    data = {
        "brand": brand,
        "caption": caption,
        "file_ids": [new_file_id],
        "media_group_id": media_group,
        "message_id": msg.message_id,
        "ts": int(time.time())
    }

    supabase.table(TABLE).insert(data).execute()


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

    res = supabase.table(TABLE)\
        .select("*")\
        .order("ts", desc=True)\
        .execute()

    return res.data or []


@app.get("/api/tgfile/{file_id}")
def tgfile(file_id: str):

    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
        params={"file_id": file_id}
    )

    data = r.json()

    if not data.get("ok"):
        return JSONResponse({"detail": "Telegram error"}, status_code=400)

    file_path = data["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    file_resp = requests.get(file_url)

    return Response(
        content=file_resp.content,
        media_type=file_resp.headers.get("content-type","image/jpeg")
    )


@app.get("/")
def root():
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "src", "index.html")
    )
