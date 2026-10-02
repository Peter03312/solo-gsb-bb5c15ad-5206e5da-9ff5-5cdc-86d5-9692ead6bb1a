"""港口潮汐通航行程规划服务 —— FastAPI 入口。"""
import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import config
from .database import get_conn, init_db
from .errors import ApiError
from .schemas import (
    BatchConfirmRequest,
    BatchConfirmResponse,
    BatchPlanRequest,
    BatchPlanResponse,
    ChannelGroupCreateRequest,
    ChannelGroupOut,
    ConfirmRequest,
    NodeIn,
    PlanRequest,
    PlanResponse,
    ReviewRequest,
    RevisionOut,
    SegmentIn,
    SegmentOut,
    TravelControlListOut,
    TravelControlOut,
    TravelControlReplaceRequest,
    TideCorrection,
    TideErrorRevision,
    TripOut,
    TripReviewOut,
    TripVoidRequest,
    TripVoidResponse,
    VoidRecordOut,
)
from .services.batch import confirm_batch, plan_batch
from .services.controls import load_controls, replace_segment_controls
from .services.groups import create_group, get_group, list_groups
from .services.planning import find_itinerary, load_occupancy
from .services.reschedule import review_and_reschedule
from .services.voids import get_void_record, void_trip


@asynccontextmanager
async def lifespan(app: FastAPI):
    with get_conn() as conn:
        init_db(conn)
    yield


app = FastAPI(
    title="港口潮汐通航行程规划服务",
    version="1.0.0",
    description=(
        "录入港口节点、单向航段与逐分钟潮位（带修订号），根据船舶吃水 / "
        "水面以上高度 / 安全余量计算受潮汐与桥高约束的最早抵达行程。"
    ),
    lifespan=lifespan,
)


# ---- 统一错误响应 ----------------------------------------------------------------

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "details": {"errors": exc.errors()},
            }
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# ---- 节点 -----------------------------------------------------------------------

@app.post("/api/nodes", status_code=201, tags=["nodes"])
def create_node(body: NodeIn):
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO nodes(node_id, name) VALUES (?, ?)",
                (body.node_id, body.name),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "NODE_EXISTS", f"节点已存在: {body.node_id}",
                           {"node_id": body.node_id})
    return {"node_id": body.node_id, "name": body.name}


@app.get("/api/nodes", tags=["nodes"])
def list_nodes():
    with get_conn() as conn:
        rows = conn.execute("SELECT node_id, name FROM nodes ORDER BY node_id").fetchall()
    return [dict(r) for r in rows]


# ---- 航段 -----------------------------------------------------------------------

@app.post("/api/segments", status_code=201, response_model=SegmentOut, tags=["segments"])
def create_segment(body: SegmentIn):
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (body.origin,)).fetchone():
            from .errors import unknown_node
            raise unknown_node(body.origin)
        if not conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (body.destination,)).fetchone():
            from .errors import unknown_node
            raise unknown_node(body.destination)
        cur = conn.execute(
            "INSERT INTO segments(origin, destination, travel_time, depth, clearance)"
            " VALUES (?, ?, ?, ?, ?)",
            (body.origin, body.destination, body.travel_time, body.depth, body.clearance),
        )
        seg_id = cur.lastrowid
    return SegmentOut(seg_id=seg_id, **body.model_dump())


@app.get("/api/segments", response_model=list[SegmentOut], tags=["segments"])
def list_segments():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT seg_id, origin, destination, travel_time, depth, clearance"
            " FROM segments ORDER BY seg_id"
        ).fetchall()
    return [dict(r) for r in rows]


# ---- 航段分时航时管制 -------------------------------------------------------------

def _control_list_out(conn, seg_id: int) -> TravelControlListOut:
    row = conn.execute(
        "SELECT seg_id, travel_time AS base_travel_time FROM segments"
        " WHERE seg_id = ?",
        (seg_id,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "SEGMENT_NOT_FOUND", f"未知航段: {seg_id}",
                       {"seg_id": seg_id})
    intervals = load_controls(conn, [seg_id]).get(seg_id, [])
    return TravelControlListOut(
        seg_id=seg_id,
        base_travel_time=row["base_travel_time"],
        controls=[
            TravelControlOut(start_minute=s, end_minute=e, travel_time=t)
            for s, e, t in intervals
        ],
    )


@app.put("/api/segments/{seg_id}/travel-controls",
         response_model=TravelControlListOut, tags=["segments"])
def replace_travel_controls(seg_id: int, body: TravelControlReplaceRequest):
    """以航段编号整组替换分时航时管制，返回替换后的生效清单。

    每个元素为半开分钟区间 [start_minute, end_minute) 及正整数通行分钟；
    船舶于进入分钟命中区间即按该通行分钟计航时（途中跨边界不变），未命中
    沿用航段基础航时。空组恢复基础航时。未知航段 404 SEGMENT_NOT_FOUND；
    非法区间 422 INVALID_CONTROL_INTERVAL；组内交叠
    422 OVERLAPPING_CONTROL_INTERVAL，任一失败均保留旧管制不变。
    """
    with get_conn() as conn:
        replace_segment_controls(
            conn, seg_id, [c.model_dump() for c in body.controls]
        )
        out = _control_list_out(conn, seg_id)
    return out


@app.get("/api/segments/{seg_id}/travel-controls",
         response_model=TravelControlListOut, tags=["segments"])
def get_travel_controls(seg_id: int):
    """读取航段当前生效的分时航时管制清单（按起点升序）。"""
    with get_conn() as conn:
        return _control_list_out(conn, seg_id)


# ---- 狭窄水道组 -------------------------------------------------------------------

@app.post("/api/channel-groups",
          status_code=201, response_model=ChannelGroupOut, tags=["channel-groups"])
def create_channel_group(body: ChannelGroupCreateRequest):
    """把至少两条现有航段登记为同一狭窄水道组，返回组号及成员。

    组建成后不可修改（无更新/解散接口），航段不得重复归组。船进入组内
    任一航段后，从进入分钟到抵达前一分钟独占整组。未知航段
    404 SEGMENT_NOT_FOUND、提交重复编号 422 DUPLICATE_SEGMENT_IN_GROUP、
    成员已归组 409 SEGMENT_ALREADY_GROUPED，任一失败均拒绝且不留半成品。
    """
    with get_conn() as conn:
        return create_group(conn, body.segments, body.note)


@app.get("/api/channel-groups",
         response_model=list[ChannelGroupOut], tags=["channel-groups"])
def list_channel_groups():
    """列出全部狭窄水道组（按组号升序，成员按提交次序）。"""
    with get_conn() as conn:
        return list_groups(conn)


@app.get("/api/channel-groups/{group_id}",
         response_model=ChannelGroupOut, tags=["channel-groups"])
def get_channel_group(group_id: int):
    """读取指定狭窄水道组的组号及成员。未知组 404 CHANNEL_GROUP_NOT_FOUND。"""
    with get_conn() as conn:
        return get_group(conn, group_id)


# ---- 潮位修订 --------------------------------------------------------------------

@app.get("/api/tide/revisions", response_model=list[RevisionOut], tags=["tide"])
def list_revisions():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT revision, note, created_at FROM tide_revisions ORDER BY revision"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/tide/revisions/{revision}", tags=["tide"])
def get_revision(revision: int, from_minute: int = 0, to_minute: int = 1439):
    with get_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM tide_revisions WHERE revision = ?", (revision,)
        ).fetchone():
            raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                           f"潮位修订不存在: {revision}", {"revision": revision})
        rows = conn.execute(
            "SELECT minute, level, error FROM tide_values"
            " WHERE revision = ? AND minute BETWEEN ? AND ? ORDER BY minute",
            (revision, from_minute, to_minute),
        ).fetchall()
    return {"revision": revision, "from_minute": from_minute, "to_minute": to_minute,
            "count": len(rows),
            "levels": {r["minute"]: r["level"] for r in rows},
            "errors": {r["minute"]: r["error"] for r in rows}}


@app.post("/api/tide/corrections", status_code=201, response_model=RevisionOut, tags=["tide"])
def apply_correction(body: TideCorrection):
    """名义潮位更正：基于某修订全量继承（名义潮位与误差）后覆盖给定分钟的
    名义潮位（被覆盖分钟误差置 0，未列出分钟连同误差一并继承），生成新修订号。"""
    if not body.corrections:
        raise ApiError(422, "VALIDATION_ERROR", "corrections 不能为空")
    bad = [m for m in body.corrections if m < 0]
    if bad:
        raise ApiError(422, "VALIDATION_ERROR", "分钟必须为非负整数",
                       {"bad_minutes": bad})

    with get_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            base = _resolve_base_revision(conn, body.base_revision)

            cur = conn.execute(
                "INSERT INTO tide_revisions(note) VALUES (?)",
                (body.note or f"基于修订 #{base} 的名义潮位更正",),
            )
            new_rev = cur.lastrowid

            # 全量继承所据修订（名义潮位与误差）
            conn.execute(
                "INSERT INTO tide_values(revision, minute, level, error)"
                " SELECT ?, minute, level, error FROM tide_values WHERE revision = ?",
                (new_rev, base),
            )
            # 覆盖更正分钟的名义潮位；继承行原地更新，新分钟补入（误差 0）
            conn.executemany(
                "UPDATE tide_values SET level = ? WHERE revision = ? AND minute = ?",
                [(lvl, new_rev, m) for m, lvl in sorted(body.corrections.items())],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO tide_values(revision, minute, level, error)"
                " VALUES (?, ?, ?, 0.0)",
                [(new_rev, m, lvl) for m, lvl in sorted(body.corrections.items())],
            )
            row = conn.execute(
                "SELECT revision, note, created_at FROM tide_revisions WHERE revision = ?",
                (new_rev,),
            ).fetchone()
            conn.execute("COMMIT")
        except ApiError:
            conn.execute("ROLLBACK")
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return RevisionOut(**dict(row))


@app.post("/api/tide/error-revisions", status_code=201,
          response_model=RevisionOut, tags=["tide"])
def apply_error_revision(body: TideErrorRevision):
    """潮位预测误差修订：名义潮位不变，仅覆盖给定分钟的非负误差（米）。

    未列出分钟继承基础修订的误差；旧修订（error 列缺失/为 0）一律视为误差 0。
    所提交分钟在基础修订中必须有名义潮位，否则报 MISSING_TIDE_DATA。
    """
    if not body.errors:
        raise ApiError(422, "VALIDATION_ERROR", "errors 不能为空")
    bad_minutes = [m for m in body.errors if m < 0]
    if bad_minutes:
        raise ApiError(422, "VALIDATION_ERROR", "分钟必须为非负整数",
                       {"bad_minutes": bad_minutes})
    bad_errors = [
        {"minute": m, "error": e} for m, e in sorted(body.errors.items()) if e < 0
    ]
    if bad_errors:
        raise ApiError(422, "VALIDATION_ERROR", "潮位误差必须为非负数（米）",
                       {"bad_errors": bad_errors})

    with get_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            base = _resolve_base_revision(conn, body.base_revision)

            # 提交分钟必须在基础修订中已有名义潮位（误差修订不新增名义潮位）
            minutes = sorted(body.errors)
            placeholders = ",".join("?" for _ in minutes)
            have = {
                r["minute"] for r in conn.execute(
                    f"SELECT minute FROM tide_values"
                    f" WHERE revision = ? AND minute IN ({placeholders})",
                    (base, *minutes),
                ).fetchall()
            }
            missing = [m for m in minutes if m not in have]
            if missing:
                raise ApiError(
                    422, "MISSING_TIDE_DATA",
                    f"基础修订 #{base} 中有 {len(missing)} 个分钟缺少名义潮位，"
                    "误差修订不能为其指定误差",
                    {"missing_minutes": missing[:20], "total_missing": len(missing),
                     "base_revision": base},
                )

            cur = conn.execute(
                "INSERT INTO tide_revisions(note) VALUES (?)",
                (body.note or f"基于修订 #{base} 的潮位预测误差修订",),
            )
            new_rev = cur.lastrowid

            # 名义潮位全量继承（含误差），再覆盖给定分钟的误差
            conn.execute(
                "INSERT INTO tide_values(revision, minute, level, error)"
                " SELECT ?, minute, level, error FROM tide_values WHERE revision = ?",
                (new_rev, base),
            )
            conn.executemany(
                "UPDATE tide_values SET error = ? WHERE revision = ? AND minute = ?",
                [(e, new_rev, m) for m, e in sorted(body.errors.items())],
            )
            row = conn.execute(
                "SELECT revision, note, created_at FROM tide_revisions WHERE revision = ?",
                (new_rev,),
            ).fetchone()
            conn.execute("COMMIT")
        except ApiError:
            conn.execute("ROLLBACK")
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return RevisionOut(**dict(row))


def _resolve_base_revision(conn, base_revision: Optional[int]) -> int:
    """解析基础修订号：显式指定须存在，否则取最新修订；否则 TIDE_REVISION_NOT_FOUND。"""
    if base_revision is not None:
        base = base_revision
        if not conn.execute(
            "SELECT 1 FROM tide_revisions WHERE revision = ?", (base,)
        ).fetchone():
            raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                           f"基础修订不存在: {base}", {"revision": base})
    else:
        row = conn.execute(
            "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                           "系统中尚无任何潮位修订，首次修订必须直接录入")
        base = row["revision"]
    return base


# ---- 行程规划 / 确认 --------------------------------------------------------------

@app.post("/api/trips/plan", response_model=PlanResponse, tags=["trips"])
def plan_trip(body: PlanRequest):
    with get_conn() as conn:
        blocked = load_occupancy(
            conn, body.start_minute,
            body.start_minute + body.window_minutes - 1,
        )
        result = find_itinerary(
            conn,
            origin=body.origin,
            destination=body.destination,
            start_minute=body.start_minute,
            window_minutes=body.window_minutes,
            draft=body.draft,
            height=body.height,
            margin=body.margin,
            revision=body.revision,
            blocked=blocked,
        )
    return result


@app.post("/api/trips/confirm", status_code=201, response_model=TripOut, tags=["trips"])
def confirm_trip(body: ConfirmRequest):
    """确认行程：必须携带所据修订号；同一事务内以最新潮位修订与当时占用
    重新计算，过期/不一致即冲突且不保存。"""
    if body.revision is None:
        raise ApiError(422, "VALIDATION_ERROR", "确认行程必须携带所据潮位修订号 revision")

    with get_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            # 1) 修订是否仍为最新（过期则冲突）
            latest = conn.execute(
                "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            if latest is None or latest["revision"] != body.revision:
                raise ApiError(
                    409,
                    "REVISION_STALE",
                    f"潮位已更新到修订 #{latest['revision'] if latest else None}，"
                    f"所据修订 #{body.revision} 已过期，请重新规划",
                    {"provided_revision": body.revision,
                     "current_revision": latest["revision"] if latest else None},
                )

            # 2) 同一事务内依据该修订与**当时占用**重新计算
            #    （既有已确认快照即使彼此重叠也一并作为占用；本行程尚未落库）
            blocked = load_occupancy(
                conn, body.start_minute, body.start_minute + body.window_minutes - 1
            )
            try:
                recalculated = find_itinerary(
                    conn,
                    origin=body.origin,
                    destination=body.destination,
                    start_minute=body.start_minute,
                    window_minutes=body.window_minutes,
                    draft=body.draft,
                    height=body.height,
                    margin=body.margin,
                    revision=body.revision,
                    blocked=blocked,
                )
            except ApiError as exc:
                # 占用变化导致窗口内已无可行行程：提交行程不再符合最优结果
                if exc.code == "NO_FEASIBLE_ITINERARY":
                    raise ApiError(
                        409,
                        "PLAN_MISMATCH",
                        "占用已变化：窗口内已不存在可行行程，提交行程不再符合"
                        "最优结果，确认失败",
                        {"reason": "no_feasible_itinerary"},
                    )
                raise

            # 3) 回填行程必须与复核结果逐段一致
            def norm(legs):
                return [
                    (l["seg_id"], l["origin"], l["destination"],
                     l["start_minute"], l["end_minute"], l["travel_time"])
                    if isinstance(l, dict) else
                    (l.seg_id, l.origin, l.destination, l.start_minute, l.end_minute, l.travel_time)
                    for l in legs
                ]

            submitted = norm(body.itinerary)
            canonical = norm(recalculated["itinerary"])
            if submitted != canonical or recalculated["arrival_minute"] != _payload_arrival(body):
                raise ApiError(
                    409,
                    "PLAN_MISMATCH",
                    "行程复核结果与所提交行程不一致（窗口/船舶参数/潮位数据可能已变化），确认失败",
                    {"recalculated": recalculated},
                )

            # 4) 复核通过：按"当时结果"保存不可变快照
            cur = conn.execute(
                "INSERT INTO confirmed_trips"
                " (revision, origin, destination, start_minute, window_minutes,"
                "  draft, height, margin, arrival_minute, itinerary)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (body.revision, body.origin, body.destination, body.start_minute,
                 body.window_minutes, body.draft, body.height, body.margin,
                 recalculated["arrival_minute"], json.dumps(recalculated["itinerary"])),
            )
            trip_id = cur.lastrowid
            row = conn.execute(
                "SELECT * FROM confirmed_trips WHERE trip_id = ?", (trip_id,)
            ).fetchone()
            conn.execute("COMMIT")
        except ApiError:
            conn.execute("ROLLBACK")
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return _trip_out(dict(row))


def _payload_arrival(body: ConfirmRequest) -> Optional[int]:
    if not body.itinerary:
        return body.start_minute
    return body.itinerary[-1].end_minute


# ---- 批次预排 / 确认（同一修订下 2～5 艘船） ---------------------------------------

@app.post("/api/trips/batch/plan",
          response_model=BatchPlanResponse, tags=["trips", "batch"])
def plan_trip_batch(body: BatchPlanRequest):
    """批次预排（只读、不占位）。

    按 ships 给出的优先顺序逐艘沿用单船最早抵达 + 同刻航段序列字典序
    择优；后船同时避开全部已确认占用与本批前船的拟占用。任一船无解
    （或未知节点/缺潮位）时，错误 details 给出首个失败序号 ship_index
    （0 起）。revision 省略时整批采用最新修订。
    """
    with get_conn() as conn:
        result = plan_batch(conn, body.ships, body.revision)
    return result


@app.post("/api/trips/batch/confirm",
          status_code=201, response_model=BatchConfirmResponse,
          tags=["trips", "batch"])
def confirm_trip_batch(body: BatchConfirmRequest):
    """批次确认：单事务内核订最新修订与当时占用，逐艘复算并逐段比对。

    - 修订过期 -> 409 REVISION_STALE；
    - 任一船无解或与提交行程不一致 -> 409 PLAN_MISMATCH（details.ship_index
      指出首个失败序号），全批不保存；
    - 全部通过则一次写入全部不可变快照并登记幂等键；同键同内容重试返回
      原结果（200，replayed=true，即使修订已更新）；同键异内容
      409 BATCH_KEY_CONFLICT。
    """
    with get_conn() as conn:
        result, replayed = confirm_batch(conn, body)
    if replayed:
        return JSONResponse(status_code=200, content=result)
    return result


@app.get("/api/trips", response_model=list[TripOut], tags=["trips"])
def list_trips():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.*, v.voided_at AS voided_at FROM confirmed_trips c"
            " LEFT JOIN voided_trips v ON v.trip_id = c.trip_id"
            " ORDER BY c.trip_id"
        ).fetchall()
    return [_trip_out(dict(r)) for r in rows]


@app.get("/api/trips/{trip_id}", response_model=TripOut, tags=["trips"])
def get_trip(trip_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT c.*, v.voided_at AS voided_at FROM confirmed_trips c"
            " LEFT JOIN voided_trips v ON v.trip_id = c.trip_id"
            " WHERE c.trip_id = ?",
            (trip_id,),
        ).fetchone()
    if row is None:
        raise ApiError(404, "TRIP_NOT_FOUND", f"已确认行程不存在: {trip_id}",
                       {"trip_id": trip_id})
    return _trip_out(dict(row))


@app.post(
    "/api/trips/{trip_id}/void",
    response_model=TripVoidResponse,
    tags=["trips", "void"],
)
def void_confirmed_trip(trip_id: int, body: TripVoidRequest):
    """误确认后作废已确认行程（事务串行）。

    凭行程号与非空作废原因作废。作废不删除确认快照——已确认行程与原批次
    确认结果仍作为历史快照可查；仅该行程按固化航时计算的航段及狭窄水道
    组占用从后续调度（单船/批次的预排、确认复算、改期建议）中失效。
    重复作废返回**首次记录**（200，replayed=true），不覆盖原因/时间；
    未知行程 404 TRIP_NOT_FOUND。
    """
    reason = body.reason.strip()
    if not reason:
        raise ApiError(422, "VALIDATION_ERROR",
                       "作废原因不能为空（仅空白字符视为空）", {"field": "reason"})
    with get_conn() as conn:
        record, replayed = void_trip(conn, trip_id, reason)
    if replayed:
        return JSONResponse(status_code=200,
                            content={**record, "replayed": True})
    return JSONResponse(status_code=201,
                        content={**record, "replayed": False})


@app.get(
    "/api/trips/{trip_id}/void",
    response_model=VoidRecordOut,
    tags=["trips", "void"],
)
def get_trip_void_record(trip_id: int):
    """按行程号查询作废记录。

    未知行程（无确认快照）404 TRIP_NOT_FOUND；行程存在但未作废
    404 VOID_RECORD_NOT_FOUND；已作废返回行程号、首次原因与作废时间。
    """
    with get_conn() as conn:
        return get_void_record(conn, trip_id)


@app.post(
    "/api/trips/{trip_id}/review",
    response_model=TripReviewOut,
    tags=["trips"],
)
def review_trip(trip_id: int, body: Optional[ReviewRequest] = None):
    """潮位更正后复核已确认行程，并在不安全时求改期行程。

    只读操作：保持快照航段编号顺序、船舶参数、原起始分钟与规划窗口，仅允许
    在节点重新等待；改期择优为「变动航段数最少 → 抵达最早 → 出发分钟序列
    字典序最小」。窗口内无解时返回原行程风险与 no_solution，不修改快照。
    """
    target_revision = body.target_revision if body is not None else None
    with get_conn() as conn:
        result = review_and_reschedule(conn, trip_id, target_revision)
    return result


def _trip_out(d: dict) -> TripOut:
    return TripOut(
        trip_id=d["trip_id"],
        revision=d["revision"],
        origin=d["origin"],
        destination=d["destination"],
        start_minute=d["start_minute"],
        window_minutes=d["window_minutes"],
        draft=d["draft"],
        height=d["height"],
        margin=d["margin"],
        arrival_minute=d["arrival_minute"],
        itinerary=json.loads(d["itinerary"]),
        confirmed_at=d["confirmed_at"],
        # 历史快照在作废后仍原样可查；voided_at 仅标识其占用已失效
        voided_at=d.get("voided_at"),
    )
