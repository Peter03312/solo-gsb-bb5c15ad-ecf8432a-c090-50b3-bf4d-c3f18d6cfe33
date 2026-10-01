"""择时通行承诺的仓库层测试（不需要 PostgreSQL）。

用内存伪游标模拟 repository 在承诺签发 / 查询 / 取消 / 区域写入前校验中
实际执行的 SQL 与几何交互，覆盖：
  * 签发成功（返回 id/revision/区间）、空区间拒签不落库、窗口已开始拒签；
  * check_commitments_safe 的「变更前有未来安全时刻、变更后没有 → 冲突」，
    变更前已无未来安全时刻的不约束，删除区域永不冲突；
  * 当前时刻之后区间裁剪（now 位于窗口中间时只剩后半段）；
  * 取消幂等、未知承诺返回 None。

运行：python3 tests/test_commitments_repo.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from shapely.geometry import Polygon
from shapely.wkt import dumps as wkt_dumps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import geometry, repository  # noqa: E402
from app.repository import CommitmentConflict, CommitmentNoSafeDeparture  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def epoch(dt: datetime) -> float:
    return dt.timestamp()


class FixedClock:
    def __init__(self, now: datetime):
        self.now = now


# 直接 monkeypatch repository.utcnow，使「当前时刻」确定
repository.utcnow = lambda: NOW  # noqa: E731


def region_row(rid, name, polygon, vf, vt, version=1):
    return {
        "id": rid,
        "name": name,
        "polygon": [[x, y] for x, y in polygon.exterior.coords],
        "wkt": wkt_dumps(polygon, rounding_precision=-1),
        "valid_from": vf,
        "valid_to": vt,
        "version": version,
        "created_at": NOW,
        "updated_at": NOW,
    }


class FakeCursor:
    """模拟承诺相关写路径用到的 psycopg dict 游标。"""

    def __init__(self, db):
        self.db = db
        self.row_factory = None
        self._rows = None

    # 仅实现 repository 承诺/凭证路径实际出现的语句模式
    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params
        if s.startswith("SELECT revision FROM region_meta"):
            self._rows = [{"revision": self.db["revision"]}]
        elif s.startswith("SELECT id, name, polygon"):  # _load_all_regions_locked
            self._rows = list(self.db["regions"].values())
        elif s.startswith("SELECT id, trajectory, window_start, window_end"):
            if "FOR UPDATE" in s:
                cid = p[0]
                self._rows = [self.db["commitments"].get(cid)] if cid in self.db["commitments"] else []
            else:
                self._rows = [
                    dict(row) for row in self.db["commitments"].values()
                    if row["status"] == "active" and row["window_end"] > p[0]
                ]
        elif s.startswith("SELECT id, trajectory"):  # passes
            self._rows = []
        elif s.startswith("INSERT INTO commitments"):
            cid = max(self.db["commitments"], default=0) + 1
            traj = json.loads(p["trajectory"])
            row = {
                "id": cid,
                "trajectory": traj,
                "window_start": p["window_start"],
                "window_end": p["window_end"],
                "status": "active",
                "issued_revision": p["revision"],
                "created_at": NOW,
                "cancelled_at": None,
            }
            self.db["commitments"][cid] = row
            self._rows = [dict(row)]
        elif s.startswith("UPDATE commitments SET status = 'cancelled'"):
            cid = p[0]
            row = dict(self.db["commitments"][cid])
            row["status"] = "cancelled"
            row["cancelled_at"] = NOW
            self.db["commitments"][cid] = row
            self._rows = [dict(row)]
        elif s.startswith("UPDATE region_meta"):
            self.db["revision"] += 1
            self._rows = [{"revision": self.db["revision"]}]
        else:
            raise AssertionError(f"未模拟的 SQL: {s}")

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows = self._rows
        self._rows = []
        return rows


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def rollback(self):
        pass

    def commit(self):
        pass


def make_db(regions=None, commitments=None):
    return {
        "revision": 1,
        "regions": regions or {},
        "commitments": commitments or {},
    }


WIDE = (NOW - timedelta(days=365), NOW + timedelta(days=365))
# 横移轨迹：(-50,50)→(150,50)，相对 0~60s；与 [0,100]x[0,100] 在 15~45s 接触
TRAJ = geometry.validate_relative_trajectory(
    [{"x": -50, "y": 50, "t": 0}, {"x": 150, "y": 50, "t": 60}],
    lambda p: (p["x"], p["y"]), lambda p: p["t"],
)
POLY_A = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
vf, vt = WIDE
window_start = NOW + timedelta(hours=1)
window_end = NOW + timedelta(hours=3)

# ---------------------------------------------------------------- 签发成功
# 区域仅在窗口中段生效，存在两段安全区间
db_ = make_db(regions={1: region_row(
    1, "A区", POLY_A,
    NOW + timedelta(hours=1, minutes=30),
    NOW + timedelta(hours=2, minutes=30),
)})
cur = FakeCursor(db_)
# 几何接触 15~45s；禁止出发区间 [vf-45s, vt-15s) 落在窗口内，窗口被切成两段
try:
    row, issued_intervals = repository.issue_commitment(
        cur, TRAJ, window_start, window_end)
except CommitmentNoSafeDeparture:
    row = None
    check("窗口存在安全时刻：签发成功", False, "误拒签")
check("窗口存在安全时刻：签发成功", row is not None and row["id"] == 1)
check("签发返回 revision=1 且已落库为 active",
      row["issued_revision"] == 1 and db_["commitments"][1]["status"] == "active",
      str(row))
if row:
    intervals = repository.commitment_available_intervals(
        row, repository.all_regions_geom(FakeCursor(db_)), NOW
    )
    check("签发区间为两段左闭右开", len(intervals) == 2
          and all(iv["start_inclusive"] and not iv["end_inclusive"] for iv in intervals),
          str(intervals))
    # 端点核对：禁止区间 [vf-45s, vt-15s)
    forbid_lo = epoch(NOW + timedelta(hours=1, minutes=30) - timedelta(seconds=45))
    forbid_hi = epoch(NOW + timedelta(hours=2, minutes=30) - timedelta(seconds=15))
    check("第一段 = [window_start, vf-45s)，第二段 = [vt-15s, window_end)",
          abs(intervals[0]["end"] - forbid_lo) < 1e-6
          and abs(intervals[1]["start"] - forbid_hi) < 1e-6,
          str(intervals))

# 长期生效区域贯穿窗口：安全区间为空，签发应直接拒签
db_long = make_db(regions={1: region_row(1, "A区", POLY_A, vf, vt)})
try:
    repository.issue_commitment(FakeCursor(db_long), TRAJ, window_start, window_end)
    check("长期生效区域贯穿窗口：拒签", False)
except CommitmentNoSafeDeparture:
    check("长期生效区域贯穿窗口：拒签", True)
check("拒签不落库", db_long["commitments"] == {})

# ---------------------------------------------------------------- 窗口已开始
cur = FakeCursor(make_db(regions={}))
try:
    repository.issue_commitment(cur, TRAJ, NOW - timedelta(seconds=1), NOW + timedelta(hours=1))
    check("窗口起点不晚于签发时刻：拒签 invalid_trajectory", False)
except geometry.TrajectoryError:
    check("窗口起点不晚于签发时刻：拒签 invalid_trajectory", True)

# ---------------------------------------------------------------- 变更冲突
# 承诺窗口 [NOW+1h, NOW+3h)；变更前无区域（全窗口安全），变更后加入长期区域
# （全窗口禁止）→ 冲突
db_ = make_db(regions={}, commitments={})
cur = FakeCursor(db_)
row, issued_intervals = repository.issue_commitment(
    cur, TRAJ, window_start, window_end)
cid = row["id"]
before_empty = []
after_block = [
    {"id": 9, "name": "新区域", "valid_from": epoch(vf), "valid_to": epoch(vt),
     "polygon": POLY_A}
]
try:
    repository.check_commitments_safe(FakeCursor(db_), before_empty, after_block, NOW)
    check("变更前安全、变更后无安全时刻 → commitment_conflict", False)
except CommitmentConflict as exc:
    check("变更前安全、变更后无安全时刻 → commitment_conflict",
          exc.commitment_ids == [cid], str(exc.commitment_ids))

# 变更后仍保留部分安全时刻 → 不冲突（新区域只在窗口第一小时生效，
# 其后 [vf-45s,...) 禁止区间只覆盖窗口开头）
vf2 = window_start - timedelta(seconds=45)
vt2 = window_start + timedelta(minutes=30)
after_partial = [
    {"id": 9, "name": "新区域", "valid_from": epoch(vf2), "valid_to": epoch(vt2),
     "polygon": POLY_A}
]
repository.check_commitments_safe(FakeCursor(db_), before_empty, after_partial, NOW)
check("变更后仍有未来安全时刻 → 放行", True)

# ---------------------------------------------------------------- 已无未来安全时刻不约束
# 当前时刻推进到窗口末尾之后：窗口结束，承诺不再约束（SQL 层 load 已过滤；
# 直接验证窗口结束时 available 区间为空）
later = window_end + timedelta(seconds=1)
iv = repository.commitment_available_intervals(
    db_["commitments"][cid], after_block, later
)
check("窗口结束：可用区间为空", iv == [])

# 当前时刻位于窗口中间：now 之前的安全时刻不再可用，区间只从 now 起算
mid = window_start + timedelta(minutes=30)
iv = repository.commitment_available_intervals(
    db_["commitments"][cid], [], mid
)
check("窗口已开始：区间下限裁剪到当前时刻（now 不可出发）",
      len(iv) == 1 and abs(iv[0]["start"] - epoch(mid)) < 1e-6
      and iv[0]["start_inclusive"] and not iv[0]["end_inclusive"], str(iv))

# 变更前已无未来安全时刻（新区域只在 NOW 之前的未来窗口里……构造：
# 区域使 NOW 之后整段窗口都禁止，但变更前承诺本就无未来安全时刻）
db2 = make_db(regions={
    7: region_row(7, "旧区域", POLY_A,
                  window_start - timedelta(seconds=45),
                  window_end + timedelta(hours=1)),
}, commitments={1: dict(db_["commitments"][cid])})
iv_before = repository.commitment_available_intervals(
    db2["commitments"][1], repository.all_regions_geom(FakeCursor(db2)), NOW
)
check("前置条件：变更前已无未来安全时刻", iv_before == [], str(iv_before))
# 再加一个区域（变更后同样为空）不应报冲突
after2 = repository.all_regions_geom(FakeCursor(db2)) + [
    {"id": 8, "name": "另一区域", "valid_from": epoch(vf), "valid_to": epoch(vt),
     "polygon": POLY_A}
]
repository.check_commitments_safe(FakeCursor(db2), repository.all_regions_geom(FakeCursor(db2)),
                                  after2, NOW)
check("变更前已无未来安全时刻 → 不再约束（无冲突）", True)

# ---------------------------------------------------------------- 删除不冲突
db3 = make_db(regions={1: region_row(1, "A区", POLY_A,
                                     window_start - timedelta(seconds=45),
                                     window_end + timedelta(hours=1))})
# 该承诺轨迹恰好擦区域上方 y=200：区域存在也安全
SAFE_TRAJ = geometry.validate_relative_trajectory(
    [{"x": -50, "y": 200, "t": 0}, {"x": 150, "y": 200, "t": 60}],
    lambda p: (p["x"], p["y"]), lambda p: p["t"],
)
db3["commitments"] = {1: {
    "id": 1, "trajectory": [{"x": x, "y": y, "t": t} for t, x, y in SAFE_TRAJ],
    "window_start": window_start, "window_end": window_end,
    "status": "active", "issued_revision": 1,
    "created_at": NOW, "cancelled_at": None,
}}
before3 = repository.all_regions_geom(FakeCursor(db3))
repository.check_commitments_safe(FakeCursor(db3), before3, [], NOW)
check("删除区域（集合变小）永不冲突", True)

# ---------------------------------------------------------------- 取消幂等
cancelled = repository.cancel_commitment(FakeCursor(db3), 1)
check("取消返回 cancelled 且带 cancelled_at",
      cancelled["status"] == "cancelled" and cancelled["cancelled_at"] == NOW)
again = repository.cancel_commitment(FakeCursor(db3), 1)
check("重复取消幂等（状态/时间不变）",
      again["status"] == "cancelled" and again["cancelled_at"] == NOW)
check("取消未知承诺返回 None", repository.cancel_commitment(FakeCursor(db3), 999) is None)
check("查询未知承诺返回 None", repository.get_commitment(FakeCursor(db3), 999) is None)

# 已取消的承诺不参与约束（load SQL 过滤 status=active；验证活跃集合构造）
active = repository.load_constraining_commitments(FakeCursor(db3), NOW)
check("已取消承诺不在约束集合中", active == [])

# ---------------------------------------------------------------- 单点接触端点
# 轨迹仅在相对 30s 擦过多边形顶点 (0,100)：接触区间 [30,30]
TOUCH_TRAJ = geometry.validate_relative_trajectory(
    [{"x": -50, "y": 100, "t": 0}, {"x": 50, "y": 100, "t": 60}],
    lambda p: (p["x"], p["y"]), lambda p: p["t"],
)
# 区域生效 [ws+30s, ws+30s+1h)：禁止出发区间 = [ws+30s-30s, ws+1h+30s-30s)
#   = [ws, ws+1h)，左端闭 → 恰在 window_start 出发禁止；安全区间从 ws+1h 起
ws = window_start
touch_vf = ws + timedelta(seconds=30)
touch_vt = ws + timedelta(hours=1, seconds=30)
safe_iv = geometry.safe_departure_intervals(
    TOUCH_TRAJ,
    [{"id": 1, "name": "T", "valid_from": epoch(touch_vf), "valid_to": epoch(touch_vt),
      "polygon": POLY_A}],
    epoch(ws), epoch(window_end),
)
check("单点接触：禁止区间左端闭（window_start 禁止），安全从 ws+1h 起",
      len(safe_iv) == 1
      and abs(safe_iv[0]["start"] - epoch(ws + timedelta(hours=1))) < 1e-6,
      str(safe_iv))

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部择时通行承诺仓库层用例通过")
