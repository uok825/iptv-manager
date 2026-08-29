import httpx
from datetime import datetime
from .database import get_db
from .m3u_parser import parse_m3u

GROUP_NAME_MAP = {
    "general": "Genel",
    "news": "Haber",
    "sports": "Spor",
    "entertainment": "Eğlence",
    "music": "Müzik",
    "kids": "Çocuk",
    "documentary": "Belgesel",
    "movies": "Sinema",
    "religious": "Dini",
    "education": "Eğitim",
    "culture": "Kültür",
    "business": "Ekonomi",
    "animation": "Çocuk",
    "lifestyle": "Yaşam",
    "travel": "Yaşam",
    "series": "Dizi",
    "undefined": "Diğer",
}


def map_group_name(raw: str) -> str:
    parts = [p.strip().lower() for p in raw.replace(";", ",").split(",")]
    for part in parts:
        if part in GROUP_NAME_MAP:
            return GROUP_NAME_MAP[part]
    return raw


async def fetch_m3u(url: str) -> str:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


def find_or_create_group(db, group_name: str) -> int:
    row = db.execute("SELECT id FROM groups WHERE name = ?", (group_name,)).fetchone()
    if row:
        return row["id"]
    max_order = db.execute("SELECT COALESCE(MAX(sort_order), -1) FROM groups").fetchone()[0]
    cursor = db.execute(
        "INSERT INTO groups (name, sort_order) VALUES (?, ?)",
        (group_name, max_order + 1),
    )
    return cursor.lastrowid


async def sync_source(source_id: int) -> dict:
    with get_db() as db:
        source = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not source:
            return {"error": "Source not found"}

    filter_group = source["filter_group"] or ""

    content = await fetch_m3u(source["url"])
    parsed = parse_m3u(content)

    added = 0
    updated = 0
    skipped = 0
    with get_db() as db:
        other_group_id = find_or_create_group(db, "Diğer")

        for ch in parsed:
            if not ch.url:
                continue

            if filter_group and ch.group_title.lower() != filter_group.lower():
                skipped += 1
                continue

            existing = None
            if ch.tvg_id:
                existing = db.execute(
                    "SELECT * FROM channels WHERE tvg_id = ? AND source_id = ?",
                    (ch.tvg_id, source_id),
                ).fetchone()

            if not existing and ch.display_name:
                existing = db.execute(
                    "SELECT * FROM channels WHERE display_name = ? AND source_id = ?",
                    (ch.display_name, source_id),
                ).fetchone()

            if existing:
                if existing["stream_url"] != ch.url:
                    db.execute(
                        "UPDATE channels SET stream_url = ?, updated_at = datetime('now') WHERE id = ?",
                        (ch.url, existing["id"]),
                    )
                    updated += 1
                if ch.tvg_logo and existing["tvg_logo"] != ch.tvg_logo:
                    db.execute(
                        "UPDATE channels SET tvg_logo = ? WHERE id = ?",
                        (ch.tvg_logo, existing["id"]),
                    )
            else:
                group_id = other_group_id
                if ch.group_title:
                    mapped_name = map_group_name(ch.group_title)
                    group_id = find_or_create_group(db, mapped_name)

                max_order = db.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) FROM channels WHERE group_id = ?",
                    (group_id,),
                ).fetchone()[0]

                db.execute(
                    """INSERT INTO channels
                       (tvg_id, tvg_name, tvg_logo, group_id, display_name, stream_url, source_id, sort_order)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ch.tvg_id, ch.tvg_name, ch.tvg_logo, group_id,
                     ch.display_name or ch.tvg_name, ch.url, source_id, max_order + 1),
                )
                added += 1

        db.execute(
            "UPDATE sources SET last_synced = datetime('now') WHERE id = ?",
            (source_id,),
        )

    return {"added": added, "updated": updated, "skipped": skipped, "total_parsed": len(parsed)}


async def sync_all_sources() -> list[dict]:
    results = []
    with get_db() as db:
        sources = db.execute("SELECT * FROM sources WHERE enabled = 1").fetchall()

    for source in sources:
        try:
            result = await sync_source(source["id"])
            results.append({"source": source["name"], **result})
        except Exception as e:
            results.append({"source": source["name"], "error": str(e)})
    return results


async def check_stream(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.head(url)
            return resp.status_code < 400
    except Exception:
        return False


async def check_all_streams():
    with get_db() as db:
        channels = db.execute(
            "SELECT id, stream_url FROM channels WHERE enabled = 1"
        ).fetchall()

    for ch in channels:
        alive = await check_stream(ch["stream_url"])
        with get_db() as db:
            db.execute(
                "UPDATE channels SET is_alive = ?, last_checked = datetime('now') WHERE id = ?",
                (1 if alive else 0, ch["id"]),
            )
