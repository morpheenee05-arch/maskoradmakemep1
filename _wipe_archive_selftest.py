"""Self-test: wipe archive prune + snapshot without a real browser."""
import json
import tempfile
from pathlib import Path


def main():
    import config
    tmp = Path(tempfile.mkdtemp(prefix="wipe_hist_"))
    config.WIPE_HISTORY_DIR = str(tmp)
    config.GV_WIPE_HISTORY_KEEP = 3

    from telephony import wipe_archive
    from telephony.google_voice import PhoneClient
    import inspect

    wipe_src = inspect.getsource(PhoneClient.wipe_cookies)
    reset_src = inspect.getsource(PhoneClient.reset_to_login_page)
    assert "snapshot_worker" in wipe_src
    assert "snapshot_worker" in reset_src
    print("PASS wipe/reset take a snapshot before cookies go")

    class FakeWorker:
        worker_id = 2
        account_email = "gone@gmail.com"
        credential_email = "gone@gmail.com"
        phone_number = "+15550001111"
        profile_dir = Path("chrome_profile_3")
        login_state = "banned"
        login_outcome = "disabled"
        login_detail = "Google disabled this account"
        last_error = None
        driver = None

    ev = wipe_archive.snapshot_worker(
        FakeWorker(), "account banned by Google (gone@gmail.com)"
    )
    assert ev is not None
    assert ev["kind"] == "banned"
    assert ev["email"] == "gone@gmail.com"
    assert ev["screenshot"] is None
    events = wipe_archive.list_events(limit=5)
    assert events and events[0]["email"] == "gone@gmail.com"
    print("PASS snapshot without browser still records the reason")

    # Fake PNGs to exercise prune.
    for i in range(6):
        p = tmp / f"wipe_2020010{i}_w0_spam_x.png"
        p.write_bytes(b"png" + bytes([i]))
        p.touch()
    wipe_archive.prune(tmp)
    left = list(tmp.glob("wipe_*.png"))
    assert len(left) <= 3, left
    print("PASS prune keeps only the newest screenshots")

    idx = json.loads((tmp / "index.json").read_text(encoding="utf-8"))
    assert isinstance(idx, list)

    assert wipe_archive.screenshot_file("../secret.png") is None
    assert wipe_archive.screenshot_file("wipe_x.png") is None  # missing file
    real = left[0]
    got = wipe_archive.screenshot_file(real.name)
    assert got is not None and got.name == real.name
    print("PASS screenshot_file rejects traversal and missing names")

    from bot.commands.wipes import _flatten, _group_accounts, _identity, _apply_filter

    reports = {
        "BtcBeer-3": {
            "events": [
                {
                    "ts": 2, "kind": "banned", "email": "a@gmail.com",
                    "phone": "+1", "profile": ".chrome_profile_2",
                    "worker_id": 1, "reason": "banned",
                },
                {
                    "ts": 1, "kind": "expired", "email": "a@gmail.com",
                    "worker_id": 1, "reason": "session",
                },
            ]
        },
        "Box-2": {
            "events": [
                {
                    "ts": 3, "kind": "captcha", "email": "b@gmail.com",
                    "worker_id": 0, "reason": "captcha",
                }
            ]
        },
    }
    rows = _flatten(reports, set())
    assert [r["_agent"] for r in rows] == ["Box-2", "BtcBeer-3", "BtcBeer-3"]
    assert _identity(rows[1]) == ("email", "a@gmail.com")
    groups = _group_accounts(rows)
    assert len(groups) == 2
    assert groups[1]["label"] == "a@gmail.com"
    assert len(groups[1]["events"]) == 2
    assert len(_apply_filter(rows, "banned")) == 1
    print("PASS archive grouping by server / account / cause")

    print("\nALL WIPE ARCHIVE TESTS PASSED")


if __name__ == "__main__":
    main()
