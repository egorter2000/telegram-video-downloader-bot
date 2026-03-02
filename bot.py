import asyncio
import os
import yt_dlp
import uuid
import subprocess
import math
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in .env file")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DOWNLOAD_PATH = "downloads"
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

user_links = {}

# Telegram Bot API limit for uploading files
MAX_FILE_SIZE = 48 * 1024 * 1024  # 48 MB (с запасом)

def get_video_info(url):
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    
    formats = []
    # Фильтруем форматы: только видео с аудио (или комбинированные) в mp4
    for f in info.get("formats", []):
        if f.get("vcodec") != "none" and f.get("height"):
            resolution = f"{f["height"]}p"
            formats.append({
                "format_id": f["format_id"],
                "resolution": resolution,
                "ext": f.get("ext", "mp4"),
                "filesize": f.get("filesize") or f.get("filesize_approx")
            })

    # Убираем дубликаты разрешений, оставляя лучший filesize
    unique = {}
    for f in formats:
        res = f["resolution"]
        if res not in unique or (f["filesize"] and (not unique[res]["filesize"] or f["filesize"] > unique[res]["filesize"])):
            unique[res] = f

    return list(unique.values()), info.get("title", "video")

def download_media(url, format_id, output_template):
    ydl_opts = {
        "format": f"{format_id}+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

def get_duration(file_path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return float(result.stdout)

def split_video(input_path, duration):
    """Разбивает видео на части менее 50МБ"""
    file_size = os.path.getsize(input_path)
    num_parts = math.ceil(file_size / MAX_FILE_SIZE)
    part_duration = duration / num_parts
    
    base_name = os.path.splitext(input_path)[0]
    parts = []
    
    for i in range(num_parts):
        start_time = i * part_duration
        output_part = f"{base_name}_part{i+1}.mp4"
        cmd = [
            "ffmpeg", "-y", "-ss", str(start_time), "-t", str(part_duration),
            "-i", input_path, "-c", "copy", "-map", "0", output_part
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        parts.append(output_part)
    
    return parts

@dp.message(F.text == '/start')
async def send_welcome(message: Message):
    await message.answer(
        "Привет! Я бот для скачивания видео с YouTube, TikTok, RuTube и VK. \n\n" +
        "Просто отправь мне ссылку на видео, и я предложу доступные качества. \n\n" +
        "**Важно:** Если видео очень большое (больше 50 МБ), я автоматически разделю его на несколько частей, чтобы сохранить качество и обойти ограничения Telegram. Не пугайся, это нормально! 😉",
        parse_mode="Markdown"
    )

@dp.message(F.text)
async def handle_link(message: Message):
    url = message.text.strip()
    if not url.startswith("http"):
        await message.answer("Пришли ссылку 🙂")
        return

    msg = await message.answer("🔎 Анализирую ссылку...")
    try:
        formats, title = get_video_info(url)
        if not formats:
            await msg.edit_text("❌ Не удалось найти подходящие форматы видео.")
            return

        key = str(uuid.uuid4())[:8]
        user_links[key] = {"url": url, "title": title}

        buttons = []
        # Сортируем по высоте (разрешению)
        sorted_formats = sorted(formats, key=lambda x: int(x["resolution"][:-1]) if x["resolution"][:-1].isdigit() else 0, reverse=True)
        
        for f in sorted_formats:
            size_str = f" (~{round(f["filesize"]/1024/1024)}MB)" if f["filesize"] else ""
            buttons.append([InlineKeyboardButton(
                text=f"{f["resolution"]}{size_str}",
                callback_data=f"dl|{key}|{f["format_id"]}"
            )])
        
        buttons.append([InlineKeyboardButton(text="🎵 Только Аудио (MP3)", callback_data=f"audio|{key}")])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await msg.edit_text(f"🎬 **{title}**\n\nВыберите качество для скачивания:\n\n" +
                            "_Если видео большое, оно будет отправлено частями для сохранения качества._", reply_markup=keyboard, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error info: {e}")
        await msg.edit_text("❌ Ошибка при получении информации о видео. Возможно, сервис не поддерживается или ссылка недоступна.")

@dp.callback_query(F.data.startswith("dl|") | F.data.startswith("audio|"))
async def process_download(callback: CallbackQuery):
    await callback.answer()
    data = callback.data.split("|")
    action = data[0]
    key = data[1]
    
    link_data = user_links.get(key)
    if not link_data:
        await callback.message.answer("❌ Сессия истекла. Отправьте ссылку снова.")
        return

    url = link_data["url"]
    title = "".join([c for c in link_data["title"] if c.isalnum() or c in (" ", ".", "_")]).strip()
    
    status_msg = await callback.message.answer("⏳ Начинаю загрузку...")
    
    try:
        file_id = str(uuid.uuid4())[:8]
        if action == "dl":
            format_id = data[2]
            output_file = f"{DOWNLOAD_PATH}/{file_id}_{title}.mp4"
            download_media(url, format_id, output_file)
            
            if not os.path.exists(output_file):
                # Попробуем скачать в любом доступном mp4 если не вышло
                download_media(url, "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best", output_file)

            size = os.path.getsize(output_file)
            if size > MAX_FILE_SIZE:
                await status_msg.edit_text("📦 Видео большое, разделяю на части для сохранения качества...")
                duration = get_duration(output_file)
                parts = split_video(output_file, duration)
                
                for i, part in enumerate(parts):
                    await callback.message.answer_video(
                        FSInputFile(part), 
                        caption=f"Часть {i+1} из {len(parts)}: {link_data["title"]}"
                    )
                    os.remove(part)
                os.remove(output_file)
            else:
                await status_msg.edit_text("🚀 Отправляю видео...")
                await callback.message.answer_video(FSInputFile(output_file), caption=link_data["title"])
                os.remove(output_file)
        
        elif action == "audio":
            output_file = f"{DOWNLOAD_PATH}/{file_id}_{title}.mp3"
            ydl_opts = {
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "outtmpl": output_file.replace(".mp3", ".%(ext)s"),
                "quiet": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
            # yt-dlp может добавить расширение к шаблону, проверяем реальный путь
            real_path = output_file if os.path.exists(output_file) else output_file + ".mp3"
            
            await status_msg.edit_text("🚀 Отправляю аудио...")
            await callback.message.answer_audio(FSInputFile(real_path), caption=link_data["title"])
            if os.path.exists(real_path): os.remove(real_path)

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Download error: {e}")
        await status_msg.edit_text(f"❌ Произошла ошибка при скачивании: {str(e)[:100]}")

async def main():
    logger.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
