"""已确认行程作废。

调度员误确认行程后，可凭**行程号 + 非空作废原因**将其作废：

* 已确认行程快照（`confirmed_trips`）原样保留——作废后 `GET /api/trips`、
  `GET /api/trips/{id}` 与原批次确认结果（幂等重放）仍作为**历史快照**可查；
* 作废只使该行程按其**固化航时**（快照各段 `travel_time`）计算的航段占用与
  狭窄水道组展开占用，从后续调度（单船/批次预排、确认复算、改期建议）中
  失效——`planning.load_occupancy` 经 `LEFT JOIN trip_voids` 跳过已作废快照；
* 作废记录可按行程号查询；未知行程报 `TRIP_NOT_FOUND`，已确认但未作废报
  `VOID_RECORD_NOT_FOUND`；重复作废返回**首次记录**且不覆盖原因。

并发：作废与单船/批次确认都在 `BEGIN IMMEDIATE` 写事务中串行执行。
SQLite 的写锁保证两者的状态变更（插入 `trip_voids`）与确认复算（读取占用、
写入快照）不会交错：要么确认先拿到锁、复算看到占用并拒绝重叠行程后提交，
作废随后才释放占用；要么作废先提交、确认复算时已不再计入该行程。因此
不会产生相互重叠的有效确认行程。

批次语义：已作废批次行程的原幂等键重放仍返回 `batch_confirmations` 中
登记的**首次确认结果**（行程号不变、不重新产生占用），本模块不参与重放。
原行程潮位安全复核仍按历史快照执行（`reschedule` 只判潮位，不读作废状态）。
"""
import sqlite3

from ..errors import ApiError, trip_not_found


def _record(row) -> dict:
    """trip_voids 行 -> 对外响应（行程号、原因、作废时间）。"""
    return {
        "trip_id": row["trip_id"],
        "reason": row["reason"],
        "voided_at": row["voided_at"],
    }


def void_trip(conn, trip_id: int, reason: str) -> tuple[dict, bool]:
    """作废已确认行程（单事务）。

    返回 (作废记录, created)：首次作废 created=True；重复作废返回首次记录、
    created=False 且**不覆盖**首次原因。未知行程 -> 404 TRIP_NOT_FOUND。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute(
            "SELECT 1 FROM confirmed_trips WHERE trip_id = ?", (trip_id,)
        ).fetchone() is None:
            raise trip_not_found(trip_id)

        existing = conn.execute(
            "SELECT trip_id, reason, voided_at FROM trip_voids WHERE trip_id = ?",
            (trip_id,),
        ).fetchone()
        if existing is not None:
            # 重复作废：幂等返回首次记录，绝不覆盖原因/时间
            conn.execute("COMMIT")
            return _record(existing), False

        cur = conn.execute(
            "INSERT INTO trip_voids(trip_id, reason) VALUES (?, ?)",
            (trip_id, reason),
        )
        row = conn.execute(
            "SELECT trip_id, reason, voided_at FROM trip_voids WHERE trip_id = ?",
            (cur.lastrowid,),
        ).fetchone()
        conn.execute("COMMIT")
        return _record(row), True
    except ApiError:
        conn.execute("ROLLBACK")
        raise
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise


def get_void(conn, trip_id: int) -> dict:
    """按行程号查询作废记录。

    未知行程 -> 404 TRIP_NOT_FOUND；已知但未作废 -> 404 VOID_RECORD_NOT_FOUND。
    """
    if conn.execute(
        "SELECT 1 FROM confirmed_trips WHERE trip_id = ?", (trip_id,)
    ).fetchone() is None:
        raise trip_not_found(trip_id)
    row = conn.execute(
        "SELECT trip_id, reason, voided_at FROM trip_voids WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()
    if row is None:
        raise ApiError(
            404, "VOID_RECORD_NOT_FOUND",
            f"行程 {trip_id} 未作废，不存在作废记录",
            {"trip_id": trip_id},
        )
    return _record(row)
