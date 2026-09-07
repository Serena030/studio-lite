#!/usr/bin/env python3
"""工作室 · 轻装版

一张单从提出到收工，全在这一个文件里：建表、七个接口、把活真的交出去跑。
没有登录体系、没有推送、没有值班——那些是各家自己的事，这里只管工单本身。

跑起来：
    pip install fastapi uvicorn
    python3 server.py
    打开 http://127.0.0.1:8770

要改的都在环境变量里，见 .env.example。
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("STUDIO_DB") or (ROOT / "studio.db"))
WORKER_CMD = os.getenv("STUDIO_WORKER_CMD", "claude -p")
WORKER_DIR = os.getenv("STUDIO_WORKDIR", str(ROOT))
WORKER_TIMEOUT = int(os.getenv("STUDIO_WORKER_TIMEOUT", "1800"))
TOKEN = os.getenv("STUDIO_TOKEN", "").strip()
AUTO_DISPATCH = os.getenv("STUDIO_AUTO_DISPATCH", "0") == "1"

STATUSES = ("inbox", "queued", "working", "review", "failed", "merged")

app = FastAPI(title="studio-lite")
_tasks: dict[str, asyncio.Task] = {}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def setup() -> None:
    """建表。单子和单子底下的话分两张，删单连着话一起删。"""
    conn = db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
              no        TEXT PRIMARY KEY,
              title     TEXT NOT NULL,
              body      TEXT DEFAULT '',
              who       TEXT DEFAULT 'me',
              worker    TEXT DEFAULT '',
              status    TEXT DEFAULT 'inbox',
              err       TEXT DEFAULT '',
              created_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS order_msgs (
              id   INTEGER PRIMARY KEY AUTOINCREMENT,
              no   TEXT NOT NULL,
              who  TEXT NOT NULL,
              text TEXT NOT NULL,
              created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_msgs_no ON order_msgs(no);
            """
        )
        conn.commit()
    finally:
        conn.close()


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def guard(token: str | None) -> None:
    """没设口令就是本机自己玩；设了就必须带对。"""
    if TOKEN and (token or "") != TOKEN:
        raise HTTPException(401, "口令不对")


def next_no() -> str:
    conn = db()
    try:
        row = conn.execute("SELECT no FROM orders ORDER BY no DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    n = int(str(row["no"]).split("-")[-1]) + 1 if row else 1
    return "ORD-%03d" % n


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["msgs"] = []
    return d


def get_order(no: str, with_msgs: bool = False) -> dict | None:
    conn = db()
    try:
        row = conn.execute("SELECT * FROM orders WHERE no=?", (no,)).fetchone()
        if not row:
            return None
        item = row_to_dict(row)
        if with_msgs:
            rows = conn.execute(
                "SELECT * FROM order_msgs WHERE no=? ORDER BY id", (no,)
            ).fetchall()
            item["msgs"] = [dict(r) for r in rows]
        return item
    finally:
        conn.close()


def list_orders(status: str = "") -> list[dict]:
    conn = db()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status=? ORDER BY no DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM orders ORDER BY no DESC").fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


def add_msg(no: str, who: str, text: str) -> dict:
    conn = db()
    try:
        cur = conn.execute(
            "INSERT INTO order_msgs (no, who, text, created_at) VALUES (?,?,?,?)",
            (no, who, text, now()),
        )
        conn.execute("UPDATE orders SET updated_at=? WHERE no=?", (now(), no))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM order_msgs WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def update_order(no: str, **fields) -> dict | None:
    allowed = ("title", "body", "who", "worker", "status", "err")
    sets, args = [], []
    for key, value in fields.items():
        if key in allowed and value is not None:
            sets.append(key + "=?")
            args.append(value)
    if not sets:
        return get_order(no)
    sets.append("updated_at=?")
    args.extend([now(), no])
    conn = db()
    try:
        conn.execute("UPDATE orders SET " + ", ".join(sets) + " WHERE no=?", args)
        conn.commit()
    finally:
        conn.close()
    return get_order(no)


def build_prompt(order: dict) -> str:
    """交出去的那段话：单子本身 + 底下已经说过的，按时间顺序摊平。

    工人拿到的是纯文本，不带这边的任何身份或人格设定——它是谁由 STUDIO_WORKER_CMD
    那头自己决定，这里只负责把活说清楚。
    """
    parts = [
        "这是一张工单，请你动手把它做完。",
        "",
        "单号：" + order["no"],
        "标题：" + order["title"],
    ]
    if (order.get("body") or "").strip():
        parts += ["", "正文：", order["body"].strip()]
    msgs = order.get("msgs") or []
    if msgs:
        parts += ["", "这张单底下已经说过的话："]
        for m in msgs:
            parts.append("[" + m["who"] + "] " + m["text"])
    parts += [
        "",
        "做完以后用几句话说清楚你改了什么、还剩什么没做。不要复述这段提示。",
    ]
    return "\n".join(parts)


async def run_order(no: str) -> None:
    """把这张单真的交出去跑一趟。

    跑的是 STUDIO_WORKER_CMD 那条命令，单子内容从标准输入进去，
    它说的话原样落回单子底下。中途出事就落 failed，不吞掉错。
    """
    order = get_order(no, with_msgs=True)
    if not order:
        return
    update_order(no, status="working", err="")
    prompt = build_prompt(order)
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(WORKER_CMD),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=WORKER_DIR,
        )
        out, _ = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")), timeout=WORKER_TIMEOUT
        )
        text = (out or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            update_order(no, status="failed", err="退出码 " + str(proc.returncode))
            add_msg(no, "worker", text[-4000:] or "这趟没跑起来，也没留下话。")
            return
        add_msg(no, "worker", text[-8000:] or "跑完了，但一个字都没说。")
        update_order(no, status="review", err="")
    except asyncio.TimeoutError:
        update_order(no, status="failed", err="跑太久了，掐了")
        add_msg(no, "worker", "这趟超时被掐断。")
    except FileNotFoundError:
        update_order(no, status="failed", err="找不到 " + WORKER_CMD.split()[0])
        add_msg(no, "worker", "命令找不到：" + WORKER_CMD)
    except Exception as exc:
        update_order(no, status="failed", err=str(exc)[:400])
        add_msg(no, "worker", "这趟塌了：" + str(exc)[:400])
    finally:
        _tasks.pop(no, None)


def dispatch(no: str) -> bool:
    """同一张单不许起两趟——她连点两下、轮询和点击撞上，都会走到这儿。"""
    task = _tasks.get(no)
    if task and not task.done():
        return False
    _tasks[no] = asyncio.create_task(run_order(no))
    return True


class NewOrder(BaseModel):
    title: str
    body: str = ""
    who: str = "me"


class PatchOrder(BaseModel):
    title: str | None = None
    body: str | None = None
    worker: str | None = None
    status: str | None = None


class NewMsg(BaseModel):
    who: str = "me"
    text: str


@app.on_event("startup")
async def _startup() -> None:
    setup()
    # 上一条命留下的活：进程一没它就没了，库里那张单还写着在做。
    conn = db()
    try:
        conn.execute(
            "UPDATE orders SET status='failed', err=? WHERE status='working'",
            ("服务重启了，这趟没跑完",),
        )
        conn.execute("UPDATE orders SET status='inbox' WHERE status='queued'")
        conn.commit()
    finally:
        conn.close()


@app.get("/api/orders")
def api_list(status: str = "", x_token: str | None = Header(None)):
    guard(x_token)
    return {"items": list_orders(status)}


@app.get("/api/orders/{no}")
def api_get(no: str, x_token: str | None = Header(None)):
    guard(x_token)
    item = get_order(no, with_msgs=True)
    if not item:
        raise HTTPException(404, "没有这张单")
    return item


@app.post("/api/orders")
async def api_create(payload: NewOrder, x_token: str | None = Header(None)):
    guard(x_token)
    title = payload.title.strip()
    if not title:
        raise HTTPException(400, "标题不能空")
    no = next_no()
    conn = db()
    try:
        conn.execute(
            "INSERT INTO orders (no, title, body, who, status, created_at, updated_at)"
            " VALUES (?,?,?,?,'inbox',?,?)",
            (no, title, payload.body.strip(), payload.who.strip() or "me", now(), now()),
        )
        conn.commit()
    finally:
        conn.close()
    if AUTO_DISPATCH:
        update_order(no, status="queued")
        dispatch(no)
    return get_order(no, with_msgs=True)


@app.patch("/api/orders/{no}")
def api_patch(no: str, payload: PatchOrder, x_token: str | None = Header(None)):
    guard(x_token)
    if not get_order(no):
        raise HTTPException(404, "没有这张单")
    if payload.status and payload.status not in STATUSES:
        raise HTTPException(400, "没有这个状态")
    item = update_order(no, **payload.model_dump(exclude_none=True))
    return item


@app.post("/api/orders/{no}/msg")
def api_msg(no: str, payload: NewMsg, x_token: str | None = Header(None)):
    guard(x_token)
    if not get_order(no):
        raise HTTPException(404, "没有这张单")
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "说点什么再发")
    return add_msg(no, payload.who.strip() or "me", text)


@app.post("/api/orders/{no}/run")
async def api_run(no: str, x_token: str | None = Header(None)):
    guard(x_token)
    item = get_order(no)
    if not item:
        raise HTTPException(404, "没有这张单")
    if item["status"] == "working":
        return {"ok": False, "why": "它正在跑"}
    update_order(no, status="queued", err="")
    started = dispatch(no)
    return {"ok": started, "why": "" if started else "它正在跑"}


@app.delete("/api/orders/{no}")
def api_delete(no: str, x_token: str | None = Header(None)):
    guard(x_token)
    conn = db()
    try:
        conn.execute("DELETE FROM order_msgs WHERE no=?", (no,))
        cur = conn.execute("DELETE FROM orders WHERE no=?", (no,))
        conn.commit()
        return {"ok": cur.rowcount > 0}
    finally:
        conn.close()


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/config")
def api_config(x_token: str | None = Header(None)):
    guard(x_token)
    return {"worker": WORKER_CMD, "auto": AUTO_DISPATCH, "workdir": WORKER_DIR}


if __name__ == "__main__":
    import uvicorn

    setup()
    uvicorn.run(
        app,
        host=os.getenv("STUDIO_HOST", "127.0.0.1"),
        port=int(os.getenv("STUDIO_PORT", "8770")),
        log_level="warning",
    )
