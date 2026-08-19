import asyncio
import os
import sqlite3
import re
import pymorphy3
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
import pymorphy3

morph = pymorphy3.MorphAnalyzer()

BAD_WORDS = {
    "пизд", "хуй", "хуе", "хуя", "бля", "сук", 
    "долбо", "еба", "ёба", "ебн", "пидор", "пидар", "пидр", 
    "казин", "спам"
}

# Теперь здесь только НАЧАЛЬНЫЕ формы слов (именительный падеж, единственное число или инфинитив)
GOOD_WORDS = {
    "оскорблять", "оскорбление", "сабля", "корабль", "рубль", 
    "грабли", "стебель", "гребля", "ансамбль", "дубль", 
    "употреблять", "расслабляться", "влюбляться", "колебание", 
    "колебаться", "амеба", "хлеб", "небо", "погреб", "ширпотреб", 
    "учебный", "учебник", "служебный", "волшебный", "судебный", 
    "лечебный", "целебный", "врачебный", "хвалебный", "ущербный", 
    "потребный", "барсук", "сукно", "сукровица", "суккулент", 
    "сук", "страховать", "скипидар"
}

def normalize_text(text: str) -> str:
    text = text.lower()
    replacements = {
        'a': 'а', 'b': 'б', 'c': 'с', 'd': 'д', 'e': 'е', 
        'i': 'и', 'k': 'к', 'm': 'м', 'o': 'о', 'p': 'р', 
        's': 'с', 't': 'т', 'u': 'у', 'x': 'х', 'y': 'у', 'z': 'з',
        '0': 'о', '3': 'з', '4': 'ч', '6': 'б', '@': 'а', '$': 'с'
    }
    for lat, cyr in replacements.items():
        text = text.replace(lat, cyr)
    text = re.sub(r'[^а-яё\s]', '', text)
    text = re.sub(r'(.)\1+', r'\1', text)
    return text

def basic_filter(text: str) -> bool:
    words = text.split()
    
    for original_word in words:
        # 1. Сначала убираем цифры, латиницу и спецсимволы
        clean_word = normalize_text(original_word)
        if not clean_word:
            continue
            
        # 2. Получаем нормальную форму слова с помощью Pymorphy
        parsed_word = morph.parse(clean_word)[0].normal_form
        
        # 3. Проверяем нормальную форму по белому списку
        if parsed_word in GOOD_WORDS:
            continue
            
        # 4. Если слова нет в белом списке, ищем плохие корни
        for bad_root in BAD_WORDS:
            if bad_root in clean_word:
                return True
                
    return False

# AI-фильтр оставляем как был...


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
