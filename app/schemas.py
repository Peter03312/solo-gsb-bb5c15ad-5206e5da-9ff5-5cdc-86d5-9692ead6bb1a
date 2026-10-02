"""Pydantic 请求 / 响应模型。"""
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from . import config


# ---- 节点 / 航段 ----------------------------------------------------------------

class NodeIn(BaseModel):
    node_id: str = Field(min_length=1, description="节点编号，如 A")
    name: Optional[str] = None


class NodeOut(BaseModel):
    node_id: str
    name: Optional[str] = None


class SegmentIn(BaseModel):
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    travel_time: int = Field(gt=0, description="航时（正整数分钟）")
    depth: float = Field(ge=0, description="静水深度（米）")
    clearance: Optional[float] = Field(
        default=None, ge=0, description="桥底净空（米），不填表示无桥"
    )


class SegmentOut(BaseModel):
    seg_id: int
    origin: str
    destination: str
    travel_time: int
    depth: float
    clearance: Optional[float] = None


# ---- 航段分时航时管制 -------------------------------------------------------------

class TravelControlIn(BaseModel):
    # 合法性（start>=0、end>start、travel_time>0）由服务层统一校验并返回
    # INVALID_CONTROL_INTERVAL，保证非法区间只有一个拒绝口径
    start_minute: int = Field(description="管制半开区间起点（含），绝对分钟，>=0")
    end_minute: int = Field(description="管制半开区间止点（不含），必须 > start_minute")
    travel_time: int = Field(description="该区间内进入航段的通行分钟（正整数）")


class TravelControlReplaceRequest(BaseModel):
    controls: list[TravelControlIn] = Field(
        description="整组替换的半开分钟区间清单（空组恢复航段基础航时）；"
                    "组内区间不得交叠"
    )


class TravelControlOut(TravelControlIn):
    pass


class TravelControlListOut(BaseModel):
    seg_id: int = Field(description="航段编号")
    base_travel_time: int = Field(description="航段基础航时（未命中管制区间时使用）")
    controls: list[TravelControlOut] = Field(
        description="当前生效的分时管制清单（按起点升序）；空列表表示全程基础航时"
    )


# ---- 狭窄水道组 -------------------------------------------------------------------

class ChannelGroupCreateRequest(BaseModel):
    segments: list[int] = Field(
        min_length=2,
        description="航段编号集合（至少 2 条）；组建成后不可修改，航段不得重复归组",
    )
    note: Optional[str] = None


class ChannelGroupMemberOut(BaseModel):
    position: int = Field(description="成员在提交集合中的次序（0 起）")
    seg_id: int = Field(description="成员航段编号")
    origin: str
    destination: str
    travel_time: int = Field(description="航段基础航时（正整数分钟）")
    depth: float
    clearance: Optional[float] = None


class ChannelGroupOut(BaseModel):
    group_id: int = Field(description="狭窄水道组号（自增、不可变）")
    note: Optional[str] = None
    created_at: str
    members: list[ChannelGroupMemberOut] = Field(
        description="组成员航段（按提交次序）；组建成后不可修改"
    )


# ---- 潮位 ----------------------------------------------------------------------

class TideCorrection(BaseModel):
    corrections: dict[int, float] = Field(
        description="要覆盖的 {分钟: 名义潮位(米)}；未列出分钟继承基础修订名义潮位与误差"
    )
    base_revision: Optional[int] = Field(
        default=None, description="所基于的修订号；不填则取最新修订"
    )
    note: Optional[str] = None


class TideErrorRevision(BaseModel):
    errors: dict[int, float] = Field(
        description="要覆盖的 {分钟: 预测误差(米，非负)}；未列出分钟继承基础修订误差，"
                    "名义潮位不变"
    )
    base_revision: Optional[int] = Field(
        default=None, description="所基于的修订号；不填则取最新修订"
    )
    note: Optional[str] = None


class RevisionOut(BaseModel):
    revision: int
    note: Optional[str] = None
    created_at: str


# ---- 规划 / 确认 ----------------------------------------------------------------

class PlanRequest(BaseModel):
    origin: str
    destination: str
    start_minute: int = Field(ge=0, description="规划窗口起点（绝对分钟，>=0）")
    window_minutes: int = Field(gt=0, le=config.MAX_WINDOW, description="窗口长度，<=1440")
    draft: float = Field(ge=0, description="船舶吃水（米）")
    height: float = Field(ge=0, description="水面以上高度（米）")
    margin: float = Field(ge=0, description="安全余量（米）")
    revision: Optional[int] = Field(default=None, description="潮位修订号；不填用最新")


class LegOut(BaseModel):
    seg_id: int
    origin: str
    destination: str
    start_minute: int
    end_minute: int
    travel_time: int


class PlanResponse(BaseModel):
    revision: int
    origin: str
    destination: str
    start_minute: int
    window_minutes: int
    draft: float
    height: float
    margin: float
    arrival_minute: int
    total_wait_minutes: int
    itinerary: list[LegOut]


class ConfirmRequest(PlanRequest):
    itinerary: list[LegOut] = Field(description="按规划结果原样回填的行程（含 seg_id/起止分钟）")


# ---- 批次预排 / 确认（同一潮位修订下 2～5 艘船成批处理） ---------------------------

class BatchShipIn(BaseModel):
    """批次中的一艘船：字段语义与 PlanRequest 的单船字段一致。"""
    origin: str
    destination: str
    start_minute: int = Field(ge=0, description="规划窗口起点（绝对分钟，>=0）")
    window_minutes: int = Field(gt=0, le=config.MAX_WINDOW, description="窗口长度，<=1440")
    draft: float = Field(ge=0, description="船舶吃水（米）")
    height: float = Field(ge=0, description="水面以上高度（米）")
    margin: float = Field(ge=0, description="安全余量（米）")


class BatchPlanRequest(BaseModel):
    ships: list[BatchShipIn] = Field(
        min_length=2, max_length=5,
        description="按优先顺序排列的 2～5 艘船；排序靠后者须避开排序靠前者的拟占用",
    )
    revision: Optional[int] = Field(
        default=None, description="整批共用的潮位修订号；不填用最新修订"
    )


class BatchPlanShipOut(PlanResponse):
    index: int = Field(description="批次内序号（0 起，与请求中的优先顺序一致）")


class BatchPlanResponse(BaseModel):
    revision: int = Field(description="本次预排实际所据修订号（省略时解析为最新修订）")
    ships: list[BatchPlanShipOut]


class BatchShipConfirmIn(BatchShipIn):
    itinerary: list[LegOut] = Field(description="该船预排结果原样回填（含 seg_id/起止分钟）")


class BatchConfirmRequest(BaseModel):
    ships: list[BatchShipConfirmIn] = Field(
        min_length=2, max_length=5,
        description="按预排时同一优先顺序排列的 2～5 艘船及其预排行程",
    )
    revision: int = Field(description="预排所据潮位修订号（批次确认必填）")
    idempotency_key: str = Field(
        min_length=1, max_length=128,
        description="批次幂等键：同键同内容重试返回原结果；同键异内容冲突",
    )


class BatchTripOut(BaseModel):
    index: int = Field(description="批次内序号（0 起，与请求顺序一致）")
    trip_id: int
    revision: int
    arrival_minute: int
    itinerary: list[LegOut]


class BatchConfirmResponse(BaseModel):
    idempotency_key: str
    revision: int
    replayed: bool = Field(
        description="true=同键同内容重试，直接返回首次结果（未重新计算/保存）"
    )
    trips: list[BatchTripOut]


class TripOut(BaseModel):
    trip_id: int
    revision: int
    origin: str
    destination: str
    start_minute: int
    window_minutes: int
    draft: float
    height: float
    margin: float
    arrival_minute: int
    itinerary: list[LegOut]
    confirmed_at: str
    voided_at: Optional[str] = Field(
        default=None,
        description="作废时间；未作废为 null。作废后确认快照仍作为历史快照原样可查，"
                    "仅其占用从后续调度失效",
    )


# ---- 已确认行程作废（误确认撤销） ---------------------------------------------------

class TripVoidRequest(BaseModel):
    reason: str = Field(
        min_length=1,
        description="非空作废原因（剥离首尾空白后至少 1 个字符）；重复作废不覆盖首次原因",
    )


class VoidRecordOut(BaseModel):
    trip_id: int = Field(description="被作废的已确认行程号")
    reason: str = Field(description="首次作废时提交的非空原因（重复作废原样返回）")
    voided_at: str = Field(description="首次作废时间")


class TripVoidResponse(VoidRecordOut):
    replayed: bool = Field(
        description="true=重复作废，返回的是首次记录（HTTP 200）；首次作废为 false（HTTP 201）",
    )


# ---- 潮位更正后的复核 / 改期 -------------------------------------------------------

class ReviewRequest(BaseModel):
    target_revision: Optional[int] = Field(
        default=None, description="目标潮位修订号；不填取最新修订"
    )


class ViolationOut(BaseModel):
    leg_index: int = Field(description="首次违规航段在快照行程中的下标（0 起）")
    seg_id: int
    origin: str
    destination: str
    minute: int = Field(description="首次违规的占用分钟（绝对分钟）")
    reasons: list[str] = Field(
        description="违规原因：depth=水深不足，clearance=桥高净空不足，missing_tide=缺潮位"
    )
    tide_level: Optional[float] = None
    tide_error: Optional[float] = Field(
        default=None, description="该分钟目标修订的预测误差（米）"
    )
    depth: float = Field(description="航段静水深度（米）")
    clearance: Optional[float] = Field(default=None, description="桥底净空（米），无桥为 null")
    required_depth: float = Field(description="要求的最小可用水深：吃水+余量（米）")
    required_clearance: float = Field(description="要求的最小净空：水面以上高度+余量（米）")
    available_depth: Optional[float] = Field(
        default=None, description="该分钟按误差带保守极值的实际可用水深：静水深度+名义潮位−误差（米）"
    )
    available_clearance: Optional[float] = Field(
        default=None,
        description="该分钟按误差带保守极值的实际桥高净空：桥底净空−名义潮位−误差（米），无桥为 null",
    )


class ChangedLegOut(BaseModel):
    leg_index: int
    seg_id: int
    original_departure_minute: int
    new_departure_minute: int


class RescheduleOut(BaseModel):
    revision: int = Field(description="改期所据目标修订号")
    arrival_minute: int
    total_wait_minutes: int
    changed_leg_count: int = Field(description="相对快照出发分钟发生变化的航段个数")
    changed_legs: list[ChangedLegOut]
    itinerary: list[LegOut]


class TripReviewOut(BaseModel):
    trip_id: int
    snapshot_revision: int = Field(description="确认快照所据修订号")
    target_revision: int = Field(description="本次复核所据目标修订号")
    origin: str
    destination: str
    start_minute: int
    window_minutes: int
    draft: float
    height: float
    margin: float
    original_arrival_minute: int
    safe: bool = Field(description="原行程在目标修订下按原占用分钟是否安全")
    first_violation: Optional[ViolationOut] = Field(
        default=None, description="不安全时的首次违规航段/分钟/原因"
    )
    reschedule: Optional[RescheduleOut] = Field(
        default=None, description="不安全且窗口内有改期解时的改期行程"
    )
    no_solution: bool = Field(
        default=False, description="不安全且窗口内无改期解时为 true（快照不被修改）"
    )
    original_itinerary: list[LegOut]
