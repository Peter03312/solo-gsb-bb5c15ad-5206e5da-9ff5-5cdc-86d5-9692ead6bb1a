"""SQLite 连接管理、建表与初始化（含演示数据）。"""
import os
import sqlite3
from contextlib import contextmanager

from . import config
from .errors import ApiError


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id     TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS segments (
    seg_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    origin       TEXT NOT NULL REFERENCES nodes(node_id),
    destination  TEXT NOT NULL REFERENCES nodes(node_id),
    travel_time  INTEGER NOT NULL CHECK (travel_time > 0),   -- 正整数分钟航时
    depth        REAL NOT NULL,                             -- 静水深度（米）
    clearance    REAL,                                     -- 桥底净空（米），可空
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (origin <> destination)
);

CREATE TABLE IF NOT EXISTS tide_revisions (
    revision   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    note       TEXT
);

CREATE TABLE IF NOT EXISTS tide_values (
    revision INTEGER NOT NULL REFERENCES tide_revisions(revision),
    minute   INTEGER NOT NULL,
    level    REAL NOT NULL,                             -- 名义潮位（米）
    error    REAL NOT NULL DEFAULT 0.0,                 -- 预测误差（米，非负）：真值落在 level±error
    PRIMARY KEY (revision, minute)
);

CREATE TABLE IF NOT EXISTS confirmed_trips (
    trip_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    revision        INTEGER NOT NULL,                       -- 确认时所据修订号（快照）
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    start_minute    INTEGER NOT NULL,
    window_minutes  INTEGER NOT NULL,
    draft           REAL NOT NULL,
    height          REAL NOT NULL,
    margin          REAL NOT NULL,
    arrival_minute  INTEGER NOT NULL,
    itinerary       TEXT NOT NULL,                          -- JSON 快照
    confirmed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batch_confirmations (
    idempotency_key TEXT PRIMARY KEY,                      -- 批次幂等键
    revision        INTEGER NOT NULL,                       -- 批次所据修订号
    content_hash    TEXT NOT NULL,                          -- 提交内容（不含幂等键）的规范化哈希
    result          TEXT NOT NULL,                          -- 首次成功结果 JSON（各行程号等）
    confirmed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 行程作废记录：每条已确认行程至多一条（trip_id 唯一）。confirmed_trips 中的
-- 原确认快照仍作为历史快照保留可查；作废只使该行程按固化航时计算的航段及
-- 狭窄水道组占用从后续调度中失效（load_occupancy 排除已作废行程）。
-- 重复作废不覆盖：服务层命中既有记录直接返回首次原因/时间，唯一约束兜底并发。
CREATE TABLE IF NOT EXISTS voided_trips (
    trip_id     INTEGER PRIMARY KEY REFERENCES confirmed_trips(trip_id),
    reason      TEXT NOT NULL,                              -- 非空作废原因（剥离首尾空白后至少 1 字符）
    voided_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 航段分时航时管制：半开分钟区间 [start_minute, end_minute) 内进入航段的
-- 船舶按 travel_time 计航时；未命中任何区间沿用 segments.travel_time 基础航时。
-- 整组替换（同 seg_id 先全删再插），区间互不交叠由服务层保证。
CREATE TABLE IF NOT EXISTS segment_travel_controls (
    seg_id        INTEGER NOT NULL REFERENCES segments(seg_id),
    start_minute  INTEGER NOT NULL CHECK (start_minute >= 0),
    end_minute    INTEGER NOT NULL CHECK (end_minute > start_minute),  -- 半开区间止（不含）
    travel_time   INTEGER NOT NULL CHECK (travel_time > 0),            -- 管制通行分钟（正整数）
    PRIMARY KEY (seg_id, start_minute)
);

-- 狭窄水道组：组建成后不可修改（无更新/删除接口）；每条航段至多属于一个组。
-- 组占用在规划时按当前组成员关系自快照原占用展开（见 services/groups.py）。
CREATE TABLE IF NOT EXISTS channel_groups (
    group_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS channel_group_members (
    group_id   INTEGER NOT NULL REFERENCES channel_groups(group_id),
    seg_id     INTEGER NOT NULL UNIQUE REFERENCES segments(seg_id),  -- 航段不得重复归组
    position   INTEGER NOT NULL,                                    -- 提交集合中的次序（0 起）
    PRIMARY KEY (group_id, seg_id)
);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn():
    """请求级连接，业务代码自行控制事务（isolation_level=None）。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    """建表；空库且 SEED_DEMO 时写入演示数据。幂等。"""
    conn.executescript(SCHEMA)
    _migrate(conn)
    if config.SEED_DEMO and conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0:
        _seed_demo(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """旧版数据库平滑升级：为 tide_values 补 error 列（既有行误差取 0）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tide_values)").fetchall()}
    if "error" not in cols:
        conn.execute("ALTER TABLE tide_values ADD COLUMN error REAL NOT NULL DEFAULT 0.0")


# ---------------------------------------------------------------------------
# 演示数据：4 个节点、3 条单向航段（其中一条带桥），修订 #1 含 0..1439 潮位
# ---------------------------------------------------------------------------

def _seed_demo(conn: sqlite3.Connection) -> None:
    import math

    cur = conn.execute("BEGIN IMMEDIATE")
    try:
        nodes = [
            ("A", "外锚地"),
            ("B", "内河节点B"),
            ("C", "桥区节点C"),
            ("D", "内港码头"),
        ]
        conn.executemany("INSERT INTO nodes(node_id, name) VALUES (?, ?)", nodes)

        # A->B：深水直航；B->C：带桥；B->D 与 C->D 构成可比较路径
        segments = [
            # origin, dest, 航时(分钟), 静水深度(m), 桥底净空(m)
            ("A", "B", 30, 6.0, None),
            ("B", "C", 40, 4.0, 8.0),
            ("C", "D", 20, 5.0, None),
            ("B", "D", 90, 3.5, None),
        ]
        conn.executemany(
            "INSERT INTO segments(origin, destination, travel_time, depth, clearance)"
            " VALUES (?, ?, ?, ?, ?)",
            segments,
        )

        rev = conn.execute(
            "INSERT INTO tide_revisions(note) VALUES (?)",
            ("初始潮位修订：周期 720 分钟正弦潮，振幅 1.5m，均值 2.0m",),
        ).lastrowid

        rows = [(rev, m, 2.0 + 1.5 * math.sin(2 * math.pi * m / 720.0)) for m in range(1440)]
        conn.executemany(
            "INSERT INTO tide_values(revision, minute, level) VALUES (?, ?, ?)", rows
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
