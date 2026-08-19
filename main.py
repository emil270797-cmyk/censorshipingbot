import asyncio
import os
import re
import psycopg2
import pymorphy3
from datetime import timedelta
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery
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

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_messages(m: Message):
    if not m.text:
        return
        
    add_chat(m.chat.id)
    text = m.text
    
    # --- НОВЫЙ БЛОК: ПРОВЕРКА НА ССЫЛКИ (Только для Premium) ---
    if is_ai(m.chat.id):
        has_link = False
        # Telegram сам помечает ссылки в сообщениях через m.entities
        if m.entities:
            for entity in m.entities:
                # url - обычные ссылки, text_link - слова со встроенной ссылкой
                if entity.type in ["url", "text_link"]:
                    has_link = True
                    break
                    
        if has_link:
            await punish(m, "Спам/Отправка ссылок")
            return # Останавливаем код, чтобы не проверять дальше
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
        
        warns = add_warn(m.from_user.id, m.chat.id)
        user_name = m.from_user.first_name
        from aiogram.types import ChatPermissions
        
        if warns == 1:
            text = f"🚫 <b>{user_name}</b>, сообщение удалено ({reason}). \nЭто ваше первое предупреждение (1/3)."
            
        elif warns == 2:
            until = m.date + timedelta(minutes=5)
            await bot.restrict_chat_member(
                chat_id=m.chat.id, user_id=m.from_user.id, 
                permissions=ChatPermissions(can_send_messages=False), until_date=until
            )
            record_stat(m.chat.id, 'mute')
            text = f"⚠️ <b>{user_name}</b>, второе предупреждение (2/3)! \nВы получаете мут на 5 минут."
            
        else:
            until = m.date + timedelta(hours=1)
            await bot.restrict_chat_member(
                chat_id=m.chat.id, user_id=m.from_user.id, 
                permissions=ChatPermissions(can_send_messages=False), until_date=until
            )
            record_stat(m.chat.id, 'mute')
            reset_warns(m.from_user.id, m.chat.id)
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


