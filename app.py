import os
import re
import time
from typing import Optional, List, Dict, Any

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from supabase import create_client

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")  # у тебя работает с JWT anon — оставляем
TABLE = os.getenv("SUPABASE_TABLE", "products")

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing env vars: BOT_TOKEN / SUPABASE_URL / SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------------------
# Helpers
# ---------------------------

HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")

def extract_brand(caption: str) -> str:
    """
    Берём первый #hashtag как бренд.
    Если нет — возвращаем пусто (чтобы не показывать ITEM).
    """
    if not caption:
        return ""
    m = HASHTAG_RE.search(caption)
    return m.group(1) if m else ""

def now_ts() -> int:
    return int(time.time())

async def get_best_photo_file_id_and_path(context: ContextTypes.DEFAULT_TYPE, msg) -> Optional[Dict[str, str]]:
    """
    Возвращает file_id и file_path для ЛУЧШЕГО качества фото.
    """
    if not msg.photo:
        return None
    best = msg.photo[-1]  # самое большое
    tg_file = await context.bot.get_file(best.file_id)
    # tg_file.file_path будет вида: photos/file_123.jpg
    return {"file_id": best.file_id, "file_path": tg_file.file_path}

def tg_file_url(file_path: str) -> str:
    """
    Прямая ссылка на файл Telegram (макс качество).
    """
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

def load_by_media_group(media_group_id: str) -> Optional[Dict[str, Any]]:
    """
    Ищем уже существующий товар по media_group_id.
    Берём самый свежий.
    """
    res = (
        supabase.table(TABLE)
        .select("*")
        .eq("media_group_id", media_group_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]
    return None

def save_single_or_album(
    *,
    message_id: int,
    media_group_id: Optional[str],
    caption: str,
    brand: str,
    photo_url: str,
    photo_file_id: str,
) -> None:
    """
    Если есть media_group_id → это альбом: дописываем фото в существующую запись или создаём новую.
    Если нет → одиночное фото: создаём отдельную запись.
    """
    ts = now_ts()

    if media_group_id:
        existing = load_by_media_group(media_group_id)

        if existing:
            # дописываем в file_ids + urls (если у тебя в таблице только file_ids — оставим file_ids)
            file_ids: List[str] = existing.get("file_ids") or []
            if photo_file_id not in file_ids:
                file_ids.append(photo_file_id)

            # caption у альбома обычно только в первом сообщении, но если пришёл позже — обновим, если пусто
            new_caption = existing.get("caption") or caption
            new_brand = existing.get("brand") or brand

            supabase.table(TABLE).update(
                {
                    "file_ids": file_ids,
                    "caption": new_caption,
                    "brand": new_brand,
                    "ts": ts,
                }
            ).eq("id", existing["id"]).execute()
            return

        # если ещё нет записи — создаём новую
        supabase.table(TABLE).insert(
            {
                "message_id": message_id,
                "media_group_id": media_group_id,
                "brand": brand,
                "caption": caption,
                "file_ids": [photo_file_id],
                "ts": ts,
            }
        ).execute()
        return

    # одиночное фото
    supabase.table(TABLE).insert(
        {
            "message_id": message_id,
            "media_group_id": None,
            "brand": brand,
            "caption": caption,
            "file_ids": [photo_file_id],
            "ts": ts,
        }
    ).execute()


# ---------------------------
# Telegram handler
# ---------------------------

async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Ловим только посты канала.
    """
    msg = update.channel_post
    if not msg:
        return

    if not msg.photo:
        return

    caption = msg.caption or ""
    brand = extract_brand(caption)

    best = await get_best_photo_file_id_and_path(context, msg)
    if not best:
        return

    photo_url = tg_file_url(best["file_path"])  # прямая ссылка (макс качество)
    photo_file_id = best["file_id"]

    # Сохраняем: одиночное или альбом
    save_single_or_album(
        message_id=msg.message_id,
        media_group_id=str(msg.media_group_id) if msg.media_group_id else None,
        caption=caption,
        brand=brand,
        photo_url=photo_url,
        photo_file_id=photo_file_id,
    )


# ---------------------------
# Run bot (polling)
# ---------------------------

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & filters.PHOTO, handler))

app.run_polling(allowed_updates=["channel_post"])
