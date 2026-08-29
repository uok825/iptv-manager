import sqlite3
import os
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent.parent / "data" / "iptv.db"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                enabled INTEGER DEFAULT 1,
                last_synced TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tvg_id TEXT,
                tvg_name TEXT,
                tvg_logo TEXT,
                group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
                display_name TEXT NOT NULL,
                stream_url TEXT,
                source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
                enabled INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                is_alive INTEGER DEFAULT 1,
                last_checked TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_channels_tvg_id ON channels(tvg_id);
            CREATE INDEX IF NOT EXISTS idx_channels_group_id ON channels(group_id);
            CREATE INDEX IF NOT EXISTS idx_channels_source_id ON channels(source_id);
        """)

        existing = db.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        if existing == 0:
            default_groups = [
                ("Genel", 0),
                ("Haber", 1),
                ("Spor", 2),
                ("Eğlence", 3),
                ("Müzik", 4),
                ("Çocuk", 5),
                ("Belgesel", 6),
                ("Sinema", 7),
                ("Diğer", 8),
            ]
            db.executemany(
                "INSERT INTO groups (name, sort_order) VALUES (?, ?)",
                default_groups,
            )

        existing_sources = db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        if existing_sources == 0:
            default_sources = [
                ("iptv-org Türkiye", "https://iptv-org.github.io/iptv/countries/tr.m3u"),
                ("iptv-org Türkçe", "https://iptv-org.github.io/iptv/languages/tur.m3u"),
                ("Free-TV", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"),
            ]
            db.executemany(
                "INSERT INTO sources (name, url) VALUES (?, ?)",
                default_sources,
            )
