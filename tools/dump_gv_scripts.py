#!/usr/bin/env python3
"""
Dump every JS source loaded by the live Google Voice page on an agent.

Runs ON AN AGENT (Windows RDP with a logged-in worker Chrome profile),
same profile convention as agent_sip_test.py:

    py -3 tools/dump_gv_scripts.py --worker 0 [--needle encryptPlaceCallContext]

  1. Spawns uc.Chrome on .chrome_profile_N, opens voice.google.com
  2. Via CDP Page.getResourceTree + Page.getResourceContent downloads the
     source of every script resource the page actually loaded (incl. lazy
     chunks)
  3. Saves everything into captures/js/<ts>/, prints which files contain
     the needle (default: encryptPlaceCallContext)
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("dump_gv_scripts")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", type=int, required=True)
    ap.add_argument("--needle", default="encryptPlaceCallContext")
    ap.add_argument("--wait", type=int, default=25,
                    help="seconds to let the app finish lazy-loading")
    ap.add_argument("--visible", action="store_true")
    args = ap.parse_args()

    import config
    import undetected_chromedriver as uc

    profile_dir = config.BASE_DIR / f".chrome_profile_{args.worker}"
    if not profile_dir.exists():
        raise SystemExit(f"profile not found: {profile_dir}")

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    if not args.visible:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-first-run")
    options.add_argument("--window-size=1280,900")

    log.info("spawning Chrome on %s …", profile_dir.name)
    drv = uc.Chrome(options=options)
    outdir = Path("captures/js") / time.strftime("%Y%m%d_%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        drv.execute_cdp_cmd("Page.enable", {})
        drv.get("https://voice.google.com/u/0/calls")
        log.info("page loading, waiting %ds for lazy chunks…", args.wait)
        time.sleep(args.wait)

        tree = drv.execute_cdp_cmd("Page.getResourceTree", {})
        resources = []

        def walk(frame_tree):
            frame = frame_tree.get("frame", {})
            fid = frame.get("id")
            for res in frame_tree.get("resources", []):
                if res.get("type") == "Script":
                    resources.append((fid, res.get("url", "")))
            for child in frame_tree.get("childFrames", []) or []:
                walk(child)

        walk(tree.get("frameTree", {}))
        log.info("found %d script resources", len(resources))

        hits = []
        for i, (fid, url) in enumerate(resources):
            try:
                content = drv.execute_cdp_cmd(
                    "Page.getResourceContent", {"frameId": fid, "url": url})
            except Exception as e:
                log.warning("getResourceContent failed for %s: %s",
                            url[:100], e)
                continue
            text = content.get("content", "")
            if content.get("base64Encoded"):
                import base64
                raw = base64.b64decode(text)
                text = raw.decode("utf-8", "replace")
            fname = outdir / f"script_{i:03d}.js"
            fname.write_text(text, encoding="utf-8")
            marker = " <== NEEDLE" if args.needle in text else ""
            if marker:
                hits.append(fname)
            log.info("[%d] %8d bytes  %s%s", i, len(text), url[:110], marker)

        log.info("saved to %s", outdir)
        if hits:
            log.info("NEEDLE %r found in:", args.needle)
            for h in hits:
                log.info("  %s", h)
        else:
            log.warning("needle %r not found in any script", args.needle)
    finally:
        try:
            drv.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
