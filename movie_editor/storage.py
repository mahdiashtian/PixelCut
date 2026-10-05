"""Small SQLite repository for defaults, the active draft and the asset library."""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .domain import Asset, Draft, Look, Quality, Settings


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP,
                name TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL
            );
        """)
        self._migrate_source_quality()

    def _migrate_source_quality(self) -> None:
        # Earlier releases saved Lossless as the default, expanding compressed uploads.
        # Migrate once; later explicit Lossless selections remain intentional.
        if self.get("source_quality_migrated"):
            return
        with self.db:
            for key in ("defaults", "draft"):
                data = self.get(key)
                if data:
                    settings = data["settings"] if key == "draft" else data
                    if settings.get("quality") != Quality.SOURCE:
                        settings["quality"] = Quality.SOURCE
                        self.db.execute(
                            "UPDATE preferences SET value=? WHERE key=?",
                            (json.dumps(data, ensure_ascii=False), key),
                        )
            self.db.execute("INSERT INTO preferences VALUES ('source_quality_migrated', 'true')")

    def close(self) -> None:
        self.db.close()

    def get(self, key: str, fallback=None):
        row = self.db.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else fallback

    def set(self, key: str, value) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO preferences VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def defaults(self) -> Settings:
        data = self.get("defaults")
        return Settings.from_dict(data) if data else Settings()

    def save_defaults(self, settings: Settings) -> None:
        settings.validate()
        self.set("defaults", asdict(settings))

    def draft(self) -> Draft | None:
        data = self.get("draft")
        return Draft.from_dict(data) if data else None

    def save_draft(self, draft: Draft | None) -> None:
        if draft:
            draft.validate()
        self.set("draft", draft.to_dict() if draft else None)

    def add_asset(self, asset: Asset) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO assets VALUES (?, ?, ?, ?)",
                (asset.id, asset.kind, asset.name, asset.path),
            )

    def asset(self, asset_id: str | None, kind: str) -> Asset | None:
        if not asset_id:
            return None
        row = self.db.execute(
            "SELECT * FROM assets WHERE id=? AND kind=?", (asset_id, kind)
        ).fetchone()
        if row is None:
            raise ValueError("فایل انتخاب‌شده در کتابخانه موجود نیست.")
        asset = Asset(**dict(row))
        if not Path(asset.path).is_file():
            raise ValueError("فایل کتابخانه از روی دیسک حذف شده است؛ دوباره آپلود کنید.")
        return asset

    def assets(self, kind: str, offset: int = 0) -> list[Asset]:
        rows = self.db.execute(
            "SELECT * FROM assets WHERE kind=? ORDER BY rowid DESC LIMIT 8 OFFSET ?",
            (kind, max(0, offset)),
        ).fetchall()
        return [Asset(**dict(row)) for row in rows]

    def delete_asset(self, asset_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM assets WHERE id=?", (asset_id,))
        bindings = self.get("look_luts", {})
        self.set(
            "look_luts", {look: value for look, value in bindings.items() if value != asset_id}
        )

    def bind_look(self, look: Look, asset_id: str) -> None:
        if not look.needs_lut:
            raise ValueError("این پروفایل به LUT متصل نمی‌شود.")
        self.asset(asset_id, "lut")
        bindings = self.get("look_luts", {})
        bindings[look.value] = asset_id
        self.set("look_luts", bindings)

    def look_lut(self, look: Look) -> str | None:
        return self.get("look_luts", {}).get(look.value)

    def record(self, name: str, status: str, detail: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO history (name, status, detail) VALUES (?, ?, ?)",
                (name[:200], status, detail[:1000]),
            )
            self.db.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT 100)"
            )

    def history(self) -> list[dict]:
        return [
            dict(row) for row in self.db.execute("SELECT * FROM history ORDER BY id DESC LIMIT 10")
        ]
