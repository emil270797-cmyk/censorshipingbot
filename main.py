import asyncio
import os
import sqlite3
import re
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from google import genai

# --- 1. ВЕБ-СЕРВЕР ДЛЯ RENDER ---
app = Flask('')

@app.route('/')
def home():
    return "Бот-модератор работает!"

def run_server():
    # Render автоматически передает порт через переменную PORT
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_server)
    t.start()

# --- 2. БАЗА ДАННЫХ ---
conn = sqlite3.connect('bot_database.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, ai_enabled BOOLEAN DEFAULT FALSE)''')
conn.commit()

def add_chat(chat_id):
    cursor.execute('INSERT OR IGNORE INTO chats (chat_id, ai_enabled) VALUES (?, FALSE)', (chat_id,))
    conn.commit()

def set_ai(chat_id, status):
    cursor.execute('UPDATE chats SET ai_enabled = ? WHERE chat_id = ?', (status, chat_id))
    conn.commit()

def is_ai(chat_id):
    cursor.execute('SELECT ai_enabled FROM chats WHERE chat_id = ?', (chat_id,))
    res = cursor.fetchone()
    return bool(res and res[0])

# --- 3. НАСТРОЙКИ БОТА ---
TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = genai.Client(api_key=GEMINI_KEY)

# --- 4. ФИЛЬТРЫ ---
# Расширяем базовый список
BAD_WORDS = {"спам", "казино", "блять", "сука", "долбоеб", "хуй", "пиздец", "пидор"}

def normalize_text(text: str) -> str:
    text = text.lower()
    
        # 1. Расширенная замена латинских букв и цифр на кириллицу
    replacements = {
        'a': 'а', 'b': 'б', 'c': 'с', 'e': 'е', 'o': 'о', 'p': 'р', 
        'x': 'х', 'y': 'у', 'k': 'к', 'm': 'м', 't': 'т', 'h': 'х',
        '0': 'о', '3': 'з', '4': 'ч', '6': 'б', '@': 'а'
    }

    for lat, cyr in replacements.items():
        text = text.replace(lat, cyr)
        
    # 2. Удаляем все спецсимволы (точки, запятые, слеши), оставляем только буквы и пробелы.
    # Это превратит "д.о.л.б.о.е.б" в "д о л б о е б" или "сука!" в "сука"
    text = re.sub(r'[^а-яё\s]', '', text)
    
    # 3. Схлопываем дубликаты букв (пппиииииииидддоорр -> пидор)
    text = re.sub(r'(.)\1+', r'\1', text)
    
    return text

def basic_filter(text: str) -> bool:
    # Прогоняем текст сообщения через нормализатор
    clean_text = normalize_text(text)
    
    # Ищем плохие слова
    for word in BAD_WORDS:
        # Нормализуем и само плохое слово на всякий случай
        clean_word = normalize_text(word) 
        
        # Проверяем, есть ли корень плохого слова в сообщении
        if clean_word in clean_text:
            return True
            
    return False

# Искусственный интеллект оставляем без изменений, 
# но убираем проверку на длину > 2 слов, чтобы ИИ ловил даже одиночные завуалированные слова
async def ai_filter(text: str) -> bool:
    prompt = f"Ты модератор. Ответь ТОЛЬКО словом BAD (если есть мат, травля, скрытый спам) или OK (если чисто). Текст: '{text}'"
    try:
        res = await client.aio.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        return "BAD" in res.text.strip().upper()
    except Exception as e:
        print(f"Ошибка ИИ: {e}")
        return False


# --- 5. ЛОГИКА ТЕЛЕГРАМ-БОТА ---
@dp.message(Command("start"))
async def start(m: Message):
    await m.answer("Привет! Добавь меня в группу и дай права удалять сообщения.\nКоманда /buy_premium включит ИИ.")

@dp.message(Command("buy_premium"), F.chat.type.in_({"group", "supergroup"}))
async def buy_prem(m: Message):
    add_chat(m.chat.id)
    set_ai(m.chat.id, True)
    await m.answer("✅ <b>Premium активирован!</b> ИИ запущен.", parse_mode="HTML")

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def moderate(m: Message):
    text = m.text or m.caption
    if not text:
        return
    
    add_chat(m.chat.id)

    if basic_filter(text):
        await punish(m, "базовым фильтром")
        return

    if is_ai(m.chat.id):
        if await ai_filter(text):
            await punish(m, "AI-модератором")

async def punish(m: Message, reason: str):
    try:
        await m.delete()
        w = await m.answer(f"🚫 Сообщение удалено {reason}.")
        await asyncio.sleep(5)
        await w.delete()
    except:
        pass

async def main():
    keep_alive()
    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
