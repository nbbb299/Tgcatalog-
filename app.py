import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from supabase import create_client

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TABLE = os.getenv("SUPABASE_TABLE", "products")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Telegram file url
def file_url(file_id):
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_id}"

# --- Save product
def save_product(msg):

    photos = []

    if msg.photo:
        best = msg.photo[-1]  # максимальное качество
        photos.append(best.file_id)

    media_group = msg.media_group_id

    caption = msg.caption or ""

    brand = "ITEM"

    if "#" in caption:
        brand = caption.split("#")[1].split()[0]

    data = {
        "brand": brand,
        "caption": caption,
        "file_ids": photos,
        "media_group_id": media_group,
        "message_id": msg.message_id
    }

    supabase.table(TABLE).insert(data).execute()


async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post

    if not msg:
        return

    if msg.photo:
        save_product(msg)


app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(MessageHandler(filters.ALL, handler))

app.run_polling()
