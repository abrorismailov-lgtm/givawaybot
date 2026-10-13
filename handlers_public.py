"""Нажатие кнопки «Участвовать» под постом в канале."""
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from db import Database
from services import CheckError, missing_channels, schedule_counter_update

router = Router()
log = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("join:"))
async def join(cb: CallbackQuery, bot: Bot, db: Database):
    gid = int(cb.data.split(":")[1])
    g = await db.get_giveaway(gid)
    if not g:
        return await cb.answer("Розыгрыш не найден.", show_alert=True)
    user = cb.from_user
    if g["status"] != "active":
        won = any(w["user_id"] == user.id for w in await db.winners(gid))
        return await cb.answer("🏆 Розыгрыш завершён — вы среди победителей!" if won
                               else "Розыгрыш завершён. Итоги опубликованы в канале.", show_alert=True)
    if await db.is_participant(gid, user.id):
        return await cb.answer("✅ Вы уже участвуете. Не отписывайтесь от каналов до подведения итогов!",
                               show_alert=True)
    try:
        missing = await missing_channels(bot, g["required"], user.id)
    except CheckError as e:
        log.warning("subscription check failed (giveaway %s): %s", gid, e)
        return await cb.answer("Сейчас очень много желающих 🙌 Нажмите кнопку ещё раз через несколько секунд.",
                               show_alert=True)
    if missing:
        names = ", ".join(ch["title"] for ch in missing)
        text = f"❌ Вы не подписаны на: {names}"
        tail = ". Подпишитесь и нажмите кнопку ещё раз."
        if len(text) + len(tail) > 200:  # лимит всплывающего окна Telegram
            text = text[:200 - len(tail) - 1] + "…"
        return await cb.answer(text + tail, show_alert=True)

    await db.add_participant(gid, user.id, user.full_name, user.username)
    schedule_counter_update(bot, db, gid)
    await cb.answer("🎉 Вы участвуете в розыгрыше! Не отписывайтесь от каналов до подведения итогов.",
                    show_alert=True)
