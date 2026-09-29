import asyncio
import logging
import os
import html
import time
import urllib.parse
import base64
import re
from datetime import datetime, timedelta

import aiosqlite
import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, BusinessMessagesDeleted, BusinessConnection, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, FSInputFile, BufferedInputFile
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command

# ================= ЗАГРУЗКА НАСТРОЕК ИЗ .env =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

if not BOT_TOKEN or not ADMIN_ID:
    raise ValueError("❌ ОШИБКА: BOT_TOKEN или ADMIN_ID не найдены в файле .env!")

DB_NAME = "business_messages.db"
BOT_USERNAME = "@nodelchat_bot"
# ДОБАВЛЕН НОВЫЙ КАНАЛ В СПИСОК
CHANNELS = ["@xSp1der42", "@neon9_news", "@RiffyOff"] 
BOT_START_TIME = datetime.now()
MESSAGE_RETENTION_TIME = 604800 # 7 дней

RESTART_NOTIFY_TEXT = (
    "🔄 <b>В БОТЕ ВЫШЛО ОБНОВЛЕНИЕ С НОВЫМИ ФИШКАМИ!</b>\n\n"
    "⚙️ <b>Чтобы бот продолжил работать, сделай следующее:</b>\n"
    "1️⃣ Зайди в <b>Настройки Telegram → Telegram для бизнеса → Чат-боты</b>\n"
    f"2️⃣ Найди <code>{BOT_USERNAME}</code> и <b>УДАЛИ ЕГО</b> оттуда\n"
    "3️⃣ Подожди 5-10 секунд\n"
    f"4️⃣ Снова введи <code>{BOT_USERNAME}</code> и нажми <b>Добавить</b>\n"
    "5️⃣ Вернись сюда и напиши /start"
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
router = Router()

# ================= БАЗА ДАННЫХ =================

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS messages_v2 (
            connection_id TEXT, chat_id INTEGER, message_id INTEGER,
            sender_name TEXT, sender_username TEXT, text TEXT,
            date INTEGER, file_id TEXT, content_type TEXT,
            PRIMARY KEY (connection_id, chat_id, message_id))""")
            
        await db.execute("CREATE TABLE IF NOT EXISTS business_connections (connection_id TEXT PRIMARY KEY, user_id INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, first_seen INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, catch_deleted INTEGER DEFAULT 1, catch_edited INTEGER DEFAULT 1, is_paused INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS bot_stats (stat_name TEXT PRIMARY KEY, stat_value INTEGER DEFAULT 0)")
        
        await db.execute("CREATE TABLE IF NOT EXISTS autoreplies (owner_id INTEGER, target_chat_id INTEGER, reply_text TEXT, PRIMARY KEY (owner_id, target_chat_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS user_notes (owner_id INTEGER, note_name TEXT, note_text TEXT, PRIMARY KEY (owner_id, note_name))")
        await db.execute("CREATE TABLE IF NOT EXISTS global_status (owner_id INTEGER PRIMARY KEY, status_text TEXT, is_active INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS keyword_replies (owner_id INTEGER, keyword TEXT, reply_text TEXT, PRIMARY KEY (owner_id, keyword))")
        await db.execute("CREATE TABLE IF NOT EXISTS watched_words (owner_id INTEGER, word TEXT, PRIMARY KEY (owner_id, word))")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS archive_deleted (
            owner_id INTEGER, chat_id INTEGER, sender_name TEXT, 
            text TEXT, date INTEGER, file_id TEXT, content_type TEXT)""")
        
        await db.execute("INSERT OR IGNORE INTO bot_stats (stat_name, stat_value) VALUES ('deleted_caught', 0)")
        await db.execute("INSERT OR IGNORE INTO bot_stats (stat_name, stat_value) VALUES ('edited_caught', 0)")
        
        # Миграция старой таблицы archive_deleted, если нужно добавить колонки для медиа
        try: await db.execute("ALTER TABLE archive_deleted ADD COLUMN file_id TEXT")
        except: pass
        try: await db.execute("ALTER TABLE archive_deleted ADD COLUMN content_type TEXT")
        except: pass

        await db.commit()

async def db_cleanup_task():
    while True:
        try:
            oldest = int(time.time()) - MESSAGE_RETENTION_TIME
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("DELETE FROM messages_v2 WHERE date < ?", (oldest,))
                await db.execute("DELETE FROM archive_deleted WHERE date < ?", (oldest,))
                await db.commit()
        except Exception as e: logging.error(f"Ошибка БД: {e}")
        await asyncio.sleep(43200)

async def translate_text(target_lang: str, text: str) -> str:
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={target_lang}&dt=t&q={urllib.parse.quote(text)}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return "".join([part[0] for part in data[0]])
        return "❌ Ошибка перевода."
    except Exception as e: return f"❌ Ошибка: {e}"

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
async def inc_stat(stat_name: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = ?", (stat_name,))
        await db.commit()

async def save_user(user_id: int, username: str, full_name: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username, full_name, first_seen) VALUES (?, ?, ?, ?)", (user_id, username or "", full_name or "", int(time.time())))
        await db.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def get_user_settings(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT catch_deleted, catch_edited, is_paused FROM user_settings WHERE user_id = ?", (user_id,)) as c:
            row = await c.fetchone()
            return {"deleted": bool(row[0]), "edited": bool(row[1]), "paused": bool(row[2])} if row else {"deleted": True, "edited": True, "paused": False}

async def update_user_setting(user_id: int, setting: str, value: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f"UPDATE user_settings SET {setting} = ? WHERE user_id = ?", (value, user_id))
        await db.commit()

async def check_subscription(bot: Bot, user_id: int) -> bool:
    if user_id == ADMIN_ID: return True
    for channel in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ['left', 'kicked', 'banned']: return False
        except: return False
    return True

async def get_owner_id(bot: Bot, connection_id: str) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM business_connections WHERE connection_id = ?", (connection_id,)) as c:
            row = await c.fetchone()
            if row: return row[0]
                
    try:
        conn = await bot.get_business_connection(connection_id)
        if conn and conn.user:
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("INSERT OR REPLACE INTO business_connections (connection_id, user_id) VALUES (?, ?)", (connection_id, conn.user.id))
                await db.commit()
            return conn.user.id
    except Exception as e:
        logging.error(f"Не удалось получить бизнес-подключение: {e}")
        
    return None

def extract_media(message: Message):
    file_id = None; content_type = message.content_type; text = message.text or message.caption or ""
    if message.photo: file_id = message.photo[-1].file_id
    elif message.video: file_id = message.video.file_id
    elif message.voice: file_id = message.voice.file_id
    elif message.video_note: file_id = message.video_note.file_id
    elif message.document: file_id = message.document.file_id
    elif message.sticker: file_id = message.sticker.file_id
    elif message.animation: file_id = message.animation.file_id
    return file_id, content_type, text

def content_type_emoji(content_type: str) -> str:
    return {"photo": "🖼", "video": "🎥", "voice": "🎤", "video_note": "⭕️", "document": "📎", "sticker": "🎭", "animation": "🎞", "audio": "🎵", "text": "💬"}.get(content_type, "📁")

async def send_media_alert(bot: Bot, target_id: int, file_id: str, content_type: str, caption: str):
    try:
        safe_caption = caption if len(caption) <= 1024 else caption[:1020] + "..."
        if file_id:
            if content_type == 'photo': await bot.send_photo(target_id, file_id, caption=safe_caption)
            elif content_type == 'video': await bot.send_video(target_id, file_id, caption=safe_caption)
            elif content_type == 'voice': await bot.send_voice(target_id, file_id, caption=safe_caption)
            elif content_type == 'document': await bot.send_document(target_id, file_id, caption=safe_caption)
            elif content_type == 'animation': await bot.send_animation(target_id, file_id, caption=safe_caption)
            elif content_type in ['video_note', 'sticker']:
                await bot.send_message(target_id, caption)
                if content_type == 'video_note': await bot.send_video_note(target_id, file_id)
                else: await bot.send_sticker(target_id, file_id)
        else: await bot.send_message(target_id, caption)
    except: await bot.send_message(target_id, f"{caption}\n\n⚠️ <i>[Файл недоступен для пересылки]</i>")

# ================= ОБРАБОТЧИКИ БИЗНЕС-СООБЩЕНИЙ =================

@router.business_connection()
async def on_business_connection(connection: BusinessConnection, bot: Bot):
    await save_user(connection.user.id, connection.user.username, connection.user.full_name)
    async with aiosqlite.connect(DB_NAME) as db:
        if connection.is_enabled:
            await db.execute("INSERT OR REPLACE INTO business_connections (connection_id, user_id) VALUES (?, ?)", (connection.id, connection.user.id))
        else:
            await db.execute("DELETE FROM business_connections WHERE connection_id = ?", (connection.id,))
        await db.commit()

@router.business_message()
async def on_new_business_message(message: Message, bot: Bot):
    owner_id = await get_owner_id(bot, message.business_connection_id)
    if not owner_id: return
    if not await check_subscription(bot, owner_id): return
    
    text_lower = (message.text or message.caption or "").lower()
    text = message.text or message.caption or ""

    # ⚡️ КОМАНДЫ ЮЗЕРБОТА
    if message.from_user.id == owner_id and text.startswith("."):
        try: await message.delete()
        except: pass

        parts = text.split()
        cmd = parts[0].lower()

        try:
            if cmd == ".history" and len(parts) >= 2:
                limit = min(int(parts[1]), 500)
                async with aiosqlite.connect(DB_NAME) as db:
                    async with db.execute("SELECT date, sender_name, text FROM messages_v2 WHERE chat_id = ? ORDER BY date DESC LIMIT ?", (message.chat.id, limit)) as cursor:
                        rows = await cursor.fetchall()
                if not rows: return await bot.send_message(chat_id=message.chat.id, text="📭 История пуста.", business_connection_id=message.business_connection_id)
                history_text = f"История чата {message.chat.id}\n\n"
                for r in reversed(rows):
                    dt = datetime.fromtimestamp(r[0]).strftime('%Y-%m-%d %H:%M')
                    history_text += f"[{dt}] {r[1]}: {r[2] or '<Медиа>'}\n"
                file = BufferedInputFile(history_text.encode('utf-8'), filename=f"history_{message.chat.id}.txt")
                await bot.send_document(chat_id=message.chat.id, document=file, caption=f"📁 Последние {len(rows)} сообщений", business_connection_id=message.business_connection_id)
                return

            elif cmd == ".deleted" and len(parts) >= 2:
                hours = int(parts[1])
                time_limit = int(time.time()) - (hours * 3600)
                async with aiosqlite.connect(DB_NAME) as db:
                    async with db.execute("SELECT date, sender_name, text FROM archive_deleted WHERE owner_id = ? AND chat_id = ? AND date > ? ORDER BY date DESC", (owner_id, message.chat.id, time_limit)) as cursor:
                        rows = await cursor.fetchall()
                if not rows: return await bot.send_message(chat_id=message.chat.id, text=f"🗑 За {hours}ч удаленных сообщений не найдено.", business_connection_id=message.business_connection_id)
                history_text = f"Удаленные сообщения за {hours}ч\n\n"
                for r in reversed(rows):
                    dt = datetime.fromtimestamp(r[0]).strftime('%Y-%m-%d %H:%M')
                    history_text += f"[{dt}] {r[1]}: {r[2] or '<Удалено Медиа>'}\n"
                file = BufferedInputFile(history_text.encode('utf-8'), filename=f"deleted_{message.chat.id}.txt")
                await bot.send_document(chat_id=message.chat.id, document=file, caption=f"🗑 Найдено {len(rows)} удаленных сообщений", business_connection_id=message.business_connection_id)
                return

            elif cmd == ".find" and len(parts) >= 2:
                word = text.split(" ", 1)[1]
                async with aiosqlite.connect(DB_NAME) as db:
                    async with db.execute("SELECT sender_name, text FROM messages_v2 WHERE chat_id = ? AND text LIKE ? LIMIT 15", (message.chat.id, f"%{word}%")) as cursor:
                        rows = await cursor.fetchall()
                if not rows: await bot.send_message(chat_id=message.chat.id, text=f"🔍 Слово <b>{word}</b> не найдено.", business_connection_id=message.business_connection_id)
                else:
                    res = "\n".join([f"👤 {r[0]}: <i>{html.escape(r[1][:50])}...</i>" for r in rows])
                    await bot.send_message(chat_id=message.chat.id, text=f"🔍 <b>Найдено:</b>\n{res}", business_connection_id=message.business_connection_id)
                return

            elif cmd == ".info":
                async with aiosqlite.connect(DB_NAME) as db:
                    async with db.execute("SELECT COUNT(*) FROM messages_v2 WHERE chat_id = ?", (message.chat.id,)) as cursor: count = (await cursor.fetchone())[0]
                await bot.send_message(chat_id=message.chat.id, text=f"ℹ️ <b>Инфо о чате:</b>\nID: <code>{message.chat.id}</code>\nСообщений в базе: {count}", business_connection_id=message.business_connection_id)
                return

            elif cmd == ".calc" and len(parts) >= 2:
                expr = text.split(" ", 1)[1].replace(" ", "")
                if re.match(r'^[0-9+\-*/().]+$', expr):
                    try: result = eval(expr)
                    except: result = "Ошибка вычисления"
                else: result = "Недопустимые символы (только цифры и + - * /)"
                await bot.send_message(message.chat.id, f"🧮 <b>Результат:</b> {result}", business_connection_id=message.business_connection_id); return

            elif cmd == ".qr" and len(parts) >= 2:
                qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=512x512&data={urllib.parse.quote(text.split(' ', 1)[1])}"
                await bot.send_photo(message.chat.id, photo=qr_url, caption="📲 Твой QR-код", business_connection_id=message.business_connection_id); return

            elif cmd == ".short" and len(parts) >= 2:
                api_url = f"https://tinyurl.com/api-create.php?url={urllib.parse.quote(text.split(' ', 1)[1])}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(api_url) as resp: short_url = await resp.text() if resp.status == 200 else "Ошибка"
                await bot.send_message(message.chat.id, f"🔗 <b>Короткая ссылка:</b>\n{short_url}", business_connection_id=message.business_connection_id); return

            elif cmd == ".unshort" and len(parts) >= 2:
                short_url = text.split(" ", 1)[1]
                if not short_url.startswith("http"): short_url = "https://" + short_url
                async with aiohttp.ClientSession() as session:
                    async with session.get(short_url, allow_redirects=True) as resp: final_url = str(resp.url)
                await bot.send_message(message.chat.id, f"🕵️‍♂️ <b>Оригинальная ссылка:</b>\n{final_url}", business_connection_id=message.business_connection_id); return

            elif cmd == ".time": await bot.send_message(message.chat.id, f"🕒 {datetime.now().strftime('%H:%M:%S')}", business_connection_id=message.business_connection_id); return
            elif cmd == ".date": await bot.send_message(message.chat.id, f"📅 {datetime.now().strftime('%d.%m.%Y')}", business_connection_id=message.business_connection_id); return

            elif cmd == ".bold": await bot.send_message(message.chat.id, f"<b>{html.escape(text.split(' ', 1)[1])}</b>", business_connection_id=message.business_connection_id); return
            elif cmd == ".italic": await bot.send_message(message.chat.id, f"<i>{html.escape(text.split(' ', 1)[1])}</i>", business_connection_id=message.business_connection_id); return
            elif cmd == ".mono": await bot.send_message(message.chat.id, f"<code>{html.escape(text.split(' ', 1)[1])}</code>", business_connection_id=message.business_connection_id); return
            
            elif cmd == ".b64en" and len(parts) >= 2:
                await bot.send_message(message.chat.id, f"🔐 <code>{base64.b64encode(text.split(' ', 1)[1].encode()).decode()}</code>", business_connection_id=message.business_connection_id); return
            elif cmd == ".b64de" and len(parts) >= 2:
                try: res = base64.b64decode(text.split(" ", 1)[1].encode()).decode()
                except: res = "❌ Ошибка декодирования"
                await bot.send_message(message.chat.id, f"🔓 {res}", business_connection_id=message.business_connection_id); return

            elif cmd == ".status":
                if len(parts) == 2 and parts[1].lower() == "off":
                    async with aiosqlite.connect(DB_NAME) as db:
                        await db.execute("UPDATE global_status SET is_active = 0 WHERE owner_id = ?", (owner_id,))
                        await db.commit()
                    await bot.send_message(owner_id, "🟢 <b>Глобальный статус отключен.</b>")
                else:
                    status_text = text.split(" ", 1)[1]
                    async with aiosqlite.connect(DB_NAME) as db:
                        await db.execute("INSERT OR REPLACE INTO global_status (owner_id, status_text, is_active) VALUES (?, ?, 1)", (owner_id, status_text))
                        await db.commit()
                    await bot.send_message(owner_id, f"🔴 <b>Глобальный статус включен!</b>\nТеперь всем пишущим будет отвечать:\n<i>{status_text}</i>")
                return

            elif cmd == ".kw_add" and "->" in text:
                kw, reply = text.replace(".kw_add ", "").split("->", 1)
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("INSERT OR REPLACE INTO keyword_replies (owner_id, keyword, reply_text) VALUES (?, ?, ?)", (owner_id, kw.strip().lower(), reply.strip()))
                    await db.commit()
                await bot.send_message(owner_id, f"✅ Автоответ на <b>{kw.strip()}</b> добавлен!")
                return
                
            elif cmd == ".kw_del" and len(parts) >= 2:
                kw = text.split(" ", 1)[1].lower()
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("DELETE FROM keyword_replies WHERE owner_id = ? AND keyword = ?", (owner_id, kw))
                    await db.commit()
                await bot.send_message(owner_id, f"🗑 Автоответ на <b>{kw}</b> удален!")
                return
                
            elif cmd == ".kw_list":
                async with aiosqlite.connect(DB_NAME) as db:
                    async with db.execute("SELECT keyword, reply_text FROM keyword_replies WHERE owner_id = ?", (owner_id,)) as cursor:
                        rows = await cursor.fetchall()
                if not rows: await bot.send_message(owner_id, "📭 У тебя нет триггеров автоответа.")
                else:
                    res = "\n".join([f"• <b>{r[0]}</b> -> {r[1]}" for r in rows])
                    await bot.send_message(owner_id, f"🤖 <b>Твои автоответы:</b>\n{res}")
                return

            elif cmd == ".watch" and len(parts) >= 2:
                word = parts[1].lower()
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("INSERT OR IGNORE INTO watched_words (owner_id, word) VALUES (?, ?)", (owner_id, word))
                    await db.commit()
                await bot.send_message(owner_id, f"👁 <b>Наблюдение включено!</b>\nЯ сообщу, если кто-то напишет слово: <code>{word}</code>")
                return
            elif cmd == ".unwatch" and len(parts) >= 2:
                word = parts[1].lower()
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("DELETE FROM watched_words WHERE owner_id = ? AND word = ?", (owner_id, word))
                    await db.commit()
                await bot.send_message(owner_id, f"👁 Наблюдение за <code>{word}</code> отключено.")
                return
            elif cmd == ".watch_list":
                async with aiosqlite.connect(DB_NAME) as db:
                    async with db.execute("SELECT word FROM watched_words WHERE owner_id = ?", (owner_id,)) as cursor: rows = await cursor.fetchall()
                if not rows: await bot.send_message(owner_id, "📭 Ты ни за чем не наблюдаешь.")
                else:
                    res = ", ".join([f"<code>{r[0]}</code>" for r in rows])
                    await bot.send_message(owner_id, f"👁 <b>Ты следишь за словами:</b>\n{res}")
                return

            elif cmd == ".remind" and len(parts) >= 3:
                time_str = parts[1]; remind_text = text.split(" ", 2)[2]; multiplier = 60
                if time_str.endswith("h"): multiplier = 3600; time_str = time_str[:-1]
                elif time_str.endswith("m"): time_str = time_str[:-1]
                seconds = float(time_str) * multiplier
                async def send_later(c_id, b_id, txt, delay_sec):
                    await asyncio.sleep(delay_sec)
                    try: await bot.send_message(chat_id=c_id, text=f"⏰ Напоминание:\n{txt}", business_connection_id=b_id)
                    except: pass
                asyncio.create_task(send_later(message.chat.id, message.business_connection_id, remind_text, seconds))
                await bot.send_message(owner_id, f"⏳ Запланировано через {time_str} {'часов' if multiplier==3600 else 'минут'}.")
                return

            elif cmd == ".spam" and len(parts) >= 3:
                count = min(int(parts[1]), 50)
                spam_text = text.split(" ", 2)[2]
                for _ in range(count):
                    await bot.send_message(chat_id=message.chat.id, text=spam_text, business_connection_id=message.business_connection_id)
                    await asyncio.sleep(0.4)
                return

            elif cmd == ".boom" and len(parts) >= 3:
                sec = min(int(parts[1]), 300)
                boom_text = text.split(" ", 2)[2]
                sent_msg = await bot.send_message(chat_id=message.chat.id, text=boom_text, business_connection_id=message.business_connection_id)
                async def destroy_msg(c_id, m_id, b_id, delay):
                    await asyncio.sleep(delay)
                    try: await bot.edit_message_text("💥 <i>[Сообщение уничтожено]</i>", chat_id=c_id, message_id=m_id, business_connection_id=b_id)
                    except: pass
                asyncio.create_task(destroy_msg(message.chat.id, sent_msg.message_id, message.business_connection_id, sec))
                return

            elif cmd == ".tr" and len(parts) >= 3:
                translated = await translate_text(parts[1].lower(), text.split(" ", 2)[2])
                await bot.send_message(chat_id=message.chat.id, text=translated, business_connection_id=message.business_connection_id); return

            elif cmd == ".save" and len(parts) >= 3:
                note_name = parts[1].lower()
                note_text = text.split(" ", 2)[2]
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("INSERT OR REPLACE INTO user_notes (owner_id, note_name, note_text) VALUES (?, ?, ?)", (owner_id, note_name, note_text))
                    await db.commit()
                await bot.send_message(owner_id, f"✅ Заметка <code>.{note_name}</code> сохранена!")
                return
            elif cmd == ".del" and len(parts) >= 2:
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("DELETE FROM user_notes WHERE owner_id = ? AND note_name = ?", (owner_id, parts[1].lower()))
                    await db.commit()
                await bot.send_message(owner_id, f"🗑 Заметка <code>.{parts[1].lower()}</code> удалена."); return
            elif cmd == ".notes":
                async with aiosqlite.connect(DB_NAME) as db:
                    async with db.execute("SELECT note_name FROM user_notes WHERE owner_id = ?", (owner_id,)) as cursor: notes = await cursor.fetchall()
                if notes: await bot.send_message(owner_id, f"🗂 <b>Шаблоны:</b>\n" + "\n".join([f"• <code>.{n[0]}</code>" for n in notes]))
                else: await bot.send_message(owner_id, "🤷‍♂️ У тебя пока нет шаблонов.")
                return

            elif cmd == ".auto" and len(parts) >= 2:
                reply_text = text.split(" ", 1)[1]
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("INSERT OR REPLACE INTO autoreplies (owner_id, target_chat_id, reply_text) VALUES (?, ?, ?)", (owner_id, message.chat.id, reply_text))
                    await db.commit()
                await bot.send_message(owner_id, f"✅ <b>Автоответчик включен</b>!\nТекст: {reply_text}"); return
            elif cmd == ".autostop":
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("DELETE FROM autoreplies WHERE owner_id = ? AND target_chat_id = ?", (owner_id, message.chat.id))
                    await db.commit()
                await bot.send_message(owner_id, "❌ <b>Автоответчик выключен</b>."); return
            else:
                async with aiosqlite.connect(DB_NAME) as db:
                    async with db.execute("SELECT note_text FROM user_notes WHERE owner_id = ? AND note_name = ?", (owner_id, cmd.replace(".", ""))) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            await bot.send_message(chat_id=message.chat.id, text=row[0], business_connection_id=message.business_connection_id)
                            return

        except Exception as e:
            logging.error(f"Ошибка юзербота: {e}")
            return

    # ⚡️ АНАЛИЗ ВХОДЯЩИХ
    if message.from_user.id != owner_id:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT reply_text FROM autoreplies WHERE owner_id = ? AND target_chat_id = ?", (owner_id, message.chat.id)) as c:
                row = await c.fetchone()
                if row:
                    try: await bot.send_message(chat_id=message.chat.id, text=row[0], business_connection_id=message.business_connection_id)
                    except: pass
                    return

            async with db.execute("SELECT status_text FROM global_status WHERE owner_id = ? AND is_active = 1", (owner_id,)) as c:
                status = await c.fetchone()
                if status:
                    try: await bot.send_message(message.chat.id, f"🤖 [Автоответ]: {status[0]}", business_connection_id=message.business_connection_id)
                    except: pass
            
            async with db.execute("SELECT keyword, reply_text FROM keyword_replies WHERE owner_id = ?", (owner_id,)) as c:
                for kw, reply in await c.fetchall():
                    if kw in text_lower:
                        try: await bot.send_message(message.chat.id, reply, business_connection_id=message.business_connection_id)
                        except: pass
                        break

            async with db.execute("SELECT word FROM watched_words WHERE owner_id = ?", (owner_id,)) as c:
                for (w,) in await c.fetchall():
                    if w in text_lower:
                        await bot.send_message(owner_id, f"👁 <b>Сработал триггер!</b>\nПользователь <b>{message.from_user.full_name}</b> написал слово <code>{w}</code>.\n\nТекст: <i>{text}</i>")

    # ⚡️ ПЕРЕХВАТ
    settings = await get_user_settings(owner_id)
    if settings['paused']: return

    file_id, content_type, text_content = extract_media(message)
    s_name = message.from_user.full_name if message.from_user else "Неизвестный"
    s_uname = message.from_user.username or ""

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO messages_v2 (connection_id, chat_id, message_id, sender_name, sender_username, text, date, file_id, content_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.business_connection_id, message.chat.id, message.message_id, s_name, s_uname, text_content, int(message.date.timestamp()), file_id, content_type)
        )
        await db.commit()

@router.edited_business_message()
async def on_edited_business_message(message: Message, bot: Bot):
    owner_id = await get_owner_id(bot, message.business_connection_id)
    if not owner_id or not await check_subscription(bot, owner_id): return
    settings = await get_user_settings(owner_id)
    if settings['paused'] or not settings['edited']: return

    new_file_id, new_content_type, new_text = extract_media(message)
    author_str = f"{message.from_user.full_name}" + (f" (@{message.from_user.username})" if message.from_user.username else "")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT text, file_id, content_type FROM messages_v2 WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
                              (message.business_connection_id, message.chat.id, message.message_id)) as cursor:
            row = await cursor.fetchone()

        old_text = row[0] if row else "[Текст не сохранен]"
        old_file_id = row[1] if row else None
        old_content_type = row[2] if row else "text"

        await db.execute("UPDATE messages_v2 SET text = ?, file_id = ?, content_type = ? WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
                         (new_text, new_file_id, new_content_type, message.business_connection_id, message.chat.id, message.message_id))
        await db.commit()

    if old_text == new_text and old_file_id == new_file_id: return

    await inc_stat("edited_caught")
    safe_old = html.escape(old_text)[:400] + ("..." if len(old_text or "")>400 else "") if old_text else ""
    safe_new = html.escape(new_text)[:400] + ("..." if len(new_text or "")>400 else "") if new_text else ""

    caption = f"✏️ <b>{author_str} ИЗМЕНИЛ(А):</b>\n\n<b>Было:</b>\n"
    if safe_old: caption += f"<blockquote>{safe_old}</blockquote>\n"
    elif old_file_id: caption += f"<i>[Медиа: {old_content_type}]</i>\n"
    caption += f"\n<b>Стало:</b>\n"
    if safe_new: caption += f"<blockquote>{safe_new}</blockquote>\n"
    elif new_file_id: caption += f"<i>[Медиа: {new_content_type}]</i>\n"

    await send_media_alert(bot, owner_id, new_file_id or old_file_id, new_content_type if new_file_id else old_content_type, caption)

@router.deleted_business_messages()
async def on_deleted_business_messages(deleted: BusinessMessagesDeleted, bot: Bot):
    owner_id = await get_owner_id(bot, deleted.business_connection_id)
    if not owner_id or not await check_subscription(bot, owner_id): return
    settings = await get_user_settings(owner_id)
    if settings['paused'] or not settings['deleted']: return

    async with aiosqlite.connect(DB_NAME) as db:
        for msg_id in deleted.message_ids:
            async with db.execute("SELECT sender_name, sender_username, text, file_id, content_type, date FROM messages_v2 WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
                                  (deleted.business_connection_id, deleted.chat.id, msg_id)) as cursor:
                row = await cursor.fetchone()

            if row:
                s_name, s_uname, text, file_id, c_type, msg_date = row
                
                await db.execute("INSERT INTO archive_deleted (owner_id, chat_id, sender_name, text, date, file_id, content_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (owner_id, deleted.chat.id, s_name, text, msg_date, file_id, c_type))

                author = f"{s_name}" + (f" (@{s_uname})" if s_uname else "")
                safe_text = html.escape(text) if text else ""
                
                caption = f"🗑 <b>{author} УДАЛИЛ(А):</b>\n\n"
                if safe_text: caption += f"{content_type_emoji(c_type)} <blockquote>{safe_text}</blockquote>"
                elif file_id: caption += f"{content_type_emoji(c_type)} <i>[Удален файл: {c_type}]</i>"

                await send_media_alert(bot, owner_id, file_id, c_type, caption)
                await inc_stat("deleted_caught")
                await db.execute("DELETE FROM messages_v2 WHERE connection_id = ? AND chat_id = ? AND message_id = ?", (deleted.business_connection_id, deleted.chat.id, msg_id))
        await db.commit()

# ================= ОБРАБОТЧИКИ UI В ЛИЧНЫХ СООБЩЕНИЯХ =================

def get_main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Инструкция (ВАЖНО!)", callback_data="tab_instructions")],
        [InlineKeyboardButton(text="⚡️ Что умеет бот?", callback_data="tab_features")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="tab_settings")]
    ])

def get_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_main")]
    ])

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    await save_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    if not await check_subscription(bot, message.from_user.id):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Канал 1", url="https://t.me/xSp1der42"), InlineKeyboardButton(text="📢 Канал 2", url="https://t.me/neon9_news")],
            [InlineKeyboardButton(text="📢 Канал 3", url="https://t.me/RiffyOff")],
            [InlineKeyboardButton(text="🔄 Проверить подписку", callback_data="check_sub")]
        ])
        text = "🔓 <b>Бот ПОЛНОСТЬЮ БЕСПЛАТНЫЙ!</b>\n\nНо для доступа к его функциям, пожалуйста, подпишись на каналы наших спонсоров:"
        return await message.answer(text, reply_markup=keyboard)

    welcome = (
        f"👋 <b>Привет! Я {BOT_USERNAME} — твой личный шпион и ассистент.</b>\n\n"
        "Главная моя фишка: <b>я перехватываю удаленные и измененные сообщения</b> в твоих переписках (включая фото, видео и кружочки)!\n\n"
        "Выбери нужный раздел ниже 👇"
    )
    if message.from_user.id == ADMIN_ID:
        welcome += "\n\n🛠 <b>Админ команды:</b> /stats, /backup, /updatenotify"
        
    await message.answer(welcome, reply_markup=get_main_menu_kb())

@router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, bot: Bot):
    if await check_subscription(bot, call.from_user.id):
        await call.message.delete()
        await cmd_start(call.message, bot)
    else:
        await call.answer("❌ Вы не подписались на все каналы!", show_alert=True)

@router.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery):
    text = (
        f"👋 <b>Главное меню</b>\n\n"
        "Главная моя фишка: <b>я перехватываю удаленные и измененные сообщения</b> в твоих переписках!\n\n"
        "Выбери нужный раздел ниже 👇"
    )
    await call.message.edit_text(text, reply_markup=get_main_menu_kb())

@router.callback_query(F.data == "tab_instructions")
async def cb_tab_instructions(call: CallbackQuery):
    text = (
        "📖 <b>КАК УСТАНОВИТЬ БОТА (ИНСТРУКЦИЯ)</b>\n\n"
        "Чтобы я начал перехватывать сообщения и отвечать на твои команды, сделай следующее:\n\n"
        "1️⃣ Открой <b>Настройки Telegram</b>\n"
        "2️⃣ Перейди в раздел <b>Telegram для бизнеса</b> (нужен Premium)\n"
        "3️⃣ Выбери <b>Чат-боты</b>\n"
        f"4️⃣ Введи мой юзернейм: <code>{BOT_USERNAME}</code>\n"
        "5️⃣ Нажми <b>Добавить</b>\n\n"
        "🚨 <b>САМОЕ ВАЖНОЕ (ЧТОБЫ РАБОТАЛИ КОМАНДЫ):</b>\n"
        "После добавления убедись, что галочка <b>«Может отвечать на сообщения»</b> ВКЛЮЧЕНА!\n"
        "Также в настройках чатов для бота нужно выбрать <b>«Все чаты»</b> (или те, которые нужны). Если не дать боту права отвечать, ты не сможешь спамить, писать команды и использовать автоответчик!"
    )
    await call.message.edit_text(text, reply_markup=get_back_button())

@router.callback_query(F.data == "tab_features")
async def cb_tab_features(call: CallbackQuery):
    text = (
        "⚡️ <b>ПОЛНЫЙ ФУНКЦИОНАЛ (СКРЫТЫЕ КОМАНДЫ)</b>\n\n"
        "Вместо тебя на эти команды будет отвечать бот (а твое сообщение с командой он попытается удалить, чтобы было беспалевно).\n\n"
        
        "🔎 <b>История, Поиск и Архивы (Шпионские фишки)</b>\n"
        "• <code>.history 50</code> — Скачивает текстовый файл с последними 50 сообщениями из этого чата (максимум 500).\n"
        "• <code>.deleted 24</code> — Скачивает файл со всеми удаленными сообщениями в этом чате за последние 24 часа.\n"
        "• <code>.find слово</code> — Ищет конкретное слово в переписке (выводит до 15 совпадений).\n"
        "• <code>.info</code> — Показывает ID чата и сколько всего сообщений из него сохранено в базе.\n\n"
        
        "🤖 <b>Умные автоответы и Триггеры</b>\n"
        "• <code>.status Я сплю, пиши завтра</code> — <b>Глобальный автоответчик.</b> Бот будет отвечать этой фразой вообще всем, кто тебе напишет в личку.\n"
        "• <code>.status off</code> — Выключить глобальный автоответчик.\n"
        "• <code>.auto Я сейчас занят</code> — <b>Локальный автоответчик.</b> Бот будет отвечать только в этом конкретном чате.\n"
        "• <code>.autostop</code> — Выключить автоответчик для этого чата.\n"
        "• <code>.kw_add привет -> Привет, как дела?</code> — Добавляет триггер. Если человек напишет слово \"привет\", бот сам ответит \"Привет, как дела?\".\n"
        "• <code>.kw_del привет</code> — Удалить триггер на это слово.\n"
        "• <code>.kw_list</code> — Посмотреть список всех твоих триггеров.\n\n"
        
        "👁 <b>Режим наблюдателя (Watch)</b>\n"
        "• <code>.watch дурак</code> — Бот будет следить за всеми твоими чатами. Если кто-то напишет это слово, бот пришлет тебе в ЛС уведомление: кто и где это сказал.\n"
        "• <code>.unwatch дурак</code> — Перестать следить за этим словом.\n"
        "• <code>.watch_list</code> — Список слов, за которыми ты следишь.\n\n"
        
        "🛠 <b>Полезные утилиты</b>\n"
        "• <code>.calc 1000-7</code> — Калькулятор (выдаст результат).\n"
        "• <code>.tr en Привет брат</code> — Переводчик. Переведет текст на английский (можно писать ru, uk, de и т.д.).\n"
        "• <code>.qr Мой текст</code> — Сгенерирует и пришлет картинку с QR-кодом твоего текста.\n"
        "• <code>.short https://длинная.ком</code> — Сократит ссылку.\n"
        "• <code>.unshort https://tinyurl.com/xyz</code> — Узнать, куда ведет короткая ссылка (анти-скам).\n"
        "• <code>.time</code> — Текущее время.\n"
        "• <code>.date</code> — Текущая дата.\n\n"
        
        "💥 <b>Развлечения и Спам</b>\n"
        "• <code>.boom 10 Текст</code> — Отправит сообщение, а через 10 секунд изменит его на \"💥 [Сообщение уничтожено]\".\n"
        "• <code>.spam 10 Текст</code> — Отправит этот текст 10 раз подряд (максимум 50).\n"
        "• <code>.action typing 30</code> — Бот будет показывать собеседнику \"печатает...\" в течение 30 секунд. (Доступные действия: typing, voice, video).\n\n"
        
        "🗂 <b>Шаблоны (Заметки)</b>\n"
        "• <code>.save card 42761111...</code> — Сохранит текст в шаблон с названием <b>card</b>.\n"
        "• <code>.card</code> — Бот отправит сохраненный текст. Очень удобно для частых ответов!\n"
        "• <code>.del card</code> — Удалить этот шаблон.\n"
        "• <code>.notes</code> — Посмотреть список всех твоих шаблонов.\n\n"
        
        "📝 <b>Форматирование текста</b>\n"
        "• <code>.bold Текст</code> — Сделает текст жирным.\n"
        "• <code>.italic Текст</code> — Сделает текст курсивом.\n"
        "• <code>.mono Текст</code> — Сделает текст моноширинным (удобно для копирования).\n"
        "• <code>.clown Привет как дела</code> — Сделает текст вОт ТаКиМ.\n"
        "• <code>.anticaps ПРИВЕТ</code> — Исправит капс (сделает \"Привет\").\n"
        "• <code>.b64en Секрет</code> / <code>.b64de Код</code> — Зашифрует/Расшифрует текст в формат Base64."
    )
    await call.message.edit_text(text, reply_markup=get_back_button())

async def render_settings_menu(user_id: int):
    settings = await get_user_settings(user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 Перехват удаленных: {'✅' if settings['deleted'] else '❌'}", callback_data="set_deleted")],
        [InlineKeyboardButton(text=f"✏️ Перехват измененных: {'✅' if settings['edited'] else '❌'}", callback_data="set_edited")],
        [InlineKeyboardButton(text=f"{'⏸ Поставить слежку на паузу' if not settings['paused'] else '▶️ Снять с паузы'}", callback_data="set_paused")],
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_main")]
    ])
    return kb

@router.callback_query(F.data == "tab_settings")
async def cb_tab_settings(call: CallbackQuery):
    await call.message.edit_text("⚙️ <b>Настройки перехватчика:</b>", reply_markup=await render_settings_menu(call.from_user.id))

@router.message(Command("settings"))
async def cmd_settings(message: Message):
    await message.answer("⚙️ <b>Настройки перехватчика:</b>", reply_markup=await render_settings_menu(message.from_user.id))

@router.callback_query(F.data.startswith("set_"))
async def cb_settings(call: CallbackQuery):
    setting_type = call.data.replace("set_", "")
    settings = await get_user_settings(call.from_user.id)
    if setting_type == "deleted": await update_user_setting(call.from_user.id, "catch_deleted", int(not settings['deleted']))
    elif setting_type == "edited": await update_user_setting(call.from_user.id, "catch_edited", int(not settings['edited']))
    elif setting_type == "paused": await update_user_setting(call.from_user.id, "is_paused", int(not settings['paused']))
    
    await call.answer("Настройки обновлены!")
    await call.message.edit_reply_markup(reply_markup=await render_settings_menu(call.from_user.id))

# ================= АДМИН КОМАНДЫ =================
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c: total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM business_connections") as c: active = (await c.fetchone())[0]
        async with db.execute("SELECT stat_value FROM bot_stats WHERE stat_name = 'deleted_caught'") as c: r = await c.fetchone(); deleted = r[0] if r else 0
        async with db.execute("SELECT stat_value FROM bot_stats WHERE stat_name = 'edited_caught'") as c: r = await c.fetchone(); edited = r[0] if r else 0
        
        # Детальная стата БД
        async with db.execute("SELECT COUNT(*) FROM messages_v2") as c: db_size = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM archive_deleted") as c: arc_size = (await c.fetchone())[0]
        
        # Медиа стата
        async with db.execute("SELECT COUNT(*) FROM messages_v2 WHERE content_type = 'photo'") as c: photo_c = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM messages_v2 WHERE content_type = 'video_note'") as c: vn_c = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM messages_v2 WHERE content_type = 'voice'") as c: voice_c = (await c.fetchone())[0]

        # Стата юзербота
        async with db.execute("SELECT COUNT(*) FROM user_notes") as c: notes_c = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM keyword_replies") as c: kw_c = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM global_status WHERE is_active=1") as c: status_c = (await c.fetchone())[0]

    delta = datetime.now() - BOT_START_TIME
    
    text = (
        "📈 <b>РАЗВЕРНУТАЯ СТАТИСТИКА:</b>\n\n"
        f"👥 <b>Пользователи:</b>\n"
        f"Всего юзеров: {total}\n"
        f"Активных подключений: {active}\n\n"
        f"🕵️‍♂️ <b>Работа перехватчика:</b>\n"
        f"Удалено сообщений: {deleted}\n"
        f"Изменено сообщений: {edited}\n\n"
        f"💾 <b>База данных:</b>\n"
        f"Кэш сообщений: {db_size}\n"
        f"Архив удаленных: {arc_size}\n"
        f"📸 Перехвачено фото: {photo_c}\n"
        f"⭕️ Перехвачено кружков: {vn_c}\n"
        f"🎤 Перехвачено войсов: {voice_c}\n\n"
        f"🤖 <b>Модули юзербота:</b>\n"
        f"Создано шаблонов: {notes_c}\n"
        f"Триггеров автоответа: {kw_c}\n"
        f"Включено статусов: {status_c}\n\n"
        f"⏳ <b>Аптайм:</b> {delta.days}д {delta.seconds//3600}ч {(delta.seconds//60)%60}м"
    )
    await message.answer(text)

@router.message(Command("backup"))
async def cmd_backup(message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer_document(FSInputFile(DB_NAME), caption="📦 Бекап базы данных")

@router.message(Command("updatenotify"))
async def cmd_update_notify(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("⏳ Рассылаю уведомление об обновлении всем юзерам...")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor: users = await cursor.fetchall()
    
    success, failed = 0, 0
    for (user_id,) in users:
        try: await bot.send_message(user_id, RESTART_NOTIFY_TEXT); success += 1; await asyncio.sleep(0.05)
        except: failed += 1

    await message.answer(f"✅ <b>Рассылка завершена!</b>\nДоставлено: {success}\nОшибок: {failed}")

# ================= СЕРВЕР И ЗАПУСК =================
async def handle_ping(request): return web.Response(text="Бот работает!")

async def main():
    await init_db()
    asyncio.create_task(db_cleanup_task())
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🚀 Бот запущен!")
    try: await dp.start_polling(bot, allowed_updates=["message", "business_connection", "business_message", "edited_business_message", "deleted_business_messages", "callback_query"])
    finally: await bot.session.close(); await runner.cleanup()

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logging.info("Остановка.")