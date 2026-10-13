import asyncio
import logging
import os
import secrets
import sqlite3
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

try:
    from dotenv import load_dotenv

    for _f in (".env", "env.txt", "env"):
        load_dotenv(_f)
except ImportError:
    pass

# ======================= НАСТРОЙКИ (.env) =======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
POSTBACK_SECRET = os.getenv("POSTBACK_SECRET", "")  # пусто = постбэк выключен
POSTBACK_PORT = int(os.getenv("POSTBACK_PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "bot.db")
DEFAULT_MAX_LINKS = 150
# ================================================================

logging.basicConfig(level=logging.INFO)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ----------------------------- БАЗА ------------------------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")


def init_db():
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            status TEXT,              -- pending / approved / rejected
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sources(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            url TEXT                  -- шаблон ссылки, {id} заменяется на код
        );
        CREATE TABLE IF NOT EXISTS links(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            num INTEGER,
            source_id INTEGER,
            code TEXT UNIQUE,
            url TEXT,
            leads INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS channels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_links_user ON links(user_id);
        CREATE INDEX IF NOT EXISTS idx_links_leads ON links(leads);
        """
    )
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(links)")]
    if "channel" not in cols:
        conn.execute("ALTER TABLE links ADD COLUMN channel TEXT")
    conn.commit()


def fetch(sql, args=()):
    return conn.execute(sql, args).fetchall()


def fetch_one(sql, args=()):
    return conn.execute(sql, args).fetchone()


def execute(sql, args=()):
    cur = conn.execute(sql, args)
    conn.commit()
    return cur


def now_str(with_seconds=True):
    fmt = "%d.%m.%Y %H:%M:%S" if with_seconds else "%d.%m.%Y %H:%M"
    return datetime.now(ZoneInfo(TIMEZONE)).strftime(fmt)


def get_max_links() -> int:
    row = fetch_one("SELECT value FROM settings WHERE key='max_links'")
    return int(row["value"]) if row else DEFAULT_MAX_LINKS


def set_max_links(value: int):
    execute("INSERT OR REPLACE INTO settings(key, value) VALUES('max_links', ?)", (str(value),))


def get_user(user_id: int):
    return fetch_one("SELECT * FROM users WHERE user_id=?", (user_id,))


def is_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    u = get_user(user_id)
    return bool(u and u["status"] == "approved")


def build_url(template: str, code: str, channel: str = "") -> str:
    template = template.replace("{channel}", quote(channel, safe=""))
    if "{id}" in template:
        return template.replace("{id}", code)
    sep = "&" if "?" in template else "?"
    return f"{template}{sep}sub_id={code}"


def display_name(row) -> str:
    if row["username"]:
        return f"@{row['username']}"
    return row["full_name"] or str(row["user_id"])


# --------------------------- КЛАВИАТУРЫ --------------------------
def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("🔗 Создать ссылку", "create"), btn("📋 Мои ссылки", "my_links")],
            [btn("📊 Моя статистика", "stats"), btn("🏆 Рейтинг", "rating")],
            [btn("ℹ️ Как это работает", "howto")],
        ]
    )


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Вернуться в меню", "menu")]])


def menu_text(name: str) -> str:
    return f"📱 Главное меню\n\n👋 Привет, {name}!"


async def safe_edit(cb: CallbackQuery, text: str, kb=None):
    try:
        await cb.message.edit_text(text, reply_markup=kb, link_preview_options=NO_PREVIEW)
    except TelegramBadRequest:
        pass


async def send_chunks(message: Message, lines: list[str], last_kb=None):
    """Отправляет список строк, разбивая на сообщения < 4096 символов."""
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await message.answer(
            chunk,
            reply_markup=last_kb if is_last else None,
            link_preview_options=NO_PREVIEW,
        )


# ------------------------------ FSM ------------------------------
class Create(StatesGroup):
    channel = State()
    name = State()
    count = State()
    source = State()


class Admin(StatesGroup):
    max_links = State()
    src_name = State()
    src_url = State()
    lead_amount = State()
    ch_name = State()


not_command = ~F.text.startswith("/")

# ============================ РОУТЕРЫ ============================
admin_router = Router()
access_router = Router()
user_router = Router()

admin_router.message.filter(F.from_user.id == ADMIN_ID)
admin_router.callback_query.filter(F.from_user.id == ADMIN_ID)
user_router.message.filter(lambda m: is_allowed(m.from_user.id))
user_router.callback_query.filter(lambda c: is_allowed(c.from_user.id))


# ======================= ДОСТУП / /start =========================
def access_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("ПОЛУЧИТЬ ДОСТУП ✅", "get_access")]])


@access_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    if is_allowed(uid):
        await m.answer(menu_text(m.from_user.first_name), reply_markup=main_kb())
        return
    u = get_user(uid)
    if u and u["status"] == "rejected":
        return  # отклонён — бот молчит
    if u and u["status"] == "pending":
        await m.answer("⏳ Ваша заявка уже отправлена и ожидает рассмотрения.")
        return
    await m.answer(
        "👋 Для начала работы с Traffic Checker\nнужно получить доступ.\n\n"
        "Нажмите кнопку ниже 👇",
        reply_markup=access_kb(),
    )


@access_router.callback_query(F.data == "get_access")
async def get_access(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    u = get_user(uid)
    if is_allowed(uid):
        await cb.answer("У вас уже есть доступ. Введите /start", show_alert=True)
        return
    if u and u["status"] == "rejected":
        await cb.answer()
        return
    if u and u["status"] == "pending":
        await cb.answer("Заявка уже отправлена, ожидайте ⏳", show_alert=True)
        return

    execute(
        "INSERT OR REPLACE INTO users(user_id, username, full_name, status, created_at) "
        "VALUES(?,?,?,?,?)",
        (uid, cb.from_user.username, cb.from_user.full_name, "pending", now_str()),
    )
    await cb.answer("Заявка отправлена ✅ Ожидайте решения.", show_alert=True)

    uname = f"@{cb.from_user.username}" if cb.from_user.username else "нет username"
    text = (
        "🆕 Новая заявка на доступ\n\n"
        f"👤 Имя: {cb.from_user.full_name}\n"
        f"🔗 Username: {uname}\n"
        f"🆔 ID: {uid}\n"
        f"🕒 Время: {now_str()}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[btn("✅ Принять", f"acc:{uid}"), btn("❌ Отклонить", f"rej:{uid}")]]
    )
    await bot.send_message(ADMIN_ID, text, reply_markup=kb)


@admin_router.callback_query(F.data.startswith("acc:"))
async def accept_user(cb: CallbackQuery, bot: Bot):
    uid = int(cb.data.split(":")[1])
    execute("UPDATE users SET status='approved' WHERE user_id=?", (uid,))
    try:
        await bot.send_message(uid, "✅ Вам выдан доступ к боту!\n\nВведите команду /start для начала работы.")
    except TelegramForbiddenError:
        pass
    await safe_edit(cb, cb.message.text + "\n\n✅ ПРИНЯТО")
    await cb.answer("Доступ выдан")


@admin_router.callback_query(F.data.startswith("rej:"))
async def reject_user(cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])
    execute("UPDATE users SET status='rejected' WHERE user_id=?", (uid,))
    await safe_edit(cb, cb.message.text + "\n\n❌ ОТКЛОНЕНО")
    await cb.answer("Отклонено")


# ======================= ГЛАВНОЕ МЕНЮ ============================
@user_router.callback_query(F.data == "menu")
async def to_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(cb, menu_text(cb.from_user.first_name), main_kb())
    await cb.answer()


# ======================= СОЗДАТЬ ССЫЛКУ ==========================
@user_router.callback_query(F.data == "create")
async def create_start(cb: CallbackQuery, state: FSMContext):
    channels = fetch("SELECT * FROM channels ORDER BY id")
    if not channels:
        await state.clear()
        await safe_edit(cb, "⚠️ Каналы для залива пока не добавлены. Обратитесь к администратору.", back_kb())
        await cb.answer()
        return
    await state.set_state(Create.channel)
    rows = [[btn(c["name"], f"ch:{c['id']}")] for c in channels]
    rows.append([btn("⬅️ Вернуться в меню", "menu")])
    await safe_edit(cb, "📺 Выберите канал для залива", InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@user_router.callback_query(Create.channel, F.data.startswith("ch:"))
async def create_channel(cb: CallbackQuery, state: FSMContext):
    ch = fetch_one("SELECT * FROM channels WHERE id=?", (int(cb.data.split(":")[1]),))
    if not ch:
        await cb.answer("Канал не найден, начните заново", show_alert=True)
        return
    await state.update_data(channel=ch["name"])
    await state.set_state(Create.name)
    await safe_edit(cb, f"📺 Канал: {ch['name']}\n\n✏️ Введите название ссылок", back_kb())
    await cb.answer()


@user_router.message(Create.name, F.text, not_command)
async def create_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not name or len(name) > 50:
        await m.answer("❌ Название должно быть до 50 символов. Попробуйте ещё раз.", reply_markup=back_kb())
        return
    await state.update_data(name=name)
    await state.set_state(Create.count)
    await m.answer(f"🔢 Введите количество ссылок (максимум {get_max_links()})", reply_markup=back_kb())


@user_router.message(Create.count, F.text, not_command)
async def create_count(m: Message, state: FSMContext):
    mx = get_max_links()
    txt = m.text.strip()
    if not txt.isdigit() or not 1 <= int(txt) <= mx:
        await m.answer(f"❌ Введите число от 1 до {mx}", reply_markup=back_kb())
        return
    sources = fetch("SELECT * FROM sources ORDER BY id")
    if not sources:
        await state.clear()
        await m.answer("⚠️ Источники пока не добавлены. Обратитесь к администратору.", reply_markup=back_kb())
        return
    await state.update_data(count=int(txt))
    await state.set_state(Create.source)

    rows, row = [], []
    for s in sources:
        row.append(btn(s["name"], f"src:{s['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("⬅️ Вернуться в меню", "menu")])
    await m.answer("📡 Выбери источник", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@user_router.callback_query(Create.source, F.data.startswith("src:"))
async def create_finish(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    src = fetch_one("SELECT * FROM sources WHERE id=?", (int(cb.data.split(":")[1]),))
    if not src or "name" not in data:
        await cb.answer("Что-то пошло не так, начните заново", show_alert=True)
        return

    name, count = data["name"], data["count"]
    lines, batch = [], []
    created = now_str()
    for i in range(1, count + 1):
        code = secrets.token_hex(5)
        url = build_url(src["url"], code, data.get("channel", ""))
        batch.append((cb.from_user.id, name, i, src["id"], code, url, created, data.get("channel", "")))
        lines.append(f"{name} {i} | {url}")
    conn.executemany(
        "INSERT INTO links(user_id, name, num, source_id, code, url, created_at, channel) VALUES(?,?,?,?,?,?,?,?)",
        batch,
    )
    conn.commit()

    await safe_edit(cb, f"✅ Создано ссылок: {count}\n📺 Канал: {data.get('channel', '—')}\n📡 Источник: {src['name']}")
    await send_chunks(cb.message, lines, back_kb())
    await cb.answer()


# ========================= МОИ ССЫЛКИ ============================
@user_router.callback_query(F.data == "my_links")
async def my_links(cb: CallbackQuery):
    rows = fetch("SELECT name, num, url FROM links WHERE user_id=? ORDER BY id", (cb.from_user.id,))
    await cb.answer()
    if not rows:
        await safe_edit(cb, "📭 Вы ещё не создали ссылки.", back_kb())
        return
    await safe_edit(cb, f"📋 Ваши ссылки (всего: {len(rows)}):")
    lines = [f"{i}. {r['name']} {r['num']} | {r['url']}" for i, r in enumerate(rows, 1)]
    await send_chunks(cb.message, lines, back_kb())


# ========================== СТАТИСТИКА ===========================
STATS_PER_PAGE = 30


def stats_view(user_id: int, page: int = 0):
    links = fetch("SELECT name, num, leads FROM links WHERE user_id=? ORDER BY id", (user_id,))
    total_leads = sum(r["leads"] for r in links)
    pages = max(1, -(-len(links) // STATS_PER_PAGE))
    page = min(max(page, 0), pages - 1)

    text = f"📊 Моя статистика\n🕒 Обновлено: {now_str()}\n\n🔗 Создано ссылок: {len(links)}\n"
    if links:
        text += "\n📎 Лиды по каждой ссылке:\n"
        start = page * STATS_PER_PAGE
        for i, r in enumerate(links[start:start + STATS_PER_PAGE], start + 1):
            text += f"{i}. {r['name']} {r['num']} — {r['leads']}\n"
        if pages > 1:
            text += f"\nСтраница {page + 1} из {pages}\n"
    text += f"\n🎯 Общее количество залитых лидов: {total_leads}"

    rows = []
    if pages > 1:
        rows.append([btn("◀️", f"stats:{page - 1}"), btn(f"{page + 1}/{pages}", "noop"), btn("▶️", f"stats:{page + 1}")])
    rows.append([btn("🔄 Обновить страницу", f"stats:{page}")])
    rows.append([btn("⬅️ Вернуться в меню", "menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@user_router.callback_query(F.data.startswith("stats"))
async def stats(cb: CallbackQuery):
    part = cb.data.split(":")
    page = int(part[1]) if len(part) > 1 and part[1].lstrip("-").isdigit() else 0
    text, kb = stats_view(cb.from_user.id, page)
    await safe_edit(cb, text, kb)
    await cb.answer()


@user_router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


# ============================ РЕЙТИНГ ============================
@user_router.callback_query(F.data == "rating")
async def rating(cb: CallbackQuery):
    rows = fetch(
        "SELECT u.user_id, u.username, u.full_name, COALESCE(SUM(l.leads),0) s "
        "FROM users u JOIN links l ON l.user_id=u.user_id "
        "GROUP BY u.user_id ORDER BY s DESC LIMIT 10"
    )
    # админ тоже может лить — добавим его при наличии ссылок
    admin_row = fetch_one(
        "SELECT user_id, COALESCE(SUM(leads),0) s FROM links WHERE user_id=? GROUP BY user_id", (ADMIN_ID,)
    )
    medals = ["🥇", "🥈", "🥉"]
    entries = [(display_name(r), r["s"]) for r in rows]
    if admin_row and not any(r["user_id"] == ADMIN_ID for r in rows):
        entries.append(("Администратор", admin_row["s"]))
        entries.sort(key=lambda x: x[1], reverse=True)
        entries = entries[:10]

    if not entries:
        text = "🏆 Рейтинг\n\nПока нет данных."
    else:
        text = "🏆 Топ-10 траферов по количеству залитых лидов\n\n"
        for i, (name, s) in enumerate(entries, 1):
            mark = medals[i - 1] if i <= 3 else f"{i}."
            text += f"{mark} {name} — {s}\n"
    await safe_edit(cb, text, back_kb())
    await cb.answer()


# ======================== КАК ЭТО РАБОТАЕТ =======================
HOWTO = (
    "ℹ️ Как пользоваться ботом\n\n"
    "🔗 Создать ссылку\n"
    "1. Нажмите «Создать ссылку».\n"
    "2. Введите название (например, GG).\n"
    "3. Введите количество ссылок.\n"
    "4. Выберите источник.\n"
    "5. Бот пришлёт готовые ссылки вида: GG 1 | ссылка\n\n"
    "📋 Мои ссылки\n"
    "Все созданные вами ссылки списком с нумерацией.\n\n"
    "📊 Моя статистика\n"
    "Число созданных ссылок, разбивка по названиям и общее количество "
    "залитых лидов. Кнопка «Обновить страницу» подтягивает свежие данные.\n\n"
    "🏆 Рейтинг\n"
    "Топ-10 траферов по количеству залитых лидов.\n\n"
    "⬅️ Кнопка «Вернуться в меню» есть на каждом экране."
)


@user_router.callback_query(F.data == "howto")
async def howto(cb: CallbackQuery):
    await safe_edit(cb, HOWTO, back_kb())
    await cb.answer()


# ========================== АДМИН-ПАНЕЛЬ =========================
def admin_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(f"⚙️ Макс. ссылок: {get_max_links()}", "adm:max")],
            [btn("📺 Каналы для залива", "adm:channels"), btn("📡 Источники", "adm:sources")],
            [btn("👥 Пользователи", "adm:users")],
            [btn("➕ Добавить лиды", "adm:lead:add"), btn("➖ Списать лиды", "adm:lead:sub")],
            [btn("📈 Общая статистика", "adm:stats"), btn("🗑 Очистка ссылок", "adm:cleanup")],
        ]
    )


def admin_back():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ В админ-панель", "adm:home")]])


@admin_router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 Админ-панель", reply_markup=admin_kb())


@admin_router.callback_query(F.data == "adm:home")
async def adm_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(cb, "🛠 Админ-панель", admin_kb())
    await cb.answer()


# --- макс. ссылок
@admin_router.callback_query(F.data == "adm:max")
async def adm_max(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Admin.max_links)
    await safe_edit(cb, f"Текущий максимум: {get_max_links()}\nВведите новое число:", admin_back())
    await cb.answer()


@admin_router.message(Admin.max_links, F.text, not_command)
async def adm_max_set(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or int(m.text) < 1:
        await m.answer("❌ Введите целое число больше 0", reply_markup=admin_back())
        return
    set_max_links(int(m.text))
    await state.clear()
    await m.answer(f"✅ Максимум ссылок за раз: {int(m.text)}", reply_markup=admin_kb())


# --- источники
async def render_sources(cb: CallbackQuery):
    sources = fetch("SELECT * FROM sources ORDER BY id")
    text = "📡 Источники\n\n"
    rows = []
    if not sources:
        text += "Пока пусто."
    for s in sources:
        text += f"• {s['name']} — {s['url']}\n"
        rows.append([btn(f"🗑 Удалить {s['name']}", f"adm:srcdel:{s['id']}")])
    rows.append([btn("➕ Добавить источник", "adm:srcadd")])
    rows.append([btn("⬅️ В админ-панель", "adm:home")])
    await safe_edit(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data == "adm:sources")
async def adm_sources(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_sources(cb)
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:srcdel:"))
async def adm_src_del(cb: CallbackQuery):
    execute("DELETE FROM sources WHERE id=?", (int(cb.data.split(":")[2]),))
    await render_sources(cb)
    await cb.answer("Удалено")


@admin_router.callback_query(F.data == "adm:srcadd")
async def adm_src_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Admin.src_name)
    await safe_edit(cb, "Введите название источника (кнопки):", admin_back())
    await cb.answer()


@admin_router.message(Admin.src_name, F.text, not_command)
async def adm_src_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:40])
    await state.set_state(Admin.src_url)
    await m.answer(
        "Теперь отправьте шаблон ссылки.\n\n"
        "Вместо уникального кода вставьте {id}, например:\n"
        "https://example.com/click?sub={id}\n\n"
        "Если {id} не указать, бот сам добавит ?sub_id=КОД в конец.\n"
        "Можно также вставить {channel} — туда подставится название выбранного канала.",
        reply_markup=admin_back(),
    )


@admin_router.message(Admin.src_url, F.text, not_command)
async def adm_src_url(m: Message, state: FSMContext):
    url = m.text.strip()
    if not url.startswith(("http://", "https://")):
        await m.answer("❌ Ссылка должна начинаться с http:// или https://", reply_markup=admin_back())
        return
    data = await state.get_data()
    execute("INSERT INTO sources(name, url) VALUES(?,?)", (data["name"], url))
    await state.clear()
    await m.answer(f"✅ Источник «{data['name']}» добавлен.", reply_markup=admin_kb())


# --- каналы для залива
async def render_channels(cb: CallbackQuery):
    chans = fetch("SELECT * FROM channels ORDER BY id")
    text = "📺 Каналы для залива\n\n" + ("\n".join(f"• {c['name']}" for c in chans) if chans else "Пока пусто.")
    rows = [[btn(f"🗑 Удалить {c['name']}", f"adm:chdel:{c['id']}")] for c in chans]
    rows.append([btn("➕ Добавить канал", "adm:chadd")])
    rows.append([btn("⬅️ В админ-панель", "adm:home")])
    await safe_edit(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data == "adm:channels")
async def adm_channels(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_channels(cb)
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:chdel:"))
async def adm_ch_del(cb: CallbackQuery):
    execute("DELETE FROM channels WHERE id=?", (int(cb.data.split(":")[2]),))
    await render_channels(cb)
    await cb.answer("Удалено")


@admin_router.callback_query(F.data == "adm:chadd")
async def adm_ch_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Admin.ch_name)
    await safe_edit(cb, "Введите название канала.\nМожно сразу несколько — каждый с новой строки:", admin_back())
    await cb.answer()


@admin_router.message(Admin.ch_name, F.text, not_command)
async def adm_ch_name(m: Message, state: FSMContext):
    names = [x.strip()[:40] for x in m.text.splitlines() if x.strip()]
    if not names:
        await m.answer("❌ Название не может быть пустым", reply_markup=admin_back())
        return
    for n in names:
        execute("INSERT INTO channels(name) VALUES(?)", (n,))
    await state.clear()
    await m.answer(f"✅ Добавлено каналов: {len(names)}", reply_markup=admin_kb())


# --- пользователи
@admin_router.callback_query(F.data == "adm:users")
async def adm_users(cb: CallbackQuery):
    users = fetch("SELECT * FROM users WHERE status='approved' ORDER BY created_at DESC LIMIT 40")
    text = "👥 Пользователи с доступом\n\n" if users else "👥 Пока никого нет."
    rows = []
    for u in users:
        rows.append([btn(f"🚫 Забрать доступ: {display_name(u)}", f"adm:revoke:{u['user_id']}")])
    rows.append([btn("⬅️ В админ-панель", "adm:home")])
    await safe_edit(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:revoke:"))
async def adm_revoke(cb: CallbackQuery):
    execute("UPDATE users SET status='rejected' WHERE user_id=?", (int(cb.data.split(":")[2]),))
    await cb.answer("Доступ отозван")
    await adm_users(cb)


# --- общая статистика
@admin_router.callback_query(F.data == "adm:stats")
async def adm_stats(cb: CallbackQuery):
    users = fetch_one("SELECT COUNT(*) c FROM users WHERE status='approved'")["c"]
    pending = fetch_one("SELECT COUNT(*) c FROM users WHERE status='pending'")["c"]
    links = fetch_one("SELECT COUNT(*) c FROM links")["c"]
    leads = fetch_one("SELECT COALESCE(SUM(leads),0) s FROM links")["s"]
    text = (
        f"📈 Общая статистика\n🕒 {now_str()}\n\n"
        f"👥 Пользователей с доступом: {users}\n"
        f"⏳ Заявок в ожидании: {pending}\n"
        f"🔗 Всего ссылок: {links}\n"
        f"🎯 Всего лидов: {leads}"
    )
    await safe_edit(cb, text, admin_back())
    await cb.answer()


# --- очистка ссылок
def cleanup_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("🗑 Удалить ВСЕ ссылки", "adm:del:all")],
            [btn("🧹 Удалить с 0 лидов", "adm:del:0")],
            [btn("🧹 Удалить с 0–1 лидом", "adm:del:1")],
            [btn("🧹 Удалить с 0–2 лидами", "adm:del:2")],
            [btn("⬅️ В админ-панель", "adm:home")],
        ]
    )


def del_filter(mode: str):
    if mode == "all":
        return "1=1", (), "ВСЕ ссылки"
    n = int(mode)
    title = "ссылки с 0 лидов" if n == 0 else f"ссылки, где залито от 0 до {n} лидов"
    return "leads <= ?", (n,), title


@admin_router.callback_query(F.data == "adm:cleanup")
async def adm_cleanup(cb: CallbackQuery):
    total = fetch_one("SELECT COUNT(*) c FROM links")["c"]
    await safe_edit(cb, f"🗑 Очистка ссылок\n\nВсего ссылок в базе: {total}\nВыберите, что удалить:", cleanup_kb())
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:del:"))
async def adm_del_confirm(cb: CallbackQuery):
    mode = cb.data.split(":")[2]
    where, args, title = del_filter(mode)
    cnt = fetch_one(f"SELECT COUNT(*) c FROM links WHERE {where}", args)["c"]
    if cnt == 0:
        await cb.answer("Таких ссылок нет", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✅ Да, удалить", f"adm:delok:{mode}")],
            [btn("❌ Отмена", "adm:cleanup")],
        ]
    )
    await safe_edit(cb, f"⚠️ Удалить {title}?\n\nБудет удалено: {cnt} шт. Это действие необратимо.", kb)
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:delok:"))
async def adm_del_do(cb: CallbackQuery):
    mode = cb.data.split(":")[2]
    where, args, title = del_filter(mode)
    cur = execute(f"DELETE FROM links WHERE {where}", args)
    await safe_edit(cb, f"✅ Удалено ссылок: {cur.rowcount}", cleanup_kb())
    await cb.answer("Готово")


# --- добавить / списать лиды: трафер → ссылка → количество
LEAD_PAGE = 20


def lead_title(mode: str) -> str:
    return "➕ Добавить лиды" if mode == "add" else "➖ Списать лиды"


def nav_row(base: str, page: int, pages: int):
    return [btn("◀️", f"{base}:{page - 1}"), btn(f"{page + 1}/{pages}", "noop"), btn("▶️", f"{base}:{page + 1}")]


def user_label(uid: int) -> str:
    u = get_user(uid)
    if u:
        return display_name(u)
    return "Администратор" if uid == ADMIN_ID else str(uid)


@admin_router.callback_query(F.data.startswith("adm:lead:"))
async def lead_users(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = cb.data.split(":")
    mode = parts[2]
    users = fetch(
        "SELECT u.user_id, u.username, u.full_name, COALESCE(SUM(l.leads),0) s "
        "FROM users u LEFT JOIN links l ON l.user_id=u.user_id "
        "GROUP BY u.user_id HAVING u.status='approved' OR COUNT(l.id)>0 "
        "ORDER BY s DESC"
    )
    if not users:
        await cb.answer("Пока нет ни одного трафера", show_alert=True)
        return
    pages = max(1, -(-len(users) // LEAD_PAGE))
    page = min(max(int(parts[3]) if len(parts) > 3 else 0, 0), pages - 1)
    rows = [
        [btn(f"{display_name(u)} · лидов: {u['s']}", f"adm:ll:{mode}:{u['user_id']}:0")]
        for u in users[page * LEAD_PAGE:(page + 1) * LEAD_PAGE]
    ]
    if pages > 1:
        rows.append(nav_row(f"adm:lead:{mode}", page, pages))
    rows.append([btn("⬅️ В админ-панель", "adm:home")])
    await safe_edit(cb, f"{lead_title(mode)}\n\nВыберите трафера:", InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:ll:"))
async def lead_links(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = cb.data.split(":")
    mode, uid = parts[2], int(parts[3])
    links = fetch("SELECT id, name, num, leads FROM links WHERE user_id=? ORDER BY id", (uid,))
    if not links:
        await cb.answer("У этого трафера нет ссылок", show_alert=True)
        return
    pages = max(1, -(-len(links) // LEAD_PAGE))
    page = min(max(int(parts[4]) if len(parts) > 4 else 0, 0), pages - 1)
    rows = [
        [btn(f"{l['name']} {l['num']} — лидов: {l['leads']}", f"adm:ls:{mode}:{l['id']}")]
        for l in links[page * LEAD_PAGE:(page + 1) * LEAD_PAGE]
    ]
    if pages > 1:
        rows.append(nav_row(f"adm:ll:{mode}:{uid}", page, pages))
    rows.append([btn("⬅️ К траферам", f"adm:lead:{mode}")])
    await safe_edit(
        cb,
        f"{lead_title(mode)}\n\n👤 {user_label(uid)}\nВыберите ссылку:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:ls:"))
async def lead_pick_link(cb: CallbackQuery, state: FSMContext):
    _, _, mode, link_id = cb.data.split(":")
    link = fetch_one("SELECT * FROM links WHERE id=?", (int(link_id),))
    if not link:
        await cb.answer("Ссылка не найдена", show_alert=True)
        return
    await state.set_state(Admin.lead_amount)
    await state.update_data(link_id=link["id"], mode=mode)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[btn("⬅️ Назад", f"adm:ll:{mode}:{link['user_id']}:0")]]
    )
    await safe_edit(
        cb,
        f"{lead_title(mode)}\n\n👤 {user_label(link['user_id'])}\n🔗 {link['name']} {link['num']}\n"
        f"🎯 Сейчас лидов: {link['leads']}\n\nВведите количество:",
        kb,
    )
    await cb.answer()


@admin_router.message(Admin.lead_amount, F.text, not_command)
async def lead_amount(m: Message, state: FSMContext):
    data = await state.get_data()
    txt = m.text.strip()
    if not txt.isdigit() or int(txt) < 1:
        await m.answer("❌ Введите целое число больше 0", reply_markup=admin_back())
        return
    n = int(txt)
    link = fetch_one("SELECT * FROM links WHERE id=?", (data.get("link_id"),))
    await state.clear()
    if not link:
        await m.answer("❌ Ссылка не найдена (возможно, удалена).", reply_markup=admin_kb())
        return
    mode, old = data["mode"], link["leads"]
    new = old + n if mode == "add" else max(old - n, 0)
    execute("UPDATE links SET leads=? WHERE id=?", (new, link["id"]))
    verb = "Добавлено" if mode == "add" else "Списано"
    text = (
        f"✅ {verb} лидов: {abs(new - old)}\n"
        f"👤 {user_label(link['user_id'])}\n🔗 {link['name']} {link['num']}\n"
        f"📉 Было: {old} → стало: {new}\n\nСсылка осталась у трафера."
    )
    if mode == "sub" and n > old:
        text += f"\n(Списано только {old}: меньше нуля уйти не может.)"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("↩️ Ещё по этому траферу", f"adm:ll:{mode}:{link['user_id']}:0")],
            [btn("⬅️ В админ-панель", "adm:home")],
        ]
    )
    await m.answer(text, reply_markup=kb)


# ====================== ПОСТБЭК (необязательно) ==================
async def postback(request: web.Request):
    """GET /postback?secret=XXX&sub=КОД&count=1 — авто-начисление лидов от трекера."""
    if request.query.get("secret") != POSTBACK_SECRET:
        return web.Response(status=403, text="forbidden")
    code = request.query.get("sub", "")
    try:
        count = int(request.query.get("count", "1"))
    except ValueError:
        count = 1
    cur = execute("UPDATE links SET leads = leads + ? WHERE code=?", (count, code))
    return web.Response(text="ok" if cur.rowcount else "not found")


async def start_postback_server():
    if not POSTBACK_SECRET:
        return None
    app = web.Application()
    app.router.add_get("/postback", postback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", POSTBACK_PORT).start()
    logging.info("Postback server on port %s", POSTBACK_PORT)
    return runner


# ============================== ЗАПУСК ===========================
async def main():
    if not BOT_TOKEN or not ADMIN_ID:
        raise SystemExit("Заполните BOT_TOKEN и ADMIN_ID в файле .env")
    init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(admin_router, access_router, user_router)
    runner = await start_postback_server()
    try:
        await dp.start_polling(bot)
    finally:
        if runner:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
