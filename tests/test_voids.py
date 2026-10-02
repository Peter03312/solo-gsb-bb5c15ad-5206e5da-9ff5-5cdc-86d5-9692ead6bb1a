"""行程作废（误确认纠正）测试。

规则：
* 凭行程号 + 非空作废原因作废，返回行程号、原因、作废时间；未知行程
  404 TRIP_NOT_FOUND；空白原因 422 VALIDATION_ERROR；
* 作废记录可按行程号查询：未作废 404 VOID_RECORD_NOT_FOUND；重复作废
  返回首次记录（200）且不覆盖原因；
* 已确认行程与原批次确认结果仍作为历史快照可查（GET /api/trips、批次幂等
  重放）；作废只使该行程按固化航时的航段与狭窄水道组占用失效；
* 单船/批次预排、确认复算与改期建议只避开仍有效的快照；
* 作废与确认在 BEGIN IMMEDIATE 事务中串行，不产生相互重叠的有效确认行程。
"""
import json
import threading

import pytest

from app.database import connect
from app.errors import ApiError
from app.main import confirm_trip, void_confirmed_trip
from app.schemas import ConfirmRequest, TripVoidRequest

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _raw_insert_trip(rev, itinerary, origin="A", destination="B",
                     start=0, window=20, draft=1.0, height=0.0, margin=0.0):
    conn = connect()
    arrival = start if not itinerary else itinerary[-1]["end_minute"]
    cur = conn.execute(
        "INSERT INTO confirmed_trips"
        " (revision, origin, destination, start_minute, window_minutes,"
        "  draft, height, margin, arrival_minute, itinerary)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rev, origin, destination, start, window, draft, height, margin,
         arrival, json.dumps(itinerary)),
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def _confirm_raw(client, rev, start=0, window=20, draft=1.0,
                 origin="A", destination="B"):
    plan = client.post("/api/trips/plan", json={
        "origin": origin, "destination": destination,
        "start_minute": start, "window_minutes": window,
        "draft": draft, "height": 0.0, "margin": 0.0, "revision": rev,
    }).json()
    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 201, r.text
    return plan, r.json()


def _valid_occupancy_pairs():
    """返回全部**未作废**快照占用的 {(seg_id, minute), ...} 多重集合校验信息。"""
    conn = connect()
    rows = conn.execute(
        "SELECT t.trip_id, t.itinerary FROM confirmed_trips t"
        " LEFT JOIN trip_voids v ON v.trip_id = t.trip_id"
        " WHERE v.trip_id IS NULL"
    ).fetchall()
    per_trip = {}
    for r in rows:
        mins = set()
        for leg in json.loads(r["itinerary"]):
            d, t = leg["start_minute"], leg["travel_time"]
            for m in range(d, d + t):
                mins.add((leg["seg_id"], m))
        per_trip[r["trip_id"]] = mins
    conn.close()
    return per_trip


def _assert_valid_trips_non_overlapping():
    """任意两条仍有效（未作废）的已确认行程不得航段同分钟重叠。"""
    per_trip = _valid_occupancy_pairs()
    tids = list(per_trip)
    for i in range(len(tids)):
        for j in range(i + 1, len(tids)):
            assert not (per_trip[tids[i]] & per_trip[tids[j]]), (
                f"有效确认行程 {tids[i]} 与 {tids[j]} 占用重叠"
            )


# ---------------------------------------------------------------------------
# 1. 作废 / 查询：返回字段、未知行程、空白原因、重复作废不覆盖
# ---------------------------------------------------------------------------

def test_void_trip_returns_trip_id_reason_time(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _, trip = _confirm_raw(client, rev)
    tid = trip["trip_id"]

    r = client.post(f"/api/trips/{tid}/void", json={"reason": "调度员误确认"})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert set(rec) == {"trip_id", "reason", "voided_at"}
    assert rec["trip_id"] == tid
    assert rec["reason"] == "调度员误确认"
    assert rec["voided_at"]

    # GET 作废记录与首次一致
    g = client.get(f"/api/trips/{tid}/void")
    assert g.status_code == 200
    assert g.json() == rec


def test_void_unknown_trip_is_trip_not_found(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    seed_initial_revision(_level(0, 19, 3.0))
    r = client.post("/api/trips/404/void", json={"reason": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRIP_NOT_FOUND"

    g = client.get("/api/trips/404/void")
    assert g.status_code == 404
    assert g.json()["error"]["code"] == "TRIP_NOT_FOUND"


def test_get_void_record_not_voided(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _, trip = _confirm_raw(client, rev)
    g = client.get(f"/api/trips/{trip['trip_id']}/void")
    assert g.status_code == 404
    assert g.json()["error"]["code"] == "VOID_RECORD_NOT_FOUND"


@pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
def test_void_requires_nonblank_reason(client, reason):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _, trip = _confirm_raw(client, rev)
    r = client.post(f"/api/trips/{trip['trip_id']}/void", json={"reason": reason})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 未留下作废记录
    assert client.get(f"/api/trips/{trip['trip_id']}/void").status_code == 404


def test_repeat_void_returns_first_record_and_keeps_reason(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _, trip = _confirm_raw(client, rev)
    tid = trip["trip_id"]

    first = client.post(f"/api/trips/{tid}/void",
                        json={"reason": "首次作废原因"})
    assert first.status_code == 201
    rec = first.json()

    second = client.post(f"/api/trips/{tid}/void",
                         json={"reason": "第二次尝试覆盖原因"})
    assert second.status_code == 200, second.text
    assert second.json() == rec
    assert client.get(f"/api/trips/{tid}/void").json() == rec

    # 库中仍只有一行作废记录
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM trip_voids WHERE trip_id = ?",
                     (tid,)).fetchone()[0]
    conn.close()
    assert n == 1


# ---------------------------------------------------------------------------
# 2. 快照仍可查，列表/详情带作废标记；确认/批次重放仍为历史结果
# ---------------------------------------------------------------------------

def test_voided_trip_snapshot_still_queryable(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _, trip = _confirm_raw(client, rev)
    tid = trip["trip_id"]

    client.post(f"/api/trips/{tid}/void", json={"reason": "误确认"})

    rows = client.get("/api/trips").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["trip_id"] == tid
    assert row["voided"] is True
    assert row["void_reason"] == "误确认"
    assert row["voided_at"]
    # 快照行程原样保留
    assert row["itinerary"] == trip["itinerary"]
    assert row["arrival_minute"] == trip["arrival_minute"]
    assert row["confirmed_at"] == trip["confirmed_at"]

    detail = client.get(f"/api/trips/{tid}").json()
    assert detail["voided"] is True
    assert detail["void_reason"] == "误确认"

    # 未作废行程的新增字段为兼容默认值
    _, other = _confirm_raw(client, rev)
    other_row = next(
        t for t in client.get("/api/trips").json() if t["trip_id"] == other["trip_id"]
    )
    assert other_row["voided"] is False
    assert other_row["void_reason"] is None
    assert other_row["voided_at"] is None


def test_batch_replay_after_member_voided_returns_first_result(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    def ship():
        return {"origin": "A", "destination": "B", "start_minute": 0,
                "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0}

    plan = client.post("/api/trips/batch/plan",
                       json={"revision": rev, "ships": [ship(), ship()]}).json()
    body = {
        "revision": rev,
        "idempotency_key": "batch-void-0001",
        "ships": [
            {**ship(), "itinerary": plan["ships"][i]["itinerary"]}
            for i in range(2)
        ],
    }
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 201, r.text
    ids = [t["trip_id"] for t in r.json()["trips"]]

    # 作废批次中的第一条行程
    client.post(f"/api/trips/{ids[0]}/void", json={"reason": "误确认批次首船"})

    # 原幂等键重放：仍返回首次确认结果、行程号不变、replayed=true
    replay = client.post("/api/trips/batch/confirm", json=body)
    assert replay.status_code == 200
    data = replay.json()
    assert data["replayed"] is True
    assert [t["trip_id"] for t in data["trips"]] == ids

    # 快照仍在，且没有新增任何快照
    rows = client.get("/api/trips").json()
    assert [t["trip_id"] for t in rows] == ids
    assert {t["trip_id"]: t["voided"] for t in rows} == {ids[0]: True, ids[1]: False}


# ---------------------------------------------------------------------------
# 3. 作废释放航段占用与狭窄水道组展开占用
# ---------------------------------------------------------------------------

def test_void_releases_segment_occupancy_for_later_planning(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _, trip = _confirm_raw(client, rev)
    assert trip["itinerary"][0]["start_minute"] == 0

    # 第二艘同航路船须避开：最早 2 进入
    blocked = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    }).json()
    assert blocked["itinerary"][0]["start_minute"] == 2

    # 作废首船后，后续规划可重新使用 0..1 分钟
    client.post(f"/api/trips/{trip['trip_id']}/void", json={"reason": "误确认"})
    freed = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    }).json()
    assert freed["itinerary"][0]["start_minute"] == 0


def test_void_replaces_then_confirm_recalc_uses_freed_minutes(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _, trip = _confirm_raw(client, rev)

    # 作废后拿**旧的、避让占用的**规划结果去确认：复算已无该占用，
    # 最优行程变为 0 进入 -> PLAN_MISMATCH（确认复算只避开仍有效的快照）
    client.post(f"/api/trips/{trip['trip_id']}/void", json={"reason": "误确认"})
    plan2 = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    })
    # 作废后规划本身就是 0 进入，手工构造一份陈旧（2 进入）提交体
    stale = plan2.json()
    stale["itinerary"] = [{
        "seg_id": 1, "origin": "A", "destination": "B",
        "start_minute": 2, "end_minute": 4, "travel_time": 2,
    }]
    stale["arrival_minute"] = 4
    r = client.post("/api/trips/confirm", json=stale)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"


def test_void_releases_channel_group_expanded_occupancy(client):
    add_node(client, "A"); add_node(client, "B"); add_node(client, "C")
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "B", "C", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    r = client.post("/api/channel-groups",
                    json={"segments": [s1, s2], "note": "g"})
    assert r.status_code == 201, r.text

    # 船走 A->B 占 seg1 的 0..1，组展开封锁 seg2 的 0..1
    _, trip = _confirm_raw(client, rev, destination="B")
    blocked = client.post("/api/trips/plan", json={
        "origin": "B", "destination": "C", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    }).json()
    assert blocked["itinerary"][0]["start_minute"] == 2

    # 作废后组展开同步失效：seg2 第 0 分钟即可进入
    client.post(f"/api/trips/{trip['trip_id']}/void", json={"reason": "误确认"})
    freed = client.post("/api/trips/plan", json={
        "origin": "B", "destination": "C", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    }).json()
    assert freed["itinerary"][0]["start_minute"] == 0


def test_batch_plan_and_reschedule_only_avoid_valid_snapshots(client):
    # 改期场景：X 占 0..1，Y（同航路）只能 2..3；低潮更正后 X 原行程不安全，
    # 改期可行的 2 进入又被 Y 挡住 -> 无改期解；作废 Y 后改期解恢复。
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev = seed_initial_revision(_level(0, 19, 10.0))

    # X：吃水 6，深水时安全、低潮（0.0）时 4.0 < 6.0 不安全；窗口 [0,4)
    plan_x = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 4, "draft": 6.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    }).json()
    rx = client.post("/api/trips/confirm", json=plan_x)
    assert rx.status_code == 201, rx.text
    xid = rx.json()["trip_id"]

    # Y：同窗口，占 2..3
    plan_y = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 4, "draft": 6.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    }).json()
    assert plan_y["itinerary"][0]["start_minute"] == 2
    ry = client.post("/api/trips/confirm", json=plan_y)
    assert ry.status_code == 201, ry.text
    yid = ry.json()["trip_id"]

    # 批次预排也只避开有效快照：此时 0..3 被 X、Y 占满，同窗口无可行解
    no = client.post("/api/trips/batch/plan", json={
        "revision": rev,
        "ships": [
            {"origin": "A", "destination": "B", "start_minute": 0,
             "window_minutes": 4, "draft": 6.0, "height": 0.0, "margin": 0.0},
            {"origin": "A", "destination": "B", "start_minute": 0,
             "window_minutes": 4, "draft": 6.0, "height": 0.0, "margin": 0.0},
        ],
    })
    assert no.status_code == 404
    assert no.json()["error"]["code"] == "NO_FEASIBLE_ITINERARY"
    assert no.json()["error"]["details"]["ship_index"] == 0

    # 更正：0、1 分钟低潮；2、3 仍高潮
    cr = client.post("/api/tide/corrections",
                     json={"corrections": {"0": 0.0, "1": 0.0},
                           "note": "低潮"})
    rev2 = cr.json()["revision"]

    review = client.post(f"/api/trips/{xid}/review",
                         json={"target_revision": rev2}).json()
    assert review["safe"] is False
    assert review["no_solution"] is True  # 2 进入被有效快照 Y 挡住

    # 作废 Y：改期建议只避开仍有效的快照，2 进入恢复可行
    client.post(f"/api/trips/{yid}/void", json={"reason": "误确认"})
    review2 = client.post(f"/api/trips/{xid}/review",
                          json={"target_revision": rev2}).json()
    assert review2["safe"] is False
    assert review2["no_solution"] is False
    assert review2["reschedule"]["itinerary"][0]["start_minute"] == 2
    assert review2["reschedule"]["arrival_minute"] == 4

    # 批次预排同样立即复用 Y 释放的分钟：X 仍有效（0..1 仍封），船 0 得 2，
    # 船 1 在窗口内无分钟可用 -> 首个失败序号 1
    bp_no = client.post("/api/trips/batch/plan", json={
        "revision": rev2,
        "ships": [
            {"origin": "A", "destination": "B", "start_minute": 0,
             "window_minutes": 4, "draft": 1.0, "height": 0.0, "margin": 0.0},
            {"origin": "A", "destination": "B", "start_minute": 0,
             "window_minutes": 4, "draft": 1.0, "height": 0.0, "margin": 0.0},
        ],
    })
    assert bp_no.status_code == 404
    assert bp_no.json()["error"]["details"]["ship_index"] == 1

    # X 也作废后：两条船分别得 0、2（两船的占用均不再来自作废快照）
    client.post(f"/api/trips/{xid}/void", json={"reason": "误确认"})
    bp = client.post("/api/trips/batch/plan", json={
        "revision": rev2,
        "ships": [
            {"origin": "A", "destination": "B", "start_minute": 0,
             "window_minutes": 4, "draft": 1.0, "height": 0.0, "margin": 0.0},
            {"origin": "A", "destination": "B", "start_minute": 0,
             "window_minutes": 4, "draft": 1.0, "height": 0.0, "margin": 0.0},
        ],
    })
    assert bp.status_code == 200, bp.text
    assert [s["itinerary"][0]["start_minute"] for s in bp.json()["ships"]] == [0, 2]


def test_review_of_voided_trip_still_uses_history_snapshot(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev = seed_initial_revision(_level(0, 19, 10.0))
    plan = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 20, "draft": 6.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    }).json()
    trip = client.post("/api/trips/confirm", json=plan).json()
    tid = trip["trip_id"]
    rev2 = client.post("/api/tide/corrections",
                       json={"corrections": {"0": 0.0}}).json()["revision"]

    before = client.post(f"/api/trips/{tid}/review",
                         json={"target_revision": rev2}).json()
    client.post(f"/api/trips/{tid}/void", json={"reason": "误确认"})
    # 原行程潮位安全复核仍按历史快照执行，结果与作废前一致
    after = client.post(f"/api/trips/{tid}/review",
                        json={"target_revision": rev2}).json()
    assert after["safe"] == before["safe"] is False
    assert after["first_violation"] == before["first_violation"]
    assert after["reschedule"]["itinerary"] == before["reschedule"]["itinerary"]


# ---------------------------------------------------------------------------
# 4. 作废与确认并发：BEGIN IMMEDIATE 串行，无重叠有效确认
# ---------------------------------------------------------------------------

def test_serial_order_void_before_confirm_rejects_stale_plan(client):
    """串行次序一：先作废（占用释放），后确认旧避让结果 -> PLAN_MISMATCH。"""
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    blocking = _raw_insert_trip(rev, [{
        "seg_id": 1, "origin": "A", "destination": "B",
        "start_minute": 0, "end_minute": 2, "travel_time": 2,
    }], window=20)
    shifted = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    }).json()
    assert shifted["itinerary"][0]["start_minute"] == 2

    vr = client.post(f"/api/trips/{blocking}/void", json={"reason": "误确认"})
    assert vr.status_code == 201
    cr = client.post("/api/trips/confirm", json=shifted)
    assert cr.status_code == 409
    assert cr.json()["error"]["code"] == "PLAN_MISMATCH"
    _assert_valid_trips_non_overlapping()


def test_serial_order_confirm_before_void_succeeds(client):
    """串行次序二：先确认（占用仍在，复算与提交一致），后作废。"""
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    blocking = _raw_insert_trip(rev, [{
        "seg_id": 1, "origin": "A", "destination": "B",
        "start_minute": 0, "end_minute": 2, "travel_time": 2,
    }], window=20)
    shifted = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    }).json()

    cr = client.post("/api/trips/confirm", json=shifted)
    assert cr.status_code == 201, cr.text
    vr = client.post(f"/api/trips/{blocking}/void", json={"reason": "误确认"})
    assert vr.status_code == 201
    _assert_valid_trips_non_overlapping()


def test_concurrent_void_and_confirm_are_serialized(client):
    """作废与确认同时发起：写事务串行，两种提交次序均合法，且最终不存在
    相互重叠的有效确认行程（也不泄漏数据库锁错误）。"""
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    blocking = _raw_insert_trip(rev, [{
        "seg_id": 1, "origin": "A", "destination": "B",
        "start_minute": 0, "end_minute": 2, "travel_time": 2,
    }], window=20)
    shifted = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    }).json()

    # 直接调用路由函数（各自打开独立连接），Barrier 让两个写事务同时开始
    confirm_req = ConfirmRequest(**shifted)
    outcomes = {}
    barrier = threading.Barrier(2)

    def do_confirm():
        barrier.wait()
        try:
            confirm_trip(confirm_req)
            outcomes["confirm"] = (201, None)
        except ApiError as exc:
            outcomes["confirm"] = (exc.status_code, exc.code)

    def do_void():
        barrier.wait()
        try:
            resp = void_confirmed_trip(
                blocking, TripVoidRequest(reason="并发作废")
            )
            outcomes["void"] = resp.status_code
        except ApiError as exc:  # 串行化失败会在这里暴露
            outcomes["void"] = exc.status_code

    t1 = threading.Thread(target=do_confirm)
    t2 = threading.Thread(target=do_void)
    t1.start(); t2.start(); t1.join(10); t2.join(10)

    assert outcomes["void"] == 201
    # 确认先拿到锁 -> 201；作废先提交 -> 复算避让结果失配 -> 409 PLAN_MISMATCH
    assert outcomes["confirm"] in {(201, None), (409, "PLAN_MISMATCH")}, outcomes
    _assert_valid_trips_non_overlapping()

    # 作废记录存在且原因未被覆盖
    rec = client.get(f"/api/trips/{blocking}/void").json()
    assert rec["reason"] == "并发作废"
