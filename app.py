import os
import requests

from fastapi import FastAPI, Request
from fastapi.responses import Response, FileResponse

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

from supabase import create_client


# ================= ENV =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

TABLE = "products"


# ================= INIT =================

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()


# ================= SAVE PRODUCT =================

def save_product(msg):

    photos = []

    if msg.photo:
        best = msg.photo[-1]
        photos.append(best.file_id)

    media_group = msg.media_group_id
    caption = msg.caption or ""

    brand = ""

    if "#" in caption:
        brand = caption.split("#")[1].split()[0]

    data = {
        "brand": brand,
        "caption": caption,
        "file_ids": photos,
        "media_group_id": media_group,
        "message_id": msg.message_id
    }

    supabase.table(TABLE).upsert(data).execute()


# ================= TELEGRAM HANDLER =================

async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    msg = update.channel_post

    if not msg:
        return

    if msg.photo:
        save_product(msg)


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

    res = supabase.table(TABLE).select("*").execute()

    return res.data or []


# ================= TELEGRAM FILE PROXY =================

@app.get("/api/tgfile/{file_id}")
def tgfile(file_id: str):

    # 1. Получаем путь файла
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
        params={"file_id": file_id}
    )

    data = r.json()

    if not data.get("ok"):
        return {"detail": "Telegram get_file failed"}

    file_path = data["result"]["file_path"]

    # 2. Качаем файл
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    file_resp = requests.get(file_url)

    if file_resp.status_code != 200:
        return {"detail": "Telegram file fetch failed"}

    return Response(
        content=file_resp.content,
        media_type="image/jpeg"
    )


# ================= ROOT (INDEX FIX) =================

@app.get("/")
def root():
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "index.html")
    )
