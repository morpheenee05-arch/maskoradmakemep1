"""Self-test: custom P1 script templates (no Telegram / browser)."""
import json
import tempfile
from pathlib import Path


def main():
    import config
    tmp = Path(tempfile.mkdtemp(prefix="script_tpl_"))
    config.SCRIPT_TEMPLATES_FILE = tmp / "script_templates.json"

    from audio.script_profiles import (
        DEFAULT_PROFILE,
        SCRIPT_PROFILES,
        normalize_profile,
        profile_from_bundle,
        resolve_profile,
        serialize_bundle,
    )
    from storage import script_templates as st

    st.SCRIPT_TEMPLATES_FILE = config.SCRIPT_TEMPLATES_FILE

    assert "suspicious" in SCRIPT_PROFILES
    built = resolve_profile("suspicious")
    assert built["intro"][1]
    bundle = serialize_bundle("suspicious", built)
    assert bundle["intro"] == built["intro"][1]
    restored = profile_from_bundle(bundle)
    assert restored["intro"][1] == built["intro"][1]
    print("PASS builtin resolve / bundle round-trip")

    key = st.slugify("Bank Alert!!", set())
    assert key == "bank_alert"
    clash = st.slugify("suspicious", set())
    assert clash.startswith("custom_")
    print("PASS slugify")

    import asyncio

    async def _crud():
        new_key = await st.add_custom(
            "Bank Alert",
            "Hello bank intro press 1 or 2",
            "You pressed one",
            "You pressed two",
            "No one there",
            "Please press 1 or 2",
        )
        assert new_key
        profile = await st.get_profile_async(new_key)
        assert profile and not profile["builtin"]
        assert "intro" in profile
        assert await st.set_active(new_key)
        assert st.get_active_key_sync() == new_key
        resolved = resolve_profile(new_key)
        assert "Hello bank intro" in resolved["intro"][1]
        assert resolved["intro"][0].endswith(".pcm")
        assert await st.update_custom(new_key, intro="Changed intro press 1")
        resolved2 = resolve_profile(new_key)
        assert resolved2["intro"][1] == "Changed intro press 1"
        assert resolved2["intro"][0] != resolved["intro"][0]
        copy_key = await st.duplicate(new_key)
        assert copy_key and copy_key != new_key
        assert not await st.delete_custom("suspicious")
        assert await st.delete_custom(new_key)
        assert st.get_active_key_sync() == DEFAULT_PROFILE
        catalog = st.list_catalog()
        keys = [r["key"] for r in catalog]
        assert "suspicious" in keys and "coinbase" in keys
        print("PASS custom CRUD + active + cache filename changes with text")

    asyncio.run(_crud())
    raw = json.loads(config.SCRIPT_TEMPLATES_FILE.read_text(encoding="utf-8"))
    assert "custom" in raw
    print("\nALL SCRIPT TEMPLATE TESTS PASSED")


if __name__ == "__main__":
    main()
