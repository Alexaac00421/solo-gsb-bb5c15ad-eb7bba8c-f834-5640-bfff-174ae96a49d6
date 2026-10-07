"""内审预览凭证（定时公开前的一次性预览包）测试。

覆盖：颁发（密钥缺失/错误/未配置、有效期边界、版本不存在、未设置未来公开
时间、已撤回）、凭证明文只返回一次且库中只存摘要、领取（一次性、到期与公开
时间窗口、统一 404 不泄露版本信息、预览包仅含成品 PNG 与冻结遮盖信息）、
领取与撤回的串行顺序、并发领取仅一笔成功、成品摘要复查失败不核销、公开
读取门控不受影响、凭证不进入归档且恢复后旧凭证不能沿用。

时钟通过 monkeypatch ``app.main._now_dt`` 注入，避免真实等待。
"""
import hashlib
import io
import json
import os
import sqlite3
import threading
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.config import Settings
from app.main import create_app
from tests.test_api import png_bytes, upload  # noqa: E402,F401  复用构造器与助手

ADMIN_KEY = "test-admin-key-preview"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=str(tmp_path / "data"), preview_admin_key=ADMIN_KEY
    )
    clock = {"now": datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(main_mod, "_now_dt", lambda: clock["now"])
    app = create_app(settings)
    with TestClient(app) as c:
        c.app.state.clock = clock
        yield c


@pytest.fixture()
def client_no_key(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"), preview_admin_key=None)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _advance(client, **delta):
    client.app.state.clock["now"] += timedelta(**delta)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _annotate(client, doc_id, revision=0, rects=None):
    rects = rects if rects is not None else [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感"}
    ]
    return client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )


def _scheduled_publish(client, doc_id, hours=12):
    """按当前时钟 +hours 小时定时发布，返回 (响应 JSON, publish_at)。"""
    publish_at = _iso(
        client.app.state.clock["now"] + timedelta(hours=hours)
    )
    r = client.post(
        f"/documents/{doc_id}/publish", json={"publish_at": publish_at}
    )
    assert r.status_code == 201, r.text
    return r.json(), publish_at


def _issue(client, doc_id, version, minutes=30, key=ADMIN_KEY):
    headers = {}
    if key is not None:
        headers["X-Preview-Admin-Key"] = key
    return client.post(
        "/publication-previews",
        json={
            "doc_id": doc_id,
            "version": version,
            "expires_in_minutes": minutes,
        },
        headers=headers,
    )


def _withdraw(client, doc_id, version, reason="遮盖遗漏：右下角工号未遮盖"):
    return client.post(
        f"/documents/{doc_id}/publications/{version}/withdraw",
        json={"reason": reason},
    )


def _db(client):
    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return settings, conn


def _token_rows(client):
    _, db = _db(client)
    rows = db.execute("SELECT * FROM preview_tokens").fetchall()
    db.close()
    return rows


def _new_scheduled_doc(client, hours=12):
    """上传 + 标注 + 定时发布，返回 (doc_id, pub, publish_at)。"""
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    pub, publish_at = _scheduled_publish(client, doc_id, hours=hours)
    return doc_id, pub, publish_at


# ---------------- 颁发：成功路径与密钥 ----------------

def test_issue_success_returns_token_once_and_stores_digest_only(client):
    doc_id, pub, publish_at = _new_scheduled_doc(client)

    r = _issue(client, doc_id, 1, minutes=30)
    assert r.status_code == 201
    body = r.json()
    token = body["token"]
    assert token and len(token) >= 32
    assert body["doc_id"] == doc_id
    assert body["version"] == 1
    assert body["publish_at"] == publish_at
    # 截止时间 = 当前时钟 + 30 分钟（统一 UTC 文本，尾部 Z）
    assert body["expires_at"] == "2026-10-05T12:30:00Z"
    assert body["created_at"]

    # 库中只存摘要，不存明文；尚未核销
    rows = _token_rows(client)
    assert len(rows) == 1
    row = rows[0]
    assert row["token_digest"] == hashlib.sha256(
        token.encode("ascii")
    ).hexdigest()
    assert row["token_digest"] != token
    assert row["doc_id"] == doc_id and row["version"] == 1
    assert row["expires_at"] == body["expires_at"]
    assert row["used_at"] is None


def test_issue_rejects_missing_or_wrong_key_uniformly(client):
    doc_id, _, _ = _new_scheduled_doc(client)

    for headers_key in (None, "wrong-key", ""):
        r = _issue(client, doc_id, 1, key=headers_key)
        assert r.status_code == 401
        detail = r.json()["detail"]
        assert detail["code"] == "preview_admin_key_invalid"
        assert "token" not in r.json()

    # 失败不留下任何凭证记录
    assert _token_rows(client) == []


def test_issue_rejected_when_key_not_configured(client_no_key):
    doc_id = upload(client_no_key, png_bytes()).json()["doc_id"]
    _annotate(client_no_key, doc_id, 0)
    r = client_no_key.post(f"/documents/{doc_id}/publish")
    assert r.status_code == 201

    # 服务端未配置密钥：即使请求带了某个密钥也一律拒绝颁发
    r = client_no_key.post(
        "/publication-previews",
        json={"doc_id": doc_id, "version": 1, "expires_in_minutes": 10},
        headers={"X-Preview-Admin-Key": "anything"},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "preview_admin_key_not_configured"
    assert "token" not in r.json()


# ---------------- 颁发：有效期与版本资格 ----------------

def test_issue_invalid_ttl_rejected(client):
    doc_id, _, _ = _new_scheduled_doc(client)
    for bad in (0, -5, 61, 120):
        r = _issue(client, doc_id, 1, minutes=bad)
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "invalid_preview_ttl"
    assert _token_rows(client) == []

    # 边界值 1 与 60 均可颁发
    assert _issue(client, doc_id, 1, minutes=1).status_code == 201
    assert _issue(client, doc_id, 1, minutes=60).status_code == 201


def test_issue_unknown_document_or_version(client):
    doc_id, _, _ = _new_scheduled_doc(client)

    r = _issue(client, "0" * 32, 1)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"

    r = _issue(client, doc_id, 99)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_version"
    assert _token_rows(client) == []


def test_issue_requires_future_scheduled_publish_time(client):
    # 立即公开（未设置公开时间）的版本拒绝颁发
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    r = client.post(f"/documents/{doc_id}/publish")
    assert r.status_code == 201 and r.json()["publish_at"] is None
    r = _issue(client, doc_id, 1)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "publication_not_scheduled"

    # 定时公开但公开时间已过的版本同样拒绝颁发
    doc_id2, _, _ = _new_scheduled_doc(client, hours=1)
    _advance(client, hours=2)
    r = _issue(client, doc_id2, 1)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "publication_not_scheduled"
    assert _token_rows(client) == []


def test_issue_withdrawn_version_rejected(client):
    doc_id, _, _ = _new_scheduled_doc(client)
    assert _withdraw(client, doc_id, 1).status_code == 201

    r = _issue(client, doc_id, 1)
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "publication_withdrawn"
    assert _token_rows(client) == []


# ---------------- 领取：一次性、窗口与统一拒绝 ----------------

def test_claim_success_package_contents_and_one_time(client):
    doc_id, pub, publish_at = _new_scheduled_doc(client)
    token = _issue(client, doc_id, 1).json()["token"]

    r = client.get(f"/publication-previews/{token}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    # 预览包仅含该版成品 PNG 与冻结遮盖信息两项，绝不包含原图
    assert sorted(zf.namelist()) == [f"{doc_id}_v1.png", "preview.json"]
    png = zf.read(f"{doc_id}_v1.png")
    assert hashlib.sha256(png).hexdigest() == pub["output_sha256"]
    # 成品字节与磁盘上的发布成品一致，且不等于原图字节
    settings, db = _db(client)
    rows = db.execute(
        "SELECT output_path FROM publications WHERE doc_id=? AND version=1",
        (doc_id,),
    ).fetchall()
    original_path = db.execute(
        "SELECT original_path FROM documents WHERE id=?", (doc_id,)
    ).fetchone()["original_path"]
    db.close()
    with open(rows[0]["output_path"], "rb") as f:
        assert png == f.read()
    with open(original_path, "rb") as f:
        assert png != f.read()

    manifest = json.loads(zf.read("preview.json"))
    assert manifest["doc_id"] == doc_id
    assert manifest["version"] == 1
    assert manifest["annotation_revision"] == 1
    assert manifest["output_sha256"] == pub["output_sha256"]
    assert manifest["redaction_count"] == 1
    assert manifest["redactions"] == [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感"}
    ]
    assert manifest["publish_at"] == publish_at
    # 仅成品与遮盖信息：不含原图摘要等原图信息
    assert "original_sha256" not in manifest

    # 一次性：再次领取统一 404
    r2 = client.get(f"/publication-previews/{token}")
    assert r2.status_code == 404
    assert r2.json()["detail"]["code"] == "preview_token_invalid"
    assert r2.headers["content-type"] != "application/zip"
    # 库中凭证已核销
    assert _token_rows(client)[0]["used_at"] is not None


def test_claim_uses_redactions_frozen_at_publication(client):
    doc_id, _, _ = _new_scheduled_doc(client)
    # 发布后修改标注：预览包仍应携带发布时冻结的修订 1 矩形
    _annotate(
        client, doc_id, 1,
        rects=[{"x": 9, "y": 9, "w": 3, "h": 3, "reason": "新标注"}],
    )
    token = _issue(client, doc_id, 1).json()["token"]

    r = client.get(f"/publication-previews/{token}")
    assert r.status_code == 200
    manifest = json.loads(
        zipfile.ZipFile(io.BytesIO(r.content)).read("preview.json")
    )
    assert manifest["annotation_revision"] == 1
    assert manifest["redactions"] == [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感"}
    ]


def test_unknown_expired_used_claims_rejected_uniformly(client):
    doc_id, _, _ = _new_scheduled_doc(client, hours=12)
    token_a = _issue(client, doc_id, 1, minutes=10).json()["token"]
    token_b = _issue(client, doc_id, 1, minutes=60).json()["token"]

    # 未知凭证
    r_unknown = client.get(f"/publication-previews/{'A' * 43}")
    # 过期凭证（前进 11 分钟，仍在公开时间之前）
    _advance(client, minutes=11)
    r_expired = client.get(f"/publication-previews/{token_a}")
    # 已用凭证
    assert client.get(f"/publication-previews/{token_b}").status_code == 200
    r_used = client.get(f"/publication-previews/{token_b}")

    # 统一拒绝：同状态码、同错误体，不泄露任何版本信息
    assert {r_unknown.status_code, r_expired.status_code,
            r_used.status_code} == {404}
    bodies = [r_unknown.json(), r_expired.json(), r_used.json()]
    assert bodies[0] == bodies[1] == bodies[2]
    detail = bodies[0]["detail"]
    assert detail["code"] == "preview_token_invalid"
    assert "doc_id" not in detail and "version" not in detail


def test_claim_after_publish_time_rejected_but_public_read_opens(client):
    # 公开时间 30 分钟后；凭证有效期 60 分钟（公开时间先到）
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    publish_at = _iso(
        client.app.state.clock["now"] + timedelta(minutes=30)
    )
    r = client.post(
        f"/documents/{doc_id}/publish", json={"publish_at": publish_at}
    )
    assert r.status_code == 201
    token = _issue(client, doc_id, 1, minutes=60).json()["token"]

    # 到点前公开读取仍 404（颁发凭证不改变公开门面控）
    r = client.get(f"/publications/{doc_id}/1")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "publication_not_yet_public"

    # 公开时间到达（凭证未过期）：领取窗口已关闭，统一 404
    _advance(client, minutes=31)
    r = client.get(f"/publication-previews/{token}")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "preview_token_invalid"
    # 公开读取到点后自动可读
    assert client.get(f"/publications/{doc_id}/1").status_code == 200
    assert client.get(f"/publications/{doc_id}/1/image").status_code == 200


# ---------------- 领取与撤回串行 ----------------

def test_withdraw_committed_first_rejects_claim_and_token_stays(client):
    doc_id, _, _ = _new_scheduled_doc(client)
    token = _issue(client, doc_id, 1).json()["token"]

    # 撤回先提交：随后的领取看到撤回记录，拒绝且不核销凭证
    assert _withdraw(client, doc_id, 1).status_code == 201
    r = client.get(f"/publication-previews/{token}")
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "publication_withdrawn"
    assert _token_rows(client)[0]["used_at"] is None


def test_claim_redeemed_first_completes_even_if_withdraw_waits(client):
    """领取在写锁内先完成校验与核销：并发撤回被写锁挡到领取事务提交之后，
    本次下载照常完成；撤回随后生效。"""
    doc_id, _, _ = _new_scheduled_doc(client)
    token = _issue(client, doc_id, 1).json()["token"]

    validated = threading.Event()
    withdraw_started = threading.Event()
    withdraw_returned = threading.Event()
    release = threading.Event()
    outcomes = {}

    def hook():
        validated.set()
        release.wait(timeout=10)

    def claimer():
        outcomes["claim"] = client.get(f"/publication-previews/{token}")

    def withdrawer():
        assert validated.wait(timeout=10)
        withdraw_started.set()
        outcomes["withdraw"] = _withdraw(client, doc_id, 1)
        withdraw_returned.set()

    main_mod.PREVIEW_CLAIM_HOOK = hook
    try:
        t_claim = threading.Thread(target=claimer)
        t_claim.start()
        assert validated.wait(timeout=10)
        t_withdraw = threading.Thread(target=withdrawer)
        t_withdraw.start()

        # 撤回请求已发起，且在写锁上排队等待领取事务
        assert withdraw_started.wait(timeout=10)
        assert not withdraw_returned.wait(timeout=0.5), (
            "撤回在领取事务提交前就完成了：领取未与撤回串行")

        # 放行领取：先核销并交付，撤回随后才完成
        release.set()
        assert withdraw_returned.wait(timeout=20), "撤回一直未完成（写锁可能泄漏）"
        t_claim.join(timeout=20)
        t_withdraw.join(timeout=20)
    finally:
        main_mod.PREVIEW_CLAIM_HOOK = None

    claim = outcomes["claim"]
    assert claim.status_code == 200, claim.text
    zf = zipfile.ZipFile(io.BytesIO(claim.content))
    assert f"{doc_id}_v1.png" in zf.namelist()
    assert outcomes["withdraw"].status_code in (200, 201)

    # 凭证已核销：再次领取统一 404；撤回也已生效
    again = client.get(f"/publication-previews/{token}")
    assert again.status_code == 404
    assert again.json()["detail"]["code"] == "preview_token_invalid"
    assert client.get(f"/publications/{doc_id}/1").status_code == 410


# ---------------- 并发领取：仅一笔成功 ----------------

def test_concurrent_claims_only_one_succeeds(client):
    doc_id, _, _ = _new_scheduled_doc(client)
    token = _issue(client, doc_id, 1).json()["token"]
    results = []

    def claimer():
        r = client.get(f"/publication-previews/{token}")
        results.append(r.status_code)

    threads = [threading.Thread(target=claimer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一笔领取成功，其余统一 404；库中只有一条已核销记录
    assert sorted(results) == [200] + [404] * 7
    rows = _token_rows(client)
    assert len(rows) == 1 and rows[0]["used_at"] is not None


# ---------------- 成品复查失败：拒绝且不核销 ----------------

def test_claim_artifact_missing_or_corrupt_rejected_token_not_consumed(client):
    doc_id, pub, _ = _new_scheduled_doc(client)
    settings, db = _db(client)
    output_path = db.execute(
        "SELECT output_path FROM publications WHERE doc_id=? AND version=1",
        (doc_id,),
    ).fetchone()["output_path"]
    db.close()

    # 成品缺失：500 artifact_missing，凭证保持未核销
    token_missing = _issue(client, doc_id, 1).json()["token"]
    saved = output_path + ".saved"
    os.rename(output_path, saved)
    try:
        r = client.get(f"/publication-previews/{token_missing}")
        assert r.status_code == 500
        assert r.json()["detail"]["code"] == "artifact_missing"
    finally:
        os.rename(saved, output_path)
    rows = _token_rows(client)
    assert all(row["used_at"] is None for row in rows)

    # 成品摘要与发布记录不符：500 artifact_corrupt，凭证同样不核销
    token_corrupt = _issue(client, doc_id, 1).json()["token"]
    with open(output_path, "wb") as f:
        f.write(b"corrupted-bytes")
    r = client.get(f"/publication-previews/{token_corrupt}")
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "artifact_corrupt"
    rows = _token_rows(client)
    assert all(row["used_at"] is None for row in rows)


# ---------------- 归档与恢复：凭证不进入归档，恢复后旧凭证无效 ----------------

def test_archive_excludes_tokens_and_restore_invalidates_old_tokens(
    client, tmp_path
):
    doc_id, _, _ = _new_scheduled_doc(client)
    token = _issue(client, doc_id, 1).json()["token"]
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()

    # 生成归档：任何成员都不含凭证明文/摘要，清单也没有凭证记录类别
    r = client.post("/service-data-archives")
    assert r.status_code == 200, r.text
    archive_bytes = bytes(bytearray(r.content))
    zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    assert not any("preview" in name for name in zf.namelist())
    for name in zf.namelist():
        data = zf.read(name)
        assert token.encode("ascii") not in data
        assert digest.encode("ascii") not in data
    manifest = json.loads(zf.read("manifest.json"))
    assert "preview_tokens" not in manifest["record_counts"]

    # 恢复到空实例：旧凭证不能沿用（统一 404，不泄露版本信息）
    settings2 = Settings(
        data_dir=str(tmp_path / "data2"), preview_admin_key=ADMIN_KEY
    )
    app2 = create_app(settings2)
    with TestClient(app2) as client2:
        r = client2.post(
            "/service-data-archives/restore",
            files={"file": ("archive.zip", archive_bytes, "application/zip")},
        )
        assert r.status_code == 200, r.text
        r = client2.get(f"/publication-previews/{token}")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "preview_token_invalid"
        # 恢复出的实例上定时公开语义不变：到点前公开读取仍 404
        r = client2.get(f"/publications/{doc_id}/1")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "publication_not_yet_public"
