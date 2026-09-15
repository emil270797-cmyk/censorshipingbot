import asyncio
import os
import re
import aiohttp
import psycopg2
import pymorphy3
import hmac
import hashlib
import json

from urllib.parse import parse_qsl, unquote
from functools import wraps
from datetime import timedelta, datetime
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
# Я почистил импорты и добавил нужный WebAppInfo
from aiogram.types import (
    Message, LabeledPrice, PreCheckoutQuery, ChatPermissions, 
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, 
    ChatMemberUpdated, WebAppInfo
)

# --- 1. НАСТРОЙКИ БОТА И API ---
TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- 2. БАЗА ДАННЫХ ---
DB_URL = os.environ.get("DATABASE_URL")
conn = psycopg2.connect(DB_URL)
conn.autocommit = True
cursor = conn.cursor()

# --- Обновляем создание таблицы в начале файла ---
cursor.execute('''CREATE TABLE IF NOT EXISTS chats_v2 (
    chat_id BIGINT PRIMARY KEY, 
    ai_enabled BOOLEAN DEFAULT FALSE,
    premium_until DOUBLE PRECISION DEFAULT 0,
    chat_title TEXT
)''')

# Безопасное добавление колонки, если таблица уже была создана раньше
try:
    cursor.execute('ALTER TABLE chats_v2 ADD COLUMN IF NOT EXISTS chat_title TEXT')
except Exception:
    pass

# Автоматическое добавление колонки owner_id, если её еще нет
try:
    cursor.execute("ALTER TABLE chats_v2 ADD COLUMN IF NOT EXISTS owner_id BIGINT;")
    conn.commit()
    print("Колонка owner_id успешно проверена/добавлена.")
except Exception as e:
    conn.rollback()
    print(f"Ошибка при добавлении колонки: {e}")

try:
    cursor.execute("ALTER TABLE chats_v2 ADD COLUMN IF NOT EXISTS added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")
    conn.commit()
except Exception:
    pass

try:
    cursor.execute('''
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
    conn.commit()
except Exception as e:
    print(f"Ошибка создания таблицы payments: {e}")



# Создаем таблицу подписок владельцев, если её еще нет
try:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_subscriptions (
            owner_id BIGINT PRIMARY KEY,
            premium_until DOUBLE PRECISION DEFAULT 0,
            slots INT DEFAULT 3
        );
    """)
    conn.commit()
    print("Таблица user_subscriptions успешно проверена/создана.")
except Exception as e:
    conn.rollback()
    print(f"Ошибка при создании таблицы user_subscriptions: {e}")

try:
    cursor.execute("UPDATE chats_v2 SET owner_id = 354584527 WHERE owner_id IS NULL;")
    conn.commit()
    print("Старые чаты успешно привязаны к владельцу!")
except Exception as e:
    conn.rollback()
    print(f"Ошибка при привязке чатов: {e}")



def add_chat(chat_id, title="Без названия"):
    cursor.execute('''
        INSERT INTO chats_v2 (chat_id, ai_enabled, premium_until, chat_title) 
        VALUES (%s, FALSE, 0, %s) 
        ON CONFLICT (chat_id) 
        DO UPDATE SET chat_title = EXCLUDED.chat_title WHERE EXCLUDED.chat_title IS NOT NULL
    ''', (chat_id, title))

import time

def check_chat_premium(chat_id):
    """Проверяет, действует ли премиум-подписка для чата"""
    cursor.execute('SELECT premium_until, ai_enabled FROM chats_v2 WHERE chat_id = %s', (chat_id,))
    res = cursor.fetchone()
    if not res:
        return False
    
    premium_until, ai_enabled = res
    current_time = time.time()
    
    # Если время подписки истекло, а ИИ был включен — выключаем его автоматически
    if premium_until < current_time and ai_enabled:
        cursor.execute('UPDATE chats_v2 SET ai_enabled = FALSE WHERE chat_id = %s', (chat_id,))
        conn.commit()
        return False
        
    return premium_until >= current_time





cursor.execute('''CREATE TABLE IF NOT EXISTS warns (
    user_id BIGINT, 
    chat_id BIGINT, 
    count INTEGER,
    UNIQUE(user_id, chat_id)
)''')

cursor.execute('''CREATE TABLE IF NOT EXISTS stats (
    chat_id BIGINT PRIMARY KEY, 
    deleted_count INTEGER DEFAULT 0, 
    mute_count INTEGER DEFAULT 0,
    ai_requests INTEGER DEFAULT 0
)''')

cursor.execute('''CREATE TABLE IF NOT EXISTS moderation_logs (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT,
    user_id BIGINT,
    user_name TEXT,
    reason TEXT,
    action_type TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)''')
conn.commit()

cursor.execute('''CREATE TABLE IF NOT EXISTS chat_admins (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT,
    admin_id BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chat_id, admin_id)
)''')
conn.commit()

cursor.execute('''CREATE TABLE IF NOT EXISTS processed_txs (
    tx_hash TEXT PRIMARY KEY,
    chat_id BIGINT
)''')
conn.commit()


# На всякий случай проверяем, есть ли колонка ai_requests (если таблица была создана до обновления)
try:
    cursor.execute('ALTER TABLE stats ADD COLUMN IF NOT EXISTS ai_requests INTEGER DEFAULT 0')
except Exception:
    pass

def set_ai(chat_id, status, days=30):
    until = (datetime.now() + timedelta(days=days)).timestamp() if status else 0
    cursor.execute('UPDATE chats_v2 SET ai_enabled = %s, premium_until = %s WHERE chat_id = %s', (status, until, chat_id))

def is_ai(chat_id):
    # Теперь мы берем ai_enabled из чата, а premium_until - из подписки владельца
    cursor.execute('''
        SELECT c.ai_enabled, u.premium_until 
        FROM chats_v2 c
        LEFT JOIN user_subscriptions u ON c.owner_id = u.owner_id
        WHERE c.chat_id = %s
    ''', (chat_id,))
    res = cursor.fetchone()
    
    if res and res[0]:  # Если тумблер включен
        premium_until = res[1] or 0
        if datetime.now().timestamp() < premium_until:
            return True
        else:
            # Если подписка владельца закончилась, выключаем ИИ в чате
            cursor.execute('UPDATE chats_v2 SET ai_enabled = FALSE WHERE chat_id = %s', (chat_id,))
            conn.commit()
            return False
    return False


def add_warn(user_id, chat_id):
    cursor.execute('SELECT count FROM warns WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))
    res = cursor.fetchone()
    if res:
        count = res[0] + 1
        cursor.execute('UPDATE warns SET count = %s WHERE user_id = %s AND chat_id = %s', (count, user_id, chat_id))
    else:
        count = 1
        cursor.execute('INSERT INTO warns (user_id, chat_id, count) VALUES (%s, %s, %s)', (user_id, chat_id, count))
    return count

def reset_warns(user_id, chat_id):
    cursor.execute('DELETE FROM warns WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))

def record_stat(chat_id, stat_type):
    cursor.execute('INSERT INTO stats (chat_id, deleted_count, mute_count, ai_requests) VALUES (%s, 0, 0, 0) ON CONFLICT (chat_id) DO NOTHING', (chat_id,))
    if stat_type == 'delete':
        cursor.execute('UPDATE stats SET deleted_count = deleted_count + 1 WHERE chat_id = %s', (chat_id,))
    elif stat_type == 'mute':
        cursor.execute('UPDATE stats SET mute_count = mute_count + 1 WHERE chat_id = %s', (chat_id,))
    elif stat_type == 'ai':
        cursor.execute('UPDATE stats SET ai_requests = ai_requests + 1 WHERE chat_id = %s', (chat_id,))

def get_stats(chat_id):
    cursor.execute('SELECT deleted_count, mute_count, ai_requests FROM stats WHERE chat_id = %s', (chat_id,))
    res = cursor.fetchone()
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
    try:
        # ОБЕЗЛИЧЕННЫЙ ЛОГ ЗАПРОСА
        print(f"🧠 AI moderation request | chat_id: {chat_id} | message_id: {message_id}", flush=True)

        prompt = f"""
        Проверь это сообщение от пользователя "{author_name}". 
        Твоя задача — классифицировать его. 
        Ответь ТОЛЬКО ОДНИМ СЛОВОМ из списка:
        ok - обычное сообщение
        spam - реклама, ссылки на каналы, призывы подписаться, заработок
        toxic - агрессия, оскорбления (прямые или скрытые), травля
        obscene - мат, завуалированный мат, непристойности

        Сообщение: {text}
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



# --- 5. КОМАНДЫ ПОЛЬЗОВАТЕЛЕЙ И АДМИНОВ ---
@dp.message(Command("start"))
async def cmd_start(m: Message):
    # Проверяем, что команда вызвана в личных сообщениях с ботом
    if m.chat.type != "private":
        return

    admin_id = m.from_user.id
    
    # Ищем привязанные чаты владельца
    cursor.execute('SELECT chat_id FROM chat_admins WHERE admin_id = %s', (admin_id,))
    chats = cursor.fetchall()

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
            # 1. Сохраняем или обновляем чат в chats_v2 СРАЗУ с owner_id
            cursor.execute(
                """INSERT INTO chats_v2 (chat_id, chat_title, owner_id, ai_enabled) 
                   VALUES (%s, %s, %s, FALSE)
                   ON CONFLICT (chat_id) 
                   DO UPDATE SET owner_id = EXCLUDED.owner_id, chat_title = EXCLUDED.chat_title""",
                (chat_id, chat_title, admin_id)
            )
            
            # 2. Оставляем вашу таблицу chat_admins (если она нужна для других фич)
            cursor.execute(
                '''INSERT INTO chat_admins (chat_id, admin_id) 
                   VALUES (%s, %s) 
                   ON CONFLICT (chat_id, admin_id) DO NOTHING''',
                (chat_id, admin_id)
            )
            
            conn.commit()
            print(f"✅ Авто-привязка: Чат '{chat_title}' ({chat_id}) закреплен за владельцем {admin_id}")
        except Exception as e:
            conn.rollback()
            print(f"❌ Ошибка авто-привязки: {e}")



# --- ОБРАБОТЧИКИ НАЖАТИЙ НА КНОПКИ МЕНЮ ---


@dp.message(Command("report"))
async def send_report(m: Message):
    chat_id = m.chat.id
    
    cursor.execute('''
        SELECT reason, COUNT(*) 
        FROM moderation_logs 
        WHERE chat_id = %s
        GROUP BY reason
    ''', (chat_id,))
    stats = cursor.fetchall()
    
    cursor.execute('''
        SELECT user_name, action_type, reason 
        FROM moderation_logs 
        WHERE chat_id = %s 
        ORDER BY created_at DESC 
        LIMIT 5
    ''', (chat_id,))
    recent_logs = cursor.fetchall()
    
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
    # Обновляем команду /status, чтобы она тоже смотрела на подписку владельца
    cursor.execute('''
        SELECT c.ai_enabled, u.premium_until 
        FROM chats_v2 c
        LEFT JOIN user_subscriptions u ON c.owner_id = u.owner_id
        WHERE c.chat_id = %s
    ''', (m.chat.id,))
    res = cursor.fetchone()
    
    if res and res[0] and res[1] and res[1] > datetime.now().timestamp():
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
        reset_warns(target_user.id, m.chat.id)
        await m.answer(f"✅ Предупреждения пользователя <b>{target_user.first_name}</b> обнулены.", parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при снятии варна: {e}")

@dp.message(Command("unmute"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_unmute(m: Message):
    admins = await m.chat.get_administrators()
    if m.from_user.id not in [admin.user.id for admin in admins]:
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
        reset_warns(target_user.id, m.chat.id)
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

    d_count, m_count, ai_reqs = get_stats(m.chat.id)
    await m.answer(
        f"📊 <b>Статистика модерации:</b>\n\n"
        f"🗑 Удалено сообщений: <b>{d_count}</b>\n"
        f"🤐 Выдано мутов: <b>{m_count}</b>",
        parse_mode="HTML"
    )


# --- 6. ПАНЕЛЬ ВЛАДЕЛЬЦА И ОПЛАТА PREMIUM ---
OWNER_ID = 354584527

@dp.message(Command("botstats"))
async def cmd_botstats(m: Message):
    if m.from_user.id != OWNER_ID: return
        
    try:
        cursor.execute('SELECT COUNT(*) FROM chats_v2')
        total_chats = cursor.fetchone()[0]
        
        current_time = datetime.now().timestamp()
        cursor.execute('SELECT COUNT(*) FROM chats_v2 WHERE ai_enabled = TRUE AND premium_until > %s', (current_time,))
        premium_chats = cursor.fetchone()[0]
        
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

    cursor.execute('''
        SELECT c.chat_id, c.ai_enabled, c.premium_until, COALESCE(s.ai_requests, 0)
        FROM chats_v2 c
        LEFT JOIN stats s ON c.chat_id = s.chat_id
        ORDER BY COALESCE(s.ai_requests, 0) DESC
    ''')
    all_chats = cursor.fetchall()
    
    if not all_chats:
        await m.answer("Список чатов пока пуст.")
        return
        
    await m.answer("⏳ Собираю аналитику по чатам...")
    text = "📊 <b>Аналитика чатов (по расходу ИИ):</b>\n\n"
    current_time = datetime.now().timestamp()
    
    active_count = 0
    
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
            cursor.execute('DELETE FROM chats_v2 WHERE chat_id = %s', (chat_id,))
            cursor.execute('DELETE FROM stats WHERE chat_id = %s', (chat_id,))
            continue
            
    if active_count == 0:
        text = "К сожалению, бот был удален из всех известных чатов."
        
    try:
        await m.answer(text, parse_mode="HTML")
    except Exception as e:
        await m.answer(f"❌ Ошибка отправки списка: {e}")

from aiogram.filters import Command
import time

# Ваш Telegram ID
MY_ADMIN_ID = int(os.environ.get("ADMIN_ID", 354584527))
OWNER_ID = int(os.environ.get("ADMIN_ID", 354584527))
 

@dp.message(Command("givepro"))
async def cmd_give_pro(message: types.Message):
    # Бот реагирует только на сообщения от владельца
    if message.from_user.id != MY_ADMIN_ID:
        return
    
    args = message.text.split()
    if len(args) != 4:
        await message.answer("⚠️ Неверный формат!\nПишите так: `/givepro <id> <дней> <слотов>`\nПример: `/givepro 354584527 30 3`", parse_mode="Markdown")
        return
        
    target_id = args[1]
    days = int(args[2])
    slots = int(args[3])
    
    current_time = int(time.time())
    premium_until = current_time + (days * 24 * 60 * 60)
    
    try:
        cursor.execute(
            """INSERT INTO user_subscriptions (owner_id, premium_until, slots) 
               VALUES (%s, %s, %s)
               ON CONFLICT (owner_id) 
               DO UPDATE SET premium_until = EXCLUDED.premium_until, slots = EXCLUDED.slots""",
            (target_id, premium_until, slots)
        )
        conn.commit()
        await message.answer(f"✅ Готово! Пользователю `{target_id}` выдана PRO-подписка на {days} дней. Слотов: {slots}.", parse_mode="Markdown")
    except Exception as e:
        conn.rollback()
        await message.answer(f"❌ Ошибка: {e}")


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
        
        cursor.execute('UPDATE chats_v2 SET ai_enabled = TRUE, premium_until = %s WHERE chat_id = %s', (future_time, target_chat_id))
        
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

from aiogram.types import PreCheckoutQuery
import psycopg2
import time

# 1. СТРОГИЙ ОБРАБОТЧИК PRE-CHECKOUT
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    # Проверяем, наш ли это payload
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
    
    # 1. Защита от дублей: Пытаемся записать транзакцию в БД
    try:
        cursor.execute("""
            INSERT INTO payments (telegram_payment_charge_id, user_id, payload, amount, currency)
            VALUES (%s, %s, %s, %s, %s)
        """, (charge_id, user_id, payload, payment_info.total_amount, payment_info.currency))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback()
        # Платёж с таким ID уже был обработан! Игнорируем дубль.
        print(f"Дубль платежа перехвачен: {charge_id}")
        return
    except Exception as e:
        conn.rollback()
        print(f"Ошибка БД при записи платежа: {e}")
        return

    # 2. Если запись прошла успешно, выдаем товар
    if payload.startswith("sub_stars_"):
        owner_id = int(payload.split("_")[2]) # Извлекаем ID из sub_stars_123456
        current_time = time.time()
        
        # Проверяем текущую подписку
        cursor.execute("SELECT premium_until, slots FROM user_subscriptions WHERE owner_id = %s", (owner_id,))
        sub_row = cursor.fetchone()
        
        if sub_row and sub_row[0] > current_time:
            # Продлеваем текущую
            new_until = sub_row[0] + (30 * 24 * 3600)
            new_slots = sub_row[1] + 3
            cursor.execute("UPDATE user_subscriptions SET premium_until = %s, slots = %s WHERE owner_id = %s", 
                           (new_until, new_slots, owner_id))
        else:
            # Создаем новую
            new_until = current_time + (30 * 24 * 3600)
            new_slots = 3
            cursor.execute("""
                INSERT INTO user_subscriptions (owner_id, premium_until, slots)
                VALUES (%s, %s, %s)
                ON CONFLICT (owner_id) 
                DO UPDATE SET premium_until = EXCLUDED.premium_until, slots = EXCLUDED.slots
            """, (owner_id, new_until, new_slots))
            
        conn.commit()
        await message.answer("🎉 Оплата успешно получена! Вам добавлено 3 слота на 30 дней. Можете включать ИИ в Личном кабинете!")



# --- 7. ОСНОВНОЙ ПРОЦЕСС МОДЕРАЦИИ ---
async def punish(m: Message, reason: str):
    try:
        if m.sender_chat:
            user_id = m.sender_chat.id
            user_name = f"Канал {m.sender_chat.title}"
        else:
            user_id = m.from_user.id
            user_name = m.from_user.first_name
            
        warns = add_warn(user_id, m.chat.id)
        
        cursor.execute(
            'INSERT INTO moderation_logs (chat_id, user_id, user_name, reason, action_type) VALUES (%s, %s, %s, %s, %s)',
            (m.chat.id, user_id, user_name, reason, f"warn_{warns}")
        )
        conn.commit() 
 
        if warns == 1:
            text = f"🚫 <b>{user_name}</b>, сообщение удалено ({reason}). \nЭто ваше первое предупреждение (1/3)."
        elif warns == 2:
            until = m.date + timedelta(minutes=5)
            if not m.sender_chat:
                await bot.restrict_chat_member(chat_id=m.chat.id, user_id=user_id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
            record_stat(m.chat.id, 'mute')
            text = f"⚠️ <b>{user_name}</b>, второе предупреждение (2/3)! \nВы получаете мут на 5 минут."
        else:
            until = m.date + timedelta(hours=1)
            if not m.sender_chat:
                await bot.restrict_chat_member(chat_id=m.chat.id, user_id=user_id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
            record_stat(m.chat.id, 'mute')
            reset_warns(user_id, m.chat.id)
            text = f"🛑 <b>{user_name}</b>, лимит исчерпан (3/3). \nВы получаете мут на 1 час."

        w = await m.reply(text, parse_mode="HTML")
        await m.delete()
        record_stat(m.chat.id, 'delete')

        await asyncio.sleep(10)
        await w.delete()
        
    except Exception as e:
        print(f"Ошибка при выдаче наказания: {e}", flush=True)

@dp.edited_message(F.chat.type.in_({"group", "supergroup"}))
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_messages(m: Message):
    if not m.text or m.is_automatic_forward: return

    # Передаем ID и актуальное название чата
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
        is_bad = await ai_filter(author, text)
        if is_bad:
            await punish(m, "Токсичность/Спам-бот (AI)")

import aiohttp


 

 

import time

def check_expiring_subscriptions():
    """Фоновая задача: проверяет подписки, которые истекают через 24 часа, и шлет уведомления в ЛС."""
    try:
        current_time = time.time()
        one_day_later = current_time + 86400 # 24 часа в секундах
        
        # Ищем чаты, у которых PRO истекает в диапазоне от «сейчас» до «через 24 часа»,
        # и которым мы еще не отправляли предупреждение (или проверяем по логике)
        cursor.execute(
            """SELECT chat_id, chat_title, owner_id, premium_until 
               FROM chats_v2 
               WHERE premium_until > %s AND premium_until <= %s""",
            (current_time, one_day_later)
        )
        expiring_chats = cursor.fetchall()
        
        # Импортируем asyncio, чтобы запустить асинхронную отправку сообщения через бота из синхронной задачи
        import asyncio
        
        for chat in expiring_chats:
            chat_id, chat_title, owner_id, premium_until = chat
            if not owner_id:
                continue
                
            hours_left = int((premium_until - current_time) / 3600)
            
            message_text = (
                f"⚠️ **Внимание!** PRO-подписка для чата *«{chat_title}»* истекает через {hours_left} ч.\n\n"
                f"Чтобы ИИ-модератор не отключился, продлите подписку в личном кабинете Mini App!"
            )
            
            # Отправляем сообщение владельцу чата в ЛС
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(bot.send_message(owner_id, message_text, parse_mode="Markdown"))
                loop.close()
            except Exception as send_err:
                print(f"Не удалось отправить уведомление пользователю {owner_id}: {send_err}")
                
    except Exception as e:
        print(f"Ошибка в фоновой задаче проверки подписок: {e}")

import asyncio

async def cleanup_old_logs():
    """
    Фоновая задача: раз в сутки удаляет логи модерации старше 30 дней.
    """
    while True:
        try:
            with conn.cursor() as cur:
                # Удаляем записи старше 30 дней
                cur.execute("DELETE FROM moderation_logs WHERE created_at < NOW() - INTERVAL '30 days'")
                deleted_count = cur.rowcount
                conn.commit()
                if deleted_count > 0:
                    print(f"🧹 Очистка БД: удалено {deleted_count} старых логов модерации.", flush=True)
        except Exception as e:
            conn.rollback()
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

@app.route('/')
def home():
    return "Бот-модератор работает!"

@app.route('/api/get_chats', methods=['GET', 'OPTIONS'])
@telegram_auth_required
def api_get_chats():
    if request.method == 'OPTIONS': return add_cors(jsonify({'status': 'ok'}))
    owner_id = request.verified_user_id
    
    try:
        # Создаем локальный курсор для защиты от параллельных запросов
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, chat_title, ai_enabled FROM chats_v2 WHERE owner_id = %s", (owner_id,))
            rows = cur.fetchall()
            
        chats_list = [{"chat_id": str(r[0]), "chat_title": r[1] or "Без названия", "ai_enabled": bool(r[2])} for r in rows]
        return add_cors(jsonify({"status": "success", "chats": chats_list}))
    except Exception as e:
        print(f"Ошибка БД в /api/get_chats: {e}", flush=True)
        return add_cors(jsonify({"status": "error", "error": "Внутренняя ошибка сервера"})), 500

@app.route('/api/get_user_sub', methods=['GET', 'OPTIONS'])
@telegram_auth_required
def api_get_user_sub():
    if request.method == 'OPTIONS': return add_cors(jsonify({'status': 'ok'}))
    owner_id = request.verified_user_id
    
    try:
        current_time = time.time()
        with conn.cursor() as cur:
            cur.execute("SELECT premium_until, slots FROM user_subscriptions WHERE owner_id = %s", (owner_id,))
            sub_row = cur.fetchone()
            
            is_active = False
            max_slots = 3
            expires_at = "Никогда"
            
            if sub_row:
                premium_until, max_slots = sub_row
                if premium_until > current_time:
                    is_active = True
                    expires_at = datetime.fromtimestamp(premium_until).strftime('%Y-%m-%d %H:%M')

            cur.execute("SELECT COUNT(*) FROM chats_v2 WHERE owner_id = %s AND ai_enabled = TRUE", (owner_id,))
            active_chats = cur.fetchone()[0]

        return add_cors(jsonify({
            "status": "success", 
            "is_active": is_active, 
            "expires_at": expires_at, 
            "max_slots": max_slots, 
            "active_chats": active_chats
        }))
    except Exception as e:
        print(f"Ошибка БД в /api/get_user_sub: {e}", flush=True)
        return add_cors(jsonify({"status": "error", "error": "Внутренняя ошибка сервера"})), 500

@app.route('/api/toggle_ai', methods=['POST', 'OPTIONS'])
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
        with conn.cursor() as cur:
            cur.execute("SELECT ai_enabled, owner_id FROM chats_v2 WHERE chat_id = %s", (chat_id_int,))
            chat_row = cur.fetchone()
            
            if not chat_row or chat_row[1] != owner_id:
                return add_cors(jsonify({"status": "error", "error": "Чат не найден или вы не владелец"})), 403

            current_ai_status = chat_row[0]
            if not current_ai_status:
                cur.execute("SELECT premium_until, slots FROM user_subscriptions WHERE owner_id = %s", (owner_id,))
                sub_row = cur.fetchone()
                if not sub_row or sub_row[0] < current_time:
                    return add_cors(jsonify({"status": "error", "error": "Сначала активируйте PRO-подписку (пакет на 3 чата)"})), 400
                    
                max_slots = sub_row[1]
                cur.execute("SELECT COUNT(*) FROM chats_v2 WHERE owner_id = %s AND ai_enabled = TRUE", (owner_id,))
                active_chats_count = cur.fetchone()[0]
                if active_chats_count >= max_slots:
                    return add_cors(jsonify({"status": "error", "error": f"Лимит исчерпан ({active_chats_count}/{max_slots} чатов). Купите дополнительный пакет."})), 400

            new_ai_status = not current_ai_status
            cur.execute("UPDATE chats_v2 SET ai_enabled = %s WHERE chat_id = %s", (new_ai_status, chat_id_int))
            conn.commit()
            
        return add_cors(jsonify({"status": "success", "ai_enabled": new_ai_status}))

    except Exception as e:
        conn.rollback()
        print(f"Критическая ошибка в /api/toggle_ai: {e}", flush=True)
        return add_cors(jsonify({"status": "error", "error": "Внутренняя ошибка сервера"})), 500


@app.route('/api/create_stars_invoice', methods=['POST', 'OPTIONS'])
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
@telegram_auth_required
def api_chat_details():
    if request.method == 'OPTIONS': return add_cors(jsonify({'status': 'ok'}))
    owner_id = request.verified_user_id
    chat_id = request.args.get('chat_id')

    if not chat_id:
        return add_cors(jsonify({"status": "error", "error": "Не передан chat_id"})), 400

    try:
        chat_id_int = int(chat_id)
        with conn.cursor() as cur:
            # 1. Получаем информацию о чате
            cur.execute("SELECT chat_title, premium_until, added_at, ai_enabled FROM chats_v2 WHERE chat_id = %s AND owner_id = %s", (chat_id_int, owner_id))
            chat_row = cur.fetchone()
            
            if not chat_row:
                return add_cors(jsonify({"status": "error", "error": "Доступ запрещен или чат не найден"})), 403

            chat_title, premium_until, added_at, ai_enabled = chat_row
            
            # 2. Получаем последние 20 действий бота в этом чате
            cur.execute("""
                SELECT user_name, reason, action_type, created_at 
                FROM moderation_logs 
                WHERE chat_id = %s 
                ORDER BY created_at DESC 
                LIMIT 20
            """, (chat_id_int,))
            logs_rows = cur.fetchall()

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



from waitress import serve

def run_web():
    port = int(os.environ.get("PORT", 10000))
    # Запускаем продакшен-сервер вместо встроенного Flask (app.run)
    serve(app, host="0.0.0.0", port=port)



async def main():
    import threading
    # Запускаем веб-сервер (Waitress) в отдельном потоке
    threading.Thread(target=run_web, daemon=True).start()
    
    # Запускаем фоновую задачу очистки старых логов
    asyncio.create_task(cleanup_old_logs())
    
    print("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

