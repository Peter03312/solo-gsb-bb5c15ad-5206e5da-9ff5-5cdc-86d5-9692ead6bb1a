"""已确认行程作废（误确认后的撤销）。

调度员误确认行程后，可凭行程号与**非空作废原因**将其作废。语义：

* 作废**不删除** `confirmed_trips` 中的确认快照——已确认行程及原批次确认
  结果仍作为**历史快照**长期可查（`GET /api/trips` / `GET /api/trips/{id}`
  及批次幂等记录均不变）；
* 作废只使该行程按**固化航时**计算的航段占用及狭窄水道组展开占用从**后续
  调度**中失效（`planning.load_occupancy` 经 `voided_trips` 过滤）；
* 作废记录不可变：重复作废**返回首次记录**（同样的原因与作废时间），
  `200 replayed=true`，绝不覆盖原因；
* 未知行程 `404 TRIP_NOT_FOUND`（作废与查询口径一致）；
  行程存在但从未作废时查询记录 `404 VOID_RECORD_NOT_FOUND`。

并发：作废与确认都在单个 `BEGIN IMMEDIATE` 事务内完成状态变更与占用复算，
SQLite 的写锁将二者串行化，因此不会产生相互重叠的有效确认行程——先提交的
作废会被后进入事务的确认在 `load_occupancy` 时读到（被作废行程不再占位，
但这不会造成重叠，因为该行程已无效）；先提交的确认同样会被作废看到。
已作废批次行程的原幂等键重放仍返回首次确认结果，不重新产生占用
（见 `services/batch.py`，重放优先且完全不读占用表）。
"""
import sqlite3

from ..errors import ApiError


def _trip_exists(conn, trip_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM confirmed_trips WHERE trip_id = ?", (trip_id,)
    ).fetchone() is not None


def get_void_record(conn, trip_id: int) -> dict:
    """按行程号查询作废记录。

    未知行程（无确认快照）-> 404 TRIP_NOT_FOUND；
    行程存在但未作废 -> 404 VOID_RECORD_NOT_FOUND。
    """
    if not _trip_exists(conn, trip_id):
        raise ApiError(
            404, "TRIP_NOT_FOUND", f"已确认行程不存在: {trip_id}",
            {"trip_id": trip_id},
        )
    row = conn.execute(
        "SELECT trip_id, reason, voided_at FROM voided_trips WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()
    if row is None:
        raise ApiError(
            404, "VOID_RECORD_NOT_FOUND",
            f"行程 {trip_id} 尚未作废，不存在作废记录",
            {"trip_id": trip_id},
        )
    return {"trip_id": row["trip_id"], "reason": row["reason"],
            "voided_at": row["voided_at"]}


def void_trip(conn, trip_id: int, reason: str) -> tuple[dict, bool]:
    """作废已确认行程。返回 (作废记录, replayed)。

    首次作废：`BEGIN IMMEDIATE` 内核验行程存在、插入不可变作废记录，
    返回 (记录, False)，HTTP 层据此返回 201。
    重复作废（含并发竞争）：返回**首次**记录与 True（HTTP 200），
    不覆盖原因、不更新时间。未知行程 -> 404 TRIP_NOT_FOUND。

    作废本身不写 confirmed_trips、不改正批次幂等结果；占用失效在
    load_occupancy 读侧即时生效。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _trip_exists(conn, trip_id):
            raise ApiError(
                404, "TRIP_NOT_FOUND", f"已确认行程不存在: {trip_id}",
                {"trip_id": trip_id},
            )

        existing = conn.execute(
            "SELECT trip_id, reason, voided_at FROM voided_trips WHERE trip_id = ?",
            (trip_id,),
        ).fetchone()
        if existing is not None:
            # 重复作废：首次记录原样返回，原因/时间不覆盖
            conn.execute("COMMIT")
            record = {"trip_id": existing["trip_id"],
                      "reason": existing["reason"],
                      "voided_at": existing["voided_at"]}
            return record, True

        try:
            cur = conn.execute(
                "INSERT INTO voided_trips(trip_id, reason) VALUES (?, ?)",
                (trip_id, reason),
            )
        except sqlite3.IntegrityError:
            # 并发竞争：另一事务刚把同一行程作废，回滚后按首次记录返回
            conn.execute("ROLLBACK")
            row = conn.execute(
                "SELECT trip_id, reason, voided_at FROM voided_trips WHERE trip_id = ?",
                (trip_id,),
            ).fetchone()
            return {"trip_id": row["trip_id"], "reason": row["reason"],
                    "voided_at": row["voided_at"]}, True

        row = conn.execute(
            "SELECT trip_id, reason, voided_at FROM voided_trips WHERE trip_id = ?",
            (cur.lastrowid,),
        ).fetchone()
        conn.execute("COMMIT")
        return {"trip_id": row["trip_id"], "reason": row["reason"],
                "voided_at": row["voided_at"]}, False
    except ApiError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
