"""Short-transaction SQLite persistence for the local inbox."""

import hmac
import json
import hashlib
import secrets
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
from collections.abc import Iterator

from garage_sdk.api import RepoRef, InboxItem, ItemState, ClientKind, SubmitWork, normalize_repo


class Store:
    """A connection-per-transaction inbox, safe across local processes."""

    def __init__(self, path: Path) -> None:
        """Initialize version one without replacing existing data."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ValueError("unsupported inbox schema version")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS clients (
                    name TEXT PRIMARY KEY, kind TEXT NOT NULL, token_hash TEXT NOT NULL,
                    default_repo TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, submit_id TEXT NOT NULL, submitted_by TEXT NOT NULL,
                    trust TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL,
                    repos TEXT NOT NULL, source TEXT, context TEXT NOT NULL, issue TEXT, run TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, issue_number INTEGER,
                    UNIQUE(submitted_by, submit_id));
                CREATE INDEX IF NOT EXISTS items_issue ON items(issue_number);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, at TEXT NOT NULL, client TEXT NOT NULL,
                    action TEXT NOT NULL, item_id TEXT NOT NULL, detail TEXT NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back one short transaction."""
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_client(self, name: str, kind: ClientKind, repo: str | None = None) -> str:
        """Register a client and return its only plaintext token copy."""
        if not name.strip() or name in {"legacy-bridge", "garage-monitor"}:
            raise ValueError("invalid or reserved client name")
        token = "gar_" + secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute(
                "INSERT INTO clients VALUES (?,?,?,?,?)",
                (
                    name,
                    kind,
                    hashlib.sha256(token.encode()).hexdigest(),
                    normalize_repo(repo) if repo else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return token

    def clients(self) -> list[dict[str, str | None]]:
        """List client metadata, never token hashes."""
        with self.connection() as db:
            return [
                dict(row) for row in db.execute("SELECT name,kind,default_repo,created_at FROM clients ORDER BY name")
            ]

    def remove_client(self, name: str) -> None:
        """Revoke a client token without deleting its history."""
        with self.connection() as db:
            db.execute("DELETE FROM clients WHERE name=?", (name,))

    def authenticate(self, token: str) -> dict[str, str | None] | None:
        """Compare only token digests using a constant-time comparison."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connection() as db:
            for row in db.execute("SELECT * FROM clients"):
                if hmac.compare_digest(digest, row["token_hash"]):
                    return dict(row)
        return None

    @staticmethod
    def _item(row: sqlite3.Row) -> InboxItem:
        data = dict(row)
        data.pop("issue_number")
        for key in ("repos", "source", "context", "issue", "run"):
            data[key] = json.loads(data[key]) if data[key] is not None else None
        return InboxItem.model_validate(data)

    @staticmethod
    def _audit(db: sqlite3.Connection, client: str, action: str, item_id: str, detail: dict[str, object]) -> None:
        db.execute(
            "INSERT INTO audit(at,client,action,item_id,detail) VALUES (?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                client,
                action,
                item_id,
                json.dumps(detail),
            ),
        )

    def submit(self, client: str, kind: ClientKind, payload: SubmitWork, repo: str | None) -> tuple[InboxItem, bool]:
        """Atomically insert one submission or return its existing record."""
        now = datetime.now(timezone.utc)
        item = InboxItem(
            id="item_" + secrets.token_hex(8),
            submit_id=payload.id,
            submitted_by=client,
            trust=kind,
            kind=payload.kind,
            text=payload.text,
            state=ItemState.new if repo else ItemState.needs_repo,
            repos=[RepoRef(full_name=repo)] if repo else [],
            source=payload.source,
            context=payload.context,
            created_at=(payload.created_at or now).astimezone(timezone.utc),
            updated_at=now,
        )
        data = item.model_dump(mode="json")
        for key in ("repos", "source", "context", "issue", "run"):
            data[key] = json.dumps(data[key]) if data[key] is not None else None
        with self.connection() as db:
            result = db.execute(
                f"INSERT OR IGNORE INTO items ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
                tuple(data.values()),
            )
            row = db.execute(
                "SELECT * FROM items WHERE submitted_by=? AND submit_id=?", (client, payload.id)
            ).fetchone()
            created = result.rowcount == 1
            if created:
                self._audit(db, client, "submit", item.id, {})
            return self._item(row), created

    def get(self, item_id: str) -> InboxItem | None:
        """Find an item by its Garage id."""
        with self.connection() as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            return self._item(row) if row else None

    def items(self, state: ItemState | None = None) -> list[InboxItem]:
        """Read newest-first inbox records."""
        with self.connection() as db:
            rows = db.execute("SELECT * FROM items ORDER BY created_at DESC,id DESC")
            return [item for row in rows if (item := self._item(row)) and (state is None or item.state == state)]

    def save(self, item: InboxItem, client: str, action: str, detail: dict[str, object] | None = None) -> None:
        """Persist mutable fields and their audit record atomically."""
        item.updated_at = datetime.now(timezone.utc)
        with self.connection() as db:
            old = db.execute("SELECT state FROM items WHERE id=?", (item.id,)).fetchone()
            db.execute(
                "UPDATE items SET state=?,issue=?,run=?,updated_at=?,issue_number=? WHERE id=?",
                (
                    item.state,
                    item.issue.model_dump_json() if item.issue else None,
                    item.run.model_dump_json() if item.run else None,
                    item.updated_at.isoformat(),
                    item.issue.number if item.issue else None,
                    item.id,
                ),
            )
            self._audit(db, client, action, item.id, detail or {})
            if old and old["state"] != item.state:
                self._audit(db, client, "state_change", item.id, {"from": old["state"], "to": item.state})
