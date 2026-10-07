#!/usr/bin/env bash
# 内审预览凭证（定时公开前的一次性预览包）端到端示例：
#   上传 + 标注 -> 定时发布（约 25 秒后公开）
#   -> 管理员凭 PREVIEW_ADMIN_KEY 颁发一次性预览凭证（10 分钟有效）
#   -> 内审员持凭证领取预览包 ZIP（仅该版成品 PNG + 发布时冻结的遮盖信息，
#      绝不包含原图）；重复领取 / 未知凭证统一 404
#   -> 到点前公开读取仍 404；到点后公开可读、预览凭证窗口关闭（统一 404）
#   -> 错误密钥 401、立即公开版本 409、已撤回版本 410 均拒绝颁发
# 依赖：curl、python3（仅用于解析 JSON、生成示例图与 RFC 3339 时间戳、解压展示）
# 启动服务前需配置管理密钥，例如：
#   PREVIEW_ADMIN_KEY=local-preview-key uvicorn app.main:app --port 8000
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
ADMIN_KEY="${PREVIEW_ADMIN_KEY:-local-preview-key}"

iso_in() {  # $1 = 距当前的秒数 -> RFC 3339（UTC，尾部 Z）
  python3 - "$1" <<'EOF'
import sys
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) + timedelta(seconds=int(sys.argv[1])))
      .replace(microsecond=0).isoformat().replace("+00:00", "Z"))
EOF
}

code() {  # 仅输出 HTTP 状态码
  curl -sS -o /dev/null -w "%{http_code}" "$@"
}

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/preview_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/preview_b.png")
EOF

upload() {
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 上传两份原图并标注；A 定时 25 秒后公开，B 立即公开 =="
A=$(upload /tmp/preview_a.png)
B=$(upload /tmp/preview_b.png)
for d in "$A" "$B"; do
  curl -sS -X PUT "$BASE/documents/$d/annotations" -H 'Content-Type: application/json' \
    -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
done
PUBLISH_AT=$(iso_in 25)
curl -sS -X POST "$BASE/documents/$A/publish" -H 'Content-Type: application/json' \
  -d "{\"publish_at\":\"$PUBLISH_AT\"}" | python3 -m json.tool
curl -sS -X POST "$BASE/documents/$B/publish" >/dev/null
echo "A=$A（定时公开于 $PUBLISH_AT）  B=$B（立即公开）"

echo
echo "== 2. 管理员凭本地密钥为 A v1 颁发一次性预览凭证（10 分钟有效）=="
ISSUE=$(curl -sS -X POST "$BASE/publication-previews" \
  -H 'Content-Type: application/json' \
  -H "X-Preview-Admin-Key: $ADMIN_KEY" \
  -d "{\"doc_id\":\"$A\",\"version\":1,\"expires_in_minutes\":10}")
echo "$ISSUE" | python3 -m json.tool
TOKEN=$(echo "$ISSUE" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

echo
echo "== 3. 内审员持凭证领取预览包（ZIP：成品 PNG + 冻结遮盖信息，不含原图）=="
rm -f /tmp/preview.zip
curl -sS -o /tmp/preview.zip -w "HTTP %{http_code}, %{size_download} bytes, %{content_type}\n" \
  "$BASE/publication-previews/$TOKEN"
python3 - <<'EOF'
import json, zipfile
zf = zipfile.ZipFile("/tmp/preview.zip")
print("包内成员:", zf.namelist())
manifest = json.loads(zf.read("preview.json"))
print(json.dumps(manifest, ensure_ascii=False, indent=2))
assert len(zf.namelist()) == 2, "预览包只能含成品 PNG 与 preview.json"
EOF

echo
echo "== 4. 一次性：重复领取与未知凭证统一 404 preview_token_invalid（不泄露版本信息）=="
echo "重复领取: $(code "$BASE/publication-previews/$TOKEN")"
curl -sS "$BASE/publication-previews/$TOKEN" | python3 -m json.tool
echo "未知凭证: $(code "$BASE/publication-previews/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")"

echo
echo "== 5. 到点前公开读取仍 404（颁发/领取凭证不改变公开门面控）=="
curl -sS "$BASE/publications/$A/1" | python3 -m json.tool

echo
echo "== 6. 颁发拒绝：错误密钥 401；立即公开版本 409；已撤回版本 410 =="
echo "错误密钥: $(code -X POST "$BASE/publication-previews" \
  -H 'Content-Type: application/json' -H 'X-Preview-Admin-Key: wrong' \
  -d "{\"doc_id\":\"$A\",\"version\":1,\"expires_in_minutes\":5}")"
echo "立即公开版本 B v1: $(code -X POST "$BASE/publication-previews" \
  -H 'Content-Type: application/json' -H "X-Preview-Admin-Key: $ADMIN_KEY" \
  -d "{\"doc_id\":\"$B\",\"version\":1,\"expires_in_minutes\":5}")"
curl -sS -X POST "$BASE/documents/$B/publications/1/withdraw" \
  -H 'Content-Type: application/json' -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}' >/dev/null
echo "已撤回版本 B v1: $(code -X POST "$BASE/publication-previews" \
  -H 'Content-Type: application/json' -H "X-Preview-Admin-Key: $ADMIN_KEY" \
  -d "{\"doc_id\":\"$B\",\"version\":1,\"expires_in_minutes\":5}")"

echo
echo "== 7. 再为 A v1 颁发一张凭证，等待公开时间到达：公开读取自动 200，预览窗口关闭 =="
ISSUE2=$(curl -sS -X POST "$BASE/publication-previews" \
  -H 'Content-Type: application/json' \
  -H "X-Preview-Admin-Key: $ADMIN_KEY" \
  -d "{\"doc_id\":\"$A\",\"version\":1,\"expires_in_minutes\":10}")
TOKEN2=$(echo "$ISSUE2" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
echo "等待公开时间（$PUBLISH_AT）到达 ..."
while [ "$(code "$BASE/publications/$A/1")" != "200" ]; do sleep 1; done
echo "公开读取: $(code "$BASE/publications/$A/1")（到点自动可读）"
echo "凭证领取: $(code "$BASE/publication-previews/$TOKEN2")（窗口已关闭，统一 404）"
curl -sS "$BASE/publication-previews/$TOKEN2" | python3 -m json.tool

echo
echo "演示完成。"
