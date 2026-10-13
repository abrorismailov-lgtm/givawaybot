import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

import config
import handlers_admin
import handlers_public
from db import Database
from services import scheduler


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN")

    db = Database(config.DB_PATH)
    await db.connect()
    logging.info("База данных: %s", config.DB_PATH)

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db)
    dp.include_router(handlers_public.router)
    dp.include_router(handlers_admin.router)
    dp.include_router(handlers_admin.fallback)

    await bot.set_my_commands(
        [BotCommand(command="new", description="Создать розыгрыш"),
         BotCommand(command="list", description="Мои розыгрыши"),
         BotCommand(command="cancel", description="Отменить создание")],
        scope=BotCommandScopeAllPrivateChats())
    await bot.delete_webhook(drop_pending_updates=False)

    task = asyncio.create_task(scheduler(bot, db))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
