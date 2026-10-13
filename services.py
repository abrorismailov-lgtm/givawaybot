"""Проверка подписок, счётчик на кнопке, выбор победителей."""
import asyncio
import html
import logging
import random

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from db import Database

log = logging.getLogger(__name__)
_rng = random.SystemRandom()  # криптографически стойкий генератор ОС


class CheckError(Exception):
    """Подписку проверить не удалось (бот не админ канала, лимиты Telegram и т.п.)."""


async def is_subscribed(bot: Bot, chat_id: int, user_id: int, patient: bool = False) -> bool:
    """patient=True — ждать при лимитах Telegram (для подведения итогов)."""
    for _ in range(5):
        try:
            m = await bot.get_chat_member(chat_id, user_id)
        except TelegramRetryAfter as e:
            if not patient:
                raise CheckError("flood") from e
            await asyncio.sleep(e.retry_after + 1)
            continue
        except TelegramBadRequest as e:
            text = str(e).lower()
            if "user not found" in text or "participant_id_invalid" in text or "member not found" in text:
                return False
            raise CheckError(str(e)) from e
        except TelegramForbiddenError as e:
            raise CheckError(str(e)) from e
        if m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            return True
        if m.status == ChatMemberStatus.RESTRICTED:
            return bool(getattr(m, "is_member", False))
        return False
    raise CheckError("flood")


async def missing_channels(bot: Bot, required: list[dict], user_id: int, patient: bool = False) -> list[dict]:
    """Каналы из списка условий, на которые пользователь НЕ подписан."""
    if patient:
        results = [await is_subscribed(bot, ch["id"], user_id, True) for ch in required]
    else:
        results = await asyncio.gather(*(is_subscribed(bot, ch["id"], user_id) for ch in required))
    return [ch for ch, ok in zip(required, results) if not ok]


# ---------- кнопка и счётчик ----------
def join_kb(g: dict, count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{g['button_text']} ({count})", callback_data=f"join:{g['id']}")
    ]])


_pending: set[int] = set()


def schedule_counter_update(bot: Bot, db: Database, gid: int):
    """Обновляет число на кнопке не чаще раза в N секунд, чтобы не упереться в лимиты Telegram."""
    if gid in _pending:
        return
    _pending.add(gid)

    async def _job():
        try:
            await asyncio.sleep(config.COUNTER_UPDATE_INTERVAL)
            _pending.discard(gid)
            g = await db.get_giveaway(gid)
            if not g or g["status"] != "active" or not g["message_id"]:
                return
            count = await db.count_participants(gid)
            await bot.edit_message_reply_markup(
                chat_id=g["channel_id"], message_id=g["message_id"], reply_markup=join_kb(g, count))
        except TelegramBadRequest:
            pass  # «message is not modified» и подобное
        except Exception:
            log.exception("counter update failed")
        finally:
            _pending.discard(gid)

    asyncio.create_task(_job())


# ---------- победители ----------
def mention(p: dict) -> str:
    link = f'<a href="tg://user?id={p["user_id"]}">{html.escape(p["full_name"] or "Участник")}</a>'
    return f"{link} (@{p['username']})" if p["username"] else link


async def pick_winners(bot: Bot, db: Database, g: dict, count: int) -> list[dict]:
    """Перемешивает участников и берёт первых `count`, кто ВСЁ ЕЩЁ подписан на все каналы."""
    already = {w["user_id"] for w in await db.winners(g["id"])}
    pool = [p for p in await db.participants(g["id"]) if p["user_id"] not in already]
    _rng.shuffle(pool)
    picked: list[dict] = []
    for p in pool:
        if len(picked) >= count:
            break
        if not await missing_channels(bot, g["required"], p["user_id"], patient=True):
            picked.append(p)
        await asyncio.sleep(0.05)
    return picked


def _chunks(lines: list[str], limit: int = 3800) -> list[str]:
    out, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        out.append(cur)
    return out


async def finish_giveaway(bot: Bot, db: Database, gid: int) -> str:
    """Подводит итоги: выбирает победителей, публикует их в канале, убирает кнопку. Возвращает отчёт."""
    if not await db.switch_status(gid, "active", "drawing"):
        return "Итоги этого розыгрыша уже подведены или подводятся прямо сейчас."
    g = await db.get_giveaway(gid)
    try:
        picked = await pick_winners(bot, db, g, g["winners_count"])
        await db.add_winners(gid, [p["user_id"] for p in picked])
    except Exception as e:
        await db.switch_status(gid, "drawing", "active")
        log.exception("draw failed")
        return ("⚠️ Не удалось подвести итоги: " + html.escape(str(e)) +
                "\nПроверьте, что бот — администратор во всех каналах из условий, и попробуйте снова.")
    await db.switch_status(gid, "drawing", "finished")

    total = await db.count_participants(gid)
    winners = await db.winners(gid)
    lines = [f"🎉 <b>Итоги розыгрыша подведены!</b>", f"Участников: <b>{total}</b>", ""]
    if winners:
        lines.append("🏆 <b>Победители:</b>")
        lines += [f"{w['place']}. {mention(w)}" for w in winners]
        lines += ["", "Поздравляем! Победители выбраны случайным образом среди выполнивших все условия."]
    else:
        lines.append("К сожалению, никто не выполнил все условия розыгрыша.")
    report = _chunks(lines)

    try:
        await bot.edit_message_reply_markup(
            chat_id=g["channel_id"], message_id=g["message_id"],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"🏁 Розыгрыш завершён ({total})", callback_data=f"join:{gid}")]]))
    except Exception:
        pass
    try:
        for i, part in enumerate(report):
            await bot.send_message(g["channel_id"], part,
                                   reply_to_message_id=g["message_id"] if i == 0 else None,
                                   allow_sending_without_reply=True)
    except Exception as e:
        log.exception("cannot post results")
        report.append(f"⚠️ В канал опубликовать не удалось ({html.escape(str(e))}) — перешлите итоги вручную.")
    if len(winners) < g["winners_count"]:
        report.append(f"ℹ️ Условия выполнили только {len(winners)} из {g['winners_count']} нужных победителей.")
    return "\n".join(report)


async def scheduler(bot: Bot, db: Database):
    """Автоматически подводит итоги, когда наступает назначенное время."""
    while True:
        try:
            for g in await db.due_giveaways():
                report = await finish_giveaway(bot, db, g["id"])
                try:
                    await bot.send_message(g["owner_id"], f"⏰ Розыгрыш #{g['id']} завершён автоматически.\n\n{report}")
                except Exception:
                    pass
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(30)
