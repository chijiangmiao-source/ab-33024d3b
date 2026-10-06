"""一次性验收服务：代码测试 + 页面构建 + 断电恢复/并发裁决 HTTP 冒烟。

在 Compose 中以 `verify` 服务运行，执行完毕自行退出：
- 退出码 0：全部通过；- 退出码 1：存在失败项。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

BASE = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# ---------- 阶段一：代码测试 ----------

def run_code_tests() -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-12:]
    for line in tail:
        print(f"  pytest| {line}", flush=True)
    return proc.returncode == 0


# ---------- 阶段二：页面构建 ----------

def build_page() -> bool:
    proc = subprocess.run(
        [sys.executable, "tools/build_page.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    print(f"  build| {proc.stdout.strip()}", flush=True)
    if proc.returncode != 0:
        print(f"  build| {proc.stderr.strip()}", flush=True)
        return False
    dist = ROOT / "frontend" / "dist" / "index.html"
    return dist.exists() and "轨道载荷升级控制台" in dist.read_text(encoding="utf-8")


# ---------- 阶段三：HTTP 冒烟 ----------

def wait_for_app(timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE}/healthz", timeout=2.0)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False


class Api:
    def __init__(self) -> None:
        self.c = httpx.Client(base_url=BASE, timeout=10.0)

    def create(self, name: str, version: int, image: str) -> dict:
        r = self.c.post("/api/devices",
                        json={"name": name, "version": version, "image": image})
        check(r.status_code == 200, f"创建设备失败：{r.text}")
        return r.json()

    def get(self, device_id: str) -> dict:
        r = self.c.get(f"/api/devices/{device_id}")
        check(r.status_code == 200, f"读取设备失败：{r.text}")
        return r.json()

    def write(self, dev: dict, version: int, image: str) -> dict:
        r = self.c.post(f"/api/devices/{dev['id']}/candidates",
                        json={"version": version, "image": image,
                              "expected_generation": dev["generation"]})
        check(r.status_code == 200, f"写入候选失败：{r.text}")
        return r.json()

    def verify(self, dev: dict) -> dict:
        r = self.c.post(f"/api/devices/{dev['id']}/verify")
        check(r.status_code == 200, f"摘要校验失败：{r.text}")
        return r.json()

    def confirm(self, dev: dict, generation: int) -> dict:
        r = self.c.post(f"/api/devices/{dev['id']}/confirm",
                        json={"expected_generation": generation})
        check(r.status_code == 200, f"确认切换失败：{r.text}")
        return r.json()

    def power_loss(self, dev: dict, corrupt: bool = False) -> dict:
        r = self.c.post(f"/api/devices/{dev['id']}/power-loss",
                        json={"corrupt_candidate": corrupt})
        check(r.status_code == 200, f"断电注入失败：{r.text}")
        return r.json()


def slot_of(view: dict, name: str) -> dict:
    return next(s for s in view["slots"] if s["slot"] == name)


def smoke_health_and_page(api: Api) -> None:
    r = api.c.get("/healthz")
    check(r.status_code == 200 and r.json().get("status") == "ok",
          f"健康响应异常：{r.status_code}")
    r = api.c.get("/")
    check(r.status_code == 200 and "轨道载荷升级控制台" in r.text,
          "页面未返回预期内容")


def smoke_power_loss_after_write(api: Api) -> None:
    dev = api.create("smoke-write", 1, "BASE-V1")
    api.write(dev, 2, "CAND-V2")
    api.power_loss(dev)
    view = api.get(dev["id"])
    check(view["active_slot"] == "A" and view["generation"] == 1,
          "写入后断电：活动槽位或代次被改写")
    check(view["recovery"]["verdict"] == "boot"
          and view["recovery"]["boot_version"] == 1,
          "写入后断电：未引导旧版本")
    check(slot_of(view, "B")["state"] == "written", "写入后断电：候选现场丢失")


def smoke_power_loss_after_verify(api: Api) -> None:
    dev = api.create("smoke-verify", 1, "BASE-V1")
    api.write(dev, 2, "CAND-V2")
    api.verify(dev)
    api.power_loss(dev)
    view = api.get(dev["id"])
    check(view["recovery"]["boot_slot"] == "A" and view["generation"] == 1,
          "校验后断电：不应引导未确认候选")
    check(slot_of(view, "B")["state"] == "verified", "校验后断电：候选阶段丢失")


def smoke_power_loss_after_confirm(api: Api) -> None:
    dev = api.create("smoke-confirm", 1, "BASE-V1")
    api.write(dev, 2, "CAND-V2")
    api.verify(dev)
    api.confirm(dev, 1)
    api.power_loss(dev)
    view = api.get(dev["id"])
    check(view["active_slot"] == "B" and view["generation"] == 2,
          "确认后断电：发生回退或代次丢失")
    check(view["recovery"]["boot_version"] == 2, "确认后断电：未引导新版本")
    check(slot_of(view, "A")["state"] == "superseded", "旧槽位应被取代")
    again = api.get(dev["id"])
    check(again["slots"] == view["slots"] and again["generation"] == 2,
          "确认后重开视图：槽位/版本/摘要/代次不一致")


def smoke_torn_write_not_bootable(api: Api) -> None:
    dev = api.create("smoke-corrupt", 1, "BASE-V1")
    api.write(dev, 2, "CAND-V2-TORN-IMAGE")
    api.power_loss(dev, corrupt=True)
    view = api.verify(dev)
    b = slot_of(view, "B")
    check(b["state"] == "corrupt" and not b["digest_ok"],
          "撕裂写入未被识别为损坏候选")
    check(b["diagnostics"] and b["diagnostics"]["reason"] == "digest_mismatch",
          "损坏候选缺少诊断证据")
    r = api.c.post(f"/api/devices/{dev['id']}/confirm",
                   json={"expected_generation": 1})
    check(r.status_code == 409
          and r.json()["error"]["code"] == "CANDIDATE_CORRUPT",
          "损坏候选竟被允许确认切换")
    view = api.get(dev["id"])
    check(view["recovery"]["boot_slot"] == "A", "损坏候选被引导")
    events = api.c.get(f"/api/devices/{dev['id']}/events").json()["events"]
    check(any(e["kind"] == "candidate_corrupt" for e in events),
          "事件日志缺少损坏证据")


def smoke_concurrent_adjudication(api: Api) -> None:
    dev = api.create("smoke-race", 1, "BASE-V1")
    barrier = threading.Barrier(3)
    results: list[httpx.Response] = []

    def submit(version: int) -> None:
        client = httpx.Client(base_url=BASE, timeout=10.0)
        barrier.wait()
        results.append(client.post(
            f"/api/devices/{dev['id']}/candidates",
            json={"version": version, "image": f"RACE-V{version}",
                  "expected_generation": dev["generation"]}))

    t1 = threading.Thread(target=submit, args=(2,))
    t2 = threading.Thread(target=submit, args=(3,))
    t1.start(); t2.start()
    barrier.wait()
    t1.join(); t2.join()

    codes = sorted(r.status_code for r in results)
    check(codes == [200, 409], f"并发提交应一成功一冲突，实际 {codes}")
    loser = next(r for r in results if r.status_code == 409)
    check(loser.json()["error"]["code"] in ("CANDIDATE_IN_PROGRESS",
                                            "GENERATION_CONFLICT"),
          "冲突缺少稳定错误码")
    view = api.get(dev["id"])
    check(slot_of(view, "A")["version"] == 1 and view["generation"] == 1,
          "并发冲突请求改写了活动版本")

    winner_version = next(r for r in results if r.status_code == 200) \
        .json()["slots"][1]["version"]
    api.verify(dev)
    api.confirm(dev, 1)
    view = api.get(dev["id"])
    check(view["generation"] == 2
          and view["recovery"]["boot_version"] == winner_version,
          "胜出候选未完成切换")
    again = api.get(dev["id"])
    check(again["slots"] == view["slots"] and again["generation"] == 2,
          "切换完成后重开视图不一致")


SMOKES = [
    ("冒烟：健康响应与页面", smoke_health_and_page),
    ("冒烟：候选写入后断电恢复", smoke_power_loss_after_write),
    ("冒烟：摘要校验后断电恢复", smoke_power_loss_after_verify),
    ("冒烟：确认切换后断电不回退", smoke_power_loss_after_confirm),
    ("冒烟：撕裂写入候选不得引导且留证", smoke_torn_write_not_bootable),
    ("冒烟：并发候选的代次裁决", smoke_concurrent_adjudication),
]


def main() -> int:
    print(f"验收目标：{BASE}", flush=True)

    healthy = wait_for_app()
    record("应用健康响应", healthy)

    record("代码测试（pytest）", run_code_tests())
    record("页面构建（frontend/dist）", build_page())

    if healthy:
        api = Api()
        for name, fn in SMOKES:
            try:
                fn(api)
                record(name, True)
            except Exception as exc:  # noqa: BLE001 - 验收须汇总全部失败
                record(name, False, str(exc))
    else:
        for name, _ in SMOKES:
            record(name, False, "应用不可达，跳过")

    failed = [n for n, ok, _ in RESULTS if not ok]
    total = len(RESULTS)
    print(f"\n验收结果：{total - len(failed)}/{total} 通过", flush=True)
    if failed:
        print("失败项：" + "；".join(failed), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
