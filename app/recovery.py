"""恢复裁决：仅从摘要完整且已确认的清单中选定唯一活动槽位。

裁决规则：
- 槽位清单须同时满足「已确认」与「摘要完整（重算摘要与清单登记一致）」
  才具备引导资格；
- 合格清单必须唯一，否则停机并保留证据；
- 未确认候选（written/verified）、损坏候选（corrupt）、被取代清单
  （superseded）一律不得引导——尤其禁止回退到已被取代的旧版本。
"""
from __future__ import annotations

from .store import (
    CONFIRMED,
    CORRUPT,
    SUPERSEDED,
    VERIFIED,
    WRITTEN,
    sha256_hex,
    utcnow,
)


def adjudicate(device: dict, slots: list[dict]) -> dict:
    checks: list[dict] = []
    eligible: list[dict] = []

    for slot in sorted(slots, key=lambda s: s["slot"]):
        image = slot.get("image")
        actual = sha256_hex(image) if image else None
        expected = slot.get("expected_digest")
        digest_ok = expected is not None and actual == expected
        state = slot["state"]
        check = {
            "slot": slot["slot"],
            "state": state,
            "version": slot["version"],
            "expected_digest": expected,
            "actual_digest": actual,
            "digest_ok": digest_ok,
            "confirmed_generation": slot["confirmed_generation"],
        }
        if state == CONFIRMED and digest_ok:
            check.update(eligible=True, reason="清单已确认且摘要完整，具备引导资格")
            eligible.append(slot)
        elif state == CONFIRMED:
            check.update(eligible=False,
                         reason="清单已确认但摘要残缺，禁止引导（保留证据）")
        elif state == SUPERSEDED:
            check.update(eligible=False,
                         reason=f"清单曾确认于代次 {slot['confirmed_generation']}，"
                                "已被更高代次取代，禁止回退引导")
        elif state in (WRITTEN, VERIFIED):
            check.update(eligible=False,
                         reason="候选尚未确认，禁止引导，保留现场待维护员处置")
        elif state == CORRUPT:
            check.update(eligible=False,
                         reason="摘要校验失败，保留诊断证据，禁止引导")
        else:
            check.update(eligible=False, reason="空槽位，无清单")
        checks.append(check)

    if len(eligible) == 1:
        winner = eligible[0]
        verdict = "boot"
        boot_slot = winner["slot"]
        boot_version = winner["version"]
        summary = (f"选定唯一合格清单：槽位 {boot_slot}"
                   f"（版本 v{boot_version}，确认代次 {winner['confirmed_generation']}）")
    elif not eligible:
        verdict = "no_boot"
        boot_slot = boot_version = None
        summary = "没有任何清单同时满足摘要完整且已确认，设备保持停机并保留全部证据"
    else:
        verdict = "ambiguous"
        boot_slot = boot_version = None
        summary = "合格清单不唯一，裁决中止以防误引导"

    return {
        "verdict": verdict,
        "boot_slot": boot_slot,
        "boot_version": boot_version,
        "persisted_active_slot": device["active_slot"],
        "consistent": verdict == "boot" and boot_slot == device["active_slot"],
        "generation": device["generation"],
        "checks": checks,
        "summary": summary,
        "decided_at": utcnow(),
    }
