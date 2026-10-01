"""择时通行承诺 API 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_commitments_api.py
脚本会清空 regions/region_meta/passes/commitments 后跑全套用例（含并发），
结束时再次清空，避免影响其它测试脚本。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def future(seconds: float, base: datetime | None = None) -> str:
    b = base or datetime.now(timezone.utc)
    return iso_z(b + timedelta(seconds=seconds))


# 横移轨迹 (-50,50)->(150,50)，0~60s；与 [0,100]x[0,100] 在 15~45s 接触
REL_TRAJ = [
    {"x": -50, "y": 50, "t": 0},
    {"x": 150, "y": 50, "t": 60},
]
# 从区域上方安全经过的横移轨迹（不接触任何 y∈[0,100] 的区域）
REL_TRAJ_HIGH = [
    {"x": -50, "y": 200, "t": 0},
    {"x": 150, "y": 200, "t": 60},
]
WIDE = ("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")
POLY = [[0, 0], [100, 0], [100, 100], [0, 100]]

db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE commitments RESTART IDENTITY")
            cur.execute("TRUNCATE passes RESTART IDENTITY")
            cur.execute("TRUNCATE regions RESTART IDENTITY")
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def commitment_count() -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM commitments")
            return cur.fetchone()[0]


def list_regions() -> list:
    return client.get("/api/regions").json()


reset_all()

# 固定候选窗口基准：[now+1h, now+3h)
BASE = datetime.now(timezone.utc)
WS = future(3600, BASE)
WE = future(3 * 3600, BASE)

# ---- 1. 无区域：签发承诺，整窗口一段安全区间（签发后立即取消以隔离后续场景） ----
r = client.post("/api/commitments", json={
    "points": REL_TRAJ, "window_start": WS, "window_end": WE,
})
check("无区域签发承诺 201", r.status_code == 201, r.text)
c1 = r.json()
c1_id = c1["id"]
check("返回 id/status=active/签发 revision=0",
      c1["status"] == "active" and c1["revision"] == 0, str(c1))
check("签发区间=整窗口一段左闭右开",
      len(c1["intervals"]) == 1
      and c1["intervals"][0]["start"] == WS
      and c1["intervals"][0]["end"] == WE
      and c1["intervals"][0]["start_inclusive"] is True
      and c1["intervals"][0]["end_inclusive"] is False, str(c1))
check("interval_revision 与签发 revision 相同",
      c1["interval_revision"] == c1["revision"], str(c1))
check("签发不推进区域 revision",
      client.get("/api/revision").json()["revision"] == 0)
check("回显相对秒数轨迹（t 为数字而非时刻）",
      c1["trajectory"][0] == {"x": -50.0, "y": 50.0, "t": 0.0}, str(c1["trajectory"]))
check("回显窗口（UTC Z）",
      c1["window_start"].endswith("Z") and c1["window_end"].endswith("Z"))
client.post(f"/api/commitments/{c1_id}/cancel")

# ---- 2. 登记贯穿窗口的长期区域 A（此时无活跃承诺） ----
r = client.post("/api/regions", json={
    "name": "A区", "polygon": POLY, "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("登记长期区域 A 201 revision=1",
      r.status_code == 201 and r.json()["revision"] == 1, r.text)
id_a = r.json()["region"]["id"]

# ---- 3. 窗口内无安全时刻：422 no_safe_departure，不落库 ----
r = client.post("/api/commitments", json={
    "points": REL_TRAJ, "window_start": WS, "window_end": WE,
})
check("窗口全无安全时刻 422 no_safe_departure",
      r.status_code == 422 and r.json()["error"]["code"] == "no_safe_departure", r.text)
check("拒签不落库（仅 1 张已取消的 c1）", commitment_count() == 1)

# ---- 4. 修订 A 为窗口中段生效：两段安全区间 → 201 ----
# 接触 15~45s（a=15, b=45）；禁止区间 [vf-45s, vt-15s) 完全落在窗口中段：
# vf=WS+90min+45s、vt=WS+115min+15s（禁止右端=WS+115min，距窗口右端 5min）
vf_mid = BASE + timedelta(seconds=3600 + 90 * 60 + 45)
vt_mid = BASE + timedelta(seconds=3600 + 115 * 60 + 15)
r = client.patch(f"/api/regions/{id_a}", json={
    "valid_from": iso_z(vf_mid), "valid_to": iso_z(vt_mid),
})
check("修订 A 为中段生效 200", r.status_code == 200, r.text)
r = client.post("/api/commitments", json={
    "points": REL_TRAJ, "window_start": WS, "window_end": WE,
})
check("中段禁止窗口两段安全：201", r.status_code == 201, r.text)
c2_id = r.json()["id"]
ivs = r.json()["intervals"]
check("两段左闭右开区间",
      len(ivs) == 2
      and all(i["start_inclusive"] and not i["end_inclusive"] for i in ivs), str(ivs))
check("第一段右端=vf-45s，第二段左端=vt-15s",
      ivs[0]["end"] == iso_z(vf_mid - timedelta(seconds=45))
      and ivs[1]["start"] == iso_z(vt_mid - timedelta(seconds=15)), str(ivs))

# ---- 5. 输入校验 ----
r = client.post("/api/commitments", json={
    "points": [{"x": 0, "y": 0, "t": 1}, {"x": 1, "y": 0, "t": 2}],
    "window_start": WS, "window_end": WE,
})
check("首点相对秒数不为 0 → 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)
r = client.post("/api/commitments", json={
    "points": [{"x": 0, "y": 0, "t": 0}, {"x": 1, "y": 0, "t": 0}],
    "window_start": WS, "window_end": WE,
})
check("相对时刻不严格递增 → 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)
r = client.post("/api/commitments", json={
    "points": [{"x": 0, "y": 0, "t": 0}],
    "window_start": WS, "window_end": WE,
})
check("轨迹点不足 → 422", r.status_code == 422, r.text)
r = client.post("/api/commitments", json={
    "points": REL_TRAJ, "window_start": WE, "window_end": WS,
})
check("窗口不递增 → 422 invalid_input",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_input", r.text)
r = client.post("/api/commitments", json={
    "points": REL_TRAJ,
    "window_start": "2026-10-02T13:00:00", "window_end": "2026-10-02T15:00:00",
})
check("窗口不带时区 → 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)
r = client.post("/api/commitments", json={
    "points": REL_TRAJ,
    "window_start": future(-10), "window_end": WE,
})
check("窗口起点早于签发时刻 → 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)
r = client.post("/api/commitments", json={
    "points": REL_TRAJ, "window_start": WS, "window_end": WE, "extra": 1,
})
check("多余字段 → 422", r.status_code == 422, r.text)
check("非法输入均不落库（仍为 c1+c2 两张）", commitment_count() == 2)

# ---- 6. 查询 ----
r = client.get("/api/commitments/9999")
check("未知承诺 404 not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)
r = client.get(f"/api/commitments/{c2_id}")
check("查询承诺 200 active",
      r.status_code == 200 and r.json()["status"] == "active", r.text)
check("查询带 interval_revision=当前 revision",
      r.json()["interval_revision"] == client.get("/api/revision").json()["revision"])
check("查询 intervals 与签发时一致（区域未变）",
      len(r.json()["intervals"]) == 2)
r = client.post("/api/commitments/9999/cancel")
check("取消未知承诺 404",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)

# 取消 c2，隔离后续「承诺约束」场景
client.post(f"/api/commitments/{c2_id}/cancel")

# ---- 7. 承诺约束区域变更 ----
# A 回到长期生效；签发一张从上方经过的活跃承诺（对当前 A 安全）
r = client.patch(f"/api/regions/{id_a}", json={
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("无活跃承诺时修订 A 回长期生效 200", r.status_code == 200, r.text)
r = client.post("/api/commitments", json={
    "points": REL_TRAJ_HIGH, "window_start": WS, "window_end": WE,
})
check("从上方经过的承诺 201", r.status_code == 201, r.text)
guard_id = r.json()["id"]

rev_before = client.get("/api/revision").json()["revision"]

# 7a. 登记压住承诺轨迹的新区域 → 409 commitment_conflict
r = client.post("/api/regions", json={
    "name": "B区-挡路", "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("登记使承诺丧失全部未来安全时刻 → 409 commitment_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "commitment_conflict", r.text)
check("409 明细给出冲突承诺 id",
      [d["commitment_id"] for d in r.json()["error"]["details"]] == [guard_id],
      str(r.json()["error"]["details"]))
check("登记 409 不落库且 revision 不变",
      client.get("/api/revision").json()["revision"] == rev_before
      and len(list_regions()) == 1)

# 7b. 修订 A 扩大覆盖承诺轨迹 → 409，version/revision 不变
r = client.patch(f"/api/regions/{id_a}", json={
    "polygon": [[0, 0], [200, 0], [200, 300], [0, 300]],
})
check("修订覆盖承诺轨迹 → 409 commitment_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "commitment_conflict", r.text)
check("修订 409 后 A 的 version/revision 不变",
      client.get(f"/api/regions/{id_a}").json()["version"] == 3
      and client.get("/api/revision").json()["revision"] == rev_before)

# 7c. 批量变更同样受限并整批回滚
r = client.post("/api/regions/batch", json={
    "expected_revision": rev_before,
    "operations": [
        {"action": "create", "name": "B区-批量挡路",
         "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
         "valid_from": WIDE[0], "valid_to": WIDE[1]},
    ],
    "trajectories": [[
        {"x": -50, "y": 500, "t": future(3600)},
        {"x": 150, "y": 500, "t": future(3700)},
    ]],
})
check("批量贯穿窗口区域 409 commitment_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "commitment_conflict", r.text)
check("批量 409 整批回滚",
      client.get("/api/revision").json()["revision"] == rev_before
      and all(x["name"] != "B区-批量挡路" for x in list_regions()))

# 7d. 无害变更（与 y=200 轨迹无关的远处区域）→ 201
r = client.post("/api/regions", json={
    "name": "远处区", "polygon": [[1000, 1000], [1100, 1000], [1100, 1100], [1000, 1100]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("无害登记 201（承诺仍有完整安全区间）", r.status_code == 201, r.text)
r = client.get(f"/api/commitments/{guard_id}")
check("无害变更后查询仍为整窗口一段",
      len(r.json()["intervals"]) == 1
      and r.json()["interval_revision"] == rev_before + 1, r.text)

# 7e. 删除区域（集合变小）不冲突
r = client.delete(f"/api/regions/{id_a}")
check("删除区域 200（承诺不会因集合变小而冲突）", r.status_code == 200, r.text)

# ---- 8. 变更后仍保留部分未来安全时刻：不冲突，查询只剩后段 ----
r = client.post("/api/commitments", json={
    "points": REL_TRAJ, "window_start": WS, "window_end": WE,
})
check("删除 A 后签发整窗口承诺 201", r.status_code == 201, r.text)
c3_id = r.json()["id"]
# 禁止区间只覆盖窗口前 30 分钟：令 vt-15s=WS+30min，即 vt=WS+30min+15s
r = client.post("/api/regions", json={
    "name": "只挡窗口开头", "polygon": POLY,
    "valid_from": iso_z(BASE + timedelta(seconds=3600 - 45)),
    "valid_to": iso_z(BASE + timedelta(seconds=3600 + 1800 + 15)),
})
check("变更后仍有未来安全时刻 → 201", r.status_code == 201, r.text)
r = client.get(f"/api/commitments/{c3_id}")
tail = r.json()["intervals"]
check("查询当前可用区间：窗口开头已禁，仅剩后段一段",
      len(tail) == 1
      and tail[0]["start"] == iso_z(BASE + timedelta(seconds=3600 + 1800))
      and tail[0]["end"] == WE, str(tail))

# ---- 9. 取消：幂等、不推进 revision，取消后原冲突变更放行 ----
rev_before = client.get("/api/revision").json()["revision"]
r = client.post(f"/api/commitments/{guard_id}/cancel")
check("取消承诺 200 cancelled 带 cancelled_at",
      r.status_code == 200 and r.json()["status"] == "cancelled"
      and r.json()["cancelled_at"] is not None, r.text)
cancel_body = r.json()
check("取消不推进区域 revision",
      client.get("/api/revision").json()["revision"] == rev_before)
r = client.post(f"/api/commitments/{guard_id}/cancel")
# intervals 随当前集合重算可能变化；幂等只要求状态与 cancelled_at 不变
check("重复取消幂等（status/cancelled_at 不变）",
      r.status_code == 200
      and r.json()["status"] == cancel_body["status"]
      and r.json()["cancelled_at"] == cancel_body["cancelled_at"], r.text)
client.post(f"/api/commitments/{c3_id}/cancel")
# 全部承诺已取消：登记贯穿窗口的区域放行
r = client.post("/api/regions", json={
    "name": "挡路-取消后", "polygon": POLY,
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("取消全部承诺后贯穿窗口区域可登记 201", r.status_code == 201, r.text)
r = client.get(f"/api/commitments/{guard_id}")
check("已取消承诺仍可查询，status=cancelled",
      r.status_code == 200 and r.json()["status"] == "cancelled", r.text)

# ---- 10. 窗口结束的承诺不再约束 ----
short_ws = datetime.now(timezone.utc) + timedelta(seconds=2)
short_we = datetime.now(timezone.utc) + timedelta(seconds=4)
r = client.post("/api/commitments", json={
    "points": REL_TRAJ_HIGH,
    "window_start": iso_z(short_ws), "window_end": iso_z(short_we),
})
check("签发短窗口承诺 201", r.status_code == 201, r.text)
short_id = r.json()["id"]
r = client.post("/api/regions", json={
    "name": "短窗口挡路", "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("窗口未结束时仍约束 409 commitment_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "commitment_conflict", r.text)
remaining = (short_we - datetime.now(timezone.utc)).total_seconds()
time.sleep(max(0.0, remaining) + 0.5)
r = client.get(f"/api/commitments/{short_id}")
check("窗口结束后查询 intervals 为空",
      r.status_code == 200 and r.json()["intervals"] == [], r.text)
r = client.post("/api/regions", json={
    "name": "短窗口挡路", "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("窗口结束后不再约束 201", r.status_code == 201, r.text)

# ---- 11. 并发：签发承诺与建区串行化，恰好一个成功（无漏检窗口） ----
with db.pool.connection() as conn:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE commitments RESTART IDENTITY")
        cur.execute("TRUNCATE regions RESTART IDENTITY")
        cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
    conn.commit()
barrier = threading.Barrier(2)
outcome: dict = {}
FWS = future(3600 + 7200)
FWE = future(3 * 3600 + 7200)


def issue_worker():
    barrier.wait()
    rr = client.post("/api/commitments", json={
        "points": REL_TRAJ, "window_start": FWS, "window_end": FWE,
    })
    outcome["issue"] = (rr.status_code, rr.json())


def create_worker():
    barrier.wait()
    rr = client.post("/api/regions", json={
        "name": "并发挡路区", "polygon": POLY,
        "valid_from": WIDE[0], "valid_to": WIDE[1],
    })
    outcome["create"] = (rr.status_code, rr.json())


t1 = threading.Thread(target=issue_worker)
t2 = threading.Thread(target=create_worker)
t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)
issue_ok = outcome["issue"][0] == 201
create_ok = outcome["create"][0] == 201
check("承诺签发与建区并发：恰好一个成功（无漏检）",
      issue_ok != create_ok, str({k: v[0] for k, v in outcome.items()}))
if issue_ok:
    check("签发先成功则建区 409 commitment_conflict",
          outcome["create"][0] == 409
          and outcome["create"][1]["error"]["code"] == "commitment_conflict",
          str(outcome["create"])[:300])
else:
    check("建区先成功则签发 422 no_safe_departure",
          outcome["issue"][0] == 422
          and outcome["issue"][1]["error"]["code"] == "no_safe_departure",
          str(outcome["issue"])[:300])

# ---- 清理 ----
reset_all()

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部择时通行承诺 API 集成用例通过")
