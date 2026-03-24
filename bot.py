import telebot
import sqlite3
import re
import time
import threading
import datetime
import os
import sys
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Принудительный сброс webhook для предотвращения ошибки 409
try:
    url = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook"
    resp = requests.post(url, json={"drop_pending_updates": True})
    if resp.status_code == 200:
        print("Webhook сброшен, pending updates удалены")
    else:
        print(f"Ошибка сброса webhook: {resp.text}")
except Exception as e:
    print(f"Исключение при сбросе webhook: {e}")
time.sleep(2)

bot = telebot.TeleBot(TOKEN)

print("Проверяю соединение с Telegram API...")
try:
    me = bot.get_me()
    print(f"✅ Бот подключён: @{me.username}")
except Exception as e:
    print(f"❌ Ошибка: {e}")
    sys.exit(1)

conn = sqlite3.connect("db.db", check_same_thread=False)
cursor = conn.cursor()
db_lock = threading.Lock()

with db_lock:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER,
            chat_id INTEGER,
            username TEXT,
            last_active INTEGER,
            weekly_posts INTEGER DEFAULT 0,
            PRIMARY KEY(id, chat_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            author INTEGER,
            author_name TEXT,
            link TEXT,
            activity TEXT,
            created INTEGER,
            message_id INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS completions (
            task_id INTEGER,
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            time INTEGER,
            verified INTEGER DEFAULT 0
        )
    """)
    conn.commit()

    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]
    if "weekly_posts" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN weekly_posts INTEGER DEFAULT 0")
        conn.commit()

link_pattern = r"(?:https?://)?(?:www\.)?(?:instagram\.com|instagr\.am|t\.me)/(?:[^\s]+)"
MSK = datetime.timezone(datetime.timedelta(hours=3))

def msk_now():
    return datetime.datetime.now(MSK)

def is_work_time(post_time):
    dt = datetime.datetime.fromtimestamp(post_time, tz=MSK)
    weekday = dt.weekday()
    hour = dt.hour
    if weekday == 0:        # понедельник
        return hour >= 7
    elif 1 <= weekday <= 3: # вторник–четверг
        return True
    elif weekday == 4:      # пятница
        return hour < 23
    else:                   # суббота, воскресенье
        return False

def is_admin(chat_id, user_id):
    # Сначала проверяем по списку из переменной окружения (опционально)
    admin_ids = os.environ.get("ADMIN_IDS", "")
    if admin_ids:
        admin_list = [int(x.strip()) for x in admin_ids.split(",") if x.strip().isdigit()]
        if user_id in admin_list:
            return True
    try:
        status = bot.get_chat_member(chat_id, user_id).status
        return status in ["administrator", "creator"]
    except:
        return False

def task_link(chat_id, message_id):
    if message_id and chat_id < 0:
        cid = str(abs(chat_id))[3:]
        return f"https://t.me/c/{cid}/{message_id}"
    return None

def keyboard(task_id):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        "✅ Актив выполнен", callback_data=f"done_{task_id}"
    ))
    return markup

# ---------- Приветствие ----------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.chat.type == "private":
        bot.send_message(
            message.chat.id,
            "👋 Привет! Я бот для взаимной активности.\n\n"
            "📌 Правила:\n"
            "• Отправляй ссылки на посты в чат – я создам задание.\n"
            "• Выполнив задание, нажми кнопку под ним.\n"
            "• Чтобы узнать свои невыполненные задания, напиши /my_tasks в личку.\n\n"
            "Если есть вопросы – пиши администратору."
        )

# ---------- Команда /my_tasks ----------
@bot.message_handler(commands=['my_tasks'])
def my_tasks(message):
    if message.chat.type != "private":
        return

    user_id = message.from_user.id
    now = int(time.time())

    with db_lock:
        cursor.execute("""
            SELECT t.chat_id, t.id, t.link, t.activity, t.author_name, t.message_id
            FROM tasks t
            WHERE t.created > ?
              AND t.author != ?
              AND NOT EXISTS (
                  SELECT 1 FROM completions c
                  WHERE c.task_id = t.id AND c.user_id = ?
              )
            ORDER BY t.created DESC
        """, (now - 86400, user_id, user_id))
        tasks = cursor.fetchall()

    if not tasks:
        bot.send_message(user_id, "✅ У вас нет активных невыполненных заданий.")
        return

    # Отфильтровываем только те чаты, где пользователь ещё участник и не администратор
    filtered = []
    for task in tasks:
        chat_id = task[0]
        try:
            member = bot.get_chat_member(chat_id, user_id)
            if member.status in ["left", "kicked"]:
                continue
            if member.status in ["administrator", "creator"]:
                continue
            filtered.append(task)
        except Exception:
            # Не удалось получить информацию о членстве – пропускаем
            continue

    if not filtered:
        bot.send_message(user_id, "✅ У вас нет активных невыполненных заданий.")
        return

    # Группируем по чатам
    chats = {}
    for chat_id, task_id, link, activity, author_name, msg_id in filtered:
        if chat_id not in chats:
            try:
                chat = bot.get_chat(chat_id)
                chat_title = chat.title if chat.title else f"Чат {chat_id}"
            except:
                chat_title = f"Чат {chat_id}"
            chats[chat_id] = {'title': chat_title, 'tasks': []}
        chats[chat_id]['tasks'].append((task_id, link, activity, author_name, msg_id))

    response = "📋 *Ваши активные задания:*\n\n"
    for chat_id, data in chats.items():
        response += f"*{data['title']}*:\n"
        for task_id, link, activity, author_name, msg_id in data['tasks']:
            msg_link = task_link(chat_id, msg_id) or link
            response += f"• [Задание]({msg_link})\n"
        response += "\n"
    response += "Нажмите на ссылку, чтобы перейти к заданию, затем выполните его и нажмите кнопку ✅ Актив выполнен."

    try:
        bot.send_message(user_id, response, parse_mode='Markdown', disable_web_page_preview=True)
    except:
        bot.send_message(user_id, response.replace('*', ''), disable_web_page_preview=True)

# ---------- Обработчик сообщений в группах ----------
@bot.message_handler(func=lambda m: True)
def handle_message(message):
    if not message.text:
        return
    # Игнорируем личные сообщения (всё, кроме команд, уже обработано)
    if message.chat.type == "private":
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    is_user_admin = is_admin(chat_id, user_id)

    # ---- Удаление сообщений в нерабочее время (выходные) ----
    if not is_work_time(message.date):
        if not is_user_admin:
            try:
                bot.delete_message(chat_id, message.message_id)
            except:
                pass
        return

    # ---- Проверка наличия ссылок ----
    matches = re.findall(link_pattern, message.text)
    if not matches:
        if not is_user_admin:
            try:
                bot.delete_message(chat_id, message.message_id)
            except:
                pass
        return

    link = matches[0]
    activity = message.text.replace(link, "").strip() or "лайк"
    now = int(time.time())

    # ---- Недельный лимит: 4 поста (не для администраторов) ----
    if not is_user_admin:
        with db_lock:
            cursor.execute(
                "SELECT weekly_posts FROM users WHERE id=? AND chat_id=?",
                (user_id, chat_id)
            )
            row = cursor.fetchone()
            current_posts = row[0] if row else 0

        if current_posts >= 4:
            bot.send_message(
                chat_id,
                f"❗ @{message.from_user.username}, лимит 4 задания в рабочую неделю исчерпан. Задание не создано."
            )
            if not is_user_admin:
                try:
                    bot.delete_message(chat_id, message.message_id)
                except:
                    pass
            return

    # ---- Создаём задание ----
    with db_lock:
        cursor.execute(
            "INSERT INTO tasks (chat_id, author, author_name, link, activity, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, message.from_user.username, link, activity, now)
        )
        task_id = cursor.lastrowid

        # Обновляем счётчик постов (для обычных пользователей)
        if not is_user_admin:
            cursor.execute(
                "INSERT INTO users (id, chat_id, username, last_active, weekly_posts) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(id, chat_id) DO UPDATE SET "
                "username=excluded.username, "
                "last_active=excluded.last_active, "
                "weekly_posts=weekly_posts+1",
                (user_id, chat_id, message.from_user.username, now)
            )
        else:
            # Администраторы не увеличивают счётчик
            cursor.execute(
                "INSERT INTO users (id, chat_id, username, last_active) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id, chat_id) DO UPDATE SET "
                "username=excluded.username, "
                "last_active=excluded.last_active",
                (user_id, chat_id, message.from_user.username, now)
            )
        conn.commit()

    sent = bot.send_message(
        chat_id,
        f"📢 Новое задание\n\n@{message.from_user.username}\n{link}\n{activity}",
        reply_markup=keyboard(task_id)
    )

    with db_lock:
        cursor.execute("UPDATE tasks SET message_id=? WHERE id=?", (sent.message_id, task_id))
        conn.commit()

    if not is_user_admin:
        try:
            bot.delete_message(chat_id, message.message_id)
        except:
            pass

# ---------- Обработчик нажатий кнопки ----------
@bot.callback_query_handler(func=lambda call: call.data.startswith("done_"))
def done(call):
    task_id = int(call.data.split("_")[1])
    now = int(time.time())

    with db_lock:
        cursor.execute("SELECT created, chat_id, author FROM tasks WHERE id=?", (task_id,))
        task = cursor.fetchone()

    if not task:
        bot.answer_callback_query(call.id)
        return

    created, chat_id, author_id = task

    if call.from_user.id == author_id:
        bot.answer_callback_query(call.id)
        return

    if now - created < 10:
        bot.answer_callback_query(call.id)
        return

    with db_lock:
        cursor.execute(
            "SELECT * FROM completions WHERE task_id=? AND user_id=? AND chat_id=?",
            (task_id, call.from_user.id, chat_id)
        )
        if cursor.fetchone():
            bot.answer_callback_query(call.id)
            return

    # Засчитываем выполнение
    with db_lock:
        cursor.execute(
            "INSERT INTO completions (task_id, chat_id, user_id, username, time, verified) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (task_id, chat_id, call.from_user.id, call.from_user.username, now)
        )
        cursor.execute(
            "INSERT OR REPLACE INTO users (id, chat_id, username, last_active, weekly_posts) "
            "VALUES (?, ?, ?, ?, COALESCE((SELECT weekly_posts FROM users WHERE id=? AND chat_id=?), 0))",
            (call.from_user.id, chat_id, call.from_user.username, now, call.from_user.id, chat_id)
        )
        conn.commit()

    bot.answer_callback_query(call.id)

# ---------- Функция обработки истекших заданий для чата ----------
def process_expired_tasks_for_chat(chat_id):
    """Проверяет истекшие задания в указанном чате и отправляет отчёт."""
    now = int(time.time())
    with db_lock:
        cursor.execute(
            "SELECT id, created, author, author_name, message_id, link FROM tasks WHERE chat_id=?",
            (chat_id,)
        )
        tasks = cursor.fetchall()

    for task in tasks:
        task_id, created, author_id, author_name, msg_id, link = task
        if now - created <= 86400:
            continue  # ещё не истекло

        with db_lock:
            cursor.execute(
                "SELECT username FROM completions WHERE task_id=? AND chat_id=?",
                (task_id, chat_id)
            )
            done_users = {x[0] for x in cursor.fetchall()}
            cursor.execute(
                "SELECT username FROM users WHERE chat_id=?",
                (chat_id,)
            )
            all_users = {x[0] for x in cursor.fetchall()}

        admins = set()
        for u in all_users:
            with db_lock:
                cursor.execute("SELECT id FROM users WHERE username=? AND chat_id=?", (u, chat_id))
                row = cursor.fetchone()
            if row and is_admin(chat_id, row[0]):
                admins.add(u)

        not_done = (all_users - done_users) - {author_name} - admins

        link_msg = task_link(chat_id, msg_id) or link
        if not_done:
            text = "❌ Не выполнили задание"
            if link_msg:
                text += f" ({link_msg})"
            text += ":\n\n" + "\n".join([f"@{u}" for u in not_done if u])
        else:
            text = "✅ Все выполнили задание"
            if link_msg:
                text += f" ({link_msg})"

        try:
            bot.send_message(chat_id, text)
        except Exception as e:
            print(f"Ошибка отправки отчёта: {e}")

        with db_lock:
            cursor.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            conn.commit()

# ---------- Команды для глобальных администраторов ----------
def is_global_admin(user_id):
    """Проверяет, является ли пользователь глобальным администратором (из ADMIN_IDS)."""
    admin_ids = os.environ.get("ADMIN_IDS", "")
    if not admin_ids:
        return False
    admin_list = [int(x.strip()) for x in admin_ids.split(",") if x.strip().isdigit()]
    return user_id in admin_list

@bot.message_handler(commands=['stats'])
def stats_command(message):
    if message.chat.type != "private":
        bot.reply_to(message, "Эта команда доступна только в личных сообщениях.")
        return
    if not is_global_admin(message.from_user.id):
        bot.reply_to(message, "⛔ У вас нет прав на использование этой команды.")
        return

    with db_lock:
        # Общее количество уникальных пользователей
        cursor.execute("SELECT COUNT(DISTINCT id) FROM users")
        total_users = cursor.fetchone()[0]

        # Количество чатов
        cursor.execute("SELECT COUNT(DISTINCT chat_id) FROM users")
        total_chats = cursor.fetchone()[0]

        # Активные задания (созданные за последние 24 часа)
        now = int(time.time())
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE created > ?", (now - 86400,))
        active_tasks = cursor.fetchone()[0]

        # Выполненные задания за последние 7 дней
        week_ago = now - 604800
        cursor.execute("SELECT COUNT(*) FROM completions WHERE time > ?", (week_ago,))
        weekly_completions = cursor.fetchone()[0]

        # Топ-5 исполнителей за неделю
        cursor.execute("""
            SELECT username, COUNT(*) as cnt
            FROM completions
            WHERE time > ?
            GROUP BY user_id
            ORDER BY cnt DESC
            LIMIT 5
        """, (week_ago,))
        top = cursor.fetchall()

    text = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"💬 Активных чатов: {total_chats}\n"
        f"📝 Активных заданий: {active_tasks}\n"
        f"✅ Выполнено за неделю: {weekly_completions}\n\n"
    )

    if top:
        text += "🏆 **Топ исполнителей (неделя):**\n"
        for username, cnt in top:
            text += f"@{username} — {cnt}\n"
    else:
        text += "Нет выполненных заданий за последнюю неделю."

    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(commands=['debug_tasks'])
def debug_tasks_command(message):
    if message.chat.type != "private":
        bot.reply_to(message, "Эта команда доступна только в личных сообщениях.")
        return
    if not is_global_admin(message.from_user.id):
        bot.reply_to(message, "⛔ У вас нет прав на использование этой команды.")
        return

    now = int(time.time())
    with db_lock:
        cursor.execute("""
            SELECT id, chat_id, author_name, link, activity, created, message_id
            FROM tasks
            WHERE created > ?
            ORDER BY created DESC
        """, (now - 86400,))
        tasks = cursor.fetchall()

    if not tasks:
        bot.send_message(message.chat.id, "Активных заданий нет.")
        return

    text = "📋 **Активные задания**\n\n"
    for task in tasks:
        task_id, chat_id, author, link, activity, created, msg_id = task
        time_str = datetime.datetime.fromtimestamp(created, tz=MSK).strftime("%d.%m %H:%M")
        link_msg = task_link(chat_id, msg_id) or link
        text += (
            f"ID: {task_id}\n"
            f"Чат: {chat_id}\n"
            f"Автор: @{author}\n"
            f"Ссылка: {link_msg}\n"
            f"Актив: {activity}\n"
            f"Создано: {time_str}\n"
            f"---\n"
        )
        if len(text) > 3800:  # защита от превышения лимита
            bot.send_message(message.chat.id, text[:4000])
            text = ""

    if text:
        bot.send_message(message.chat.id, text, parse_mode="Markdown", disable_web_page_preview=True)

@bot.message_handler(commands=['force_report'])
def force_report_command(message):
    if message.chat.type != "private":
        bot.reply_to(message, "Эта команда доступна только в личных сообщениях.")
        return
    if not is_global_admin(message.from_user.id):
        bot.reply_to(message, "⛔ У вас нет прав на использование этой команды.")
        return

    # Собираем все чаты, где есть задания или пользователи
    with db_lock:
        cursor.execute("SELECT DISTINCT chat_id FROM users")
        chats = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT DISTINCT chat_id FROM tasks")
        chats |= {row[0] for row in cursor.fetchall()}

    if not chats:
        bot.reply_to(message, "Нет чатов для обработки.")
        return

    bot.reply_to(message, f"🔄 Обрабатываю {len(chats)} чатов...")
    for chat_id in chats:
        process_expired_tasks_for_chat(chat_id)

    bot.reply_to(message, "✅ Отчёты отправлены.")

# ---------- Планировщик ----------
def scheduler():
    weekly_reported = set()
    friday_notified = set()
    monday_notified = set()
    last_week_reset = 0

    while True:
        now = int(time.time())
        now_dt = msk_now()
        day = now_dt.weekday()
        hour = now_dt.hour
        week_num = now_dt.isocalendar()[1]

        # ---- Сброс счётчика weekly_posts в понедельник 00:00 МСК ----
        if day == 0 and hour == 0 and now - last_week_reset > 3600:
            with db_lock:
                cursor.execute("UPDATE users SET weekly_posts = 0")
                conn.commit()
            last_week_reset = now
            print("Сброшен недельный счётчик постов")

        with db_lock:
            cursor.execute("SELECT DISTINCT chat_id FROM users")
            chats = {r[0] for r in cursor.fetchall()}
            cursor.execute("SELECT DISTINCT chat_id FROM tasks")
            chats |= {r[0] for r in cursor.fetchall()}

        for chat_id in chats:
            # Пятница 23:00
            fri_key = (chat_id, week_num)
            if day == 4 and hour == 23 and fri_key not in friday_notified:
                try:
                    bot.send_message(chat_id, "🌙 Пост-чат ушел на выходные! Отличных выходных!")
                except:
                    pass
                friday_notified.add(fri_key)

            # Понедельник 7:00
            mon_key = (chat_id, week_num)
            if day == 0 and hour == 7 and mon_key not in monday_notified:
                try:
                    bot.send_message(chat_id, "☀️ Доброе утро, пост-чат работает в нормальном режиме")
                except:
                    pass
                monday_notified.add(mon_key)

            # Обработка истекших заданий
            process_expired_tasks_for_chat(chat_id)

            # Недельный отчёт (воскресенье 12:00)
            week_key = (chat_id, week_num)
            if day == 6 and hour == 12 and week_key not in weekly_reported:
                week_ago = now - 604800
                with db_lock:
                    cursor.execute(
                        "SELECT username FROM users WHERE chat_id=? AND last_active<?",
                        (chat_id, week_ago)
                    )
                    inactive = [f"@{x[0]}" for x in cursor.fetchall() if x[0]]
                    cursor.execute(
                        "SELECT username, COUNT(*) as c FROM completions WHERE chat_id=? "
                        "GROUP BY user_id ORDER BY c DESC LIMIT 5",
                        (chat_id,)
                    )
                    top = cursor.fetchall()

                text = "📊 **Недельный отчёт**\n\n"
                if inactive:
                    text += "❌ Неактивные:\n" + "\n".join(inactive) + "\n\n"
                else:
                    text += "✅ Все активны!\n\n"
                if top:
                    text += "🏆 **Топ по выполнениям:**\n"
                    for t in top:
                        text += f"@{t[0]} — {t[1]}\n"

                try:
                    bot.send_message(chat_id, text, parse_mode="Markdown")
                except:
                    pass
                weekly_reported.add(week_key)

        time.sleep(60)

# ---------- Health-сервер ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()
threading.Thread(target=scheduler, daemon=True).start()

print("Бот запущен...")
# Сбросьте вебхук и очистите очередь
bot.delete_webhook(drop_pending_updates=True)
time.sleep(2)  # Дайте время на очистку

# Теперь запустите polling
bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
