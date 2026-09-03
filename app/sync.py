import httpx
import gzip
import re
import xml.etree.ElementTree as ET
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


def normalize_name(name: str) -> str:
    name = re.sub(r'\s*\([\d]+p\)', '', name)
    name = re.sub(r'\s*\[.*?\]', '', name)
    return name.strip().lower()


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
                    "SELECT * FROM channels WHERE tvg_id = ?",
                    (ch.tvg_id,),
                ).fetchone()

            if not existing and ch.display_name:
                existing = db.execute(
                    "SELECT * FROM channels WHERE display_name = ?",
                    (ch.display_name,),
                ).fetchone()

            if not existing and ch.display_name:
                norm = normalize_name(ch.display_name)
                all_channels = db.execute("SELECT * FROM channels").fetchall()
                for row in all_channels:
                    if normalize_name(row["display_name"]) == norm:
                        existing = row
                        break

            if existing:
                if existing["stream_url"] != ch.url:
                    db.execute(
                        "UPDATE channels SET stream_url = ?, updated_at = datetime('now') WHERE id = ?",
                        (ch.url, existing["id"]),
                    )
                    updated += 1
                if ch.tvg_logo and not existing["tvg_logo"]:
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


async def repair_logos() -> int:
    with get_db() as db:
        sources = db.execute("SELECT * FROM sources WHERE enabled = 1").fetchall()

    logo_map = {}
    for source in sources:
        try:
            content = await fetch_m3u(source["url"])
            parsed = parse_m3u(content)
            for ch in parsed:
                if ch.tvg_logo and ch.tvg_logo.startswith("https://i.imgur.com"):
                    if ch.tvg_id:
                        logo_map[ch.tvg_id] = ch.tvg_logo
                    norm = normalize_name(ch.display_name)
                    if norm:
                        logo_map[norm] = ch.tvg_logo
            for ch in parsed:
                if ch.tvg_logo and ch.tvg_logo.startswith("https://i.imgur.com"):
                    continue
                if ch.tvg_logo:
                    if ch.tvg_id and ch.tvg_id not in logo_map:
                        logo_map[ch.tvg_id] = ch.tvg_logo
                    norm = normalize_name(ch.display_name)
                    if norm and norm not in logo_map:
                        logo_map[norm] = ch.tvg_logo
        except Exception:
            continue

    repaired = 0
    with get_db() as db:
        channels = db.execute(
            "SELECT id, tvg_id, display_name, tvg_logo FROM channels"
        ).fetchall()
        for ch in channels:
            logo = None
            if ch["tvg_id"]:
                logo = logo_map.get(ch["tvg_id"])
            if not logo:
                logo = logo_map.get(normalize_name(ch["display_name"]))
            if logo and logo != ch["tvg_logo"]:
                db.execute("UPDATE channels SET tvg_logo = ? WHERE id = ?", (logo, ch["id"]))
                repaired += 1
    return repaired


_TR_CHAR_MAP = str.maketrans('İıÇçŞşĞğÖöÜü', 'iiccssggoouü')


def normalize_epg(name: str) -> str:
    import unicodedata
    name = re.sub(r'\s*\([\d]+p\)', '', name)
    name = re.sub(r'\s*\[.*?\]', '', name)
    name = name.translate(_TR_CHAR_MAP)
    name = unicodedata.normalize('NFKD', name)
    name = ''.join(c for c in name if not unicodedata.combining(c))
    return name.strip().lower().replace('.', '').replace(' ', '').replace('-', '')


async def match_epg(epg_url: str) -> int:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(epg_url)
        resp.raise_for_status()

    content = resp.content
    if epg_url.endswith('.gz'):
        content = gzip.decompress(content)

    root = ET.fromstring(content)

    epg_channels = {}
    for ch_el in root.findall('channel'):
        ch_id = ch_el.get('id', '')
        if not ch_id:
            continue
        names = [dn.text for dn in ch_el.findall('display-name') if dn.text]
        epg_channels[ch_id] = names

    epg_by_norm = {}
    epg_by_id_norm = {}
    for ch_id, names in epg_channels.items():
        for name in names:
            norm = normalize_epg(name)
            if norm not in epg_by_norm:
                epg_by_norm[norm] = ch_id
        id_norm = normalize_epg(ch_id.replace('.tr', ''))
        if id_norm not in epg_by_id_norm:
            epg_by_id_norm[id_norm] = ch_id

    def strip_suffixes(s):
        s = s.replace('hd', '')
        if s.startswith('tv'):
            s = s[2:]
        if s.endswith('tv'):
            s = s[:-2]
        return s

    epg_by_norm_nohd = {}
    for norm, ch_id in epg_by_norm.items():
        key = strip_suffixes(norm)
        if key not in epg_by_norm_nohd:
            epg_by_norm_nohd[key] = ch_id
    for norm, ch_id in epg_by_id_norm.items():
        key = strip_suffixes(norm)
        if key not in epg_by_norm_nohd:
            epg_by_norm_nohd[key] = ch_id

    matched = 0
    with get_db() as db:
        channels = db.execute(
            "SELECT id, display_name, tvg_id, epg_channel_id FROM channels"
        ).fetchall()
        for ch in channels:
            if ch["epg_channel_id"]:
                continue
            best = None
            norm_name = normalize_epg(ch["display_name"])
            best = epg_by_norm.get(norm_name)
            if not best:
                best = epg_by_id_norm.get(norm_name)
            if not best:
                sn = strip_suffixes(norm_name)
                if len(sn) >= 3:
                    best = epg_by_norm_nohd.get(sn)
            if not best and ch["tvg_id"]:
                tvg_base = ch["tvg_id"].split('@')[0]
                norm_tvg = normalize_epg(tvg_base.replace('.tr', ''))
                best = epg_by_norm.get(norm_tvg)
                if not best:
                    best = epg_by_id_norm.get(norm_tvg)
                if not best:
                    st = strip_suffixes(norm_tvg)
                    if len(st) >= 3:
                        best = epg_by_norm_nohd.get(st)
            if best:
                db.execute(
                    "UPDATE channels SET epg_channel_id = ? WHERE id = ?",
                    (best, ch["id"]),
                )
                matched += 1
    return matched


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
                "UPDATE channels SET is_alive = ?, enabled = CASE WHEN ? THEN enabled ELSE 0 END, last_checked = datetime('now') WHERE id = ?",
                (1 if alive else 0, 1 if alive else 0, ch["id"]),
            )
