import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import PlainTextResponse, FileResponse
from pydantic import BaseModel
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .database import init_db, get_db
from .m3u_parser import generate_m3u
from .sync import sync_source, sync_all_sources, check_all_streams, repair_logos, match_epg

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
EPG_URL = os.environ.get("EPG_URL", "https://epgshare01.online/epgshare01/epg_ripper_TR1.xml.gz")
SYNC_INTERVAL_DAYS = int(os.environ.get("SYNC_INTERVAL_DAYS", "7"))

app = FastAPI(title="IPTV Playlist Manager")
scheduler = AsyncIOScheduler()


@app.on_event("startup")
async def startup():
    init_db()
    scheduler.add_job(sync_all_sources, "interval", days=SYNC_INTERVAL_DAYS, id="weekly_sync")
    scheduler.start()


# --- Playlist endpoint (TiviMate points here) ---

@app.get("/playlist.m3u", response_class=PlainTextResponse)
async def get_playlist():
    with get_db() as db:
        rows = db.execute("""
            SELECT c.*, g.name as group_name
            FROM channels c
            LEFT JOIN groups g ON c.group_id = g.id
            WHERE c.enabled = 1
            ORDER BY g.sort_order, c.sort_order
        """).fetchall()
    channels = [dict(r) for r in rows]
    return generate_m3u(channels, epg_url=EPG_URL)


# --- Sources ---

class SourceCreate(BaseModel):
    name: str
    url: str
    filter_group: str = ""

class SourceUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    enabled: bool | None = None
    filter_group: str | None = None


@app.get("/api/sources")
async def list_sources():
    with get_db() as db:
        rows = db.execute("SELECT * FROM sources ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/sources")
async def create_source(source: SourceCreate):
    with get_db() as db:
        try:
            cursor = db.execute(
                "INSERT INTO sources (name, url, filter_group) VALUES (?, ?, ?)",
                (source.name, source.url, source.filter_group),
            )
            return {"id": cursor.lastrowid}
        except Exception:
            raise HTTPException(400, "Source URL already exists")


@app.put("/api/sources/{source_id}")
async def update_source(source_id: int, source: SourceUpdate):
    with get_db() as db:
        existing = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not existing:
            raise HTTPException(404)
        if source.name is not None:
            db.execute("UPDATE sources SET name = ? WHERE id = ?", (source.name, source_id))
        if source.url is not None:
            db.execute("UPDATE sources SET url = ? WHERE id = ?", (source.url, source_id))
        if source.enabled is not None:
            db.execute("UPDATE sources SET enabled = ? WHERE id = ?", (int(source.enabled), source_id))
        if source.filter_group is not None:
            db.execute("UPDATE sources SET filter_group = ? WHERE id = ?", (source.filter_group, source_id))
    return {"ok": True}


@app.delete("/api/sources/{source_id}")
async def delete_source(source_id: int):
    with get_db() as db:
        db.execute("DELETE FROM channels WHERE source_id = ?", (source_id,))
        db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    return {"ok": True}


@app.post("/api/sources/{source_id}/sync")
async def sync_single_source(source_id: int):
    try:
        result = await sync_source(source_id)
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/sync")
async def sync_all():
    results = await sync_all_sources()
    return results


# --- Groups ---

class GroupCreate(BaseModel):
    name: str

class GroupUpdate(BaseModel):
    name: str | None = None

class GroupReorder(BaseModel):
    order: list[int]


@app.get("/api/groups")
async def list_groups():
    with get_db() as db:
        rows = db.execute("""
            SELECT g.*, COUNT(c.id) as channel_count
            FROM groups g
            LEFT JOIN channels c ON c.group_id = g.id
            GROUP BY g.id
            ORDER BY g.sort_order
        """).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/groups")
async def create_group(group: GroupCreate):
    with get_db() as db:
        max_order = db.execute("SELECT COALESCE(MAX(sort_order), -1) FROM groups").fetchone()[0]
        try:
            cursor = db.execute(
                "INSERT INTO groups (name, sort_order) VALUES (?, ?)",
                (group.name, max_order + 1),
            )
            return {"id": cursor.lastrowid}
        except Exception:
            raise HTTPException(400, "Group already exists")


@app.put("/api/groups/{group_id}")
async def update_group(group_id: int, group: GroupUpdate):
    with get_db() as db:
        if group.name is not None:
            db.execute("UPDATE groups SET name = ? WHERE id = ?", (group.name, group_id))
    return {"ok": True}


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: int):
    with get_db() as db:
        other = db.execute("SELECT id FROM groups WHERE name = 'Diğer'").fetchone()
        if other and other["id"] == group_id:
            raise HTTPException(400, "Cannot delete the default group")
        fallback_id = other["id"] if other else None
        if fallback_id:
            db.execute("UPDATE channels SET group_id = ? WHERE group_id = ?", (fallback_id, group_id))
        db.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    return {"ok": True}


@app.put("/api/groups/reorder")
async def reorder_groups(data: GroupReorder):
    with get_db() as db:
        for idx, group_id in enumerate(data.order):
            db.execute("UPDATE groups SET sort_order = ? WHERE id = ?", (idx, group_id))
    return {"ok": True}


# --- Channels ---

class ChannelUpdate(BaseModel):
    display_name: str | None = None
    group_id: int | None = None
    enabled: bool | None = None
    stream_url: str | None = None
    epg_channel_id: str | None = None

class ChannelReorder(BaseModel):
    group_id: int
    order: list[int]

class ChannelMove(BaseModel):
    channel_ids: list[int]
    group_id: int

class BulkToggle(BaseModel):
    channel_ids: list[int]
    enabled: bool


class ChannelCreate(BaseModel):
    display_name: str
    stream_url: str
    group_id: int
    tvg_id: str = ""
    tvg_logo: str = ""


@app.post("/api/channels")
async def create_channel(data: ChannelCreate):
    with get_db() as db:
        max_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM channels WHERE group_id = ?",
            (data.group_id,)
        ).fetchone()[0]
        cursor = db.execute(
            """INSERT INTO channels (display_name, stream_url, group_id, tvg_id, tvg_logo, sort_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (data.display_name, data.stream_url, data.group_id, data.tvg_id, data.tvg_logo, max_order + 1),
        )
        return {"id": cursor.lastrowid}


@app.get("/api/channels")
async def list_channels(
    group_id: int | None = None,
    search: str | None = None,
    enabled_only: bool = False,
):
    with get_db() as db:
        query = """
            SELECT c.*, g.name as group_name, s.name as source_name
            FROM channels c
            LEFT JOIN groups g ON c.group_id = g.id
            LEFT JOIN sources s ON c.source_id = s.id
            WHERE 1=1
        """
        params = []
        if group_id is not None:
            query += " AND c.group_id = ?"
            params.append(group_id)
        if search:
            query += " AND (c.display_name LIKE ? OR c.tvg_name LIKE ? OR c.tvg_id LIKE ?)"
            params.extend([f"%{search}%"] * 3)
        if enabled_only:
            query += " AND c.enabled = 1"
        query += " ORDER BY g.sort_order, c.sort_order"
        rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]


class ChannelSetPosition(BaseModel):
    channel_id: int
    position: int


@app.put("/api/channels/set-position")
async def set_channel_position(data: ChannelSetPosition):
    with get_db() as db:
        channel = db.execute("SELECT * FROM channels WHERE id = ?", (data.channel_id,)).fetchone()
        if not channel:
            raise HTTPException(404)
        group_id = channel["group_id"]
        siblings = db.execute(
            "SELECT id FROM channels WHERE group_id = ? ORDER BY sort_order",
            (group_id,),
        ).fetchall()
        ids = [r["id"] for r in siblings]
        if data.channel_id in ids:
            ids.remove(data.channel_id)
        pos = max(0, min(data.position, len(ids)))
        ids.insert(pos, data.channel_id)
        for idx, cid in enumerate(ids):
            db.execute("UPDATE channels SET sort_order = ? WHERE id = ?", (idx, cid))
    return {"ok": True}


@app.put("/api/channels/reorder")
async def reorder_channels(data: ChannelReorder):
    with get_db() as db:
        for idx, channel_id in enumerate(data.order):
            db.execute(
                "UPDATE channels SET sort_order = ?, group_id = ? WHERE id = ?",
                (idx, data.group_id, channel_id),
            )
    return {"ok": True}


@app.put("/api/channels/move")
async def move_channels(data: ChannelMove):
    with get_db() as db:
        max_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM channels WHERE group_id = ?",
            (data.group_id,)
        ).fetchone()[0]
        for i, ch_id in enumerate(data.channel_ids):
            db.execute(
                "UPDATE channels SET group_id = ?, sort_order = ?, updated_at = datetime('now') WHERE id = ?",
                (data.group_id, max_order + 1 + i, ch_id),
            )
    return {"ok": True}


@app.put("/api/channels/bulk-toggle")
async def bulk_toggle(data: BulkToggle):
    with get_db() as db:
        for ch_id in data.channel_ids:
            db.execute(
                "UPDATE channels SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
                (int(data.enabled), ch_id),
            )
    return {"ok": True}


@app.put("/api/channels/{channel_id}")
async def update_channel(channel_id: int, data: ChannelUpdate):
    with get_db() as db:
        if data.display_name is not None:
            db.execute("UPDATE channels SET display_name = ?, updated_at = datetime('now') WHERE id = ?",
                       (data.display_name, channel_id))
        if data.group_id is not None:
            max_order = db.execute(
                "SELECT COALESCE(MAX(sort_order), -1) FROM channels WHERE group_id = ?",
                (data.group_id,)
            ).fetchone()[0]
            db.execute("UPDATE channels SET group_id = ?, sort_order = ?, updated_at = datetime('now') WHERE id = ?",
                       (data.group_id, max_order + 1, channel_id))
        if data.enabled is not None:
            db.execute("UPDATE channels SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
                       (int(data.enabled), channel_id))
        if data.stream_url is not None:
            db.execute("UPDATE channels SET stream_url = ?, updated_at = datetime('now') WHERE id = ?",
                       (data.stream_url, channel_id))
        if data.epg_channel_id is not None:
            db.execute("UPDATE channels SET epg_channel_id = ?, updated_at = datetime('now') WHERE id = ?",
                       (data.epg_channel_id, channel_id))
    return {"ok": True}


@app.delete("/api/channels/{channel_id}")
async def delete_channel(channel_id: int):
    with get_db() as db:
        db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    return {"ok": True}


# --- Logo repair ---

@app.post("/api/repair-logos")
async def repair_logos_endpoint():
    repaired = await repair_logos()
    return {"repaired": repaired}


# --- EPG matching ---

@app.post("/api/match-epg")
async def match_epg_endpoint():
    matched = await match_epg(EPG_URL)
    return {"matched": matched}


# --- Stream health check ---

@app.post("/api/check-streams")
async def check_streams():
    await check_all_streams()
    return {"ok": True}


# --- Stats ---

@app.get("/api/stats")
async def get_stats():
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        enabled = db.execute("SELECT COUNT(*) FROM channels WHERE enabled = 1").fetchone()[0]
        alive = db.execute("SELECT COUNT(*) FROM channels WHERE is_alive = 1 AND enabled = 1").fetchone()[0]
        sources = db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        groups = db.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    return {
        "total_channels": total,
        "enabled_channels": enabled,
        "alive_channels": alive,
        "total_sources": sources,
        "total_groups": groups,
    }


# --- Config (public URL for frontend) ---

@app.get("/api/config")
async def get_config():
    return {"public_url": PUBLIC_URL, "epg_url": EPG_URL}


# --- Serve frontend ---

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
