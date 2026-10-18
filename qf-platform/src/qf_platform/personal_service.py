"""Local QQ conversations and a separate, durable plan-usage request journal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quantforge.product.storage import local_root, writer_lock

from . import personal_provider, personal_vision
from .chart_analysis import stamp
from .personal_auth import AccountStore
from .storage import PlatformError, canonical


def account_directory(root: Path) -> Path:
    return Path(
        os.environ.get(
            "QF_PERSONAL_ACCOUNT_DIR", str(root / "personal-assistant/account")
        )
    )


class PersonalService:
    def __init__(self, root: Path, account: AccountStore | None = None) -> None:
        self.root = local_root(root) / "personal-assistant"
        self.root.mkdir(exist_ok=True)
        local_root(self.root)
        self.path = self.root / "conversations.sqlite"
        self.account = account or AccountStore(account_directory(root))
        self.workspace = root
        self.vision = personal_vision.VisionBudget(root)
        self.images = self.root / "images"
        self.images.mkdir(mode=0o700, exist_ok=True)
        local_root(self.images)
        with closing(self.connect()) as db, db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS sessions (
                owner TEXT,profile TEXT,current TEXT,PRIMARY KEY(owner,profile));
              CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY,owner TEXT,profile TEXT,session TEXT,
                question TEXT,status TEXT,answer TEXT,error TEXT,created TEXT,
                sent_day TEXT,usage TEXT);
              CREATE TABLE IF NOT EXISTS request_journal (
                seq INTEGER PRIMARY KEY,turn_id TEXT,kind TEXT,at TEXT);
              CREATE TABLE IF NOT EXISTS turn_images (
                turn_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
              CREATE TRIGGER IF NOT EXISTS personal_image_no_update
                BEFORE UPDATE ON turn_images BEGIN SELECT RAISE(ABORT,'immutable'); END;
              CREATE TRIGGER IF NOT EXISTS personal_image_no_delete
                BEFORE DELETE ON turn_images BEGIN SELECT RAISE(ABORT,'immutable'); END;
              CREATE TRIGGER IF NOT EXISTS personal_no_journal_update
                BEFORE UPDATE ON request_journal BEGIN
                SELECT RAISE(ABORT,'append-only'); END;
              CREATE TRIGGER IF NOT EXISTS personal_no_journal_delete
                BEFORE DELETE ON request_journal BEGIN
                SELECT RAISE(ABORT,'append-only'); END;
            """)

    def connect(self) -> sqlite3.Connection:
        local_root(self.root)
        if self.path.is_symlink():
            raise PlatformError("PERSONAL_STORE_INVALID")
        return sqlite3.connect(self.path, timeout=10)

    def queue(
        self, owner: str, key: str, text: str, *, image: bytes | None = None
    ) -> str:
        if not text.strip() or len(text) > 4000:
            raise PlatformError("PERSONAL_QUESTION_SIZE_INVALID")
        status = self.account.status()
        metadata = None
        if image is not None:
            metadata = personal_vision.validate_image(image)
        if not status["connected"] and metadata is None:
            raise PlatformError("PERSONAL_AUTH_REQUIRED")
        if text.strip().startswith("/联网 ") and not personal_provider.web_enabled(
            self.account
        ):
            raise PlatformError("PERSONAL_WEB_DISABLED")
        profile = status["profile"] or "vision-only"
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT owner,question FROM turns WHERE id=?", (key,)
            ).fetchone()
            if prior:
                prior_image = db.execute(
                    "SELECT payload FROM turn_images WHERE turn_id=?", (key,)
                ).fetchone()
                old_meta = json.loads(prior_image[0]) if prior_image else None
                if prior != (owner, text) or old_meta != metadata:
                    raise PlatformError("PERSONAL_IDEMPOTENCY_CONFLICT")
                return key
            if metadata is not None and image is not None:
                local_root(self.images)
                path = self.images / (metadata["sha256"] + ".image")
                if path.is_symlink():
                    raise PlatformError("PERSONAL_VISION_IMAGE_REDIRECTED")
                if path.exists():
                    if path.read_bytes() != image:
                        raise PlatformError("PERSONAL_VISION_IMAGE_INTEGRITY_FAILED")
                else:
                    if (
                        sum(
                            x.stat().st_size
                            for x in self.images.iterdir()
                            if x.is_file()
                        )
                        + len(image)
                        > 64 * 1024 * 1024
                    ):
                        raise PlatformError("PERSONAL_VISION_IMAGE_ARCHIVE_FULL")
                    descriptor = os.open(
                        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(image)
            if db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] >= 2000:
                raise PlatformError("PERSONAL_ARCHIVE_FULL")
            if db.execute(
                "SELECT id FROM turns WHERE owner=? AND status IN ('QUEUED','RUNNING')",
                (owner,),
            ).fetchone():
                raise PlatformError("PERSONAL_QUESTION_BUSY")
            session = db.execute(
                "SELECT current FROM sessions WHERE owner=? AND profile=?",
                (owner, profile),
            ).fetchone()
            if not session:
                session = (uuid.uuid4().hex,)
                db.execute(
                    "INSERT INTO sessions VALUES(?,?,?)", (owner, profile, session[0])
                )
            db.execute(
                "INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    owner,
                    profile,
                    session[0],
                    text,
                    "QUEUED",
                    None,
                    None,
                    stamp(),
                    None,
                    None,
                ),
            )
            if metadata is not None:
                db.execute(
                    "INSERT INTO turn_images VALUES(?,?)", (key, canonical(metadata))
                )
        return key

    def new_session(self, owner: str) -> None:
        profile = self.account.status()["profile"]
        if not profile:
            raise PlatformError("PERSONAL_AUTH_REQUIRED")
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(owner,profile) "
                "DO UPDATE SET current=excluded.current",
                (owner, profile, uuid.uuid4().hex),
            )

    def results(self) -> list[dict[str, Any]]:
        profile = self.account.status()["profile"]
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT id,owner,status,answer,error FROM turns "
                "WHERE profile IN (?, 'vision-only') "
                "AND status IN ('COMPLETED','FAILED','UNKNOWN')",
                (profile,),
            ).fetchall()
        return [
            dict(zip(("id", "owner", "status", "answer", "error"), row, strict=True))
            for row in rows
        ]

    def status(self, owner: str) -> dict[str, Any]:
        account = self.account.status()
        last_login = self.account.read("login-status.json").get("state", "")
        login_error = (
            last_login
            if not account["connected"]
            and isinstance(last_login, str)
            and re.fullmatch(
                r"(?:PERSONAL_[A-Z0-9_]{1,160}|AUTHORIZATION_TIMEOUT)", last_login
            )
            else None
        )
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with closing(self.connect()) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM turns t WHERE owner=? AND sent_day=? "
                "AND NOT EXISTS(SELECT 1 FROM turn_images i WHERE i.turn_id=t.id)",
                (owner, day),
            ).fetchone()[0]
            account_count = db.execute(
                "SELECT COUNT(*) FROM turns t WHERE sent_day=? "
                "AND NOT EXISTS(SELECT 1 FROM turn_images i WHERE i.turn_id=t.id)",
                (day,),
            ).fetchone()[0]
        return {
            "connected": account["connected"],
            "login_error": login_error,
            "model": self.account.read("settings.json").get("model"),
            "requests_today": count,
            "account_requests_today": account_count,
            "daily_limit": 20,
            "web_search_enabled": personal_provider.web_enabled(self.account),
            "image_generation_supported": False,
            "vision": self.vision.status(),
        }

    def tick(self) -> bool:
        with writer_lock(self.root):
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                interrupted = db.execute(
                    "SELECT id FROM turns WHERE status='RUNNING'"
                ).fetchall()
                for (key,) in interrupted:
                    if db.execute(
                        "SELECT 1 FROM turn_images WHERE turn_id=?", (key,)
                    ).fetchone():
                        self.vision.finish(key, "UNKNOWN")
                    db.execute(
                        "UPDATE turns SET status='UNKNOWN',"
                        "error='PERSONAL_RESPONSE_UNCONFIRMED' WHERE id=?",
                        (key,),
                    )
                    db.execute(
                        "INSERT INTO request_journal(turn_id,kind,at) VALUES(?,?,?)",
                        (key, "INTERRUPTED_NOT_REPLAYED", stamp()),
                    )
                row = db.execute(
                    "SELECT id,owner,profile,session,question FROM turns "
                    "WHERE status='QUEUED' ORDER BY rowid LIMIT 1"
                ).fetchone()
                if not row:
                    return bool(interrupted)
            key, owner, profile, session, question = row
            with closing(self.connect()) as db:
                attachment = db.execute(
                    "SELECT payload FROM turn_images WHERE turn_id=?", (key,)
                ).fetchone()
            image_meta = None
            try:
                image_meta = json.loads(attachment[0]) if attachment else None
                if attachment and (
                    not isinstance(image_meta, dict)
                    or not isinstance(image_meta.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", image_meta["sha256"])
                    or image_meta.get("mime")
                    not in {"image/png", "image/jpeg", "image/webp"}
                ):
                    raise PlatformError("PERSONAL_VISION_IMAGE_INTEGRITY_FAILED")
                if self.account.status()["profile"] != profile and not (
                    image_meta and profile == "vision-only"
                ):
                    raise PlatformError("PERSONAL_ACCOUNT_CHANGED")
                model: Any = None
                if image_meta:
                    if not isinstance(
                        image_meta.get("sha256"), str
                    ) or not re.fullmatch(r"[a-f0-9]{64}", image_meta["sha256"]):
                        raise PlatformError("PERSONAL_VISION_IMAGE_INTEGRITY_FAILED")
                    local_root(self.images)
                    path = self.images / (image_meta["sha256"] + ".image")
                    if path.is_symlink() or not path.is_file():
                        raise PlatformError("PERSONAL_VISION_IMAGE_INTEGRITY_FAILED")
                    image_bytes = path.read_bytes()
                    if hashlib.sha256(image_bytes).hexdigest() != image_meta["sha256"]:
                        raise PlatformError("PERSONAL_VISION_IMAGE_INTEGRITY_FAILED")
                    token = personal_vision.api_key(self.workspace)
                    model = "deepseek-flash"
                    self.vision.reserve(key, question, image_meta)
                else:
                    token = self.account.access_token()
                    model = self.account.read("settings.json").get("model")
                if not image_meta and (
                    not isinstance(model, str)
                    or model
                    not in {
                        x["slug"]
                        for x in self.account.read("models.json").get("models", [])
                    }
                ):
                    raise PlatformError("PERSONAL_MODEL_NOT_SELECTED")
                if (
                    question.strip().startswith("/联网 ")
                    and not personal_provider.web_enabled(self.account)
                    and not image_meta
                ):
                    raise PlatformError("PERSONAL_WEB_DISABLED")
                day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
                with closing(self.connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    if not image_meta and (
                        db.execute(
                            "SELECT COUNT(*) FROM turns t WHERE sent_day=? "
                            "AND NOT EXISTS "
                            "(SELECT 1 FROM turn_images i WHERE i.turn_id=t.id)",
                            (day,),
                        ).fetchone()[0]
                        >= 20
                    ):
                        raise PlatformError("PERSONAL_ACCOUNT_DAILY_LIMIT")
                    if not image_meta and (
                        db.execute(
                            "SELECT COUNT(*) FROM turns t WHERE owner=? AND sent_day=? "
                            "AND NOT EXISTS "
                            "(SELECT 1 FROM turn_images i WHERE i.turn_id=t.id)",
                            (owner, day),
                        ).fetchone()[0]
                        >= 20
                    ):
                        raise PlatformError("PERSONAL_DAILY_LIMIT")
                    history = db.execute(
                        "SELECT question,answer FROM turns WHERE owner=? AND profile=? "
                        "AND session=? AND status='COMPLETED' "
                        "ORDER BY rowid DESC LIMIT 5",
                        (owner, profile, session),
                    ).fetchall()
                    messages = []
                    for user, assistant in reversed(history):
                        messages.extend(
                            [
                                {"role": "user", "content": user},
                                {"role": "assistant", "content": assistant},
                            ]
                        )
                    while sum(len(x["content"]) for x in messages) > 20000:
                        messages = messages[2:]
                    messages.append({"role": "user", "content": question})
                    db.execute(
                        "UPDATE turns SET status='RUNNING',sent_day=? WHERE id=?",
                        (day, key),
                    )
                    db.execute(
                        "INSERT INTO request_journal(turn_id,kind,at) VALUES(?,?,?)",
                        (key, "REQUEST_STARTED", stamp()),
                    )
                if image_meta:
                    text, usage = personal_vision.describe(
                        token, image_bytes, image_meta["mime"], question
                    )
                    self.vision.finish(key, "COMPLETED")
                else:
                    text, usage = personal_provider.answer(
                        self.account, messages, model, token
                    )
                with closing(self.connect()) as db, db:
                    db.execute(
                        "UPDATE turns SET status='COMPLETED',answer=?,usage=? "
                        "WHERE id=?",
                        (text, canonical(usage), key),
                    )
                    db.execute(
                        "INSERT INTO request_journal(turn_id,kind,at) VALUES(?,?,?)",
                        (key, "REQUEST_COMPLETED", stamp()),
                    )
            except (PlatformError, OSError, ValueError, KeyError, TypeError) as error:
                reason = (
                    str(error)
                    if isinstance(error, PlatformError)
                    else "PERSONAL_RESPONSE_UNCONFIRMED"
                )
                state = (
                    "UNKNOWN"
                    if reason
                    in {
                        "PERSONAL_RESPONSE_UNCONFIRMED",
                        "PERSONAL_VISION_EFFECT_UNKNOWN",
                    }
                    else "FAILED"
                )
                if image_meta:
                    self.vision.finish(key, state)
                with closing(self.connect()) as db, db:
                    db.execute(
                        "UPDATE turns SET status=?,error=? WHERE id=?",
                        (state, reason, key),
                    )
                    db.execute(
                        "INSERT INTO request_journal(turn_id,kind,at) VALUES(?,?,?)",
                        (key, "REQUEST_" + state, stamp()),
                    )
            return True
