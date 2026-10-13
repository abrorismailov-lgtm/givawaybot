import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Tashkent"))

# На Railway при подключённом Volume переменная RAILWAY_VOLUME_MOUNT_PATH задаётся автоматически.
_volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
DB_PATH = os.getenv("DB_PATH") or (os.path.join(_volume, "giveaway.db") if _volume else "giveaway.db")

DEFAULT_WINNERS = 20
DEFAULT_BUTTON = "✅ УЧАСТВОВАТЬ В КОНКУРСЕ"
COUNTER_UPDATE_INTERVAL = 5  # сек: как часто обновлять счётчик на кнопке
