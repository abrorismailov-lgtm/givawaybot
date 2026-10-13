import json
from datetime import datetime, timezone

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS giveaways (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,
    channel_title TEXT    NOT NULL,
    message_id    INTEGER,
    text          TEXT    NOT NULL,
    photo_id      TEXT,
    button_text   TEXT    NOT NULL,
    winners_count INTEGER NOT NULL,
    required      TEXT    NOT NULL,          -- JSON: [{id, title, link}]
    end_at        TEXT,                      -- UTC ISO или NULL (итоги вручную)
    status        TEXT    NOT NULL DEFAULT 'active',  -- active | drawing | finished
    created_at    TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS participants (
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    full_name   TEXT    NOT NULL,
    username    TEXT,
    joined_at   TEXT    NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
CREATE TABLE IF NOT EXISTS winners (
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    place       INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        # если бот упал посреди подведения итогов — возвращаем розыгрыш в активные
        await self.conn.execute("UPDATE giveaways SET status='active' WHERE status='drawing'")
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    @staticmethod
    def _g(row) -> dict | None:
        if row is None:
            return None
        g = dict(row)
        g["required"] = json.loads(g["required"])
        return g

    # ---------- розыгрыши ----------
    async def create_giveaway(self, *, owner_id, channel_id, channel_title, text, photo_id,
                              button_text, winners_count, required, end_at) -> int:
        cur = await self.conn.execute(
            "INSERT INTO giveaways (owner_id, channel_id, channel_title, text, photo_id, button_text,"
            " winners_count, required, end_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (owner_id, channel_id, channel_title, text, photo_id, button_text, winners_count,
             json.dumps(required, ensure_ascii=False), end_at, now_iso()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def set_message_id(self, gid: int, message_id: int):
        await self.conn.execute("UPDATE giveaways SET message_id=? WHERE id=?", (message_id, gid))
        await self.conn.commit()

    async def delete_giveaway(self, gid: int):
        await self.conn.execute("DELETE FROM giveaways WHERE id=?", (gid,))
        await self.conn.commit()

    async def get_giveaway(self, gid: int) -> dict | None:
        cur = await self.conn.execute("SELECT * FROM giveaways WHERE id=?", (gid,))
        return self._g(await cur.fetchone())

    async def list_giveaways(self, owner_id: int, limit: int = 20) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM giveaways WHERE owner_id=? ORDER BY id DESC LIMIT ?", (owner_id, limit))
        return [self._g(r) for r in await cur.fetchall()]

    async def due_giveaways(self) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM giveaways WHERE status='active' AND end_at IS NOT NULL AND end_at<=?",
            (now_iso(),))
        return [self._g(r) for r in await cur.fetchall()]

    async def switch_status(self, gid: int, old: str, new: str) -> bool:
        """Атомарная смена статуса: защищает от двойного подведения итогов."""
        cur = await self.conn.execute(
            "UPDATE giveaways SET status=? WHERE id=? AND status=?", (new, gid, old))
        await self.conn.commit()
        return cur.rowcount == 1

    # ---------- участники ----------
    async def add_participant(self, gid: int, user_id: int, full_name: str, username: str | None) -> bool:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO participants VALUES (?,?,?,?,?)",
            (gid, user_id, full_name, username, now_iso()))
        await self.conn.commit()
        return cur.rowcount == 1

    async def is_participant(self, gid: int, user_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM participants WHERE giveaway_id=? AND user_id=?", (gid, user_id))
        return await cur.fetchone() is not None

    async def count_participants(self, gid: int) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) FROM participants WHERE giveaway_id=?", (gid,))
        return (await cur.fetchone())[0]

    async def participants(self, gid: int) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM participants WHERE giveaway_id=? ORDER BY joined_at", (gid,))
        return [dict(r) for r in await cur.fetchall()]

    # ---------- победители ----------
    async def add_winners(self, gid: int, user_ids: list[int]):
        cur = await self.conn.execute("SELECT COALESCE(MAX(place),0) FROM winners WHERE giveaway_id=?", (gid,))
        start = (await cur.fetchone())[0]
        await self.conn.executemany(
            "INSERT OR IGNORE INTO winners VALUES (?,?,?)",
            [(gid, uid, start + i + 1) for i, uid in enumerate(user_ids)])
        await self.conn.commit()

    async def winners(self, gid: int) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT p.*, w.place FROM winners w JOIN participants p"
            " ON p.giveaway_id=w.giveaway_id AND p.user_id=w.user_id"
            " WHERE w.giveaway_id=? ORDER BY w.place", (gid,))
        return [dict(r) for r in await cur.fetchall()]
