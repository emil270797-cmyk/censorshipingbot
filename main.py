import asyncio
import os
import re
import aiohttp
import psycopg2
import pymorphy3
import hmac
import hashlib
import json
import time

from psycopg2 import pool
from contextlib import contextmanager
from urllib.parse import parse_qsl, unquote
from functools import wraps
from datetime import timedelta, datetime
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, LabeledPrice, PreCheckoutQuery, ChatPermissions, 
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, 
    ChatMemberUpdated, WebAppInfo
)

# --- 1. НАСТРОЙКИ БОТА И API ---
TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

# Инициализация и запуск Gemini
import google.generativeai as genai
genai.configure(api_key=GEMINI_KEY)

# --- УЗНАЕМ ДОСТУПНЫЕ МОДЕЛИ ---
print("=== ДОСТУПНЫЕ МОДЕЛИ GEMINI ===", flush=True)
try:
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(m.name, flush=True)
except Exception as e:
    print(f"Ошибка получения списка: {e}", flush=True)
print("===============================", flush=True)

# Пока ставим любое название, чтобы код прошел дальше
model = genai.GenerativeModel('gemini-3.8-flash')
chat_session = model.start_chat(history=[])

bot = Bot(token=TOKEN)
dp = Dispatcher()
flood_cache = {} # Словарь для отслеживания активности пользователей


# --- 2. БАЗА ДАННЫХ И ПУЛ СОЕДИНЕНИЙ ---
DB_URL = os.environ.get("DATABASE_URL")

# 1. Создаем пул на 20 одновременных подключений
db_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=20,
    dsn=DB_URL
)

# 2. Создаем удобный "менеджер контекста" для получения курсора
@contextmanager
def get_db():
    """Выдает соединение из пула и возвращает его обратно после использования."""
    conn = db_pool.getconn()
    try:
        yield conn, conn.cursor()
    finally:
        db_pool.putconn(conn)

# --- 3. ИНИЦИАЛИЗАЦИЯ ТАБЛИЦ ---
def init_db():
    """Проверяет и создает все нужные таблицы при запуске бота."""
    with get_db() as (local_conn, local_cursor):
        # Базовые таблицы
        local_cursor.execute('''CREATE TABLE IF NOT EXISTS chats_v2 (
            chat_id BIGINT PRIMARY KEY, 
            ai_enabled BOOLEAN DEFAULT FALSE,
            premium_until DOUBLE PRECISION DEFAULT 0,
            chat_title TEXT
        )''')
        
        local_cursor.execute('''CREATE TABLE IF NOT EXISTS warns (
            user_id BIGINT, 
            chat_id BIGINT, 
            count INTEGER,
            UNIQUE(user_id, chat_id)
        )''')

        local_cursor.execute('''CREATE TABLE IF NOT EXISTS stats (
            chat_id BIGINT PRIMARY KEY, 
            deleted_count INTEGER DEFAULT 0, 
            mute_count INTEGER DEFAULT 0,
            ai_requests INTEGER DEFAULT 0
        )''')

        local_cursor.execute('''CREATE TABLE IF NOT EXISTS moderation_logs (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT,
            user_id BIGINT,
            user_name TEXT,
            reason TEXT,
            action_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        local_cursor.execute('''CREATE TABLE IF NOT EXISTS chat_admins (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT,
            admin_id BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, admin_id)
        )''')

        local_cursor.execute('''CREATE TABLE IF NOT EXISTS processed_txs (
            tx_hash TEXT PRIMARY KEY,
            chat_id BIGINT
        )''')

        # Таблица платежей Stars
        local_cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                telegram_payment_charge_id VARCHAR(255) UNIQUE NOT NULL,
                user_id BIGINT NOT NULL,
                payload VARCHAR(255) NOT NULL,
                amount INTEGER NOT NULL,
                currency VARCHAR(10) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        # Таблица подписок
        local_cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                owner_id BIGINT PRIMARY KEY,
                premium_until DOUBLE PRECISION DEFAULT 0,
                slots INT DEFAULT 3
            );
        """)
        
        local_conn.commit()

        # Безопасное добавление колонок (если они еще не существуют)
        try:
            local_cursor.execute('ALTER TABLE chats_v2 ADD COLUMN IF NOT EXISTS chat_title TEXT')
            local_cursor.execute('ALTER TABLE chats_v2 ADD COLUMN IF NOT EXISTS owner_id BIGINT')
            local_cursor.execute("ALTER TABLE chats_v2 ADD COLUMN IF NOT EXISTS added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")
            local_cursor.execute('ALTER TABLE stats ADD COLUMN IF NOT EXISTS ai_requests INTEGER DEFAULT 0')
            
            # --- ДОБАВЛЯЕМ НЕДОСТАЮЩИЕ КОЛОНКИ ДЛЯ ЛИМИТОВ ИИ ---
            local_cursor.execute('ALTER TABLE chats_v2 ADD COLUMN IF NOT EXISTS ai_requests_today INTEGER DEFAULT 0')
            local_cursor.execute('ALTER TABLE chats_v2 ADD COLUMN IF NOT EXISTS last_request_date DATE DEFAULT CURRENT_DATE')
            
            local_conn.commit()
        except Exception as e:
            local_conn.rollback() # Откат в случае, если колонки уже есть, чтобы транзакция не зависла
            print(f"Ошибка при обновлении таблиц: {e}")

# Вызываем функцию создания таблиц сразу при старте файла
init_db()

# --- 4. ФУНКЦИИ РАБОТЫ С БД ---
def add_chat(chat_id, title="Без названия"):
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('''
            INSERT INTO chats_v2 (chat_id, ai_enabled, premium_until, chat_title) 
            VALUES (%s, FALSE, 0, %s) 
            ON CONFLICT (chat_id) 
            DO UPDATE SET chat_title = EXCLUDED.chat_title WHERE EXCLUDED.chat_title IS NOT NULL
        ''', (chat_id, title))
        local_conn.commit()

def check_chat_premium(chat_id):
    """Проверяет, действует ли премиум-подписка для чата"""
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('SELECT premium_until, ai_enabled FROM chats_v2 WHERE chat_id = %s', (chat_id,))
        res = local_cursor.fetchone()
        
        if not res:
            return False
        
        premium_until, ai_enabled = res
        current_time = time.time()
        
        # Если время подписки истекло, а ИИ был включен — выключаем его автоматически
        if premium_until < current_time and ai_enabled:
            local_cursor.execute('UPDATE chats_v2 SET ai_enabled = FALSE WHERE chat_id = %s', (chat_id,))
            local_conn.commit()
            return False
            
        return premium_until >= current_time

def set_ai(chat_id, status, days=30):
    until = (datetime.now() + timedelta(days=days)).timestamp() if status else 0
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('UPDATE chats_v2 SET ai_enabled = %s, premium_until = %s WHERE chat_id = %s', (status, until, chat_id))
        local_conn.commit()

def is_ai(chat_id):
    DAILY_AI_LIMIT = 1000
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('''
            SELECT c.ai_enabled, u.premium_until, c.ai_requests_today, c.last_request_date
            FROM chats_v2 c
            LEFT JOIN user_subscriptions u ON c.owner_id = u.owner_id
            WHERE c.chat_id = %s
        ''', (chat_id,))
        res = local_cursor.fetchone()
        
        if res and res[0]:
            premium_until = res[1] or 0
            requests_today = res[2] or 0
            last_date = res[3]
            current_date = datetime.now().date()

            if datetime.now().timestamp() > premium_until:
                local_cursor.execute('UPDATE chats_v2 SET ai_enabled = FALSE WHERE chat_id = %s', (chat_id,))
                local_conn.commit()
                return False

            if last_date != current_date:
                requests_today = 0
                local_cursor.execute('UPDATE chats_v2 SET ai_requests_today = 0, last_request_date = CURRENT_DATE WHERE chat_id = %s', (chat_id,))
                local_conn.commit()

            if requests_today >= DAILY_AI_LIMIT:
                return False
                
            local_cursor.execute('UPDATE chats_v2 SET ai_requests_today = ai_requests_today + 1 WHERE chat_id = %s', (chat_id,))
            local_conn.commit()
            return True

        return False

def add_warn(user_id, chat_id):
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('''
            SELECT count FROM warns WHERE user_id = %s AND chat_id= %s
            ''', (user_id, chat_id))
        res = local_cursor.fetchone()
        if res:
            count = res[0] + 1
            local_cursor.execute('UPDATE warns SET count = %s WHERE user_id = %s AND chat_id = %s', (count, user_id, chat_id))
        else:
            count = 1
            local_cursor.execute('''
                    INSERT INTO warns (user_id, chat_id, count) VALUES (%s, %s, %s)
                ''', (user_id, chat_id, count))
        local_conn.commit()
        return count

def is_chat_premium(chat_id: int) -> bool:
    """Проверяет, есть ли у чата активный ИИ и оплачена ли подписка у его владельца."""
    current_time = time.time()
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('''
            SELECT c.ai_enabled, u.premium_until 
            FROM chats_v2 c
            LEFT JOIN user_subscriptions u ON c.owner_id = u.owner_id
            WHERE c.chat_id = %s
        ''', (chat_id,))
        res = local_cursor.fetchone()
        
        if res and res[0] and res[1] and res[1] > current_time:
            return True
    return False

def reset_warns(user_id, chat_id):
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('DELETE FROM warns WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))
        local_conn.commit()

def record_stat(chat_id, stat_type):
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('INSERT INTO stats (chat_id, deleted_count, mute_count, ai_requests) VALUES (%s, 0, 0, 0) ON CONFLICT (chat_id) DO NOTHING', (chat_id,))
        if stat_type == 'delete':
            local_cursor.execute('UPDATE stats SET deleted_count = deleted_count + 1 WHERE chat_id = %s', (chat_id,))
        elif stat_type == 'mute':
            local_cursor.execute('UPDATE stats SET mute_count = mute_count + 1 WHERE chat_id = %s', (chat_id,))
        elif stat_type == 'ai':
            local_cursor.execute('UPDATE stats SET ai_requests = ai_requests + 1 WHERE chat_id = %s', (chat_id,))
        local_conn.commit()

def get_stats(chat_id):
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('SELECT deleted_count, mute_count, ai_requests FROM stats WHERE chat_id = %s', (chat_id,))
        res = local_cursor.fetchone()
        return res if res else (0, 0, 0)


# --- 3. БАЗОВЫЙ ФИЛЬТР (МАТ И СЛОВАРЬ) ---
morph = pymorphy3.MorphAnalyzer()

BAD_WORDS = {
    "пизд", "хуй", "хуе", "хуя", "бля", "сук", 
    "долбо", "еба", "ёба", "ебн", "пидор", "пидар", "пидр", 
    "казин", "спам"
}

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
        clean_word = normalize_text(original_word)
        if not clean_word:
            continue
        parsed_word = morph.parse(clean_word)[0].normal_form
        if parsed_word in GOOD_WORDS:
            continue
        for bad_root in BAD_WORDS:
            if bad_root in clean_word:
                return True
    return False


# --- 4. ФУНКЦИЯ ИИ-МОДЕРАЦИИ (Gemini REST API) ---

async def ai_filter(text: str, author_name: str, chat_id: int, message_id: int) -> str:
    """
    Отправляет текст в Gemini.
    Возвращает строку: "ok", "spam", "toxic", "obscene" или "error".
    """
    # === ЗАЩИТА ОТ РАЗДУВАНИЯ ТОКЕНОВ ===
    if not text:
        return "safe"
        
    # Обрезаем сообщение до 2000 символов (этого более чем достаточно для чата)
    safe_text = text[:2000]
    # ====================================
    try:
        # ОБЕЗЛИЧЕННЫЙ ЛОГ ЗАПРОСА
        print(f"🧠 AI moderation request | chat_id: {chat_id} | message_id: {message_id}", flush=True)
        
        # ТЕХНИЧЕСКИЙ ЩИТ: скрываем личные данные ДО отправки в нейросеть
        # Сначала заменяем телефоны в оригинальном тексте (создаем safe_text)
        safe_text = re.sub(r'\+?[\d\-\(\)\s]{10,15}', '[ТЕЛЕФОН]', text)
        
        # Затем заменяем email-адреса уже в безопасном тексте
        safe_text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-zA-Z]{2,}\b', '[EMAIL]', safe_text)
        
        prompt = f"""
        Проверь это сообщение от пользователя "{author_name}". 
        Твоя задача — классифицировать его. 
        Ответь ТОЛЬКО ОДНИМ СЛОВОМ из списка:
        ok - обычное сообщение
        spam - реклама, ссылки на каналы, призывы подписаться, заработок
        toxic - агрессия, оскорбления (прямые или скрытые), травля
        obscene - мат, завуалированный мат, непристойности

        Сообщение: {safe_text}
        """

        response = chat_session.send_message(prompt)
        result = response.text.strip().lower()

        # Очищаем ответ от лишних знаков препинания, если ИИ вдруг их добавит
        result = ''.join(c for c in result if c.isalpha())

        # Проверка, чтобы ИИ не выдал отсебятину
        valid_responses = ["ok", "spam", "toxic", "obscene"]
        if result not in valid_responses:
            result = "error"

        # ОБЕЗЛИЧЕННЫЙ ЛОГ ОТВЕТА
        print(f"🤖 AI moderation result | chat_id: {chat_id} | message_id: {message_id} | result: {result}", flush=True)
        return result

    except Exception as e:
        # В случае ошибки тоже не выводим сам текст
        print(f"⚠️ Ошибка Gemini | chat_id: {chat_id} | message_id: {message_id} | Error: {e}", flush=True)
        return "error"

@dp.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def handle_group_messages(m: Message):
    user_id = m.from_user.id
    chat_id = m.chat.id
    current_time = time.time()
    
    # === 1. RATE LIMITING (АНТИ-ФЛУД С ПРИВЯЗКОЙ К ЧАТУ) ===
    cache_key = (chat_id, user_id) # Ключ теперь уникален для парой (чат + юзер)
    
    if cache_key in flood_cache:
        last_time, msg_count = flood_cache[cache_key]
        if current_time - last_time < 2:  # Если прошло меньше 2 секунд
            if msg_count >= 3:            # И отправлено больше 3 сообщений
                try:
                    await m.delete()      # Молча удаляем флуд
                except:
                    pass
                return                    # ПРЕРЫВАЕМ обработку
            else:
                flood_cache[cache_key] = (last_time, msg_count + 1)
        else:
            flood_cache[cache_key] = (current_time, 1)
    else:
        flood_cache[cache_key] = (current_time, 1)

    # === 2. ПРОВЕРКА СООБЩЕНИЯ (МОДЕРАЦИЯ) ===
    reason = None
    
    # Сначала проверяем, включен ли ИИ и есть ли лимиты
    if is_ai(chat_id):
        # Отправляем в Gemini
        ai_result = await ai_filter(m.text, m.from_user.full_name, chat_id, m.message_id)
        
        if ai_result in ["spam", "toxic", "obscene"]:
            reason = ai_result
        elif ai_result == "error":
            # Если Gemini упал (например, лимиты Google), используем базовый фильтр как запасной
            if basic_filter(m.text):
                reason = "obscene (словарный фильтр)"
    else:
        # Если ИИ выключен, проверяем только по нашему словарю
        if basic_filter(m.text):
            reason = "obscene (словарный фильтр)"

    # === 3. НАКАЗАНИЕ И ЗАПИСЬ ЛОГОВ ===
    if reason:
        # Удаляем плохое сообщение
        try:
            await m.delete()
        except:
            pass # Бот может не иметь прав на удаление
            
        # Добавляем предупреждение пользователю (функция add_warn уже использует get_db)
        warns = add_warn(user_id, chat_id)
        action_taken = "deleted"
        
        # Если это третье нарушение — выдаем мут на 1 час
        if warns >= 3:
            try:
                until_date = int(time.time()) + 3600
                await bot.restrict_chat_member(
                    chat_id, 
                    user_id, 
                    permissions=ChatPermissions(can_send_messages=False), 
                    until_date=until_date
                )
                action_taken = "muted"
                reset_warns(user_id, chat_id) # Сбрасываем варны после мута
            except Exception as e:
                print(f"Не удалось выдать мут: {e}")

        # ЗАПИСЬ В ЛОГИ БД (Используем наш новый пул соединений!)
        try:
            with get_db() as (local_conn, local_cursor):
                local_cursor.execute("""
                    INSERT INTO moderation_logs (chat_id, user_id, user_name, reason, action_type)
                    VALUES (%s, %s, %s, %s, %s)
                """, (chat_id, user_id, m.from_user.full_name, reason, action_taken))
                local_conn.commit()
        except Exception as e:
            print(f"Ошибка записи лога: {e}")
            
        # Обновляем статистику (функция record_stat тоже использует get_db)
        record_stat(chat_id, "delete" if action_taken == "deleted" else "mute")

        # Отправляем сервисное сообщение в чат
        if action_taken == "muted":
            msg = await m.answer(f"🚫 Пользователь <b>{m.from_user.full_name}</b> получил мут на 1 час.\nПричина: {reason}.", parse_mode="HTML")
        else:
            msg = await m.answer(f"⚠️ Сообщение от <b>{m.from_user.full_name}</b> удалено.\nПричина: {reason}. Предупреждение {warns}/3.", parse_mode="HTML")
            
        # Удаляем сервисное сообщение через 5 секунд, чтобы не засорять чат
        await asyncio.sleep(5)
        try:
            await msg.delete()
        except:
            pass


# --- 5. КОМАНДЫ ПОЛЬЗОВАТЕЛЕЙ И АДМИНОВ ---

@dp.message(Command("start"))
async def cmd_start(m: Message):
    # Проверяем, что команда вызвана в личных сообщениях с ботом
    if m.chat.type != "private":
        return

    admin_id = m.from_user.id
    
    # Ищем привязанные чаты владельца через безопасный пул соединений
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('SELECT chat_id FROM chat_admins WHERE admin_id = %s', (admin_id,))
        chats = local_cursor.fetchall()

    # Оставляем ТОЛЬКО кнопку Личного кабинета
    inline_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🎛 Открыть Личный Кабинет", 
                web_app=WebAppInfo(url="https://emil270797-cmyk.github.io/censorshipingbot/")
            )
        ]
    ])

    text = (
        "👋 <b>Добро пожаловать! Я — умный AI-модератор.</b>\n\n"
        "Моя задача — автоматически очищать ваши чаты и комментарии в каналах от скрытого спама, рекламы и токсичных пользователей с помощью нейросети.\n\n"
        "🛠 <b>КАК НАЧАТЬ РАБОТУ:</b>\n"
        "<b>1.</b> Добавьте меня в вашу группу (или в привязанный чат канала для комментариев).\n"
        "<b>2.</b> Выдайте мне права администратора: <i>«Удаление сообщений»</i> и <i>«Блокировка пользователей»</i>. Без них я не смогу наводить порядок!\n\n"
        "📌 <b>ОСНОВНЫЕ КОМАНДЫ (вводить в самой группе):</b>\n"
        "🔹 /status — проверить, активна ли защита в чате.\n"
        "🔹 /stats — посмотреть статистику удаленного мусора.\n\n"
        "👇 <b>Управление подпиской и ИИ:</b>"
    )

    if chats:
        text += "\n\n🎛 <b>Ваши привязанные чаты:</b>\n"
        for row in chats:
            c_id = row[0]
            text += f"• Чат ID: <code>{c_id}</code>\n"
    else:
        text += "\n\n📭 У вас пока нет привязанных чатов."
        
    await m.answer(text, reply_markup=inline_keyboard, parse_mode="HTML")

# Ловим события добавления бота в группу или выдачи ему прав
@dp.my_chat_member()
async def bot_added_to_chat(event: ChatMemberUpdated):
    if event.new_chat_member.status in ['member', 'administrator']:
        chat_id = str(event.chat.id)
        admin_id = event.from_user.id
        chat_title = event.chat.title or "Без названия"
        
        try:
            # Безопасная запись с пулом соединений
            with get_db() as (local_conn, local_cursor):
                # 1. Сохраняем или обновляем чат в chats_v2 СРАЗУ с owner_id
                local_cursor.execute(
                    """INSERT INTO chats_v2 (chat_id, chat_title, owner_id, ai_enabled) 
                       VALUES (%s, %s, %s, FALSE)
                       ON CONFLICT (chat_id) 
                       DO UPDATE SET owner_id = EXCLUDED.owner_id, chat_title = EXCLUDED.chat_title""",
                    (chat_id, chat_title, admin_id)
                )
                
                # 2. Оставляем вашу таблицу chat_admins (если она нужна для других фич)
                local_cursor.execute(
                    '''INSERT INTO chat_admins (chat_id, admin_id) 
                       VALUES (%s, %s) 
                       ON CONFLICT (chat_id, admin_id) DO NOTHING''',
                    (chat_id, admin_id)
                )
                local_conn.commit()
                
            print(f"✅ Авто-привязка: Чат '{chat_title}' ({chat_id}) закреплен за владельцем {admin_id}")
        except Exception as e:
            # Если произойдет ошибка, менеджер контекста get_db() сам безопасно закроет и откатит транзакцию
            print(f"❌ Ошибка авто-привязки: {e}")
        

# --- 6. ОБРАБОТЧИКИ НАЖАТИЙ НА КНОПКИ МЕНЮ И КОМАНДЫ ---

@dp.message(Command("privacy"))
async def cmd_privacy(m: Message):
    await m.answer(
        "🛡 <b>Политика конфиденциальности:</b>\n\n"
        "1. Сообщения из чата проходят автоматическую обработку ИИ (Gemini).\n"
        "2. Мы <b>не храним</b> тексты ваших сообщений в базах данных.\n"
        "3. Перед анализом номера телефонов и email-адреса автоматически скрываются локальным фильтром.\n"
        "4. Логи нарушений (имя пользователя и причина) хранятся не более 30 дней для статистики администратора.",
        parse_mode="HTML"
    )

@dp.message(Command("report"))
async def send_report(m: Message):
    chat_id = m.chat.id
    
    # Безопасное чтение статистики через пул соединений
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('''
            SELECT reason, COUNT(*) 
            FROM moderation_logs 
            WHERE chat_id = %s
            GROUP BY reason
        ''', (chat_id,))
        stats = local_cursor.fetchall()
        
        local_cursor.execute('''
            SELECT user_name, action_type, reason 
            FROM moderation_logs 
            WHERE chat_id = %s 
            ORDER BY created_at DESC 
            LIMIT 5
        ''', (chat_id,))
        recent_logs = local_cursor.fetchall()
    
    if not stats:
        await m.answer("📭 В этом чате пока нет записей о нарушениях.")
        return

    total_bans = sum([row[1] for row in stats])
    
    text = f"📋 **Отчет модерации для этого чата**\n\n"
    text += f"🛡 Всего отражено угроз: **{total_bans}**\n"
    
    for row in stats:
        reason_name = row[0]
        count = row[1]
        text += f"├ {reason_name}: {count}\n"
        
    text += "\n👤 **Последние нарушители:**\n"
    for log in recent_logs:
        user, action, reason = log
        text += f"• `{user}` — {action} *(причина: {reason})*\n"
        
    text += "\n💡 *Ваш чат под защитой нейросети.*"
    
    await m.answer(text, parse_mode="Markdown")

@dp.message(Command("status"), F.chat.type.in_({"group", "supergroup"}))
async def chat_status(m: Message):
    current_time = time.time()
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('''
            SELECT c.ai_enabled, u.premium_until 
            FROM chats_v2 c
            LEFT JOIN user_subscriptions u ON c.owner_id = u.owner_id
            WHERE c.chat_id = %s
        ''', (m.chat.id,))
        res = local_cursor.fetchone()
    
    if res and res[0] and res[1] and res[1] > current_time:
        end_date = datetime.fromtimestamp(res[1]).strftime('%d.%m.%Y %H:%M')
        await m.answer(
            f"🌟 <b>Статус чата:</b> PREMIUM\n"
            f"🧠 <b>Нейросеть (Gemini):</b> Активна\n"
            f"⏳ <b>Оплачено до:</b> {end_date}",
            parse_mode="HTML"
        )
    else:
        await m.answer(
            f"🌑 <b>Статус чата:</b> Базовый\n"
            f"🤖 <b>Фильтр:</b> Стандартный словарный\n"
            f"💡 Чтобы включить ИИ-модерацию, используйте Личный Кабинет",
            parse_mode="HTML"
        )

@dp.message(Command("unwarn"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_unwarn(m: Message):
    admins = await m.chat.get_administrators()
    if m.from_user.id not in [admin.user.id for admin in admins]:
        await m.answer("❌ Эта команда доступна только администраторам.")
        return

    if not m.reply_to_message:
        await m.answer("⚠️ Чтобы снять предупреждения, ответьте этой командой на сообщение пользователя.")
        return

    target_user = m.reply_to_message.from_user
    try:
        reset_warns(target_user.id, m.chat.id) # Функция reset_warns уже безопасна
        await m.answer(f"✅ Предупреждения пользователя <b>{target_user.first_name}</b> обнулены.", parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при снятии варна: {e}")

@dp.message(Command("unmute"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_unmute(m: Message):
    admins = await m.chat.get_administrators()
    if m.from_user.id not in [
        admin.user.id for admin in admins
    ]:
        await m.answer("❌ Эта команда доступна только администраторам.")
        return

    if not m.reply_to_message:
        await m.answer("⚠️ Чтобы снять мут, ответьте этой командой на сообщение пользователя.")
        return
    
    target_user = m.reply_to_message.from_user
    
    try:
        permissions = ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True
        )
        await bot.restrict_chat_member(chat_id=m.chat.id, user_id=target_user.id, permissions=permissions)
        reset_warns(target_user.id, m.chat.id) # Функция reset_warns уже безопасна
        await m.answer(f"🔊 Мут снят! <b>{target_user.first_name}</b> снова может писать сообщения.", parse_mode="HTML")
    except Exception as e:
        await m.answer("❌ Не удалось снять мут. Возможно, этот пользователь не в муте, или у бота не хватает прав.")
        print(f"Ошибка при снятии мута: {e}")

@dp.message(Command("stats"), F.chat.type.in_({"group", "supergroup"}))
async def show_stats(m: Message):
    admins = await m.chat.get_administrators()
    if m.from_user.id not in [admin.user.id for admin in admins]:
        await m.answer("❌ Эта команда доступна только администраторам чата.")
        return

    d_count, m_count, ai_reqs = get_stats(m.chat.id) # Функция get_stats уже безопасна
    await m.answer(
        f"📊 <b>Статистика модерации:</b>\n\n"
        f"🗑 Удалено сообщений: <b>{d_count}</b>\n"
        f"🤐 Выдано мутов: <b>{m_count}</b>",
        parse_mode="HTML"
    )


# --- 6. ПАНЕЛЬ ВЛАДЕЛЬЦА И ОПЛАТА PREMIUM ---
from aiogram.filters import Command
import time
import os
from datetime import datetime

# Ваш Telegram ID (оставил как у вас, чуть причесал)
OWNER_ID = int(os.environ.get("ADMIN_ID", 354584527))
MY_ADMIN_ID = OWNER_ID 

@dp.message(Command("botstats"))
async def cmd_botstats(m: Message):
    if m.from_user.id != OWNER_ID: return
        
    try:
        # Быстро открыли БД, прочитали цифры, закрыли
        with get_db() as (local_conn, local_cursor):
            local_cursor.execute('SELECT COUNT(*) FROM chats_v2')
            total_chats = local_cursor.fetchone()[0]
            
            current_time = datetime.now().timestamp()
            local_cursor.execute('SELECT COUNT(*) FROM chats_v2 WHERE ai_enabled = TRUE AND premium_until > %s', (current_time,))
            premium_chats = local_cursor.fetchone()[0]
            
        basic_chats = total_chats - premium_chats
        
        await m.answer(
            f"📈 <b>Глобальная статистика проекта:</b>\n\n"
            f"👥 Всего чатов с ботом: <b>{total_chats}</b>\n"
            f"🌑 На базовом тарифе: <b>{basic_chats}</b>\n"
            f"🌟 С активным Premium: <b>{premium_chats}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await m.answer(f"❌ Ошибка при получении статистики: {e}")

@dp.message(Command("chatlist"))
async def cmd_chatlist(m: Message, bot: Bot):
    if m.from_user.id != OWNER_ID: return

    # 1. Быстро получаем данные из БД и сразу освобождаем соединение
    with get_db() as (local_conn, local_cursor):
        local_cursor.execute('''
            SELECT c.chat_id, c.ai_enabled, c.premium_until, COALESCE(s.ai_requests, 0)
            FROM chats_v2 c
            LEFT JOIN stats s ON c.chat_id = s.chat_id
            ORDER BY COALESCE(s.ai_requests, 0) DESC
        ''')
        all_chats = local_cursor.fetchall()
    
    if not all_chats:
        await m.answer("Список чатов пока пуст.")
        return
        
    await m.answer("⏳ Собираю аналитику по чатам...")
    text = "📊 <b>Аналитика чатов (по расходу ИИ):</b>\n\n"
    current_time = datetime.now().timestamp()
    
    active_count = 0
    dead_chats = [] # Сюда соберем ID чатов, откуда бота кикнули
    
    # 2. Спокойно общаемся с API Telegram (БД в это время свободна!)
    for chat_id, ai_enabled, premium_until, ai_reqs in all_chats:
        if active_count >= 20:
            break
            
        try:
            chat_info = await bot.get_chat(chat_id)
            chat_name = chat_info.title or "Без названия"
            status = "🌟 PREMIUM" if (ai_enabled and premium_until and premium_until > current_time) else "🌑 Базовый"
            
            text += f"🔹 <b>{chat_name}</b>\n"
            text += f"├ ID: <code>{chat_id}</code>\n"
            text += f"├ Статус: {status}\n"
            text += f"└ Запросов к ИИ: <b>{ai_reqs}</b>\n\n"

            active_count += 1
            
        except Exception:
            # Если бот удален, запоминаем ID
            dead_chats.append(chat_id)
            continue
            
    # 3. Если есть "мертвые" чаты, быстро открываем БД и удаляем их пачкой
    if dead_chats:
        with get_db() as (local_conn, local_cursor):
            for dc_id in dead_chats:
                local_cursor.execute('DELETE FROM chats_v2 WHERE chat_id = %s', (dc_id,))
                local_cursor.execute('DELETE FROM stats WHERE chat_id = %s', (dc_id,))
            local_conn.commit()
            
    if active_count == 0:
        text = "К сожалению, бот был удален из всех известных чатов."
        
    try:
        await m.answer(text, parse_mode="HTML")
    except Exception as e:
        await m.answer(f"❌ Ошибка отправки списка: {e}")
 


@dp.message(Command("givepro"))
async def cmd_give_pro(message: Message):
    # Бот реагирует только на сообщения от владельца
    if message.from_user.id != MY_ADMIN_ID:
        return
    
    args = message.text.split()
    if len(args) != 4:
        await message.answer("⚠️ Неверный формат!\nПишите так: `/givepro <id> <дней> <слотов>`\nПример: `/givepro 354584527 30 3`", parse_mode="Markdown")
        return
        
    try:
        target_id = int(args[1])
        days = int(args[2])
        slots = int(args[3])
    except ValueError:
        await message.answer("❌ Ошибка: ID, количество дней и слотов должны быть целыми числами.")
        return

    # Защита от дурака (валидация диапазонов)
    if target_id <= 0:
        await message.answer("❌ ID пользователя должен быть положительным числом.")
        return
        
    if not (1 <= days <= 3650):  # от 1 дня до 10 лет
        await message.answer("❌ Количество дней должно быть в диапазоне от 1 до 3650.")
        return
        
    if not (1 <= slots <= 100):  # от 1 до 100 слотов
        await message.answer("❌ Количество слотов должно быть в диапазоне от 1 до 100.")
        return

    current_time = int(time.time())
    premium_until = current_time + (days * 24 * 60 * 60)
    
    try:
        # Безопасная запись с пулом соединений
        with get_db() as (local_conn, local_cursor):
            local_cursor.execute(
                """INSERT INTO user_subscriptions (owner_id, premium_until, slots) 
                   VALUES (%s, %s, %s)
                   ON CONFLICT (owner_id) 
                   DO UPDATE SET premium_until = EXCLUDED.premium_until, slots = EXCLUDED.slots""",
                (target_id, premium_until, slots)
            )
            local_conn.commit()
            
        await message.answer(f"✅ Готово! Пользователю `{target_id}` выдана PRO-подписка на {days} дней. Слотов: {slots}.", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка базы данных: {e}")

@dp.message(Command("checkpay"))
async def cmd_checkpay(m: Message):
    if m.from_user.id != MY_ADMIN_ID: 
        return
        
    args = m.text.split()
    if len(args) != 2:
        await m.answer("⚠️ Укажите ID пользователя: `/checkpay 123456789`", parse_mode="Markdown")
        return
        
    try:
        target_id = int(args[1])
        with get_db() as (local_conn, local_cursor):
            # Проверяем подписку
            local_cursor.execute("SELECT premium_until, slots FROM user_subscriptions WHERE owner_id = %s", (target_id,))
            sub = local_cursor.fetchone()
            
            # Проверяем последние 5 платежей
            local_cursor.execute("""
                SELECT amount, currency, telegram_payment_charge_id, created_at 
                FROM payments 
                WHERE user_id = %s 
                ORDER BY created_at DESC LIMIT 5
            """, (target_id,))
            pays = local_cursor.fetchall()
            
        # Формируем ответ
        text = f"👤 <b>Пользователь:</b> {target_id}\n\n"
        
        if sub:
            until_date = datetime.fromtimestamp(sub[0]).strftime('%d.%m.%Y %H:%M') if sub[0] > 0 else "Нет"
            text += f"🌟 <b>PRO-статус:</b> до {until_date}\n"
            text += f"📦 <b>Слоты:</b> {sub[1]}\n\n"
        else:
            text += "🌟 <b>PRO-статус:</b> Записей нет\n\n"
            
        text += "💳 <b>Последние платежи:</b>\n"
        if pays:
            for p in pays:
                date_str = p[3].strftime('%d.%m %H:%M')
                text += f"• {p[0]} {p[1]} ({date_str})\n  Чек: {p[2]}\n"
        else:
            text += "Платежей не найдено."
            
        await m.answer(text, parse_mode="HTML")
    except ValueError:
        await m.answer("❌ Ошибка: ID должен быть числом.")
    except Exception as e:
        await m.answer(f"❌ Ошибка БД: {e}")

@dp.message(Command("give_premium"))
async def cmd_give_premium(m: Message):
    if m.from_user.id != OWNER_ID: return
    
    args = m.text.split()
    
    if len(args) < 2:
        await m.answer("⚠️ <b>Ошибка:</b> Укажите ID чата.\nПример: <code>/give_premium -100123456789</code>", parse_mode="HTML")
        return
        
    try:
        target_chat_id = int(args[1])
        future_time = (datetime.now() + timedelta(days=30)).timestamp()
        
        # Безопасная запись с пулом соединений (и добавленным commit!)
        with get_db() as (local_conn, local_cursor):
            local_cursor.execute('UPDATE chats_v2 SET ai_enabled = TRUE, premium_until = %s WHERE chat_id = %s', (future_time, target_chat_id))
            local_conn.commit() 
            
        await m.answer(f"✅ <b>Успешно!</b>\nPremium на 30 дней выдан чату: <code>{target_chat_id}</code>", parse_mode="HTML")
        
    except ValueError:
        await m.answer("❌ ID чата должен быть числом.")
    except Exception as e:
        await m.answer(f"❌ Ошибка при выдаче Premium: {e}")

@dp.message(Command("buy_premium"), F.chat.type.in_({"group", "supergroup"}))
async def send_invoice(m: Message):
    prices = [LabeledPrice(label="Premium AI Модератор", amount=100)] 
    await bot.send_invoice(
        chat_id=m.chat.id,
        title="Premium AI Модератор",
        description="Включение нейросети Gemini для точного распознавания скрытой агрессии и завуалированного мата на 30 дней.",
        payload="premium_activation",
        provider_token="",
        currency="XTR",
        prices=prices
    )

from aiogram.types import PreCheckoutQuery, Message
from aiogram import F
import psycopg2
import time

# 1. СТРОГИЙ ОБРАБОТЧИК PRE-CHECKOUT
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    # Проверяем, наш ли это payload (Тут база данных не нужна, оставляем как есть)
    if not pre_checkout_query.invoice_payload.startswith("sub_stars_"):
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Неизвестный товар. Пожалуйста, перезапустите приложение.")
        return
        
    # Проверяем валюту (Telegram Stars)
    if pre_checkout_query.currency != "XTR":
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Оплата принимается только в Telegram Stars.")
        return
        
    # Проверяем точную сумму (100 Stars)
    if pre_checkout_query.total_amount != 100:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Неверная сумма платежа. Попробуйте еще раз.")
        return

    # Если всё идеально, разрешаем оплату
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

# 2. ЕДИНСТВЕННЫЙ ОБРАБОТЧИК УСПЕШНОГО ПЛАТЕЖА
@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment_info = message.successful_payment
    charge_id = payment_info.telegram_payment_charge_id
    user_id = message.from_user.id
    payload = payment_info.invoice_payload
    
    # Открываем единое соединение для всей цепочки обработки платежа
    with get_db() as (local_conn, local_cursor):
        
        # 1. Защита от дублей: Пытаемся записать транзакцию в БД
        try:
            local_cursor.execute("""
                INSERT INTO payments (telegram_payment_charge_id, user_id, payload, amount, currency)
                VALUES (%s, %s, %s, %s, %s)
            """, (charge_id, user_id, payload, payment_info.total_amount, payment_info.currency))
            local_conn.commit()
        except psycopg2.IntegrityError:
            local_conn.rollback() # Очищаем транзакцию перед возвратом в пул
            # Платёж с таким ID уже был обработан! Игнорируем дубль.
            print(f"Дубль платежа перехвачен: {charge_id}")
            return
        except Exception as e:
            local_conn.rollback() # Очищаем транзакцию перед возвратом в пул
            print(f"Ошибка БД при записи платежа: {e}")
            return

        # 2. Если запись прошла успешно, выдаем товар
        if payload.startswith("sub_stars_"):
            owner_id = int(payload.split("_")[2]) # Извлекаем ID из sub_stars_123456
            current_time = time.time()
            
            # Проверяем текущую подписку
            local_cursor.execute("SELECT premium_until, slots FROM user_subscriptions WHERE owner_id = %s", (owner_id,))
            sub_row = local_cursor.fetchone()
            
            if sub_row and sub_row[0] > current_time:
                # Продлеваем текущую
                new_until = sub_row[0] + (30 * 24 * 3600)
                new_slots = sub_row[1] + 3
                local_cursor.execute("UPDATE user_subscriptions SET premium_until = %s, slots = %s WHERE owner_id = %s", 
                               (new_until, new_slots, owner_id))
            else:
                # Создаем новую
                new_until = current_time + (30 * 24 * 3600)
                new_slots = 3
                local_cursor.execute("""
                    INSERT INTO user_subscriptions (owner_id, premium_until, slots)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (owner_id) 
                    DO UPDATE SET premium_until = EXCLUDED.premium_until, slots = EXCLUDED.slots
                """, (owner_id, new_until, new_slots))
                
            local_conn.commit()
            
    # Сообщение юзеру отправляем уже вне блока with, чтобы как можно быстрее освободить базу данных
    await message.answer("🎉 Оплата успешно получена! Вам добавлено 3 слота на 30 дней. Можете включать ИИ в Личном кабинете!")



# --- 7. ОСНОВНОЙ ПРОЦЕСС МОДЕРАЦИИ И ФОНОВЫЕ ЗАДАЧИ ---

async def punish(m: Message, reason: str):
    try:
        if m.sender_chat:
            user_id = m.sender_chat.id
            user_name = f"Канал {m.sender_chat.title}"
        else:
            user_id = m.from_user.id
            user_name = m.from_user.first_name
            
        warns = add_warn(user_id, m.chat.id) # Эта функция уже безопасна
        
        # Безопасная запись лога наказания через пул соединений
        with get_db() as (local_conn, local_cursor):
            local_cursor.execute(
                'INSERT INTO moderation_logs (chat_id, user_id, user_name, reason, action_type) VALUES (%s, %s, %s, %s, %s)',
                (m.chat.id, user_id, user_name, reason, f"warn_{warns}")
            )
            local_conn.commit() 
 
        if warns == 1:
            text = f"🚫 <b>{user_name}</b>, сообщение удалено ({reason}). \nЭто ваше первое предупреждение (1/3)."
        elif warns == 2:
            until = m.date + timedelta(minutes=5)
            if not m.sender_chat:
                await bot.restrict_chat_member(chat_id=m.chat.id, user_id=user_id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
            record_stat(m.chat.id, 'mute') # Уже безопасно
            text = f"⚠️ <b>{user_name}</b>, второе предупреждение (2/3)! \nВы получаете мут на 5 минут."
        else:
            until = m.date + timedelta(hours=1)
            if not m.sender_chat:
                await bot.restrict_chat_member(chat_id=m.chat.id, user_id=user_id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
            record_stat(m.chat.id, 'mute') # Уже безопасно
            reset_warns(user_id, m.chat.id) # Уже безопасно
            text = f"🛑 <b>{user_name}</b>, лимит исчерпан (3/3). \nВы получаете мут на 1 час."

        w = await m.reply(text, parse_mode="HTML")
        await m.delete()
        record_stat(m.chat.id, 'delete') # Уже безопасно

        await asyncio.sleep(10)
        await w.delete()
        
    except Exception as e:
        print(f"Ошибка при выдаче наказания: {e}", flush=True)

@dp.edited_message(F.chat.type.in_({"group", "supergroup"}))
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_messages(m: Message):
    if not m.text or m.is_automatic_forward: return

    # Передаем ID и актуальное название чата (функция add_chat уже безопасна)
    add_chat(m.chat.id, m.chat.title or "Без названия")
    text = m.text

    # 1. Иммунитет для администраторов
    if m.from_user:
        try:
            member = await bot.get_chat_member(m.chat.id, m.from_user.id)
            if member.status in ['creator', 'administrator']:
                return 
        except Exception:
            pass

    if is_ai(m.chat.id):
        has_link = False
        if m.entities:
            for entity in m.entities:
                if entity.type in ["url", "text_link"]:
                    has_link = True
                    break
                    
        if has_link:
            await punish(m, "Спам/Отправка ссылок")
            return
            
    if basic_filter(text):
        await punish(m, "Мат/Запрещенное слово")
        return
        
    if is_ai(m.chat.id):
        record_stat(m.chat.id, 'ai')
        author = m.sender_chat.title if m.sender_chat else m.from_user.first_name
        
        # Передаем параметры ровно так, как ожидает наша функция ai_filter
        is_bad_str = await ai_filter(text, author, m.chat.id, m.message_id)
        if is_bad_str in ["spam", "toxic", "obscene"]:
            await punish(m, f"Нарушение ({is_bad_str})")

def check_expiring_subscriptions():
    """Фоновая задача: проверяет подписки владельцев, которые истекают через 24 часа, и шлет уведомления в ЛС."""
    try:
        current_time = time.time()
        one_day_later = current_time + 86400 # 24 часа в секундах
        
        # Ищем владельцев, у которых PRO истекает в диапазоне от «сейчас» до «через 24 часа»
        with get_db() as (local_conn, local_cursor):
            local_cursor.execute(
                """SELECT owner_id, premium_until 
                   FROM user_subscriptions 
                   WHERE premium_until > %s AND premium_until <= %s""",
                (current_time, one_day_later)
            )
            expiring_subs = local_cursor.fetchall()
            
            for sub in expiring_subs:
                owner_id, premium_until = sub
                
                # Находим все чаты этого владельца, чтобы перечислить их в сообщении
                local_cursor.execute(
                    """SELECT chat_title FROM chats_v2 WHERE owner_id = %s AND ai_enabled = TRUE""",
                    (owner_id,)
                )
                owner_chats = local_cursor.fetchall()
                chat_titles = ", ".join([f"«{c[0]}»" for c in owner_chats]) if owner_chats else "ваших чатов"
                
                hours_left = int((premium_until - current_time) / 3600)
                
                message_text = (
                    f"⚠️ **Внимание!** Ваша PRO-подписка для чатов ({chat_titles}) истекает через {hours_left} ч.\n\n"
                    f"Чтобы ИИ-модератор не отключился, продлите подписку в личном кабинете Mini App!"
                )
                
                # Отправляем сообщение владельцу в ЛС
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(bot.send_message(owner_id, message_text, parse_mode="Markdown"))
                    loop.close()
                except Exception as send_err:
                    print(f"Не удалось отправить уведомление пользователю {owner_id}: {send_err}")
                
    except Exception as e:
        print(f"Ошибка в фоновой задаче проверки подписок: {e}")

async def cleanup_old_logs():
    """
    Фоновая задача: раз в сутки удаляет логи модерации старше 30 дней.
    """
    while True:
        try:
            # Безопасное удаление старых логов через пул соединений
            with get_db() as (local_conn, local_cursor):
                local_cursor.execute("DELETE FROM moderation_logs WHERE created_at < NOW() - INTERVAL '30 days'")
                deleted_count = local_cursor.rowcount
                local_conn.commit()
                if deleted_count > 0:
                    print(f"🧹 Очистка БД: удалено {deleted_count} старых логов модерации.", flush=True)
        except Exception as e:
            print(f"⚠️ Ошибка при очистке старых логов: {e}", flush=True)
        
        # Ждем 24 часа (86400 секунд) перед следующим запуском
        await asyncio.sleep(86400)



# --- 8. ЗАПУСК БОТА И ВЕБ-СЕРВЕРА ---
# --- БЛОК FLASK WEB-SERVER И API ---
from flask import Flask, request, jsonify
import hmac
import hashlib
import json
import requests
from urllib.parse import parse_qsl, unquote
from functools import wraps
import time
from datetime import datetime
from waitress import serve
import threading

app = Flask(__name__)

# 🔒 Указываем точный адрес вашего сайта (защита от CORS-атак)
ALLOWED_ORIGIN = "https://emil270797-cmyk.github.io"

def add_cors(response):
    response.headers.add("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
    response.headers.add("Access-Control-Allow-Headers", "Content-Type, Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return response

# 🔒 Функция криптографической проверки подписи Telegram
def validate_telegram_data(init_data: str, bot_token: str):
    try:
        parsed_data = dict(parse_qsl(init_data))
        if "hash" not in parsed_data:
            return None
            
        # === ПРОВЕРКА ВОЗРАСТА initData (Защита от Replay-атак) ===
        auth_date = parsed_data.get("auth_date")
        if not auth_date:
            return None
            
        # Устанавливаем лимиты: токен живет 24 часа (86400 секунд)
        # 5 минут (300 сек) для Mini App слишком мало — будет ломать сессии пользователей
        if time.time() - int(auth_date) > 86400:
            print("⚠️ Срок действия initData истек (больше 24 часов)", flush=True)
            return None
        # ========================================================
            
        received_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if hmac.compare_digest(calculated_hash, received_hash):
            user_raw = parsed_data.get("user")
            if user_raw:
                return json.loads(unquote(user_raw))
        return None
    except Exception as e:
        print(f"Ошибка проверки initData: {e}", flush=True)
        return None

# 🔒 Декоратор, который не пустит запрос без правильной подписи
def telegram_auth_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'OPTIONS':
            return add_cors(jsonify({'status': 'ok'}))
            
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('tma '):
            return add_cors(jsonify({"status": "error", "error": "Требуется авторизация через Telegram"})), 401
            
        init_data_str = auth_header[4:]
        user_data = validate_telegram_data(init_data_str, TOKEN)
        
        if not user_data or "id" not in user_data:
            return add_cors(jsonify({"status": "error", "error": "Недействительная подпись данных Telegram"})), 403
            
        # Надежно сохраняем подтвержденный ID пользователя
        request.verified_user_id = int(user_data["id"])
        return f(*args, **kwargs)
    return decorated_function


# Словарь для хранения истории API запросов: {ip_address: [timestamp1, timestamp2, ...]}
api_request_history = {}

def api_rate_limit(limit=60, per=60): 
    """
    Блокирует IP-адрес, если он делает больше 'limit' запросов за 'per' секунд.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # Для служебных OPTIONS (CORS) запросов лимит не нужен
            if request.method == 'OPTIONS':
                return f(*args, **kwargs)
        
            # Получаем реальный IP клиента (Render прячет его за прокси X-Forwarded-For)
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            if ip:
                ip = ip.split(',')[0].strip()
            else:
                ip = "unknown"

            current_time = time.time()

            if ip not in api_request_history:
                api_request_history[ip] = []

            # Очищаем старые запросы, которые вышли за рамки временного окна
            api_request_history[ip] = [t for t in api_request_history[ip] if current_time - t < per]

            # Если запросов слишком много — бьем по рукам
            if len(api_request_history[ip]) >= limit:
                print(f"🛑 БЛОКИРОВКА API (DDoS): IP {ip} превысил лимит запросов", flush=True)
                response = jsonify({"status": "error", "error": "Слишком много запросов. Подождите минуту."})
                return add_cors(response), 429

            # Фиксируем новый легальный запрос
            api_request_history[ip].append(current_time)

            return f(*args, **kwargs)
        return wrapped
    return decorator


@app.route('/')
def home():
    return "Бот-модератор работает!"


@app.route('/api/get_chats', methods=['GET', 'OPTIONS'])
@api_rate_limit(limit=60, per=60)
@telegram_auth_required
def api_get_chats():
    if request.method == 'OPTIONS': return add_cors(jsonify({'status': 'ok'}))
    owner_id = request.verified_user_id
    
    try:
        # Безопасное чтение через пул соединений get_db()
        with get_db() as (local_conn, local_cursor):
            local_cursor.execute("SELECT chat_id, chat_title, ai_enabled FROM chats_v2 WHERE owner_id = %s", (owner_id,))
            rows = local_cursor.fetchall()
            
        chats_list = [{"chat_id": str(r[0]), "chat_title": r[1] or "Без названия", "ai_enabled": bool(r[2])} for r in rows]
        return add_cors(jsonify({"status": "success", "chats": chats_list}))
    except Exception as e:
        print(f"Ошибка БД в /api/get_chats: {e}", flush=True)
        return add_cors(jsonify({"status": "error", "error": "Внутренняя ошибка сервера"})), 500

@app.route('/api/admin/all_chats', methods=['GET', 'OPTIONS'])
@api_rate_limit(limit=60, per=60)
@telegram_auth_required
def api_admin_all_chats():
    if request.method == 'OPTIONS': return add_cors(jsonify({'status': 'ok'}))
    owner_id = request.verified_user_id
    
    # Жесткая проверка: пускаем только вас
    if owner_id != MY_ADMIN_ID:
        return add_cors(jsonify({"status": "error", "error": "Доступ запрещен"})), 403
        
    try:
        with get_db() as (local_conn, local_cursor):
            local_cursor.execute('''
                SELECT c.chat_id, c.chat_title, c.ai_enabled, COALESCE(s.ai_requests, 0)
                FROM chats_v2 c
                LEFT JOIN stats s ON c.chat_id = s.chat_id
                ORDER BY COALESCE(s.ai_requests, 0) DESC
                LIMIT 50
            ''')
            rows = local_cursor.fetchall()
            
        chats_list = [{
            "chat_id": str(r[0]), 
            "chat_title": r[1] or "Без названия", 
            "ai_enabled": bool(r[2]),
            "ai_requests": r[3]
        } for r in rows]
        
        return add_cors(jsonify({"status": "success", "chats": chats_list}))
    except Exception as e:
        return add_cors(jsonify({"status": "error", "error": str(e)})), 500


@app.route('/api/get_user_sub', methods=['GET', 'OPTIONS'])
@api_rate_limit(limit=60, per=60)
@telegram_auth_required
def api_get_user_sub():
    if request.method == 'OPTIONS': return add_cors(jsonify({'status': 'ok'}))
    owner_id = request.verified_user_id
    
    try:
        current_time = time.time()
        with get_db() as (local_conn, local_cursor):
            local_cursor.execute("SELECT premium_until, slots FROM user_subscriptions WHERE owner_id = %s", (owner_id,))
            sub_row = local_cursor.fetchone()
            
            is_active = False
            max_slots = 3
            expires_at = "Никогда"
            
            if sub_row:
                premium_until, max_slots = sub_row
                if premium_until > current_time:
                    is_active = True
                    expires_at = datetime.fromtimestamp(premium_until).strftime('%Y-%m-%d %H:%M')

            local_cursor.execute("SELECT COUNT(*) FROM chats_v2 WHERE owner_id = %s AND ai_enabled = TRUE", (owner_id,))
            active_chats = local_cursor.fetchone()[0]

        return add_cors(jsonify({
            "status": "success", 
            "is_active": is_active, 
            "expires_at": expires_at, 
            "max_slots": max_slots, 
            "active_chats": active_chats,
            "is_admin": owner_id == MY_ADMIN_ID
        }))
    except Exception as e:
        print(f"Ошибка БД в /api/get_user_sub: {e}", flush=True)
        return add_cors(jsonify({"status": "error", "error": "Внутренняя ошибка сервера"})), 500


@app.route('/api/toggle_ai', methods=['POST', 'OPTIONS'])
@api_rate_limit(limit=60, per=60)
@telegram_auth_required
def api_toggle_ai():
    if request.method == 'OPTIONS': return add_cors(jsonify({'status': 'ok'}))
    
    owner_id = request.verified_user_id
    data = request.get_json(silent=True) or {}
    chat_id = data.get('chat_id')

    if not chat_id:
        return add_cors(jsonify({"status": "error", "error": "Не передан chat_id"})), 400

    try:
        chat_id_int = int(chat_id)
    except (ValueError, TypeError):
        return add_cors(jsonify({"status": "error", "error": "Некорректный chat_id"})), 400

    try:
        current_time = time.time()
        with get_db() as (local_conn, local_cursor):
            local_cursor.execute("SELECT ai_enabled, owner_id FROM chats_v2 WHERE chat_id = %s", (chat_id_int,))
            chat_row = local_cursor.fetchone()
            
            if not chat_row or chat_row[1] != owner_id:
                return add_cors(jsonify({"status": "error", "error": "Чат не найден или вы не владелец"})), 403

            current_ai_status = chat_row[0]
            if not current_ai_status:
                local_cursor.execute("SELECT premium_until, slots FROM user_subscriptions WHERE owner_id = %s", (owner_id,))
                sub_row = local_cursor.fetchone()
                if not sub_row or sub_row[0] < current_time:
                    return add_cors(jsonify({"status": "error", "error": "Сначала активируйте PRO-подписку (пакет на 3 чата)"})), 400
                    
                max_slots = sub_row[1]
                local_cursor.execute("SELECT COUNT(*) FROM chats_v2 WHERE owner_id = %s AND ai_enabled = TRUE", (owner_id,))
                active_chats_count = local_cursor.fetchone()[0]
                if active_chats_count >= max_slots:
                    return add_cors(jsonify({"status": "error", "error": f"Лимит исчерпан ({active_chats_count}/{max_slots} чатов). Купите дополнительный пакет."})), 400

            new_ai_status = not current_ai_status
            local_cursor.execute("UPDATE chats_v2 SET ai_enabled = %s WHERE chat_id = %s", (new_ai_status, chat_id_int))
            local_conn.commit()
            
        return add_cors(jsonify({"status": "success", "ai_enabled": new_ai_status}))

    except Exception as e:
        print(f"Критическая ошибка в /api/toggle_ai: {e}", flush=True)
        return add_cors(jsonify({"status": "error", "error": "Внутренняя ошибка сервера"})), 500


@app.route('/api/create_stars_invoice', methods=['POST', 'OPTIONS'])
@api_rate_limit(limit=60, per=60)
@telegram_auth_required
def api_create_stars_invoice():
    if request.method == 'OPTIONS': return add_cors(jsonify({'status': 'ok'}))
    owner_id = request.verified_user_id
    
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/createInvoiceLink"
        payload = {
            "title": "PRO Подписка (+3 чата)",
            "description": "Снятие лимитов и включение ИИ-модератора",
            "payload": f"sub_stars_{owner_id}",
            "currency": "XTR",
            "prices": [{"label": "PRO Подписка", "amount": 100}]
        }
        resp = requests.post(url, json=payload, timeout=10)
        res_data = resp.json()
        
        if res_data.get("ok"):
            return add_cors(jsonify({"status": "success", "invoice_link": res_data["result"]}))
        else:
            return add_cors(jsonify({"status": "error", "error": "Ошибка генерации счета"})), 400
    except Exception as e:
        print(f"Ошибка выписки счета Stars: {e}", flush=True)
        return add_cors(jsonify({"status": "error", "error": "Внутренняя ошибка сервера"})), 500


@app.route('/api/chat_details', methods=['GET', 'OPTIONS'])
@api_rate_limit(limit=60, per=60)
@telegram_auth_required
def api_chat_details():
    if request.method == 'OPTIONS': return add_cors(jsonify({'status': 'ok'}))
    owner_id = request.verified_user_id
    chat_id = request.args.get('chat_id')

    if not chat_id:
        return add_cors(jsonify({"status": "error", "error": "Не передан chat_id"})), 400

    try:
        chat_id_int = int(chat_id)
        with get_db() as (local_conn, local_cursor):
            # 1. Получаем информацию о чате
            local_cursor.execute("SELECT chat_title, premium_until, added_at, ai_enabled FROM chats_v2 WHERE chat_id = %s AND owner_id = %s", (chat_id_int, owner_id))
            chat_row = local_cursor.fetchone()
            
            if not chat_row:
                return add_cors(jsonify({"status": "error", "error": "Доступ запрещен или чат не найден"})), 403

            chat_title, premium_until, added_at, ai_enabled = chat_row
            
            # 2. Получаем последние 20 действий бота в этом чате
            local_cursor.execute("""
                SELECT user_name, reason, action_type, created_at 
                FROM moderation_logs 
                WHERE chat_id = %s 
                ORDER BY created_at DESC 
                LIMIT 20
            """, (chat_id_int,))
            logs_rows = local_cursor.fetchall()

        # Формируем список логов
        logs = []
        for lr in logs_rows:
            logs.append({
                "user_name": lr[0],
                "reason": lr[1],
                "action": lr[2],
                "date": lr[3].strftime('%d.%m %H:%M') if lr[3] else "Неизвестно"
            })

        current_time = time.time()
        is_premium = bool(premium_until and premium_until > current_time)
        added_date_str = added_at.strftime('%d.%m.%Y') if added_at else "Нет данных"

        return add_cors(jsonify({
            "status": "success",
            "title": chat_title,
            "is_premium": is_premium,
            "ai_enabled": ai_enabled,
            "added_at": added_date_str,
            "logs": logs
        }))

    except Exception as e:
        print(f"Ошибка в /api/chat_details: {e}", flush=True)
        return add_cors(jsonify({"status": "error", "error": "Внутренняя ошибка сервера"})), 500


def run_web():
    port = int(os.environ.get("PORT", 10000))
    serve(app, host="0.0.0.0", port=port)


async def main():
    # Запускаем веб-сервер (Waitress) в отдельном потоке
    threading.Thread(target=run_web, daemon=True).start()
    
    # Запускаем фоновую задачу очистки старых логов
    asyncio.create_task(cleanup_old_logs())
    
    print("🚀 Бот запущен и работает на Enterprise-архитектуре (Пул соединений)!", flush=True)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


