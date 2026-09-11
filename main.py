import asyncio
import os
import re
import aiohttp
import psycopg2
import pymorphy3
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

def add_chat(chat_id, title="Без названия"):
    cursor.execute('''
        INSERT INTO chats_v2 (chat_id, ai_enabled, premium_until, chat_title) 
        VALUES (%s, FALSE, 0, %s) 
        ON CONFLICT (chat_id) 
        DO UPDATE SET chat_title = EXCLUDED.chat_title WHERE EXCLUDED.chat_title IS NOT NULL
    ''', (chat_id, title))


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

# На всякий случай проверяем, есть ли колонка ai_requests (если таблица была создана до обновления)
try:
    cursor.execute('ALTER TABLE stats ADD COLUMN IF NOT EXISTS ai_requests INTEGER DEFAULT 0')
except Exception:
    pass

def add_chat(chat_id):
    cursor.execute('INSERT INTO chats_v2 (chat_id, ai_enabled, premium_until) VALUES (%s, FALSE, 0) ON CONFLICT (chat_id) DO NOTHING', (chat_id,))

def set_ai(chat_id, status, days=30):
    until = (datetime.now() + timedelta(days=days)).timestamp() if status else 0
    cursor.execute('UPDATE chats_v2 SET ai_enabled = %s, premium_until = %s WHERE chat_id = %s', (status, until, chat_id))

def is_ai(chat_id):
    cursor.execute('SELECT ai_enabled, premium_until FROM chats_v2 WHERE chat_id = %s', (chat_id,))
    res = cursor.fetchone()
    if res and res[0]: 
        if datetime.now().timestamp() < res[1]:
            return True
        else:
            set_ai(chat_id, False)
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
async def ai_filter(author_name: str, text: str) -> bool:
    try:
        print(f"🧠 ОТПРАВЛЯЮ В GEMINI: {author_name} - {text[:20]}...", flush=True)
        
        prompt = (
            "Ты — строгий модератор публичного чата. Твоя задача — анализировать сообщения и находить скрытый спам.\n"
            "Отвечай ТОЛЬКО словом 'True' (если сообщение нужно удалить) или 'False' (если оно нормальное).\n\n"
            "Что нужно удалять (True):\n"
            "1. Завуалированный мат, токсичность и пассивную агрессию.\n"
            "2. Спам, рекламу заработка, казино, криптовалюты.\n"
            "3. Комментарии от ботов (бессмысленные комплименты, неестественный флирт, пустые вопросы-наживки для начала диалога).\n\n"
            f"Имя автора: {author_name}\n"
            f"Сообщение: {text}"
        )
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_KEY}"
        
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
            ]
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload
            ) as resp:
                data = await resp.json()
                
                if resp.status != 200:
                    print(f"❌ Ошибка API Google: {data}", flush=True)
                    return False
                    
                result = data['candidates'][0]['content']['parts'][0]['text'].strip().lower()
                print(f"🤖 ОТВЕТ GEMINI: {result}", flush=True)
                
                return "true" in result
                
    except Exception as e:
        print(f"❌ Системная ошибка ИИ: {e}", flush=True)
        return False


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

    # Создаем прозрачные кнопки-меню
    inline_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌟 Подключить Premium", callback_data="buy_premium")
        ],
        [
            InlineKeyboardButton(
                text="🎛 Открыть Личный Кабинет", 
                web_app=WebAppInfo(url="https://emil270797-cmyk.github.io/censorshipingbot/")
            )
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
            InlineKeyboardButton(text="⚙️ Статус", callback_data="status")
        ]
    ])

    text = (
        "👋 <b>Добро пожаловать! Я — умный AI-модератор.</b>\n\n"
        "Моя задача — автоматически очищать ваши чаты и комментарии в каналах от скрытого спама, рекламы и токсичных пользователей с помощью нейросети.\n\n"
        "🛠 <b>КАК НАЧАТЬ РАБОТУ:</b>\n"
        "<b>1.</b> Добавьте меня в вашу группу (или в привязанный чат канала для комментариев).\n"
        "<b>2.</b> Выдайте мне права администратора: <i>«Удаление сообщений»</i> и <i>«Блокировка пользователей»</i>. Без них я не смогу наводить порядок!\n\n"
        "📌 <b>ОСНОВНЫЕ КОМАНДЫ:</b>\n"
        "<i>(Вводите их прямо в вашей группе или используйте нижнее меню)</i>\n"
        "🔹 /status — проверить, активна ли защита в чате.\n"
        "🔹 /stats — посмотреть статистику удаленного мусора.\n"
        "🔹 /buy_premium — активировать ИИ-модуль Gemini.\n\n"
        "👇 Используйте кнопки ниже для быстрого управления:"
    )

    # ДОБАВЛЯЕМ БЛОК ЛИЧНОГО КАБИНЕТА
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
        chat_id = event.chat.id
        admin_id = event.from_user.id 
        
        try:
            cursor.execute(
                '''INSERT INTO chat_admins (chat_id, admin_id) 
                   VALUES (%s, %s) 
                   ON CONFLICT (chat_id, admin_id) DO NOTHING''',
                (chat_id, admin_id)
            )
            conn.commit()
            print(f"✅ Авто-привязка: Чат {event.chat.title} закреплен за {admin_id}")
        except Exception as e:
            print(f"❌ Ошибка авто-привязки: {e}")


# --- ОБРАБОТЧИКИ НАЖАТИЙ НА КНОПКИ МЕНЮ ---

@dp.callback_query(F.data == "menu_premium")
async def process_premium_btn(callback: CallbackQuery):
    await callback.message.answer(
        "💎 <b>Premium AI Модератор</b>\n\n"
        "Включение нейросети Gemini для точного распознавания скрытой агрессии и завуалированного мата.\n\n"
        "<i>Для оплаты добавьте бота (50 Stars) в группу и напишите команду /buy_premium или нажмите серую кнопку внизу.</i>",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_stats")
async def process_stats_btn(callback: CallbackQuery):
    await callback.message.answer(
        "⚠️ <b>Обратите внимание:</b>\n"
        "Чтобы посмотреть статистику удалений и мутов, используйте команду /stats прямо внутри вашей группы, где добавлен бот.",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_status")
async def process_status_btn(callback: CallbackQuery):
    await callback.message.answer(
        "⚠️ <b>Обратите внимание:</b>\n"
        "Чтобы проверить статус, используйте команду /status прямо внутри вашей группы.",
        parse_mode="HTML"
    )
    await callback.answer()

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
    cursor.execute('SELECT ai_enabled, premium_until FROM chats_v2 WHERE chat_id = %s', (m.chat.id,))
    res = cursor.fetchone()
    
    if res and res[0] and res[1] > datetime.now().timestamp():
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
            f"💡 Чтобы включить ИИ-модерацию, используйте /buy_premium",
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
    prices = [LabeledPrice(label="Premium AI Модератор", amount=50)] 
    await bot.send_invoice(
        chat_id=m.chat.id,
        title="Premium AI Модератор",
        description="Включение нейросети Gemini для точного распознавания скрытой агрессии и завуалированного мата на 30 дней.",
        payload="premium_activation",
        provider_token="",
        currency="XTR",
        prices=prices
    )

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(m: Message):
    add_chat(m.chat.id)
    set_ai(m.chat.id, True, days=30) 
    stars = m.successful_payment.total_amount
    await m.answer(
        f"🎉 <b>Спасибо за поддержку!</b> Оплата в {stars} Stars получена.\n"
        f"✅ <b>Premium активирован на 30 дней!</b>", 
        parse_mode="HTML"
    )


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
    if not m.text or m.is_automatic_forward:
        return
        
    add_chat(m.chat.id)
    text = m.text
    
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


# --- 8. ЗАПУСК БОТА И ВЕБ-СЕРВЕРА ---
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/')
def home():
    return "Бот-модератор работает!"

@app.route('/api/get_chats', methods=['GET'])
def api_get_chats():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "Не передан user_id"}), 400
        
    try:
        # Достаем чаты через связку с админами и сразу забираем chat_title
        cursor.execute('''
            SELECT c.chat_id, c.ai_enabled, c.chat_title 
            FROM chat_admins a 
            JOIN chats_v2 c ON a.chat_id = c.chat_id 
            WHERE a.admin_id = %s
        ''', (user_id,))
        chats = cursor.fetchall()
        
        chat_list = []
        for row in chats:
            c_id, ai_status, c_title = row
            chat_list.append({
                "chat_id": str(c_id),
                "chat_title": c_title or f"Чат {c_id}",
                "is_protected": ai_status
            })
            
        response = jsonify({"status": "success", "chats": chat_list})
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response
        
    except Exception as e:
        response = jsonify({"error": str(e)})
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response, 500

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

async def main():
    Thread(target=run_web).start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
