"""SQLite 持久化层：槽位清单、候选阶段、确认代次与诊断事件。

设计要点：
- 每个阶段迁移（写入/校验/确认）都是一次同步提交的写事务，
  因此任意时刻断电都不会丢失已提交状态；
- 写路径全部经过单连接 + 可重入锁串行化，"比较并交换"（代次校验）
  在锁内完成，保证同一确认代次的升级资格只会被一个请求取得；
- 诊断事件（events）只增不改，候选损坏、断电、恢复裁决均留有证据。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

EMPTY = "empty"            # 空槽位
WRITTEN = "written"        # 候选已写入，未校验
VERIFIED = "verified"      # 候选摘要校验通过，未确认
CONFIRMED = "confirmed"    # 已确认清单（当前活动槽位）
SUPERSEDED = "superseded"  # 曾确认、已被更高代次取代（禁止回退）
CORRUPT = "corrupt"        # 摘要校验失败（保留诊断证据，禁止引导）

SLOT_NAMES = ("A", "B")

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    generation INTEGER NOT NULL,      -- 确认代次：每次确认切换 +1
    active_slot TEXT NOT NULL,        -- 持久化的活动槽位指针
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots (
    device_id TEXT NOT NULL,
    slot TEXT NOT NULL,               -- 'A' | 'B'
    state TEXT NOT NULL,              -- 候选阶段 / 清单状态
    version INTEGER,
    expected_digest TEXT,             -- 清单登记的镜像摘要
    image BLOB,                       -- 实际存放的镜像字节
    confirmed_generation INTEGER,     -- 该清单被确认时的代次
    diagnostics TEXT,                 -- JSON：损坏证据等
    updated_at TEXT NOT NULL,
    PRIMARY KEY (device_id, slot)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,             -- JSON
    created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class NotFoundError(Exception):
    def __init__(self, message: str = "设备不存在"):
        super().__init__(message)
        self.message = message


class ConflictError(Exception):
    """稳定的领域冲突（HTTP 409），code 供客户端机械识别。"""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class Store:
    """单连接 + 写锁的 SQLite 存储；所有写操作在锁内以事务提交。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        with self._lock:
            self._conn.execute("SELECT 1")
        return True

    # ---- 内部助手（调用方须持有锁） ----

    def _device_row(self, device_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM devices WHERE id=?", (device_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"设备 {device_id} 不存在")
        return dict(row)

    def _slot_row(self, device_id: str, slot: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM slots WHERE device_id=? AND slot=?", (device_id, slot)
        ).fetchone()
        return dict(row)

    def _slots_of(self, device_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM slots WHERE device_id=? ORDER BY slot", (device_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def _put_slot(self, device_id: str, slot: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(
            f"UPDATE slots SET {cols}, updated_at=? WHERE device_id=? AND slot=?",
            (*fields.values(), utcnow(), device_id, slot),
        )

    def _log(self, device_id: str, kind: str, detail: dict) -> None:
        self._conn.execute(
            "INSERT INTO events(device_id, kind, detail, created_at) VALUES (?,?,?,?)",
            (device_id, kind, json.dumps(detail, ensure_ascii=False), utcnow()),
        )

    @staticmethod
    def _inactive_slot(active_slot: str) -> str:
        return "B" if active_slot == "A" else "A"

    # ---- 设备 ----

    def create_device(self, name: str, version: int, image: bytes) -> dict:
        device_id = uuid.uuid4().hex[:12]
        digest = sha256_hex(image)
        now = utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO devices(id,name,generation,active_slot,created_at)"
                " VALUES (?,?,?,?,?)",
                (device_id, name, 1, "A", now),
            )
            self._conn.execute(
                "INSERT INTO slots(device_id,slot,state,version,expected_digest,image,"
                "confirmed_generation,diagnostics,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (device_id, "A", CONFIRMED, version, digest, image, 1, None, now),
            )
            self._conn.execute(
                "INSERT INTO slots(device_id,slot,state,version,expected_digest,image,"
                "confirmed_generation,diagnostics,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (device_id, "B", EMPTY, None, None, None, None, None, now),
            )
            self._log(device_id, "device_created", {
                "name": name, "version": version, "digest": digest,
                "active_slot": "A", "generation": 1,
            })
        return self.get_device(device_id)

    def list_devices(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM devices ORDER BY created_at, id"
            ).fetchall()
            out = []
            for row in rows:
                dev = dict(row)
                active = self._slot_row(dev["id"], dev["active_slot"])
                out.append({
                    "id": dev["id"],
                    "name": dev["name"],
                    "generation": dev["generation"],
                    "active_slot": dev["active_slot"],
                    "active_version": active["version"],
                    "created_at": dev["created_at"],
                })
            return out

    def get_device(self, device_id: str) -> dict:
        with self._lock:
            device = self._device_row(device_id)
            slots = self._slots_of(device_id)
        return {"device": device, "slots": slots}

    # ---- 候选生命周期 ----

    def submit_candidate(self, device_id: str, version: int, image: bytes,
                         expected_generation: int) -> dict:
        """写入候选。同一确认代次的升级资格只授予一个请求，其余稳定冲突。"""
        with self._lock, self._conn:
            dev = self._device_row(device_id)
            if dev["generation"] != expected_generation:
                raise ConflictError(
                    "GENERATION_CONFLICT",
                    f"预期代次 {expected_generation} 与当前代次 {dev['generation']} 不符，"
                    "该代次的升级资格已被取代",
                    current_generation=dev["generation"],
                )
            active = self._slot_row(device_id, dev["active_slot"])
            if active["version"] is not None and version <= active["version"]:
                raise ConflictError(
                    "VERSION_NOT_NEWER",
                    f"候选版本 v{version} 必须高于当前活动版本 v{active['version']}",
                    active_version=active["version"],
                )
            target_slot = self._inactive_slot(dev["active_slot"])
            target = self._slot_row(device_id, target_slot)
            if target["state"] in (WRITTEN, VERIFIED):
                raise ConflictError(
                    "CANDIDATE_IN_PROGRESS",
                    f"当前代次 {dev['generation']} 的升级资格已被槽位 {target_slot} "
                    "的候选占据，本次提交不得改写任何槽位",
                    occupying_slot=target_slot,
                    occupying_state=target["state"],
                )
            digest = sha256_hex(image)
            self._put_slot(
                device_id, target_slot,
                state=WRITTEN, version=version, expected_digest=digest, image=image,
                confirmed_generation=None, diagnostics=None,
            )
            self._log(device_id, "candidate_written", {
                "slot": target_slot, "version": version,
                "digest": digest, "generation": dev["generation"],
            })
        return self.get_device(device_id)

    def verify_candidate(self, device_id: str) -> dict:
        """重算候选镜像摘要并与清单比对；不符则封存为 corrupt 并留证。"""
        with self._lock, self._conn:
            dev = self._device_row(device_id)
            cand_slot = self._inactive_slot(dev["active_slot"])
            cand = self._slot_row(device_id, cand_slot)
            if cand["state"] not in (WRITTEN, VERIFIED, CORRUPT):
                raise ConflictError(
                    "NO_CANDIDATE",
                    f"槽位 {cand_slot} 没有待校验的候选（当前状态 {cand['state']}）",
                    slot_state=cand["state"],
                )
            image = cand["image"]
            actual = sha256_hex(image) if image else None
            if actual is not None and actual == cand["expected_digest"]:
                if cand["state"] != VERIFIED:
                    self._put_slot(device_id, cand_slot, state=VERIFIED, diagnostics=None)
                    self._log(device_id, "candidate_verified", {
                        "slot": cand_slot, "version": cand["version"], "digest": actual,
                    })
            else:
                diagnostics = {
                    "reason": "digest_mismatch",
                    "expected_digest": cand["expected_digest"],
                    "actual_digest": actual,
                    "detail": "候选镜像摘要与清单不符（疑似断电撕裂写入或位翻转），"
                              "禁止引导，保留现场",
                }
                self._put_slot(device_id, cand_slot, state=CORRUPT,
                               diagnostics=json.dumps(diagnostics, ensure_ascii=False))
                self._log(device_id, "candidate_corrupt", {
                    "slot": cand_slot, "version": cand["version"], **diagnostics,
                })
        return self.get_device(device_id)

    def confirm_switch(self, device_id: str, expected_generation: int) -> dict:
        """确认切换：原子地翻转活动槽位并推进确认代次，旧清单转为 superseded。"""
        with self._lock, self._conn:
            dev = self._device_row(device_id)
            if dev["generation"] != expected_generation:
                raise ConflictError(
                    "GENERATION_CONFLICT",
                    f"预期代次 {expected_generation} 与当前代次 {dev['generation']} 不符，"
                    "确认请求已过期",
                    current_generation=dev["generation"],
                )
            cand_slot = self._inactive_slot(dev["active_slot"])
            cand = self._slot_row(device_id, cand_slot)
            if cand["state"] == CORRUPT:
                raise ConflictError(
                    "CANDIDATE_CORRUPT",
                    "候选摘要校验失败，禁止确认切换，诊断证据已保留",
                    slot=cand_slot,
                )
            if cand["state"] != VERIFIED:
                raise ConflictError(
                    "INVALID_STAGE",
                    f"候选须先通过摘要校验（当前状态 {cand['state']}）",
                    slot_state=cand["state"],
                )
            # 纵深防御：确认前最后一刻复核摘要，防止已校验镜像被后续损坏
            actual = sha256_hex(cand["image"]) if cand["image"] else None
            if actual != cand["expected_digest"]:
                diagnostics = {
                    "reason": "digest_mismatch_at_confirm",
                    "expected_digest": cand["expected_digest"],
                    "actual_digest": actual,
                    "detail": "确认前复核发现摘要残缺，候选已封存，禁止引导",
                }
                self._put_slot(device_id, cand_slot, state=CORRUPT,
                               diagnostics=json.dumps(diagnostics, ensure_ascii=False))
                self._log(device_id, "candidate_corrupt", {
                    "slot": cand_slot, "version": cand["version"], **diagnostics,
                })
                raise ConflictError(
                    "CANDIDATE_CORRUPT",
                    "确认前复核发现摘要残缺，候选已封存",
                    slot=cand_slot,
                )
            new_gen = dev["generation"] + 1
            old_slot = dev["active_slot"]
            self._put_slot(device_id, old_slot, state=SUPERSEDED)
            self._put_slot(device_id, cand_slot, state=CONFIRMED,
                           confirmed_generation=new_gen, diagnostics=None)
            self._conn.execute(
                "UPDATE devices SET active_slot=?, generation=? WHERE id=?",
                (cand_slot, new_gen, device_id),
            )
            self._log(device_id, "switch_confirmed", {
                "from_slot": old_slot, "to_slot": cand_slot,
                "version": cand["version"], "generation": new_gen,
                "digest": cand["expected_digest"],
            })
        return self.get_device(device_id)

    # ---- 故障注入 ----

    def power_loss(self, device_id: str, corrupt_candidate: bool = False) -> dict:
        """模拟断电。所有已提交阶段均已持久化，断电不丢失已提交状态；
        可选地对候选镜像做撕裂写入（截断字节），模拟写途中断电。"""
        with self._lock, self._conn:
            dev = self._device_row(device_id)
            cand_slot = self._inactive_slot(dev["active_slot"])
            cand = self._slot_row(device_id, cand_slot)
            torn = False
            if (corrupt_candidate and cand["state"] in (WRITTEN, VERIFIED)
                    and cand["image"]):
                kept = max(1, len(cand["image"]) // 2)
                self._put_slot(device_id, cand_slot, image=cand["image"][:kept])
                torn = True
            self._log(device_id, "power_loss", {
                "stage": cand["state"],
                "candidate_slot": cand_slot,
                "corrupt_candidate": torn,
                "note": "所有已提交阶段均已持久化；断电仅丢失未提交操作",
            })
        return self.get_device(device_id)

    # ---- 证据 ----

    def list_events(self, device_id: str, limit: int = 200) -> list[dict]:
        with self._lock:
            self._device_row(device_id)
            rows = self._conn.execute(
                "SELECT * FROM events WHERE device_id=? ORDER BY id DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
        return [{
            "id": r["id"],
            "kind": r["kind"],
            "detail": json.loads(r["detail"]),
            "created_at": r["created_at"],
        } for r in rows]
