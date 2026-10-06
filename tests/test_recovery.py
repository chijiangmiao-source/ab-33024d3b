"""恢复裁决单元测试：仅摘要完整且已确认的清单具备引导资格，且必须唯一。"""
from app.recovery import adjudicate
from app.store import sha256_hex

DEVICE = {"id": "d1", "name": "d", "generation": 1, "active_slot": "A",
          "created_at": "t"}


def mk_slot(slot, state, version=None, gen=None, image=b"img", corrupt=False):
    stored = image + b"-torn" if corrupt else image
    return {
        "slot": slot,
        "state": state,
        "version": version,
        "expected_digest": sha256_hex(image) if image is not None else None,
        "image": stored,
        "confirmed_generation": gen,
        "diagnostics": None,
        "updated_at": "t",
    }


def test_single_confirmed_slot_boots():
    report = adjudicate(DEVICE, [
        mk_slot("A", "confirmed", version=1, gen=1),
        mk_slot("B", "empty", image=None),
    ])
    assert report["verdict"] == "boot"
    assert report["boot_slot"] == "A"
    assert report["boot_version"] == 1
    assert report["consistent"] is True


def test_unconfirmed_candidate_never_boots():
    for stage in ("written", "verified"):
        report = adjudicate(DEVICE, [
            mk_slot("A", "confirmed", version=1, gen=1),
            mk_slot("B", stage, version=2),
        ])
        assert report["verdict"] == "boot"
        assert report["boot_slot"] == "A"
        check_b = next(c for c in report["checks"] if c["slot"] == "B")
        assert check_b["eligible"] is False
        assert "未确认" in check_b["reason"]


def test_corrupt_candidate_kept_as_evidence_not_bootable():
    report = adjudicate(DEVICE, [
        mk_slot("A", "confirmed", version=1, gen=1),
        mk_slot("B", "corrupt", version=2, corrupt=True),
    ])
    assert report["boot_slot"] == "A"
    check_b = next(c for c in report["checks"] if c["slot"] == "B")
    assert check_b["eligible"] is False
    assert check_b["digest_ok"] is False
    assert "诊断证据" in check_b["reason"]


def test_no_rollback_to_superseded_slot():
    # 新版本确认后，即使其镜像受损，也绝不回退到已被取代的旧清单
    device = {**DEVICE, "generation": 2, "active_slot": "B"}
    report = adjudicate(device, [
        mk_slot("A", "superseded", version=1, gen=1),
        mk_slot("B", "confirmed", version=2, gen=2, corrupt=True),
    ])
    assert report["verdict"] == "no_boot"
    assert report["boot_slot"] is None
    check_a = next(c for c in report["checks"] if c["slot"] == "A")
    assert check_a["eligible"] is False
    assert "禁止回退" in check_a["reason"]


def test_after_confirm_new_slot_is_unique_bootable():
    device = {**DEVICE, "generation": 2, "active_slot": "B"}
    report = adjudicate(device, [
        mk_slot("A", "superseded", version=1, gen=1),
        mk_slot("B", "confirmed", version=2, gen=2),
    ])
    assert report["verdict"] == "boot"
    assert report["boot_slot"] == "B"
    assert report["boot_version"] == 2


def test_two_confirmed_slots_is_ambiguous_and_does_not_boot():
    report = adjudicate(DEVICE, [
        mk_slot("A", "confirmed", version=1, gen=1),
        mk_slot("B", "confirmed", version=2, gen=2),
    ])
    assert report["verdict"] == "ambiguous"
    assert report["boot_slot"] is None


def test_no_eligible_slot_means_no_boot():
    report = adjudicate(DEVICE, [
        mk_slot("A", "corrupt", version=1, gen=1, corrupt=True),
        mk_slot("B", "empty", image=None),
    ])
    assert report["verdict"] == "no_boot"
