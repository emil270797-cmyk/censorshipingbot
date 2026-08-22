import asyncio
import os
import re
import psycopg2
import pymorphy3
from datetime import timedelta, datetime
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery
from aiogram.filters import Command

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
import psycopg2

# Подключаемся к базе данных через URL из настроек Render
DB_URL = os.environ.get("DATABASE_URL")

# Открываем соединение (autocommit=True избавляет от необходимости писать conn.commit())
conn = psycopg2.connect(DB_URL)
conn.autocommit = True
cursor = conn.cursor()

# Создаем таблицы (используем BIGINT, так как ID в Telegram очень длинные)
cursor.execute('''CREATE TABLE IF NOT EXISTS chats_v2 (
    chat_id BIGINT PRIMARY KEY, 
    ai_enabled BOOLEAN DEFAULT FALSE,
    premium_until DOUBLE PRECISION DEFAULT 0
)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS warns (
    user_id BIGINT, 
    chat_id BIGINT, 
    count INTEGER,
    UNIQUE(user_id, chat_id)
)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS stats (
    chat_id BIGINT PRIMARY KEY, 
    deleted_count INTEGER DEFAULT 0, 
    mute_count INTEGER DEFAULT 0
)''')

def add_chat(chat_id):
    # ON CONFLICT DO NOTHING - безопасное добавление (если чат уже есть, ошибка не выскочит)
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
    cursor.execute('INSERT INTO stats (chat_id, deleted_count, mute_count) VALUES (%s, 0, 0) ON CONFLICT (chat_id) DO NOTHING', (chat_id,))
    if stat_type == 'delete':
        cursor.execute('UPDATE stats SET deleted_count = deleted_count + 1 WHERE chat_id = %s', (chat_id,))
    elif stat_type == 'mute':
        cursor.execute('UPDATE stats SET mute_count = mute_count + 1 WHERE chat_id = %s', (chat_id,))

def get_stats(chat_id):
    cursor.execute('SELECT deleted_count, mute_count FROM stats WHERE chat_id = %s', (chat_id,))
    res = cursor.fetchone()
    return res if res else (0, 0)

# --- 3. НАСТРОЙКИ БОТА ---
TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Правильная инициализация Gemini
import google.generativeai as genai
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-pro')




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
# Приветствие (команда /start)
@dp.message(Command("start"))
async def cmd_start(m: Message):
    # Если пишут в личку боту
    if m.chat.type == "private":
        await m.answer(
            "👋 <b>Привет! Я — Premium AI Модератор.</b>\n\n"
            "Я умею распознавать скрытую агрессию, завуалированный мат и токсичность с помощью нейросети Gemini.\n\n"
            "<b>Как меня настроить:</b>\n"
            "1. Добавьте меня в свою группу.\n"
            "2. Дайте права администратора (удаление сообщений и ограничение пользователей).\n"
            "3. Напишите в группе команду <code>/buy_premium</code>, чтобы активировать ИИ.\n\n"
            "🛡 Без Premium-подписки я работаю как базовый фильтр запрещенных слов.",
            parse_mode="HTML"
        )
    # Если команду /start написали прямо в группе
    else:
        await m.answer(
            "👋 Привет! Я готов следить за порядком в этом чате.\n"
            "Администраторы могут использовать <code>/buy_premium</code> для включения ИИ.",
            parse_mode="HTML"
        )
# Команда для проверки статуса подписки
@dp.message(Command("status"), F.chat.type.in_({"group", "supergroup"}))
async def chat_status(m: Message):
    # Достаем данные чата из базы
    cursor.execute('SELECT ai_enabled, premium_until FROM chats_v2 WHERE chat_id = %s', (m.chat.id,))
    res = cursor.fetchone()
    
    # Проверяем, есть ли запись, включен ли ИИ и не истекло ли время
    if res and res[0] and res[1] > datetime.now().timestamp():
        # Переводим сохраненные секунды в понятную дату
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

# --- УПРАВЛЕНИЕ НАКАЗАНИЯМИ (АМНИСТИЯ) ---

# Команда /unwarn (Снять предупреждения)
@dp.message(Command("unwarn"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_unwarn(m: Message):
    # Проверка на администратора
    admins = await m.chat.get_administrators()
    if m.from_user.id not in [admin.user.id for admin in admins]:
        await m.answer("❌ Эта команда доступна только администраторам.")
        return

    # Проверка, что команда отправлена реплаем (ответом) на сообщение
    if not m.reply_to_message:
        await m.answer("⚠️ Чтобы снять предупреждения, ответьте этой командой на сообщение пользователя.")
        return

    target_user = m.reply_to_message.from_user
    
    try:
        # Сбрасываем счетчик варнов в базе (используем нашу готовую функцию)
        reset_warns(target_user.id, m.chat.id)
        await m.answer(f"✅ Предупреждения пользователя <b>{target_user.first_name}</b> обнулены.", parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при снятии варна: {e}")

# Команда /unmute (Снять мут)
@dp.message(Command("unmute"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_unmute(m: Message):
    # Проверка на администратора
    admins = await m.chat.get_administrators()
    if m.from_user.id not in [admin.user.id for admin in admins]:
        await m.answer("❌ Эта команда доступна только администраторам.")
        return

    # Проверка на реплай
    if not m.reply_to_message:
        await m.answer("⚠️ Чтобы снять мут, ответьте этой командой на сообщение пользователя.")
        return

    target_user = m.reply_to_message.from_user
    from aiogram.types import ChatPermissions
    
    try:
        # Возвращаем пользователю все базовые права на отправку сообщений и медиа
        permissions = ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
        await bot.restrict_chat_member(
            chat_id=m.chat.id, 
            user_id=target_user.id, 
            permissions=permissions
        )
        
        # Заодно обнуляем варны, чтобы он не улетел в мут при следующем же нарушении
        reset_warns(target_user.id, m.chat.id)
        
        await m.answer(f"🔊 Мут снят! <b>{target_user.first_name}</b> снова может писать сообщения.", parse_mode="HTML")
    except Exception as e:
        await m.answer("❌ Не удалось снять мут. Возможно, этот пользователь не в муте, или у бота не хватает прав.")
        print(f"Ошибка при снятии мута: {e}")

# Команда статистики (только для админов)
@dp.message(Command("stats"), F.chat.type.in_({"group", "supergroup"}))
async def show_stats(m: Message):
    admins = await m.chat.get_administrators()
    if m.from_user.id not in [admin.user.id for admin in admins]:
        await m.answer("❌ Эта команда доступна только администраторам чата.")
        return

    d_count, m_count = get_stats(m.chat.id)
    
    text = (
        f"📊 <b>Статистика модерации:</b>\n\n"
        f"🗑 Удалено сообщений: <b>{d_count}</b>\n"
        f"🤐 Выдано мутов: <b>{m_count}</b>"
    )
    await m.answer(text, parse_mode="HTML")

# --- ПАНЕЛЬ ВЛАДЕЛЬЦА БОТА ---

@dp.message(Command("botstats"))
async def cmd_botstats(m: Message):
    # ЗАМЕНИТЕ ЦИФРЫ НИЖЕ НА ВАШ СКОПИРОВАННЫЙ TELEGRAM ID!
    OWNER_ID = 354584527 
    
    # Если команду пишет кто-то другой, бот просто промолчит
    if m.from_user.id != OWNER_ID:
        return
        
    try:
        # Считаем общее количество чатов (базовые + премиум)
        cursor.execute('SELECT COUNT(*) FROM chats_v2')
        total_chats = cursor.fetchone()[0]
        
        # Считаем только чаты с активным Premium
        current_time = datetime.now().timestamp()
        cursor.execute('SELECT COUNT(*) FROM chats_v2 WHERE ai_enabled = TRUE AND premium_until > %s', (current_time,))
        premium_chats = cursor.fetchone()[0]
        
        # Считаем базовые чаты (Математика: Всего - Премиум)
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

# Команда /chatlist (Показать список чатов)
@dp.message(Command("chatlist"))
async def cmd_chatlist(m: Message, bot: Bot):
    # ЗАМЕНИТЕ НА ВАШ ID (тот же самый, что и в /botstats)
    OWNER_ID = 354584527
    
    if m.from_user.id != OWNER_ID:
        return

    # Берем последние 30 чатов из базы, чтобы не упереться в лимиты Телеграма
    cursor.execute('SELECT chat_id, ai_enabled, premium_until FROM chats_v2 LIMIT 30')
    chats = cursor.fetchall()
    
    if not chats:
        await m.answer("Список чатов пока пуст.")
        return
        
    await m.answer("⏳ Собираю информацию о чатах, подождите...")
    
    text = "📋 <b>Список чатов с ботом:</b>\n\n"
    current_time = datetime.now().timestamp()
    
    for chat_id, ai_enabled, premium_until in chats:
        try:
            # Спрашиваем у Телеграма название чата по его ID
            chat_info = await bot.get_chat(chat_id)
            chat_name = chat_info.title or "Без названия"
            
            # Определяем статус подписки
            if ai_enabled and premium_until and premium_until > current_time:
                status = "🌟 PREMIUM"
            else:
                status = "🌑 Базовый"
                
            text += f"🔹 <b>{chat_name}</b>\n└ Статус: {status}\n\n"
        except Exception:
            # Если бот был удален из чата, Телеграм выдаст ошибку, обрабатываем её
            text += f"🔹 <i>Чат недоступен (ID: {chat_id})</i>\n└ Скорее всего, бота оттуда удалили\n\n"
            
    # Отправляем итоговый список
    try:
        await m.answer(text, parse_mode="HTML")
    except Exception as e:
        await m.answer(f"❌ Ошибка отправки списка (возможно, он слишком длинный): {e}")

# Команда /give_premium (Выдать премиум бесплатно - только для владельца)
@dp.message(Command("give_premium"))
async def cmd_give_premium(m: Message):
    # ВПИШИТЕ СЮДА ВАШ TELEGRAM ID (как в /botstats)
    OWNER_ID = 354584527 
    
    if m.from_user.id != OWNER_ID:
        return
        
    try:
        # Даем премиум на 30 дней от текущего момента
        future_time = (datetime.now() + timedelta(days=30)).timestamp()
        
        # Обновляем данные текущего чата в базе
        cursor.execute('''
            UPDATE chats_v2 
            SET ai_enabled = TRUE, premium_until = %s 
            WHERE chat_id = %s
        ''', (future_time, m.chat.id))
        
        # Если вы используете conn.commit() в других местах, раскомментируйте строку ниже
        # conn.commit() 
        
        await m.answer("🎁 <b>Режим разработчика:</b> Premium-статус (ИИ-модерация) успешно активирован в этом чате на 30 дней!", parse_mode="HTML")
    except Exception as e:
        await m.answer(f"❌ Ошибка при выдаче Premium: {e}")



# --- ОПЛАТА PREMIUM ЧЕРЕЗ TELEGRAM STARS ---

# 1. Отправка счета (Инвойса)
@dp.message(Command("buy_premium"), F.chat.type.in_({"group", "supergroup"}))
async def send_invoice(m: Message):
    prices = [LabeledPrice(label="Premium AI Модератор", amount=50)] 
    
    await bot.send_invoice(
        chat_id=m.chat.id,
        title="Premium AI Модератор",
        description="Включение нейросети Gemini для точного распознавания скрытой агрессии и завуалированного мата на 30 дней.",
        payload="premium_activation",
        provider_token="", # Оставляем пустым для Stars
        currency="XTR",    # Валюта Telegram Stars
        prices=prices
    )

# 2. Подтверждение перед оплатой
@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

# 3. Обработка успешного платежа и выдача прав
@dp.message(F.successful_payment)
async def successful_payment_handler(m: Message):
    add_chat(m.chat.id)
    set_ai(m.chat.id, True, days=30) 
    
    stars = m.successful_payment.total_amount
    await m.answer(
        f"🎉 <b>Спасибо за поддержку!</b> Оплата в {stars} Stars получена.\n"
        f"✅ <b>Premium активирован на 30 дней!</b> Нейросеть Gemini успешно подключена к этому чату.", 
        parse_mode="HTML"
    )

# --- ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ ---

# --- ФУНКЦИЯ ИИ-МОДЕРАЦИИ (Gemini) ---
async def ai_filter(text: str) -> bool:
    import aiohttp # Используем прямое подключение вместо библиотеки Google
    
    try:
        print(f"🧠 ОТПРАВЛЯЮ В GEMINI: {text[:20]}...", flush=True)
        
        prompt = (
            "Ты — строгий модератор публичного чата. Твоя задача — анализировать сообщения "
            "и находить в них нарушения. "
            "Отвечай ТОЛЬКО словом 'True' (если сообщение нужно удалить) или 'False' (если оно нормальное).\n\n"
            "Что нужно удалять (True):\n"
            "1. Завуалированный мат, оскорбления, токсичность и пассивную агрессию.\n"
            "2. Спам, рекламу легкого заработка, ставки, казино, криптовалюту.\n"
            "3. Мошеннические схемы (например, обнал 'Пушкинских карт', перевод бонусов в деньги).\n"
            "4. Бессмысленные комментарии-наживки от ботов (шаблонные комплименты не по теме).\n\n"
            f"Сообщение для проверки: {text}"
        )
        
                # Прямая ссылка на сервер Google (стабильная модель 1.5-flash)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"

        # Упаковываем запрос и отключаем цензуру
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
            ]
        }
        
        # Отправляем прямой запрос
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
                
                # Если Google ругается, выводим его ответ
                if resp.status != 200:
                    print(f"❌ Ошибка API Google: {data}", flush=True)
                    return False
                    
                # Достаем ответ нейросети
                result = data['candidates'][0]['content']['parts'][0]['text'].strip().lower()
                print(f"🤖 ОТВЕТ GEMINI: {result}", flush=True)
                
                return "true" in result

                
    except Exception as e:
        print(f"❌ Системная ошибка ИИ: {e}", flush=True)
        return False





@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_messages(m: Message):
    # Игнорируем системные пересылки постов из канала
    if not m.text or m.is_automatic_forward:
        return
        
    add_chat(m.chat.id)
    text = m.text
    
    # --- НОВЫЙ БЛОК: ИММУНИТЕТ ДЛЯ АДМИНОВ ---
    # Если пишет обычный человек (не от имени канала)
    if m.from_user:
        try:
            member = await bot.get_chat_member(m.chat.id, m.from_user.id)
            if member.status in ['creator', 'administrator']:
                return # Админам можно всё, прекращаем проверку сообщения
        except Exception:
            pass
    # ----------------------------------------
    
    # ... дальше идет ваш код проверки на ссылки и мат ...

    # ... дальше идет ваш старый код проверки на ссылки и мат ...

    
        # --- НОВЫЙ БЛОК: ПРОВЕРКА НА ССЫЛКИ (Только для Premium) ---
    if is_ai(m.chat.id):
        has_link = False
        # Telegram сам помечает ссылки в сообщениях через m.entities
        if m.entities:
            for entity in m.entities:
                if entity.type in ["url", "text_link"]:
                    has_link = True
                    break
                    
        if has_link:
            # Получаем список всех администраторов чата
            admins = await m.chat.get_administrators()
            admin_ids = [admin.user.id for admin in admins]
            
            # Если отправителя нет в списке админов — выдаем наказание
            if m.from_user.id not in admin_ids:
                await punish(m, "Спам/Отправка ссылок")
                return # Останавливаем код, чтобы не проверять дальше
    # -----------------------------------------------------------

    # -----------------------------------------------------------
    
    # 1. Сначала проверяем базовым фильтром (быстро)
    if basic_filter(text):
        await punish(m, "Мат/Запрещенное слово")
        return
        
    # 2. Если базовый фильтр ничего не нашел, но включен ИИ - проверяем нейросетью
    if is_ai(m.chat.id):
        is_bad = await ai_filter(text)
        if is_bad:
            await punish(m, "Токсичность/Скрытый мат (AI)")


# --- ФУНКЦИЯ НАКАЗАНИЯ ---
async def punish(m: Message, reason: str):
    try:
        await m.delete()
        record_stat(m.chat.id, 'delete')
        
        # Проверяем, пишет ли человек от себя или от имени своего канала
        if m.sender_chat:
            user_id = m.sender_chat.id
            user_name = f"Канал {m.sender_chat.title}"
        else:
            user_id = m.from_user.id
            user_name = m.from_user.first_name
            
        warns = add_warn(user_id, m.chat.id)
        from aiogram.types import ChatPermissions
        
        if warns == 1:
            text = f"🚫 <b>{user_name}</b>, сообщение удалено ({reason}). \nЭто ваше первое предупреждение (1/3)."
            
        elif warns == 2:
            until = m.date + timedelta(minutes=5)
            # Если это обычный человек, даем мут
            if not m.sender_chat:
                await bot.restrict_chat_member(
                    chat_id=m.chat.id, user_id=user_id, 
                    permissions=ChatPermissions(can_send_messages=False), until_date=until
                )
            record_stat(m.chat.id, 'mute')
            text = f"⚠️ <b>{user_name}</b>, второе предупреждение (2/3)! \nВы получаете мут на 5 минут."
            
        else:
            until = m.date + timedelta(hours=1)
            if not m.sender_chat:
                await bot.restrict_chat_member(
                    chat_id=m.chat.id, user_id=user_id, 
                    permissions=ChatPermissions(can_send_messages=False), until_date=until
                )
            record_stat(m.chat.id, 'mute')
            reset_warns(user_id, m.chat.id)
            text = f"🛑 <b>{user_name}</b>, лимит исчерпан (3/3). \nВы получаете мут на 1 час."

        w = await m.answer(text, parse_mode="HTML")
        await asyncio.sleep(10)
        await w.delete()
        
    except Exception as e:
        print(f"Ошибка при выдаче наказания: {e}")



# --- ЗАПУСК БОТА ---
from flask import Flask
import os
from threading import Thread

# Создаем фейковый веб-сервер для Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот работает!"

def run_web():
    # Render сам выдает нужный порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

async def main():
    # Запускаем веб-сервер в фоновом режиме (в отдельном потоке)
    Thread(target=run_web).start()
    
    # Запускаем самого бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())


