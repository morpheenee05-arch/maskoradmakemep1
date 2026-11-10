import argparse
import time
from pathlib import Path

from loguru import logger

try:
    import undetected_chromedriver as uc
except ImportError:
    print("Please run: pip install undetected-chromedriver")
    raise SystemExit(1)

import config
from telephony.uc_driver import ensure_persistent_uc_driver, resolve_uc_version_main


def configure_profile(profile_number: int):
    """Launch browser using a specific profile folder for manual login."""
    base_dir = Path(__file__).parent
    profile_dir = base_dir / f".chrome_profile_{profile_number}"
    worker_index = profile_number - 1
    routing = None
    if 0 <= worker_index < len(config.WORKER_POOL):
        routing = config.get_worker_audio_routing(worker_index)

    logger.info(f"Launching worker instance for Profile {profile_number}...")
    logger.info(f"Saving session data to: {profile_dir}")
    if routing is not None:
        logger.info(
            "Expected Google Voice audio for "
            f"Profile {profile_number}: microphone={routing['gv_microphone']} "
            f"ringing={routing['gv_ringing']} speaker={routing['gv_speaker']}"
        )

    options = uc.ChromeOptions()

    # Persistent profile to save login.
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_dir}")


    options.add_argument("--host-resolver-rules=\"MAP * ~NOTFOUND , EXCL 127.0.0.1\"")
    # Auto-allow microphone access.
    prefs = {
        "profile.default_content_setting_values.media_stream_mic": 1,
        "profile.default_content_setting_values.media_stream_camera": 1,
        "profile.default_content_setting_values.notifications": 2,
    }
    options.add_experimental_option("prefs", prefs)
    current_dir = Path(__file__).parent.absolute()
    try:
        version_main = resolve_uc_version_main()
        driver_binary = config.resolve_chromedriver_path()
        chrome_kwargs = {
            "options": options,
            "driver_executable_path": str(driver_binary),
        }
        if version_main is not None:
            chrome_kwargs["version_main"] = version_main

        driver = uc.Chrome(**chrome_kwargs)
        logger.info(
            "Browser launched "
            f"(version_main={chrome_kwargs.get('version_main', 'auto')}, "
            f"driver={driver_binary.name})."
        )

        driver.get("https://voice.google.com/u/0/calls")

        steps = [
            "1. Log into your account in the browser window.",
            "2. Navigate to the dialer page.",
        ]
        if routing is not None:
            steps.extend(
                [
                    f"3. In Google Voice settings, set Microphone = {routing['gv_microphone']}",
                    f"4. In Google Voice settings, set Ringing = {routing['gv_ringing']}",
                    f"5. In Google Voice settings, set Speakers = {routing['gv_speaker']}",
                    (
                        "6. App-side mapping for this worker is "
                        f"Capture = {routing['capture_device']}, "
                        f"Playback = {routing['playback_device']}"
                    ),
                    "7. Ensure you can see your active phone number.",
                    "8. Close this terminal window (Ctrl+C) when you are completely finished.",
                ]
            )
        else:
            steps.extend(
                [
                    "3. Ensure you can see your active phone number.",
                    "4. Close this terminal window (Ctrl+C) when you are completely finished.",
                ]
            )

        print("\n" + "=" * 80)
        print("ACTION REQUIRED")
        for step in steps:
            print(step)
        print("=" * 80 + "\n")

        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Closing browser and saving profile...")
        try:
            driver.quit()
        except Exception:
            pass
        logger.info(f"Profile {profile_number} saved successfully.")
    except Exception as e:
        logger.error(f"Failed to start browser: {e}")


if __name__ == "__main__":

    configure_profile(int(input("Profile id:")))
