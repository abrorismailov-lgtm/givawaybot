"""Личный чат с ботом: создание розыгрышей и управление ими."""
import csv
import html
import io
import re
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

import config
from db import Database
from services import finish_giveaway, join_kb, mention, pick_winners

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
if config.ADMIN_IDS:
    router.message.filter(F.from_user.id.in_(config.ADMIN_IDS))
    router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))

ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


class New(StatesGroup):
    channel = State()
    content = State()
    required = State()
    winners = State()
    end_at = State()
    confirm = State()


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


# ---------- распознавание каналов ----------
REF_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,})|@([A-Za-z][A-Za-z0-9_]{3,})|(-100\d{5,})")


def parse_refs(message: Message) -> list[int | str]:
    origin = message.forward_origin
    if origin and origin.type == "channel":
        return [origin.chat.id]
    refs = []
    for link, at, num in REF_RE.findall(message.text or ""):
        refs.append(int(num) if num else "@" + (link or at))
    return refs


async def fetch_channel(bot: Bot, ref: int | str) -> dict:
    """Проверяет, что канал существует и бот в нём админ. Возвращает {id, title, link}."""
    try:
        chat = await bot.get_chat(ref)
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramAPIError:
        raise ValueError(f"{ref}: канал не найден или бот в него не добавлен")
    if chat.type not in (ChatType.CHANNEL, ChatType.SUPERGROUP):
        raise ValueError(f"{ref}: это не канал и не группа")
    if me.status not in ADMIN_STATUSES:
        raise ValueError(f"{chat.title}: бот не администратор — добавьте его в админы канала")
    link = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
    return {"id": chat.id, "title": f"@{chat.username}" if chat.username else chat.title,
            "link": link, "can_post": me.status == ChatMemberStatus.CREATOR
            or chat.type == ChatType.SUPERGROUP or bool(getattr(me, "can_post_messages", False))}


def channels_text(required: list[dict]) -> str:
    return "\n".join(
        f"• <a href=\"{c['link']}\">{html.escape(c['title'])}</a>" if c.get("link")
        else f"• {html.escape(c['title'])}" for c in required)


# ---------- команды ----------
HELP = (
    "👋 <b>Бот розыгрышей для Telegram-каналов</b>\n\n"
    "Я публикую в канале пост с кнопкой участия, проверяю подписку на нужные каналы "
    "и случайно выбираю победителей.\n\n"
    "<b>Как начать</b>\n"
    "1. Добавьте меня администратором в ваш канал (право «Публикация сообщений») "
    "и во все каналы, подписку на которые нужно проверять.\n"
    "2. Отправьте /new и следуйте шагам.\n\n"
    "/new — создать розыгрыш\n/list — мои розыгрыши и подведение итогов\n/cancel — отменить создание"
)


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message):
    await message.answer(HELP)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. /new — создать розыгрыш.")


@router.message(Command("new"))
async def new(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(New.channel)
    await message.answer(
        "<b>Шаг 1/5. Канал для публикации</b>\n\n"
        "Пришлите @username канала, в котором выйдет пост розыгрыша. "
        "Если канал приватный — перешлите сюда любой пост из него.\n\n"
        "Бот уже должен быть администратором этого канала.")


@router.message(New.channel)
async def step_channel(message: Message, state: FSMContext, bot: Bot):
    refs = parse_refs(message)
    if not refs:
        return await message.answer("Не вижу канала. Пришлите @username или перешлите пост из канала.")
    try:
        ch = await fetch_channel(bot, refs[0])
    except ValueError as e:
        return await message.answer(f"⚠️ {html.escape(str(e))}")
    if not ch["can_post"]:
        return await message.answer("⚠️ У бота нет права «Публикация сообщений» в этом канале. Выдайте его и повторите.")
    try:
        member = await bot.get_chat_member(ch["id"], message.from_user.id)
        is_admin = member.status in ADMIN_STATUSES
    except TelegramAPIError:
        is_admin = False
    if not is_admin:
        return await message.answer("⚠️ Создавать розыгрыш может только администратор этого канала.")
    await state.update_data(channel=ch, required=[ch])
    await state.set_state(New.content)
    await message.answer(
        f"Канал: <b>{html.escape(ch['title'])}</b> ✅\n\n"
        "<b>Шаг 2/5. Пост розыгрыша</b>\n\n"
        "Пришлите текст поста (можно с одной картинкой и форматированием): приз, условия, дата итогов. "
        "Кнопку участия я добавлю сам.")


@router.message(New.content, F.text | F.photo)
async def step_content(message: Message, state: FSMContext):
    text = message.html_text or ""
    if not text:
        return await message.answer("Добавьте к картинке подпись с условиями розыгрыша.")
    if message.text and message.text.startswith("/"):
        return await message.answer("Пришлите текст поста или /cancel для отмены.")
    await state.update_data(text=text, photo_id=message.photo[-1].file_id if message.photo else None)
    await state.set_state(New.required)
    await message.answer(
        "<b>Шаг 3/5. Каналы для обязательной подписки</b>\n\n"
        "Канал публикации уже в списке. Пришлите остальные — @username через пробел "
        "или пересылайте по одному посту из каждого канала (для приватных).\n\n"
        "⚠️ Бот должен быть администратором в каждом из них, иначе Telegram не даст проверить подписку.",
        reply_markup=kb([("Готово, каналов достаточно ▶️", "req_done")]))


@router.message(New.required)
async def step_required(message: Message, state: FSMContext, bot: Bot):
    refs = parse_refs(message)
    if not refs:
        return await message.answer("Не вижу каналов. Пришлите @username или перешлите пост из канала.")
    data = await state.get_data()
    required, errors = data["required"], []
    for ref in refs:
        try:
            ch = await fetch_channel(bot, ref)
        except ValueError as e:
            errors.append(str(e))
            continue
        if all(c["id"] != ch["id"] for c in required):
            required.append(ch)
    await state.update_data(required=required)
    text = f"Сейчас в условиях ({len(required)}):\n{channels_text(required)}"
    if errors:
        text += "\n\n⚠️ Не добавлены:\n" + "\n".join("• " + html.escape(e) for e in errors)
    await message.answer(text + "\n\nПришлите ещё или нажмите «Готово».", disable_web_page_preview=True,
                         reply_markup=kb([("Готово ▶️", "req_done")]))


@router.callback_query(New.required, F.data == "req_done")
async def required_done(cb: CallbackQuery, state: FSMContext):
    await state.set_state(New.winners)
    await cb.message.answer(
        "<b>Шаг 4/5. Количество победителей</b>\n\nПришлите число.",
        reply_markup=kb([(f"Оставить {config.DEFAULT_WINNERS}", "win_default")]))
    await cb.answer()


async def _ask_end(message: Message, state: FSMContext):
    await state.set_state(New.end_at)
    example = datetime.now(config.TZ).strftime("%d.%m.%Y 20:00")
    await message.answer(
        "<b>Шаг 5/5. Когда подвести итоги</b>\n\n"
        f"Пришлите дату и время в формате <code>{example}</code> ({config.TZ.key}) — итоги подведу автоматически. "
        "Либо подведите их сами кнопкой в /list.",
        reply_markup=kb([("Подведу итоги вручную", "end_manual")]))


@router.callback_query(New.winners, F.data == "win_default")
async def winners_default(cb: CallbackQuery, state: FSMContext):
    await state.update_data(winners_count=config.DEFAULT_WINNERS)
    await _ask_end(cb.message, state)
    await cb.answer()


@router.message(New.winners, F.text)
async def step_winners(message: Message, state: FSMContext):
    if not message.text.isdigit() or not 1 <= int(message.text) <= 500:
        return await message.answer("Нужно число от 1 до 500.")
    await state.update_data(winners_count=int(message.text))
    await _ask_end(message, state)


async def _send_post(bot: Bot, chat_id: int, data: dict, markup) -> Message:
    if data["photo_id"]:
        return await bot.send_photo(chat_id, data["photo_id"], caption=data["text"], reply_markup=markup)
    return await bot.send_message(chat_id, data["text"], reply_markup=markup)


async def _preview(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.set_state(New.confirm)
    fake = {"id": 0, "button_text": config.DEFAULT_BUTTON}
    await _send_post(bot, message.chat.id, data, join_kb(fake, 0))
    end = (datetime.fromisoformat(data["end_at"]).astimezone(config.TZ).strftime("%d.%m.%Y %H:%M")
           if data.get("end_at") else "вручную")
    await message.answer(
        "👆 Так пост будет выглядеть в канале.\n\n"
        f"Канал: <b>{html.escape(data['channel']['title'])}</b>\n"
        f"Победителей: <b>{data['winners_count']}</b>\n"
        f"Итоги: <b>{end}</b>\n"
        f"Обязательная подписка:\n{channels_text(data['required'])}",
        disable_web_page_preview=True,
        reply_markup=kb([("🚀 Опубликовать", "publish")], [("✖️ Отмена", "discard")]))


@router.callback_query(New.end_at, F.data == "end_manual")
async def end_manual(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.update_data(end_at=None)
    await _preview(cb.message, state, bot)
    await cb.answer()


@router.message(New.end_at, F.text)
async def step_end(message: Message, state: FSMContext, bot: Bot):
    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=config.TZ)
    except ValueError:
        return await message.answer("Не понял дату. Формат: <code>25.12.2026 20:00</code>")
    if dt <= datetime.now(config.TZ):
        return await message.answer("Эта дата уже прошла — пришлите будущую.")
    await state.update_data(end_at=dt.astimezone(timezone.utc).isoformat(timespec="seconds"))
    await _preview(message, state, bot)


@router.callback_query(New.confirm, F.data == "discard")
async def discard(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Розыгрыш отменён. /new — создать заново.")


@router.callback_query(New.confirm, F.data == "publish")
async def publish(cb: CallbackQuery, state: FSMContext, bot: Bot, db: Database):
    data = await state.get_data()
    await state.clear()
    required = [{k: c[k] for k in ("id", "title", "link")} for c in data["required"]]
    gid = await db.create_giveaway(
        owner_id=cb.from_user.id, channel_id=data["channel"]["id"], channel_title=data["channel"]["title"],
        text=data["text"], photo_id=data["photo_id"], button_text=config.DEFAULT_BUTTON,
        winners_count=data["winners_count"], required=required, end_at=data.get("end_at"))
    g = await db.get_giveaway(gid)
    try:
        post = await _send_post(bot, g["channel_id"], data, join_kb(g, 0))
    except TelegramAPIError as e:
        await db.delete_giveaway(gid)
        return await cb.message.edit_text(f"⚠️ Не удалось опубликовать: {html.escape(str(e))}")
    await db.set_message_id(gid, post.message_id)
    await cb.message.edit_text(
        f"✅ Розыгрыш #{gid} опубликован в {html.escape(g['channel_title'])}.\n\n"
        "Статистика, выгрузка участников и подведение итогов — в /list.")


# ---------- управление ----------
STATUS = {"active": "🟢 идёт", "drawing": "⏳ подводятся итоги", "finished": "🏁 завершён"}


@router.message(Command("list"))
async def list_cmd(message: Message, db: Database):
    items = await db.list_giveaways(message.from_user.id)
    if not items:
        return await message.answer("У вас пока нет розыгрышей. /new — создать.")
    rows = [[(f"#{g['id']} · {g['channel_title']} · {STATUS[g['status']]}", f"g:{g['id']}")] for g in items]
    await message.answer("Ваши розыгрыши:", reply_markup=kb(*rows))


async def _owned(cb: CallbackQuery, db: Database) -> dict | None:
    g = await db.get_giveaway(int(cb.data.split(":")[1]))
    if not g or g["owner_id"] != cb.from_user.id:
        await cb.answer("Розыгрыш не найден.", show_alert=True)
        return None
    return g


@router.callback_query(F.data.startswith("g:"))
async def card(cb: CallbackQuery, db: Database):
    if not (g := await _owned(cb, db)):
        return
    count = await db.count_participants(g["id"])
    end = (datetime.fromisoformat(g["end_at"]).astimezone(config.TZ).strftime("%d.%m.%Y %H:%M")
           if g["end_at"] else "вручную")
    text = (f"<b>Розыгрыш #{g['id']}</b> — {STATUS[g['status']]}\n"
            f"Канал: {html.escape(g['channel_title'])}\n"
            f"Участников: <b>{count}</b>\nПобедителей: {g['winners_count']}\nИтоги: {end}\n"
            f"Условия:\n{channels_text(g['required'])}")
    rows = [[("🔄 Обновить", f"g:{g['id']}"), ("📥 Участники CSV", f"csv:{g['id']}")]]
    if g["status"] == "active":
        rows.append([("🎲 Подвести итоги сейчас", f"draw:{g['id']}")])
    elif g["status"] == "finished":
        winners = await db.winners(g["id"])
        text += "\n\n🏆 Победители:\n" + "\n".join(f"{w['place']}. {mention(w)}" for w in winners)[:2500]
        rows.append([("➕ Выбрать ещё одного победителя", f"extra:{g['id']}")])
    try:
        await cb.message.edit_text(text, reply_markup=kb(*rows), disable_web_page_preview=True)
    except TelegramAPIError:
        pass  # текст не изменился
    await cb.answer()


@router.callback_query(F.data.startswith("draw:"))
async def draw_ask(cb: CallbackQuery, db: Database):
    if not (g := await _owned(cb, db)):
        return
    await cb.message.edit_text(
        f"Подвести итоги розыгрыша #{g['id']} прямо сейчас?\n"
        f"Будет выбрано победителей: {g['winners_count']}. Результат сразу опубликуется в канале, отменить нельзя.",
        reply_markup=kb([("✅ Да, подвести", f"drawok:{g['id']}")], [("◀️ Назад", f"g:{g['id']}")]))
    await cb.answer()


@router.callback_query(F.data.startswith("drawok:"))
async def draw_ok(cb: CallbackQuery, bot: Bot, db: Database):
    if not (g := await _owned(cb, db)):
        return
    await cb.answer()
    await cb.message.edit_text("⏳ Перепроверяю подписки и выбираю победителей…")
    await cb.message.answer(await finish_giveaway(bot, db, g["id"]))


@router.callback_query(F.data.startswith("extra:"))
async def extra(cb: CallbackQuery, bot: Bot, db: Database):
    """Запасной победитель — если кто-то из основных не вышел на связь. Виден только организатору."""
    if not (g := await _owned(cb, db)) or g["status"] != "finished":
        return
    await cb.answer("Выбираю…")
    try:
        picked = await pick_winners(bot, db, g, 1)
    except Exception as e:
        return await cb.message.answer(f"⚠️ Не удалось: {html.escape(str(e))}")
    if not picked:
        return await cb.message.answer("Больше нет участников, выполнивших условия.")
    await db.add_winners(g["id"], [picked[0]["user_id"]])
    await cb.message.answer(f"➕ Дополнительный победитель: {mention(picked[0])}\n"
                            "В канал он не публикуется — объявите сами, если нужно.")


@router.callback_query(F.data.startswith("csv:"))
async def export_csv(cb: CallbackQuery, db: Database):
    if not (g := await _owned(cb, db)):
        return
    winners = {w["user_id"]: w["place"] for w in await db.winners(g["id"])}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "name", "username", "joined_at_utc", "winner_place"])
    for p in await db.participants(g["id"]):
        writer.writerow([p["user_id"], p["full_name"], p["username"] or "", p["joined_at"],
                         winners.get(p["user_id"], "")])
    await cb.message.answer_document(
        BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename=f"giveaway_{g['id']}.csv"))
    await cb.answer()


# ---------- ответы на всё, что не распознано (подключается последним) ----------
fallback = Router()
fallback.message.filter(F.chat.type == ChatType.PRIVATE)


@fallback.message()
async def unknown_message(message: Message):
    uid = message.from_user.id
    if config.ADMIN_IDS and uid not in config.ADMIN_IDS:
        return await message.answer(
            f"⛔️ У вас нет доступа к созданию розыгрышей.\nВаш Telegram ID: <code>{uid}</code>\n\n"
            "Если вы владелец бота — добавьте этот ID в переменную ADMIN_IDS (или очистите её) и перезапустите бота.")
    await message.answer("Не понял сообщение 🤔\n\n/new — создать розыгрыш\n/list — мои розыгрыши\n/help — справка")


@fallback.callback_query()
async def unknown_callback(cb: CallbackQuery):
    await cb.answer("Кнопка устарела (бот перезапускался). Начните заново: /new", show_alert=True)
