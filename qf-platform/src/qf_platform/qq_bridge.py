"""Durable private QQ inbox/outbox; the existing assistant worker alone calls models."""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quantforge.product.storage import local_root

from .chart_analysis import ChartAnalysisService, stamp
from .chart_channels import short_reply
from .chart_errors import failure_message
from .chart_reports import markdown, validate_image
from .personal_service import PersonalService
from .personal_vision import failure_message as vision_failure_message
from .personal_vision import validate_image as validate_personal_image
from .qq_protocol import (
    HELP,
    QQConfig,
    accepted_event,
    chart_metadata,
    conversation_owner,
    failure_reply,
    group_destination,
    message_parts,
)
from .qq_transport import OneBot
from .storage import PlatformError, canonical, digest


class QQBridge:
    def __init__(
        self, root: Path, config: QQConfig, transport: OneBot | None = None
    ) -> None:
        self.workspace = local_root(root)
        self.root = self.workspace / "qq-bridge"
        self.root.mkdir(exist_ok=True)
        local_root(self.root)
        self.images = self.root / "images"
        self.images.mkdir(exist_ok=True)
        local_root(self.images)
        self.path = self.root / "transport.sqlite"
        self.config = config
        self.transport = transport or OneBot(config)
        self.charts = ChartAnalysisService(root)
        self.personal = PersonalService(root) if config.personal_enabled else None
        self.online = False
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox (
                  id TEXT PRIMARY KEY,sender TEXT,body TEXT,signature TEXT,state TEXT);
                CREATE TABLE IF NOT EXISTS pending (
                  sender TEXT PRIMARY KEY,artifact TEXT,created TEXT);
                CREATE TABLE IF NOT EXISTS routes (
                  source TEXT PRIMARY KEY,sender TEXT,job TEXT,created TEXT,day TEXT);
                CREATE TABLE IF NOT EXISTS outbox (
                  id TEXT PRIMARY KEY,sender TEXT,action TEXT,body TEXT,
                  state TEXT,result TEXT);
            """)
            db.execute("UPDATE outbox SET state='UNKNOWN' WHERE state='SENDING'")
            interrupted = db.execute(
                "SELECT id,sender FROM inbox WHERE state='PROCESSING'"
            ).fetchall()
            for key, sender in interrupted:
                # Recover only an already-created task; never call create/model again.
                with closing(self.charts.connect()) as charts:
                    found = charts.execute(
                        "SELECT id FROM jobs WHERE request_id=?", (key,)
                    ).fetchone()
                if found:
                    self._route(db, key, sender, found[0])
                db.execute(
                    "UPDATE inbox SET state=? WHERE id=?",
                    ("DONE" if found else "REVIEW_REQUIRED", key),
                )

    def connect(self) -> sqlite3.Connection:
        local_root(self.root)
        if self.path.is_symlink():
            raise PlatformError("QQ_STORE_REDIRECTED")
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def accept(self, event: dict[str, Any]) -> bool:
        if not accepted_event(event, self.config):
            return False
        sender = str(event["user_id"])
        key = "qq-" + digest([self.config.bot_id, sender, str(event["message_id"])])
        if event.get("message_type") == "group":
            key = "qq-group-" + digest(
                [
                    self.config.bot_id,
                    str(event["group_id"]),
                    sender,
                    str(event["message_id"]),
                ]
            )
        signature = digest(event)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT signature FROM inbox WHERE id=?", (key,)
            ).fetchone()
            if prior:
                if prior[0] != signature:
                    raise PlatformError("QQ_EVENT_IDEMPOTENCY_CONFLICT")
                return False
            if db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] >= 1000:
                raise PlatformError("QQ_INBOX_FULL")
            db.execute(
                "INSERT INTO inbox VALUES(?,?,?,?,?)",
                (key, sender, canonical(event), signature, "NEW"),
            )
        return True

    @staticmethod
    def _route(db: sqlite3.Connection, key: str, sender: str, job: str) -> None:
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        db.execute(
            "INSERT OR IGNORE INTO routes VALUES(?,?,?,?,?)",
            (key, sender, job, stamp(), day),
        )

    @staticmethod
    def _text(db: sqlite3.Connection, key: str, sender: str, text: str) -> None:
        group = group_destination(sender)
        body = {
            "user_id" if group is None else "group_id": sender
            if group is None
            else group[0],
            "message": [{"type": "text", "data": {"text": text[:2000]}}],
        }
        if group:
            body["message"] = [
                {"type": "at", "data": {"qq": group[1]}},
                {"type": "text", "data": {"text": "\n" + text[:1900]}},
            ]
        db.execute(
            "INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?,?)",
            (
                key,
                sender,
                "send_group_msg" if group else "send_private_msg",
                canonical(body),
                "NEW",
                None,
            ),
        )

    def _destination_allowed(self, address: str) -> bool:
        group = group_destination(address)
        if group:
            return bool(
                self.config.group_enabled
                and self.config.personal_enabled
                and group[0] in self.config.allowed_groups
                and group[1] in self.config.group_allowed_users
            )
        return address in self.config.allowed_users

    def _remember_image(
        self,
        db: sqlite3.Connection,
        sender: str,
        segment: dict[str, Any],
        *,
        personal: bool = False,
    ) -> bytes:
        data = self.transport.image(segment)
        if personal:
            meta = validate_personal_image(data)
        else:
            _, meta = validate_image(base64.b64encode(data).decode())
        local_root(self.images)
        path = self.images / (meta["sha256"] + ".image")
        if path.is_symlink():
            raise PlatformError("QQ_IMAGE_REDIRECTED")
        if path.exists():
            if path.read_bytes() != data:
                raise PlatformError("QQ_IMAGE_INTEGRITY_FAILED")
        else:
            if (
                sum(x.stat().st_size for x in self.images.iterdir() if x.is_file())
                + len(data)
                > 64 * 1024 * 1024
            ):
                raise PlatformError("QQ_IMAGE_ARCHIVE_FULL")
            with path.open("xb") as stream:
                stream.write(data)
        db.execute(
            "INSERT INTO pending VALUES(?,?,?) ON CONFLICT(sender) "
            "DO UPDATE SET artifact=excluded.artifact,created=excluded.created",
            (sender, path.name, stamp()),
        )
        return data

    def _report(
        self, db: sqlite3.Connection, key: str, sender: str, job: dict[str, Any]
    ) -> None:
        if job["status"] == "SUCCEEDED":
            self._text(
                db,
                key + "-text",
                sender,
                short_reply(job)
                + "\n任务："
                + job["id"]
                + "\n完整报告随后以文件发送。",
            )
            content = markdown(job).encode("utf-8")
            if len(content) > 200000:
                raise PlatformError("QQ_REPORT_TOO_LARGE")
            body = {
                "user_id": sender,
                "file": "base64://" + base64.b64encode(content).decode(),
                "name": job["id"] + ".md",
            }
            db.execute(
                "INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?,?)",
                (
                    key + "-file",
                    sender,
                    "upload_private_file",
                    canonical(body),
                    "NEW",
                    None,
                ),
            )
        else:
            self._text(
                db,
                key + "-text",
                sender,
                f"任务 {job['id']}：{job['status']}。\n"
                + (
                    failure_message(job) + "已保留记录及模型预留，不会自动重发。"
                    if job["status"] in {"FAILED", "UNKNOWN"}
                    else "任务尚未完成；等待模型配置或预算时请联系维护者。"
                ),
            )

    def _handle(
        self, db: sqlite3.Connection, key: str, sender: str, event: dict[str, Any]
    ) -> None:
        in_group = event.get("message_type") == "group"
        if in_group and any(
            x.get("type") not in {"text", "at", "reply", "image"}
            for x in event["message"]
        ):
            self._text(
                db,
                key,
                sender,
                "群内支持文字及单张图片识别；正式股票报告请使用已授权的私聊入口。",
            )
            return
        text, images = message_parts(event)
        if self.personal is not None:
            command = text.split(maxsplit=1)[0] if text else ""
            stock_command = command in {"/分析", "分析"}
            if in_group and stock_command:
                self._text(
                    db,
                    key,
                    sender,
                    "正式股票报告仅在原授权私聊中使用 /分析。群内 /识图 只做图片观察。",
                )
                return
            vision_request = command in {"/识图", "/看图"} or bool(
                images
                and text
                and (
                    (not text.startswith("/") and not stock_command)
                    or text.startswith("/问 ")
                )
            )
            if vision_request:
                question = text
                if command in {"/识图", "/看图", "/问"}:
                    question = (
                        text.split(maxsplit=1)[1]
                        if len(text.split(maxsplit=1)) > 1
                        else "请描述图片并提取可见文字，说明无法确认的信息。"
                    )
                if images:
                    data = self._remember_image(db, sender, images[0], personal=True)
                else:
                    recent = db.execute(
                        "SELECT artifact,created FROM pending WHERE sender=?", (sender,)
                    ).fetchone()
                    if not recent or datetime.now(UTC) - datetime.fromisoformat(
                        recent[1]
                    ) > timedelta(minutes=30):
                        self._text(
                            db,
                            key,
                            sender,
                            "请先发送一张图片，或把 /识图 问题 与图片同发；"
                            "群内每条仍需@机器人。",
                        )
                        return
                    path = self.images / recent[0]
                    if (
                        not re.fullmatch(r"[a-f0-9]{64}\.image", recent[0])
                        or path.is_symlink()
                    ):
                        raise PlatformError("QQ_IMAGE_INTEGRITY_FAILED")
                    data = path.read_bytes()
                self.personal.queue(sender, key, question, image=data)
                self._text(
                    db,
                    key + "-accepted",
                    sender,
                    "图片问题已接收，交给DeepSeek识图；使用独立识图预算，不会自动重发。",
                )
                return
            if in_group and images and not text:
                self._remember_image(db, sender, images[0], personal=True)
                self._text(
                    db,
                    key,
                    sender,
                    "图片已保存，尚未调用模型。请@机器人发 /识图 你的问题，"
                    "或下次把图片与问题同发。",
                )
                return
            if command in {"/生图", "/画图", "/image"}:
                self._text(
                    db,
                    key,
                    sender,
                    "当前ChatGPT账号授权通道暂不支持机器人生图。"
                    "可在ChatGPT官方网页或App中生图；本次没有调用模型。",
                )
                return
            if text == "/能力":
                enabled = self.personal.status(sender)["web_search_enabled"]
                self._text(
                    db,
                    key,
                    sender,
                    "联网搜索："
                    + ("已启用，仍受模型和账号权限限制。" if enabled else "未启用。")
                    + "\n生图：当前账号授权通道不支持。\n"
                    + "识图：DeepSeek独立预算，图片与问题同发，或 /识图 问题。\n"
                    "使用 /联网 问题 发起检索；回复附来源。群内仍需每次@机器人。",
                )
                return
            if text == "/联网":
                self._text(
                    db, key, sender, "请发送 /联网 你的问题；群内需要先@机器人。"
                )
                return
            if (
                text.startswith("/联网 ")
                and not self.personal.status(sender)["web_search_enabled"]
            ):
                self._text(db, key, sender, "联网搜索尚未启用，本次未调用模型。")
                return
            if text == "/最近回答":
                rows = [
                    row for row in self.personal.results() if row["owner"] == sender
                ]
                if not rows:
                    self._text(db, key, sender, "还没有已完成或已停止的个人问答记录。")
                else:
                    row = rows[-1]
                    answer = (
                        row["answer"]
                        if row["status"] == "COMPLETED"
                        else "上次个人问答未完成，不自动重发模型请求。"
                    )
                    for index in range(0, len(answer), 1700):
                        self._text(
                            db,
                            key + "-" + str(index),
                            sender,
                            "[已有回答] " + answer[index : index + 1700],
                        )
                return
            if text in {"/个人状态", "/助手状态"}:
                status = self.personal.status(sender)
                self._text(
                    db,
                    key,
                    sender,
                    "个人助手："
                    + ("账号已连接" if status["connected"] else "等待本机ChatGPT授权")
                    + (f"（{status['login_error']}）" if status["login_error"] else "")
                    + f"；今日请求{status['requests_today']}/20。\n"
                    + f"ChatGPT共享请求{status['account_requests_today']}/20。\n"
                    + f"模型：{status['model'] or '待授权选择'}。QQ上下文独立保存。",
                    # Vision is separately metered; no ChatGPT-plan debit.
                )
                vision = status["vision"]
                self._text(
                    db,
                    key + "-vision",
                    sender,
                    f"DeepSeek识图：今日{vision['requests_today']}/20；累计保守预留"
                    f"{vision['reserved_cny']}/{vision['max_cost_cny']}元。与股票预算分开。",
                )
                return
            if text == "/新对话":
                self.personal.new_session(sender)
                self._text(db, key, sender, "已开始新的QQ对话；旧记录与请求用量保留。")
                return
            if text == "/模型":
                catalog = self.personal.account.read("models.json").get("models", [])
                self._text(
                    db,
                    key,
                    sender,
                    "授权时可用模型：\n"
                    + "\n".join(
                        x["slug"] + " · " + x["display_name"] for x in catalog[:15]
                    )
                    + "\n用 /模型 模型标识 切换；"
                    "尚未授权时先在本机完成Continue with ChatGPT。",
                )
                return
            if text.startswith("/模型 "):
                if sender != self.config.allowed_users[0]:
                    raise PlatformError("PERSONAL_MODEL_CHANGE_OWNER_ONLY")
                model = text.split(maxsplit=1)[1]
                models = self.personal.account.read("models.json").get("models", [])
                if model not in {x["slug"] for x in models}:
                    raise PlatformError("PERSONAL_MODEL_NOT_SELECTED")
                self.personal.account.write("settings.json", {"model": model})
                self._text(db, key, sender, "已选择模型：" + model)
                return
            question = text[3:].strip() if text.startswith("/问 ") else text
            if question and (
                text.startswith(("/问 ", "/联网 "))
                or not text.startswith(("/", "分析 "))
                and text != "帮助"
            ):
                if images:
                    self._text(
                        db,
                        key,
                        sender,
                        "此指令暂不直接处理图片。请使用 /识图 问题；"
                        "需要联网时可在识图完成后发 /联网 文字问题。",
                    )
                    return
                self.personal.queue(sender, key, question)
                self._text(
                    db,
                    key + "-accepted",
                    sender,
                    "问题已接收，使用你授权的ChatGPT账号回答。",
                )
                return
            if text in {"/帮助", "帮助"}:
                self._text(
                    db,
                    key,
                    sender,
                    "个人助手：直接发文字即可连续问答；/问 问题 也可使用。\n"
                    "/新对话 开始新上下文；/个人状态 查看连接和请求次数；\n"
                    "/模型 查看或选择模型。\n"
                    "/联网 问题 检索网页并附来源；/能力 查看可用功能。\n"
                    "/识图 问题 与单张图片同发，使用独立DeepSeek识图预算。\n"
                    "/最近回答 重新领取已有回答，不调用模型。\n"
                    "个人问答不继承ChatGPT网页历史、记忆；额度以授权账号为准，"
                    "未知结果不自动重发。\n\n"
                    + (
                        "群内每次需@机器人；当前成员上下文独立于私聊和其他成员。"
                        if in_group
                        else HELP
                    ),
                )
                return
        if in_group:
            self._text(
                db,
                key,
                sender,
                "群内仅支持文字个人助手。请@机器人发问题，或使用 /帮助。",
            )
            return
        if images:
            self._remember_image(
                db, sender, images[0], personal=self.personal is not None
            )
        if text in {"/状态", "状态"}:
            row = db.execute(
                "SELECT job FROM routes WHERE sender=? ORDER BY rowid DESC LIMIT 1",
                (sender,),
            ).fetchone()
            if row:
                job = self.charts.get(row[0])
                self._text(
                    db,
                    key,
                    sender,
                    f"最新任务 {job['id']}：{job['status']}。\n"
                    f"使用 /报告 {job['id']} 领取已有结果，不会调用模型。",
                )
            else:
                self._text(db, key, sender, "你还没有通过此机器人创建任务。\n" + HELP)
        elif text.startswith("/报告 "):
            job_id = text.split(maxsplit=1)[1]
            row = db.execute(
                "SELECT job FROM routes WHERE sender=? AND job=?", (sender, job_id)
            ).fetchone()
            if not row:
                raise PlatformError("QQ_REPORT_NOT_OWNED")
            self._report(db, key, sender, self.charts.get(row[0]))
        elif text.startswith(("/分析", "分析")):
            metadata = chart_metadata(text)
            row = db.execute(
                "SELECT artifact,created FROM pending WHERE sender=?", (sender,)
            ).fetchone()
            if not row or datetime.now(UTC) - datetime.fromisoformat(
                row[1]
            ) > timedelta(minutes=30):
                raise PlatformError("QQ_RECENT_IMAGE_REQUIRED")
            day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            used = db.execute(
                "SELECT COUNT(*) FROM routes WHERE sender=? AND day=?", (sender, day)
            ).fetchone()[0]
            last = db.execute(
                "SELECT created FROM routes WHERE sender=? ORDER BY rowid DESC LIMIT 1",
                (sender,),
            ).fetchone()
            if used >= self.config.daily_limit or (
                last
                and datetime.now(UTC) - datetime.fromisoformat(last[0])
                < timedelta(seconds=60)
            ):
                raise PlatformError("QQ_DAILY_OR_COOLDOWN_LIMIT")
            path = self.images / row[0]
            if path.is_symlink() or path.parent.resolve() != self.images.resolve():
                raise PlatformError("QQ_IMAGE_REDIRECTED")
            encoded = base64.b64encode(path.read_bytes()).decode()
            # This creates a durable task only; no model call occurs in the transport.
            job = self.charts.create({**metadata, "image_base64": encoded}, key)
            self._route(db, key, sender, job["id"])
            self._text(
                db,
                key,
                sender,
                f"截图任务已保存：{job['id']}。\n日线未来5根；最长持仓观察{metadata['max_holding_bars']}根，每根复评。完成后返回摘要和报告文件。",
            )
        else:
            self._text(
                db,
                key,
                sender,
                ("截图已保存（30分钟内补充参数，不产生模型费用）。\n" if images else "")
                + HELP
                + (
                    "\n通用图片也可继续发送 /识图 你的问题。"
                    if self.personal is not None and images
                    else ""
                ),
            )

    def process_one(self) -> bool:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,sender,body FROM inbox WHERE state='NEW' "
                "ORDER BY rowid LIMIT 1"
            ).fetchone()
            if not row:
                return False
            db.execute("UPDATE inbox SET state='PROCESSING' WHERE id=?", (row[0],))
        key, sender, raw = row
        destination = sender
        with closing(self.connect()) as db, db:
            try:
                event = json.loads(raw)
                if (
                    not accepted_event(event, self.config)
                    or str(event.get("user_id")) != sender
                ):
                    raise PlatformError("QQ_SENDER_DISABLED")
                if (
                    digest(event)
                    != db.execute(
                        "SELECT signature FROM inbox WHERE id=?", (key,)
                    ).fetchone()[0]
                ):
                    raise PlatformError("QQ_EVENT_INTEGRITY_FAILED")
                destination = conversation_owner(event)
                self._handle(db, key, destination, event)
                db.execute("UPDATE inbox SET state='DONE' WHERE id=?", (key,))
            except (ValueError, OSError, KeyError, TypeError) as error:
                # No exception details or secret-bearing URLs are sent to QQ.
                code = (
                    str(error)
                    if isinstance(error, PlatformError)
                    else "QQ_REQUEST_FAILED"
                )
                if code not in {"QQ_EVENT_INTEGRITY_FAILED", "QQ_SENDER_DISABLED"}:
                    self._text(
                        db,
                        key + "-error",
                        destination,
                        "个人助手尚未连接或请求未完成（"
                        + code
                        + "）。请查看 /个人状态；不会切换到其他付费模型或自动重发。"
                        if code.startswith("PERSONAL_")
                        else failure_reply(code),
                    )
                db.execute("UPDATE inbox SET state='FAILED' WHERE id=?", (key,))
        return True

    def collect_results(self) -> None:
        with closing(self.connect()) as db, db:
            if self.personal is not None and self.config.active:
                for result in self.personal.results():
                    if not self._destination_allowed(result["owner"]):
                        continue
                    text = (
                        result["answer"]
                        if result["status"] == "COMPLETED"
                        else vision_failure_message(str(result["error"]))
                        if str(result["error"]).startswith("PERSONAL_VISION_")
                        else "本机账号今日20次共享请求已用完，"
                        "群聊和私聊共用此限额；不会自动重发。"
                        if result["error"] == "PERSONAL_ACCOUNT_DAILY_LIMIT"
                        else "本次未确认联网检索或可靠来源，"
                        "未返回未经检索确认的实时答案；不会自动重发。"
                        if result["error"]
                        in {
                            "PERSONAL_WEB_SEARCH_NOT_CONFIRMED",
                            "PERSONAL_WEB_SOURCES_MISSING",
                        }
                        else "当前模型或账号不支持请求的联网能力；"
                        "可用普通文字聊天，或在私聊切换模型。不会自动重试。"
                        if str(result["error"]).endswith(
                            "SUBSCRIPTION_SHARING_UNSUPPORTED_CAPABILITY"
                        )
                        else "个人问答未完成（"
                        + result["status"]
                        + "），不会自动重发。可发送新问题；授权或额度异常请在本机查看。"
                    )
                    for index in range(0, len(text), 1700):
                        self._text(
                            db,
                            "personal-" + result["id"] + "-" + str(index),
                            result["owner"],
                            "[个人助手] " + text[index : index + 1700],
                        )
            for sender, job_id in db.execute(
                "SELECT sender,job FROM routes"
            ).fetchall():
                if sender not in self.config.allowed_users or not self.config.active:
                    continue
                job = self.charts.get(job_id)
                if job["status"] in {
                    "SUCCEEDED",
                    "FAILED",
                    "UNKNOWN",
                    "WAITING_FOR_MODEL",
                }:
                    self._report(
                        db, "result-" + job_id + "-" + job["status"], sender, job
                    )

    def deliver_one(self) -> bool:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,sender,action,body FROM outbox WHERE state='NEW' "
                "ORDER BY rowid LIMIT 1"
            ).fetchone()
            if not row or not self.online:
                return False
            key, sender, action, raw = row
            body = json.loads(raw)
            group = group_destination(sender)
            matches = (
                action == "send_group_msg" and str(body.get("group_id")) == group[0]
                if group
                else action in {"send_private_msg", "upload_private_file"}
                and str(body.get("user_id")) == sender
            )
            if (
                not self.config.active
                or not self._destination_allowed(sender)
                or not matches
            ):
                db.execute("UPDATE outbox SET state='CANCELLED' WHERE id=?", (key,))
                return True
            db.execute("UPDATE outbox SET state='SENDING' WHERE id=?", (key,))
        try:
            value = self.transport.call(action, body)
            state, result = "SENT", canonical(value.get("data"))
        except PlatformError as error:
            state, result = (
                ("REJECTED" if str(error) == "QQ_ACTION_REJECTED" else "UNKNOWN"),
                str(error),
            )
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE outbox SET state=?,result=? WHERE id=?", (state, result, key)
            )
        return True

    def probe(self) -> None:
        online = False
        try:
            if self.config.active:
                login = self.transport.call("get_login_info", {}).get("data") or {}
                status = self.transport.call("get_status", {}).get("data") or {}
                online = (
                    str(login.get("user_id")) == self.config.bot_id
                    and status.get("online") is True
                )
        except (PlatformError, TypeError, AttributeError):
            pass
        self.online = online
        value = {
            "schema_version": "qq-connection-v1",
            "connected": online,
            "observed_at": stamp(),
        }
        path = self.root / "connection.json"
        temporary = self.root / "connection.pending"
        if path.is_symlink() or temporary.is_symlink():
            raise PlatformError("QQ_STORE_REDIRECTED")
        temporary.write_text(canonical(value), encoding="utf-8")
        os.replace(temporary, path)
