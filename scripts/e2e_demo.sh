#!/usr/bin/env bash
set -euo pipefail

BASE=${BASE:-http://localhost:8000}
curl -fsS "$BASE/health" >/dev/null

project=$(curl -fsS -X POST "$BASE/api/projects" -H 'Content-Type: application/json' \
  -d '{"code":"DEMO-2026","name":"验收演示水准网"}')
project_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$project")

curl -fsS -X POST "$BASE/api/projects/$project_id/import" -H 'Content-Type: application/json' -d '{
  "points": [
    {"code":"BM-A","name":"基准A"},{"code":"P1","name":"测点1"},{"code":"P2","name":"测点2"},{"code":"BM-B","name":"基准B"}
  ],
  "observations": [
    {"line_code":"L1","from_code":"BM-A","to_code":"P1","observed_delta_m":1.001,"distance_m":1000},
    {"line_code":"L2","from_code":"P1","to_code":"P2","observed_delta_m":1.000,"distance_m":1000},
    {"line_code":"L3","from_code":"P2","to_code":"BM-B","observed_delta_m":1.002,"distance_m":1000},
    {"line_code":"L4","from_code":"BM-A","to_code":"BM-B","observed_delta_m":3.004,"distance_m":3000}
  ]
}'

curl -fsS -X POST "$BASE/api/projects/$project_id/datums?point_code=BM-A&elevation_m=100&sigma_m=0.0001"
curl -fsS -X POST "$BASE/api/projects/$project_id/datums?point_code=BM-B&elevation_m=103.001&sigma_m=0.0001"
curl -fsS -X POST "$BASE/api/projects/$project_id/weight-rules?name=mm-sqrt-km" \
  -H 'Content-Type: application/json' \
  -d '{"rule":{"method":"millimeter_sqrt_km","c_km":1.0,"base_sigma_m":0.001}}'

job=$(curl -fsS -X POST "$BASE/api/projects/$project_id/jobs")
echo "$job"
job_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' <<<"$job")

for _ in $(seq 1 30); do
  detail=$(curl -fsS "$BASE/api/jobs/$job_id")
  status=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"$detail")
  echo "status=$status"
  [[ "$status" == "completed" || "$status" == "failed" || "$status" == "audited_only" ]] && break
  sleep 1
done
curl -fsS "$BASE/api/jobs/$job_id" | python3 -m json.tool
curl -fsS "$BASE/api/jobs/$job_id/residuals?limit=20" | python3 -m json.tool

echo "--- 测段修订 CSV 验收 ---"
# 一条旧版本行（L1 声明 lock_version=99，草稿实际为 1）+ 一条合法修订行（L2）
cat > /tmp/revisions.csv <<'CSV'
line_code,lock_version,observed_delta_m,distance_m
L1,99,1.0015,1000
L2,1,1.0008,1000
CSV

echo "# 预览：L1 版本冲突，L2 可应用"
curl -fsS -X POST "$BASE/api/projects/$project_id/revisions/preview" \
  -F file=@/tmp/revisions.csv | python3 -m json.tool

echo "# 逐行确认：L2 应用（锁版本 1→2，写入审计），L1 跳过"
curl -fsS -X POST "$BASE/api/projects/$project_id/revisions/confirm" \
  -F file=@/tmp/revisions.csv -F strategy=per_row | python3 -m json.tool

echo "# 同文件重复确认：applied_count=0，不重复改值"
curl -fsS -X POST "$BASE/api/projects/$project_id/revisions/confirm" \
  -F file=@/tmp/revisions.csv -F strategy=per_row | python3 -m json.tool

echo "# 修订后的草稿生成新快照/新任务代次"
new_job=$(curl -fsS -X POST "$BASE/api/projects/$project_id/jobs")
echo "$new_job"

echo "# 旧 Job 仍只可审计：发布旧代次被拒（HTTP 409）"
http_code=$(curl -s -o /tmp/publish_old.json -w '%{http_code}' -X POST "$BASE/api/jobs/$job_id/publish" \
  -H 'Content-Type: application/json' -d '{"confirm":true}')
echo "publish old job -> HTTP $http_code"
cat /tmp/publish_old.json
