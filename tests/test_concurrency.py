"""并发仲裁测试：同一确认代次的升级资格只有一个请求能取得。"""
import threading

from starlette.testclient import TestClient

from app.main import create_app


def _seed(tmp_path, name="race.db"):
    app = create_app(str(tmp_path / name))
    with TestClient(app) as c:
        dev = c.post("/api/devices",
                     json={"name": "race", "version": 1, "image": "BASE"}).json()
    return app, dev


def test_concurrent_candidate_submission_single_winner(tmp_path):
    app, dev = _seed(tmp_path)
    barrier = threading.Barrier(9)
    results = []

    def worker(ver):
        client = TestClient(app)
        barrier.wait()
        r = client.post(
            f"/api/devices/{dev['id']}/candidates",
            json={"version": ver, "image": f"CAND-V{ver}",
                  "expected_generation": 1})
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker, args=(v,)) for v in range(2, 10)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    winners = [b for s, b in results if s == 200]
    losers = [b for s, b in results if s == 409]
    assert len(winners) == 1
    assert len(losers) == 7
    for body in losers:
        assert body["error"]["code"] in ("CANDIDATE_IN_PROGRESS",
                                         "GENERATION_CONFLICT")

    # 活动版本未被任何并发请求改写
    with TestClient(app) as c:
        view = c.get(f"/api/devices/{dev['id']}").json()
    assert view["generation"] == 1
    assert view["active_slot"] == "A"
    active = next(s for s in view["slots"] if s["slot"] == "A")
    assert active["version"] == 1
    candidates = [s for s in view["slots"] if s["state"] in ("written", "verified")]
    assert len(candidates) == 1

    # 冲突稳定：失败者以相同代次重试仍是 409
    with TestClient(app) as c:
        r = c.post(f"/api/devices/{dev['id']}/candidates",
                   json={"version": 9, "image": "RETRY",
                         "expected_generation": 1})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CANDIDATE_IN_PROGRESS"


def test_concurrent_confirm_single_winner(tmp_path):
    app, dev = _seed(tmp_path, "confirm-race.db")
    with TestClient(app) as c:
        c.post(f"/api/devices/{dev['id']}/candidates",
               json={"version": 2, "image": "V2", "expected_generation": 1})
        c.post(f"/api/devices/{dev['id']}/verify")

    barrier = threading.Barrier(5)
    results = []

    def worker():
        client = TestClient(app)
        barrier.wait()
        r = client.post(f"/api/devices/{dev['id']}/confirm",
                        json={"expected_generation": 1})
        results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409, 409, 409]
    with TestClient(app) as c:
        view = c.get(f"/api/devices/{dev['id']}").json()
        again = c.get(f"/api/devices/{dev['id']}").json()
    assert view["generation"] == 2
    assert view["active_slot"] == "B"
    assert view["recovery"]["boot_version"] == 2
    # 完成切换后重开：槽位、版本、摘要、确认代次一致
    assert again["slots"] == view["slots"]
    assert again["generation"] == view["generation"]
