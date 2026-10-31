"""Memory Tavern companion service.

Dify owns the conversation workflow; this service only persists and recalls the
story state in seekdb. It deliberately uses deterministic narrative templates
so the demo can run without an external LLM API key.
"""
import os
import random
from datetime import datetime

import pymysql
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

DB = dict(
    host=os.getenv("SEEKDB_HOST", "127.0.0.1"),
    port=int(os.getenv("SEEKDB_PORT", "2881")),
    user=os.getenv("SEEKDB_USER", "root"),
    password=os.getenv("SEEKDB_PASSWORD", ""),
    database=os.getenv("SEEKDB_DATABASE", "memory_tavern"),
    charset="utf8mb4",
    autocommit=True,
)

app = FastAPI(title="Memory Tavern companion")

TEMPLATES = [
    "酒馆的灯芯爆了一下。{action}。柜台后的老人把这件事记进账本，说：{memory_hint}",
    "你把「{action}」说出口，墙上的旧地图自己卷了起来。{memory_hint} 新的线索出现了。",
    "铜壶里的水正好烧开。「{action}」——老人点点头，仿佛早就等着这一句。{memory_hint}",
]


def connect(create_db: bool = False):
    cfg = dict(DB)
    if create_db:
        dbname = cfg.pop("database")
        conn = pymysql.connect(**cfg)
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{dbname}`")
        conn.close()
        cfg["database"] = dbname
    return pymysql.connect(**cfg)


def ensure_schema():
    conn = connect(create_db=True)
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS story_state (
                   session_id VARCHAR(128) PRIMARY KEY,
                   turn INT NOT NULL DEFAULT 0,
                   last_action VARCHAR(1024) NOT NULL DEFAULT '',
                   updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS story_beats (
                   id BIGINT PRIMARY KEY AUTO_INCREMENT,
                   session_id VARCHAR(128) NOT NULL,
                   turn INT NOT NULL,
                   action VARCHAR(1024) NOT NULL,
                   story TEXT NOT NULL,
                   created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   INDEX ix_session_turn(session_id, turn)
               )"""
        )
    conn.close()


def recall(session_id: str):
    conn = connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT turn,last_action FROM story_state WHERE session_id=%s", (session_id,))
            state = cur.fetchone()
            cur.execute(
                "SELECT turn,action,story FROM story_beats WHERE session_id=%s ORDER BY turn DESC LIMIT 3",
                (session_id,),
            )
            beats = cur.fetchall()
        return state, beats
    finally:
        conn.close()


class ContinueRequest(BaseModel):
    session_id: str = "demo"
    action: str


@app.on_event("startup")
def startup():
    ensure_schema()
    # Make the memory table searchable too: Dify/PowerContext can also recall
    # previous beats with a normal SQL LIKE/MATCH query; the companion keeps a
    # full-text index for deployments that enable it.
    conn = connect()
    with conn.cursor() as cur:
        try:
            cur.execute("CREATE FULLTEXT INDEX ft_story ON story_beats(story) WITH PARSER ik")
        except Exception:
            pass
    conn.close()


@app.get("/api/story/recall")
def get_recall(session_id: str = "demo"):
    state, beats = recall(session_id)
    return {
        "session_id": session_id,
        "turn": (state or {}).get("turn", 0),
        "last_action": (state or {}).get("last_action", ""),
        "memory": [b["story"] for b in reversed(beats)],
    }


@app.post("/api/story/continue")
def continue_story(req: ContinueRequest):
    action = req.action.strip()
    if not action:
        raise HTTPException(status_code=400, detail="action is required")
    state, beats = recall(req.session_id)
    turn = (state or {}).get("turn", 0) + 1
    previous = " ".join(b["story"] for b in reversed(beats))
    memory_hint = (
        f"你上次说过「{state['last_action']}」，这次它又回来了。"
        if state and state.get("last_action")
        else "这是酒馆今晚的第一段故事。"
    )
    story = random.Random(turn).choice(TEMPLATES).format(action=action, memory_hint=memory_hint)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO story_state(session_id,turn,last_action)
                   VALUES(%s,%s,%s)
                   ON DUPLICATE KEY UPDATE turn=VALUES(turn), last_action=VALUES(last_action)""",
                (req.session_id, turn, action),
            )
            cur.execute(
                "INSERT INTO story_beats(session_id,turn,action,story) VALUES(%s,%s,%s,%s)",
                (req.session_id, turn, action, story),
            )
    finally:
        conn.close()
    return {
        "session_id": req.session_id,
        "turn": turn,
        "story": story,
        "memory_used": bool(previous),
    }


@app.get("/api/health")
def health():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM story_beats")
            n = cur.fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "beats": n, "database": DB["database"]}
