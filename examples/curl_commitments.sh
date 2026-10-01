#!/usr/bin/env bash
# 择时通行承诺调用示例（申请 / 查询 / 取消 + 承诺对区域变更的约束）。
#
# 演示：
#   1. 登记一个只在未来窗口中段生效的限行区域 A（窗口内存在两段安全出发时刻）
#   2. 以相对秒数轨迹 + 带时区候选窗口申请承诺（201，返回 id/revision/安全区间）
#   3. 按 ID 查询承诺当前可用安全出发区间
#   4. 修订区域 A 使其长期生效 → 409 commitment_conflict（承诺未取消且窗口未结束，
#      变更前仍有未来安全时刻而变更后没有，整笔回滚）
#   5. 取消承诺（重复取消幂等）
#   6. 取消后再次修订 → 200
#   7. 全程无安全时刻 → 422 no_safe_departure；窗口起点在过去 → 422；未知 ID → 404
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_commitments.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }

# 候选出发窗口：当前时刻 +1h 到 +3h（带时区，左闭右开）
WS=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=1)).isoformat().replace("+00:00","Z"))')
WE=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=3)).isoformat().replace("+00:00","Z"))')
# 区域 A 只在窗口中段生效（服务端归一化时区）
VF=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=1,minutes=44,seconds=15)).isoformat().replace("+00:00","Z"))')
VT=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=2,minutes=30,seconds=15)).isoformat().replace("+00:00","Z"))')

echo "== 1. 登记限行区域 A：正方形 [0,100]x[0,100]，仅在候选窗口中段生效 =="
A=$(curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d "{
        \"name\": \"A区-择时演示\",
        \"polygon\": [[0,0],[100,0],[100,100],[0,100]],
        \"valid_from\": \"$VF\",
        \"valid_to\":   \"$VT\"
      }")
echo "$A" | python3 -m json.tool
A_ID=$(echo "$A" | J "['region']['id']")

echo "== 2. 申请择时通行承诺：横移轨迹相对 0~60s，候选窗口 [+1h, +3h) =="
# 几何接触为闭区间 [15s,45s]；禁止出发区间 [vf-45s, vt-15s) 落在窗口内，
# 窗口被切成两段安全区间（精确区间运算，非抽样）。
COMMIT=$(curl -fsS -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 0},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$WS\",
        \"window_end\":   \"$WE\"
      }")
echo "$COMMIT" | python3 -m json.tool
COMMIT_ID=$(echo "$COMMIT" | J "['id']")
echo "承诺 id=$COMMIT_ID，签发 revision=$(echo "$COMMIT" | J "['revision']")，安全区间 $(echo "$COMMIT" | J "['intervals'].__len__()") 段"

echo "== 3. 查询承诺当前可用安全出发区间（随区域集合实时重算） =="
curl -fsS "$BASE_URL/api/commitments/$COMMIT_ID" | python3 -m json.tool

echo "== 4. 修订 A 为长期生效：承诺将丧失全部未来安全时刻 → 409 commitment_conflict =="
curl -sS -o /tmp/commitment_conflict.json -w 'HTTP %{http_code}\n' \
  -X PATCH "$BASE_URL/api/regions/$A_ID" \
  -H 'Content-Type: application/json' \
  -d '{
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to":   "2030-01-01T00:00:00Z"
      }'
python3 -m json.tool /tmp/commitment_conflict.json
echo "revision 仍为：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")（未推进，整笔回滚）"

echo "== 5. 取消承诺（重复取消幂等，status/cancelled_at 不变） =="
curl -fsS -X POST "$BASE_URL/api/commitments/$COMMIT_ID/cancel" | python3 -m json.tool
curl -fsS -X POST "$BASE_URL/api/commitments/$COMMIT_ID/cancel" | python3 -m json.tool

echo "== 6. 取消后再次修订 A 为长期生效 → 200 =="
curl -fsS -X PATCH "$BASE_URL/api/regions/$A_ID" \
  -H 'Content-Type: application/json' \
  -d '{
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to":   "2030-01-01T00:00:00Z"
      }' | python3 -m json.tool

echo "== 7a. 窗口内无任何安全出发时刻 → 422 no_safe_departure，不落库 =="
curl -sS -o /tmp/commitment_empty.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 0},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$WS\",
        \"window_end\":   \"$WE\"
      }"
python3 -m json.tool /tmp/commitment_empty.json

echo "== 7b. 窗口起点在签发时刻之前 → 422 invalid_trajectory，不落库 =="
PAST=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat().replace("+00:00","Z"))')
curl -sS -o /tmp/commitment_past.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 500, \"t\": 0},
          {\"x\": 150, \"y\": 500, \"t\": 60}
        ],
        \"window_start\": \"$PAST\",
        \"window_end\":   \"$WE\"
      }"
python3 -m json.tool /tmp/commitment_past.json

echo "== 7c. 未知承诺 → 404 =="
curl -sS -o /tmp/commitment_404.json -w 'HTTP %{http_code}\n' "$BASE_URL/api/commitments/999999"
python3 -m json.tool /tmp/commitment_404.json

echo "完成。提示：示例区域会保留，可用 DELETE /api/regions/$A_ID 清理。"
