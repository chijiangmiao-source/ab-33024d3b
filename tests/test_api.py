"""API 级测试：断电恢复、阶段守卫、确认代次与持久化。"""
from starlette.testclient import TestClient

from app.main import create_app


def create(client, version=1, image="PAYLOAD-V1", name="dev"):
    r = client.post("/api/devices",
                    json={"name": name, "version": version, "image": image})
    assert r.status_code == 200, r.text
    return r.json()


def write_candidate(client, dev, version=2, image="PAYLOAD-V2"):
    r = client.post(f"/api/devices/{dev['id']}/candidates",
                    json={"version": version, "image": image,
                          "expected_generation": dev["generation"]})
    assert r.status_code == 200, r.text
    return r.json()


def slot(view, name):
    return next(s for s in view["slots"] if s["slot"] == name)


def test_create_device_and_reopen(client):
    dev = create(client, version=3, image="IMG-THREE")
    assert dev["generation"] == 1
    assert dev["active_slot"] == "A"
    a = slot(dev, "A")
    assert a["state"] == "confirmed"
    assert a["version"] == 3
    assert a["digest_ok"] is True
    assert a["confirmed_generation"] == 1
    assert slot(dev, "B")["state"] == "empty"
    assert dev["recovery"]["verdict"] == "boot"
    assert dev["recovery"]["boot_slot"] == "A"

    reopened = client.get(f"/api/devices/{dev['id']}").json()
    assert reopened["recovery"]["boot_version"] == 3
    assert reopened["generation"] == 1


def test_candidate_must_be_newer_version(client):
    dev = create(client, version=2)
    for bad in (2, 1):
        r = client.post(f"/api/devices/{dev['id']}/candidates",
                        json={"version": bad, "image": "X",
                              "expected_generation": 1})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "VERSION_NOT_NEWER"


def test_power_loss_after_write_keeps_old_active(client):
    dev = create(client)
    write_candidate(client, dev)
    client.post(f"/api/devices/{dev['id']}/power-loss", json={})
    view = client.get(f"/api/devices/{dev['id']}").json()
    assert view["active_slot"] == "A"
    assert view["generation"] == 1
    assert view["recovery"]["verdict"] == "boot"
    assert view["recovery"]["boot_version"] == 1
    b = slot(view, "B")
    assert b["state"] == "written"  # 未确认候选保留现场但不得引导
    check_b = next(c for c in view["recovery"]["checks"] if c["slot"] == "B")
    assert check_b["eligible"] is False


def test_power_loss_after_verify_still_boots_old(client):
    dev = create(client)
    write_candidate(client, dev)
    client.post(f"/api/devices/{dev['id']}/verify")
    client.post(f"/api/devices/{dev['id']}/power-loss", json={})
    view = client.get(f"/api/devices/{dev['id']}").json()
    assert view["recovery"]["boot_slot"] == "A"
    assert slot(view, "B")["state"] == "verified"
    assert view["generation"] == 1


def test_power_loss_after_confirm_never_rolls_back(client):
    dev = create(client)
    write_candidate(client, dev)
    client.post(f"/api/devices/{dev['id']}/verify")
    r = client.post(f"/api/devices/{dev['id']}/confirm",
                    json={"expected_generation": 1})
    assert r.status_code == 200
    client.post(f"/api/devices/{dev['id']}/power-loss", json={})

    view = client.get(f"/api/devices/{dev['id']}").json()
    assert view["active_slot"] == "B"
    assert view["generation"] == 2
    assert view["recovery"]["verdict"] == "boot"
    assert view["recovery"]["boot_slot"] == "B"
    assert view["recovery"]["boot_version"] == 2
    assert slot(view, "A")["state"] == "superseded"
    assert slot(view, "A")["confirmed_generation"] == 1
    assert slot(view, "B")["confirmed_generation"] == 2

    # 重开视图：槽位、版本、摘要、确认代次完全一致
    again = client.get(f"/api/devices/{dev['id']}").json()
    assert again["slots"] == view["slots"]
    assert again["generation"] == view["generation"]
    assert again["active_slot"] == view["active_slot"]


def test_torn_write_candidate_is_corrupt_and_never_boots(client):
    dev = create(client)
    write_candidate(client, dev)
    # 写途中断电：候选镜像被截断
    client.post(f"/api/devices/{dev['id']}/power-loss",
                json={"corrupt_candidate": True})
    view = client.post(f"/api/devices/{dev['id']}/verify").json()
    b = slot(view, "B")
    assert b["state"] == "corrupt"
    assert b["digest_ok"] is False
    assert b["diagnostics"]["reason"] == "digest_mismatch"
    assert b["diagnostics"]["expected_digest"] != b["diagnostics"]["actual_digest"]

    # 损坏候选禁止确认切换
    r = client.post(f"/api/devices/{dev['id']}/confirm",
                    json={"expected_generation": 1})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CANDIDATE_CORRUPT"

    view = client.get(f"/api/devices/{dev['id']}").json()
    assert view["recovery"]["boot_slot"] == "A"
    assert view["generation"] == 1
    # 诊断证据保留在事件日志中
    events = client.get(f"/api/devices/{dev['id']}/events").json()["events"]
    kinds = [e["kind"] for e in events]
    assert "candidate_corrupt" in kinds
    assert "power_loss" in kinds


def test_confirm_requires_verified_stage(client):
    dev = create(client)
    write_candidate(client, dev)
    r = client.post(f"/api/devices/{dev['id']}/confirm",
                    json={"expected_generation": 1})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INVALID_STAGE"


def test_confirm_with_stale_generation_conflicts(client):
    dev = create(client)
    write_candidate(client, dev)
    client.post(f"/api/devices/{dev['id']}/verify")
    r = client.post(f"/api/devices/{dev['id']}/confirm",
                    json={"expected_generation": 99})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GENERATION_CONFLICT"


def test_submit_with_stale_generation_conflicts(client):
    dev = create(client)
    r = client.post(f"/api/devices/{dev['id']}/candidates",
                    json={"version": 2, "image": "V2", "expected_generation": 7})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GENERATION_CONFLICT"


def test_second_candidate_while_one_in_progress_conflicts(client):
    dev = create(client)
    write_candidate(client, dev, version=2)
    r = client.post(f"/api/devices/{dev['id']}/candidates",
                    json={"version": 3, "image": "V3", "expected_generation": 1})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CANDIDATE_IN_PROGRESS"
    # 冲突请求不得改写活动版本
    view = client.get(f"/api/devices/{dev['id']}").json()
    assert slot(view, "A")["version"] == 1
    assert slot(view, "B")["version"] == 2


def test_verify_without_candidate_conflicts(client):
    dev = create(client)
    r = client.post(f"/api/devices/{dev['id']}/verify")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_CANDIDATE"


def test_unknown_device_404(client):
    r = client.get("/api/devices/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "DEVICE_NOT_FOUND"


def test_state_survives_service_restart(tmp_path):
    db = str(tmp_path / "restart.db")
    app1 = create_app(db)
    with TestClient(app1) as c1:
        dev = create(c1, version=5, image="BASE-V5")
        write_candidate(c1, dev, version=6, image="NEXT-V6")
        c1.post(f"/api/devices/{dev['id']}/verify")
        c1.post(f"/api/devices/{dev['id']}/confirm",
                json={"expected_generation": 1})

    # 全新进程/实例加载同一数据库：恢复后状态完全一致
    app2 = create_app(db)
    with TestClient(app2) as c2:
        view = c2.get(f"/api/devices/{dev['id']}").json()
    assert view["generation"] == 2
    assert view["active_slot"] == "B"
    assert view["recovery"]["boot_slot"] == "B"
    assert view["recovery"]["boot_version"] == 6
    assert slot(view, "A")["state"] == "superseded"
    assert slot(view, "B")["confirmed_generation"] == 2
    assert slot(view, "B")["digest_ok"] is True


def test_full_upgrade_cycle_twice(client):
    dev = create(client, version=1, image="V1")
    for gen, ver in ((1, 2), (2, 3)):
        write_candidate(client, dev, version=ver, image=f"V{ver}")
        client.post(f"/api/devices/{dev['id']}/verify")
        r = client.post(f"/api/devices/{dev['id']}/confirm",
                        json={"expected_generation": gen})
        assert r.status_code == 200, r.text
        dev = client.get(f"/api/devices/{dev['id']}").json()
    assert dev["generation"] == 3
    assert dev["recovery"]["boot_version"] == 3
    assert dev["active_slot"] == "A"  # A→B→A 交替
