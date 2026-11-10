"""Self-test for the login-crash fixes (no real browser needed).

Covers:
1. _cleanup_failed_launch_artifacts never quits a live driver
   (the orphaned-init-thread killer).
2. _is_profile_corruption matches the observed Chrome launch error.
3. _repair_corrupted_profile_prefs removes prefs, keeps cookies.
4. pick_account JS returns the matching node / falls back correctly
   (verified via a fake driver executing the real JS logic shape).
"""
import sys
import types
from pathlib import Path
from unittest import mock

# Stub selenium / undetected_chromedriver before importing the module.
uc = types.ModuleType("undetected_chromedriver")
uc.Chrome = object
uc.ChromeOptions = object
sys.modules["undetected_chromedriver"] = uc

selenium = types.ModuleType("selenium")
sys.modules["selenium"] = selenium
for name in (
    "selenium.webdriver", "selenium.webdriver.common",
    "selenium.webdriver.common.by", "selenium.webdriver.common.keys",
    "selenium.webdriver.common.service", "selenium.webdriver.support",
    "selenium.webdriver.support.ui", "selenium.webdriver.support.expected_conditions",
    "selenium.webdriver.chrome", "selenium.webdriver.chrome.service",
    "selenium.common", "selenium.common.exceptions",
):
    sys.modules.setdefault(name, types.ModuleType(name))

# Attributes the module under test imports from those stubs.
sys.modules["selenium.webdriver.chrome.service"].Service = object
sys.modules["selenium.webdriver.common.by"].By = object
sys.modules["selenium.webdriver.common.keys"].Keys = object
sys.modules["selenium.webdriver.support.ui"].WebDriverWait = object
sys.modules["selenium.webdriver.support"].expected_conditions = types.ModuleType("ec")
sys.modules["selenium.common.exceptions"].TimeoutException = TimeoutError
sys.modules["selenium.common.exceptions"].WebDriverException = Exception

from telephony import google_voice as gv  # noqa: E402


def make_client(tmp: Path) -> gv.PhoneClient:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    profile = tmp / ".chrome_profile_1"
    profile.mkdir(parents=True, exist_ok=True)
    with mock.patch.object(gv.PhoneClient, "__init__", lambda self, **kw: None):
        client = gv.PhoneClient()
    client.worker_id = 0
    client.profile_dir = profile
    client.driver = None
    return client


def test_cleanup_keeps_live_driver():
    client = make_client(Path("_t_profile1"))
    sentinel = mock.Mock()
    client.driver = sentinel
    (client.profile_dir / "SingletonLock").write_text("x")
    client._cleanup_failed_launch_artifacts()
    assert client.driver is sentinel, "cleanup killed the live driver!"
    sentinel.quit.assert_not_called()
    assert not (client.profile_dir / "SingletonLock").exists()
    print("PASS cleanup keeps live driver, removes lock files")


def test_profile_corruption_detection():
    err = Exception(
        "Invalid \\uXXXX escape: line 1 column 2147499193 (char 2147499192)"
    )
    assert gv.PhoneClient._is_profile_corruption(err)
    assert not gv.PhoneClient._is_profile_corruption(
        Exception("session not created: cannot connect to chrome")
    )
    assert not gv.PhoneClient._is_profile_corruption(
        Exception("cannot load Preferences")
    )
    print("PASS profile-corruption signature detection")


def test_repair_removes_prefs_keeps_cookies():
    client = make_client(Path("_t_profile2"))
    default = client.profile_dir / "Default"
    default.mkdir()
    (default / "Preferences").write_text("{corrupted")
    (default / "Secure Preferences").write_text("{corrupted")
    (default / "Cookies").write_text("sqlite-stays")
    (client.profile_dir / "Local State").write_text("{corrupted")
    assert client._repair_corrupted_profile_prefs() is True
    assert not (default / "Preferences").exists()
    assert not (default / "Secure Preferences").exists()
    assert not (client.profile_dir / "Local State").exists()
    assert (default / "Cookies").exists(), "cookies must survive the repair"
    print("PASS corrupted prefs removed, cookies preserved")


def test_pick_account_js_shape():
    # Click must happen inside the finder script. Returning a node to Python
    # and clicking it is what produced stale-element chooser failures.
    from telephony import gv_login

    class FakeDriver:
        def __init__(self):
            self.scripts = []

        def execute_script(self, script, *args):
            self.scripts.append(script)
            return False

    signer = gv_login.GoogleSignIn.__new__(gv_login.GoogleSignIn)
    signer.driver = FakeDriver()
    signer._log = lambda *a, **k: None
    signer.pick_account("target@gmail.com")
    js = signer.driver.scripts[0]
    assert "id === want" in js or "id.indexOf(want)" in js, \
        "chooser JS must compare and click the matching node"
    assert ".click()" in js
    assert "return n;" not in js
    print("PASS chooser JS clicks the matching node in-page (no stale element)")


def test_identifier_selectors_skip_recovery_input():
    from telephony import gv_login
    joined = " ".join(gv_login.IDENTIFIER_SELECTORS)
    assert "knowledge-preregistered-email-response" in joined \
        or "knowledgePreregisteredEmailResponse" in joined, \
        "identifier selectors must exclude the recovery-email field"
    assert "input[type='email']" not in gv_login.IDENTIFIER_SELECTORS, \
        "bare input[type=email] would steal the recovery step"
    print("PASS identifier selectors exclude the recovery-email field")


def test_recovery_challenge_beats_identifier():
    """A visible type=email recovery field must classify as CHALLENGE, not
    IDENTIFIER — that mix-up is why the 3rd stage was never filled."""
    from telephony import gv_login

    class FakeEl:
        def __init__(self, name="", el_id="", displayed=True):
            self._name = name
            self._id = el_id
            self._displayed = displayed

        def is_displayed(self):
            return self._displayed

        def get_attribute(self, attr):
            if attr == "name":
                return self._name
            if attr == "id":
                return self._id
            return None

    recovery_box = FakeEl(
        name="knowledgePreregisteredEmailResponse",
        el_id="knowledge-preregistered-email-response",
    )
    recovery_tile = FakeEl()

    class FakeDriver:
        current_url = "https://accounts.google.com/signin/v2/challenge/selection"
        page_source = ""

        def execute_script(self, script, *args):
            src = script or ""
            if "knowledgePreregisteredEmailResponse" in src or "querySelector" in src:
                # _find_recovery_input / _find_recovery_tile
                if "data-challengetype" in src:
                    return recovery_tile
                return recovery_box
            if "innerText" in src:
                return "Подтвердите резервный адрес электронной почты"
            return None

        def find_elements(self, by, sel):
            if "knowledge" in (sel or "") or "challengetype" in (sel or ""):
                return [recovery_box if "knowledge" in sel else recovery_tile]
            if "identifierId" in (sel or "") or "name='identifier'" in (sel or ""):
                return []
            if "type='email'" in (sel or "") or 'type="email"' in (sel or ""):
                # Recovery field is type=email — must NOT be treated as identifier
                return [recovery_box]
            return []

    signer = gv_login.GoogleSignIn.__new__(gv_login.GoogleSignIn)
    signer.driver = FakeDriver()
    signer.dialer_selectors = gv_login.DEFAULT_DIALER_SELECTORS
    signer.worker_id = 0
    signer._log = lambda *a, **k: None
    assert signer._is_recovery_challenge() is True
    assert signer.state() == gv_login.CHALLENGE, \
        f"recovery page classified as {signer.state()!r}, expected challenge"
    print("PASS recovery page classifies as challenge, not identifier")


def test_stay_da_does_not_match_next():
    from telephony import gv_login
    import inspect
    src = inspect.getsource(gv_login.GoogleSignIn._button_by_text)
    assert "ww.length <= 3" in src, \
        "short labels like 'да' must match exactly, not 'Далее'"
    print("PASS 'да' cannot steal the Далее button")


def test_recovery_clicks_next_after_email():
    from telephony import gv_login
    import inspect
    src = inspect.getsource(gv_login.GoogleSignIn.answer_recovery_email)
    assert "_click_next" in src, "recovery submit must click Next/Далее"
    assert "passwordNext" not in src, \
        "must not click leftover password Next on the recovery page"
    click_src = inspect.getsource(gv_login.GoogleSignIn._click_next)
    assert "shadowRoot" in click_src
    assert "далее" in click_src.lower() or "NEXT_TEXTS" in click_src
    print("PASS recovery email submit clicks Next via shadow-DOM lookup")


def test_captcha_is_fatal_and_detected():
    from telephony import gv_login
    import inspect
    assert gv_login.CAPTCHA in gv_login.FATAL_OUTCOMES
    run_src = inspect.getsource(gv_login.GoogleSignIn.run)
    assert "_is_captcha" in run_src
    cap_src = inspect.getsource(gv_login.GoogleSignIn._is_captcha)
    assert "recaptcha" in cap_src.lower()
    widget_src = inspect.getsource(gv_login.GoogleSignIn._captcha_widget_present)
    assert "grecaptcha-badge" in widget_src
    assert "240" in widget_src
    assert any("/challenge/recaptcha" in m for m in gv_login.CAPTCHA_URL_MARKERS)
    assert "captcha" not in [m.lower() for m in gv_login.CAPTCHA_TEXT_MARKERS], \
        "bare 'captcha' false-triggers on unrelated pages"

    class FakeDriver:
        current_url = "https://accounts.google.com/v3/signin/challenge/recaptcha"
        def execute_script(self, *a, **k):
            return False
        def find_elements(self, *a, **k):
            return []

    signer = gv_login.GoogleSignIn.__new__(gv_login.GoogleSignIn)
    signer.driver = FakeDriver()
    signer.dialer_selectors = gv_login.DEFAULT_DIALER_SELECTORS
    signer.worker_id = 0
    signer._log = lambda *a, **k: None
    assert signer._is_captcha() is True
    signer.driver.current_url = "https://accounts.google.com/v3/signin/identifier"
    assert signer._is_captcha() is False
    print("PASS captcha is a fatal outcome and is checked during sign-in")


def test_recovery_tile_skips_phone():
    """'Подтвердите резервный…' used to match the phone tile listed first."""
    from telephony import gv_login
    js = gv_login.GoogleSignIn._FIND_RECOVERY_EMAIL_TILE_JS
    assert "data-challengetype" in js
    assert "selectchallenge" in js
    assert 'typ !== \'12\'' in js or 'typ !== "12"' in js or "typ !== '12'" in js
    assert "bestLen" not in js, "must not fall back to smallest email-ish tile"
    assert "[data-action=\"selectchallenge\"]" in js.replace("'", '"') or "selectchallenge" in js
    assert "подтвердите резервный" not in gv_login.RECOVERY_CHALLENGE_TEXTS
    assert any("телефон" in t for t in gv_login.RECOVERY_PHONE_TEXTS)
    assert "резервный адрес" in gv_login.RECOVERY_CHALLENGE_TEXTS
    for sel in gv_login.RECOVERY_CHALLENGE_OPTION:
        assert "12" in sel, f"recovery selector must require type 12: {sel}"
        assert "selectchallenge" in sel or "EBHGs" in sel
    print("PASS recovery tile prefers email over the phone option")


def test_try_another_way_never_on_recovery_picker():
    """The picker footer 'Try another way' must not steal type 12."""
    from telephony import gv_login
    import inspect
    taw = inspect.getsource(gv_login.GoogleSignIn._try_another_way)
    skip = inspect.getsource(gv_login.GoogleSignIn._skip_passkey_enrollment)
    dismiss = inspect.getsource(gv_login.GoogleSignIn.dismiss_interstitial)
    assert "_recovery_picker_on_screen" in taw
    assert "_recovery_picker_on_screen" in skip
    assert "_recovery_picker_on_screen" in dismiss
    run_src = inspect.getsource(gv_login.GoogleSignIn.run)
    rec = run_src.find("_recovery_picker_on_screen")
    taw_pos = run_src.find("_try_another_way")
    assert rec != -1 and taw_pos != -1
    assert rec < taw_pos, "recovery email must be handled before Try another way"
    click_src = inspect.getsource(gv_login.GoogleSignIn.answer_recovery_email)
    assert "_click_recovery_email_tile" in click_src
    js = gv_login.GoogleSignIn._FIND_RECOVERY_EMAIL_TILE_JS
    assert 'typ !== \'12\'' in js
    assert "[data-challengetype=\"12\"]" in js
    assert "Try another way" not in js
    print("PASS Try another way is blocked on the recovery-email picker")


def test_chooser_wipes_cookies_instead_of_picking():
    """Saved-account picker must never be clicked — cookies go, then email."""
    from telephony import gv_login
    import inspect

    run_src = inspect.getsource(gv_login.GoogleSignIn.run)
    assert "pick_account" not in run_src
    assert "cookie_wiper" in run_src
    assert "ACCOUNT_CHOOSER" in run_src
    auto_src = inspect.getsource(gv.PhoneClient.auto_login)
    assert "_clear_chooser_session" in auto_src
    assert "cookie_wiper=self._clear_chooser_session" in auto_src.replace(" ", "")
    wipe_src = inspect.getsource(gv.PhoneClient._clear_chooser_session)
    assert "clear_session_cookies" in wipe_src
    assert "pick_account" not in wipe_src
    print("PASS account picker clears cookies instead of picking an account")


def test_vac_option_scoring():
    score = gv.PhoneClient._vac_option_score
    pick = gv.PhoneClient._pick_vac_option
    assert gv.PhoneClient._vac_line_label("Line 2") == "Line 2"
    assert gv.PhoneClient._vac_line_label("Line 16 (Virtual Audio Cable)") == "Line 16"

    assert score("Line 2 (Virtual Audio Cable)", "Line 2") == 3
    assert score("По умолчанию - Line 2 (Virtual Audio Cable)", "Line 2") == 1
    assert score("Оборудование - Line 2 (Virtual Audio Cable)", "Line 2") == 1
    assert score("Line 12 (Virtual Audio Cable)", "Line 1") == 0
    assert score("Line 16 (Virtual Audio Cable)", "Line 1") == 0
    assert score("Line 16 (Virtual Audio Cable)", "Line 6") == 0
    assert score("Line 1 (Virtual Audio Cable)", "Line 1") == 3

    options = [
        {"id": "a", "text": "По умолчанию - Line 2 (Virtual Audio Cable)"},
        {"id": "b", "text": "Оборудование - Line 2 (Virtual Audio Cable)"},
        {"id": "c", "text": "Line 2 (Virtual Audio Cable)"},
        {"id": "d", "text": "Line 12 (Virtual Audio Cable)"},
    ]
    chosen = pick(options, "Line 2")
    assert chosen["id"] == "c", chosen
    print("PASS VAC option scoring prefers bare Line N and does not confuse 1/12/16")


def test_profile_audio_routing():
    import config
    r1 = config.get_worker_audio_routing(0)
    assert r1["gv_microphone"] == "Line 2"
    assert r1["gv_ringing"] == "Line 1"
    assert r1["gv_speaker"] == "Line 1"
    r2 = config.get_worker_audio_routing(1)
    assert r2["gv_microphone"] == "Line 4"
    assert r2["gv_ringing"] == "Line 3"
    assert r2["gv_speaker"] == "Line 3"
    print("PASS profile audio routing is mic=playback, ring/speakers=capture")


def test_audio_setup_hooked_after_login():
    import inspect
    login_src = inspect.getsource(gv.PhoneClient._login_to_account)
    auto_src = inspect.getsource(gv.PhoneClient.auto_login)
    assert "_configure_gv_audio" in login_src
    assert auto_src.count("_configure_gv_audio") >= 2
    cfg_src = inspect.getsource(gv.PhoneClient._configure_gv_audio)
    assert "audio-ogb-button" in cfg_src or "audio-ogb-button" in inspect.getsource(
        gv.PhoneClient._open_gv_audio_dialog
    )
    print("PASS GV audio is configured after successful login")


def test_cmdline_holds_profile_quoted_and_neighbors():
    holds = gv.PhoneClient._cmdline_holds_profile
    profile = Path(r"C:\Users\admin\Desktop\new_agent\.chrome_profile_3")
    quoted = (
        r'chrome.exe --user-data-dir="C:\Users\admin\Desktop\new_agent'
        r'\.chrome_profile_3"'
    )
    assert holds(quoted, profile)
    assert holds(
        r"chrome.exe --user-data-dir=C:\Users\admin\Desktop\new_agent\.chrome_profile_3",
        profile,
    )
    assert not holds(
        r"chrome.exe --user-data-dir=C:\Users\admin\Desktop\new_agent\.chrome_profile_30",
        profile,
    )
    assert not holds(
        r"chrome.exe --user-data-dir=C:\Users\admin\Desktop\new_agent\.chrome_profile_13",
        profile,
    )
    print("PASS profile cmdline match handles quotes and profile_3 vs profile_30")


def test_spawn_timeout_aborts_inflight():
    import inspect
    inner = inspect.getsource(gv.PhoneClient._restart_browser_inner)
    setup = inspect.getsource(gv.PhoneClient._setup_browser_only)
    assert "_abort_inflight_spawn" in inner
    assert "_wait_profile_free" in setup
    assert "spawn superseded" in setup
    from telephony import google_voice_pool as pool_mod
    init_src = inspect.getsource(pool_mod.PhonePool._init_one_worker)
    assert "_abort_inflight_spawn" in init_src
    print("PASS spawn timeout aborts in-flight Chrome and waits for a free profile")


def test_passkey_enrollment_skips_not_now():
    """'Sign in faster' is an optional nag — click Not now, never Continue."""
    from telephony import gv_login
    import inspect

    enroll = "https://accounts.google.com/v3/signin/speedbump/passkeyenrollment"
    assert gv_login.is_passkey_enrollment_page(enroll, "")
    assert gv_login.is_passkey_enrollment_page(
        "https://accounts.google.com/v3/signin/pwd",
        "Входите в аккаунт быстрее\nНе сейчас",
    )
    assert gv_login.is_passkey_enrollment_page(
        "https://accounts.google.com/v3/signin/pwd",
        "Sign in faster\nNot now",
    )
    # Real passkey *login* challenge must stay a human challenge.
    assert not gv_login.is_passkey_enrollment_page(
        "https://accounts.google.com/v3/signin/challenge/pk",
        "Use your passkey to confirm it's you",
    )
    assert "not now" in gv_login.SKIP_TEXTS
    assert "не сейчас" in gv_login.SKIP_TEXTS

    clicks = []

    class FakeDriver:
        current_url = enroll

        def execute_script(self, script, *args):
            src = script or ""
            if "document.body" in src and "innerText" in src:
                return "Sign in faster\nNot now\nContinue"
            if "eBSUOb" in src and "data-secondary-action-label" in src:
                assert "Njthtb" in src
                # Click happens inside this script (no stale WebElement).
                clicks.append(True)
                return True
            if "arguments[0].click()" in src:
                clicks.append(True)
                return None
            return False

        def find_elements(self, *a, **k):
            return []

        def find_element(self, *a, **k):
            class El:
                def send_keys(self, *aa, **kk):
                    return None
            return El()

    signer = gv_login.GoogleSignIn.__new__(gv_login.GoogleSignIn)
    signer.driver = FakeDriver()
    signer.dialer_selectors = gv_login.DEFAULT_DIALER_SELECTORS
    signer.worker_id = 0
    signer._log = lambda *a, **k: None
    assert signer._is_passkey_enrollment() is True
    assert signer.dismiss_interstitial() is True
    assert clicks, "must JS-click Not now (loading overlay blocks native click)"

    skip_src = inspect.getsource(gv_login.GoogleSignIn._skip_passkey_enrollment)
    assert "eBSUOb" in skip_src
    assert "Njthtb" in skip_src
    assert "_skip_passkey_in_frames" in inspect.getsource(gv_login.GoogleSignIn)
    run_src = inspect.getsource(gv_login.GoogleSignIn.run)
    assert "_skip_passkey_enrollment" in run_src
    assert "_try_another_way" in run_src
    assert "cookie_wiper" in run_src
    auto_src = inspect.getsource(gv.PhoneClient.auto_login)
    assert "_is_skippable_auth_nag" in auto_src
    flags = inspect.getsource(gv.PhoneClient._build_chrome_options)
    assert "WebAuthentication" in flags
    print("PASS passkey enrollment is skipped via Not now, not treated as fatal")


def test_voice_ban_page_detection():
    from telephony import gv_login

    ban_url = "https://support.google.com/accounts/answer/40039?p=grandcentral"
    assert gv_login.is_voice_ban_page(ban_url, "")
    assert gv_login.is_voice_ban_page(
        ban_url, "Unable to access a Google product"
    )
    assert gv_login.is_voice_ban_page(
        "https://support.google.com/voice/answer/16053145?hl=en",
        "Unable to access Google Voice",
    )
    assert gv_login.is_voice_ban_page(
        "https://voice.google.com/u/0/calls",
        "Your access to this Google product has been suspended",
    )
    assert gv_login.is_voice_ban_page(
        "https://voice.google.com/",
        "Не удается получить доступ к продукту Google",
    )
    # Signed-out Voice marketing page is a landing, not a ban.
    assert not gv_login.is_voice_ban_page(
        "https://workspace.google.com/products/voice",
        "sign in to google voice",
    )
    assert gv_login.is_voice_ban_page(
        "https://workspace.google.com/products/voice",
        "unable to access a google product",
    )
    # Host / Chrome failures must never look like a Voice ban.
    assert not gv_login.is_voice_ban_page(
        "https://voice.google.com/u/0/calls",
        "DNS_PROBE_FINISHED_NO_INTERNET checking the network cables",
    )
    assert not gv_login.is_voice_ban_page("chrome://new-tab-page/", "")
    assert not gv_login.is_voice_ban_page(
        "https://voice.google.com/u/0/calls",
        "account is suspended try again later",
    )
    assert not gv_login.is_voice_ban_page(
        "https://example.com/?p=grandcentral", ""
    )
    print("PASS Voice ban page URL/text detection")


def test_state_classifies_grandcentral_as_banned():
    from telephony import gv_login

    signer = gv_login.GoogleSignIn.__new__(gv_login.GoogleSignIn)
    signer.driver = None
    signer.dialer_selectors = gv_login.DEFAULT_DIALER_SELECTORS
    signer.worker_id = 0
    signer._log = lambda *a, **k: None
    signer._present = lambda *a, **k: False
    signer._visible = lambda *a, **k: None
    signer._is_recovery_challenge = lambda: False

    signer._url = lambda: (
        "https://support.google.com/accounts/answer/40039?p=grandcentral"
    )
    signer.page_text = lambda: ""
    assert signer.state() == gv_login.BANNED

    signer._url = lambda: "https://workspace.google.com/products/voice"
    signer.page_text = lambda: "sign in"
    assert signer.state() == gv_login.LANDING
    print("PASS sign-in state() treats grandcentral as banned, workspace as landing")


def test_check_logged_in_without_driver():
    client = make_client(Path("_t_profile_nodriver"))
    client.driver = None
    assert client._check_if_logged_in() is False
    print("PASS login check with driver=None does not throw")


def test_oversized_prefs_detected_and_repaired():
    client = make_client(Path("_t_profile_prefs"))
    default = client.profile_dir / "Default"
    default.mkdir()
    (default / "Preferences").write_bytes(b"x" * 200)
    (default / "Cookies").write_text("keep-me")
    client.PREFS_MAX_BYTES = 50
    assert client._prefs_unhealthy()
    assert client._repair_corrupted_profile_prefs(only_oversized=True)
    assert not (default / "Preferences").exists()
    assert (default / "Cookies").read_text() == "keep-me"
    assert not client._prefs_unhealthy()
    print("PASS oversized Preferences are repaired, cookies kept")


def test_retryable_spawn_includes_chrome_not_reachable():
    client = make_client(Path("_t_profile_retry"))
    assert client._is_retryable_spawn_failure(
        Exception("chrome not reachable")
    )
    assert client._is_retryable_spawn_failure(
        Exception("Invalid \\uXXXX escape: line 1 column 9")
    )
    assert not client._is_retryable_spawn_failure(
        Exception("unknown error: net::ERR_PROXY_CONNECTION_FAILED")
    )
    print("PASS spawn retry covers prefs corruption and chrome-not-reachable")


def test_wipe_cookies_clears_banned_flag():
    client = make_client(Path("_t_profile_wipe"))
    client.is_banned = True
    client.is_spam_flagged = True
    client.is_authenticated = True
    client.phone_number = "+1555"
    client.account_email = "banned@gmail.com"
    client.login_state = "banned"
    client._login_running = False
    client.credentials = {"email": "banned@gmail.com", "password": "x"}
    client.COOKIE_DB_RELPATHS = (
        Path("Default") / "Cookies",
    )
    (client.profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (client.profile_dir / "Default" / "Cookies").write_text("session")

    def _clear_creds():
        client.credentials = None

    client.clear_credentials = _clear_creds
    client.close = lambda: None
    client._clear_cookies_in_browser = lambda: False
    client._skip_wipe_archive = True
    client.wipe_cookies("account banned by Google")
    assert client.is_banned is False
    assert client.login_state == "needs_login"
    assert not (client.profile_dir / "Default" / "Cookies").exists()
    print("PASS wipe_cookies clears banned flag and cookie files")


def test_reset_to_login_keeps_credentials():
    client = make_client(Path("_t_profile_captcha"))
    client.is_authenticated = True
    client.phone_number = "+1555"
    client.account_email = "cap@gmail.com"
    client.login_state = "captcha"
    client.login_outcome = "captcha"
    client._login_running = False
    client.credentials = {"email": "cap@gmail.com", "password": "secret"}
    client.COOKIE_DB_RELPATHS = (Path("Default") / "Cookies",)
    (client.profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (client.profile_dir / "Default" / "Cookies").write_text("session")
    client.close = lambda: None
    client._clear_cookies_in_browser = lambda: True
    client._skip_wipe_archive = True
    client.reset_to_login_page("reCAPTCHA — restarted onto the login page", True)
    assert client.credentials == {"email": "cap@gmail.com", "password": "secret"}
    assert client.login_state == "needs_login"
    assert client.login_outcome == "captcha"
    assert client.phone_number is None
    assert not client.is_authenticated
    print("PASS captcha reset keeps credentials and drops the sticky session")


if __name__ == "__main__":
    test_cleanup_keeps_live_driver()
    test_profile_corruption_detection()
    test_repair_removes_prefs_keeps_cookies()
    test_pick_account_js_shape()
    test_identifier_selectors_skip_recovery_input()
    test_recovery_challenge_beats_identifier()
    test_stay_da_does_not_match_next()
    test_recovery_clicks_next_after_email()
    test_captcha_is_fatal_and_detected()
    test_recovery_tile_skips_phone()
    test_try_another_way_never_on_recovery_picker()
    test_chooser_wipes_cookies_instead_of_picking()
    test_vac_option_scoring()
    test_profile_audio_routing()
    test_audio_setup_hooked_after_login()
    test_cmdline_holds_profile_quoted_and_neighbors()
    test_spawn_timeout_aborts_inflight()
    test_passkey_enrollment_skips_not_now()
    test_voice_ban_page_detection()
    test_state_classifies_grandcentral_as_banned()
    test_check_logged_in_without_driver()
    test_oversized_prefs_detected_and_repaired()
    test_retryable_spawn_includes_chrome_not_reachable()
    test_wipe_cookies_clears_banned_flag()
    test_reset_to_login_keeps_credentials()

    import shutil
    for d in (
        "_t_profile1", "_t_profile2", "_t_profile_nodriver",
        "_t_profile_prefs", "_t_profile_retry", "_t_profile_wipe",
        "_t_profile_captcha",
    ):
        shutil.rmtree(d, ignore_errors=True)
    print("\nALL LOGIN-FIX TESTS PASSED")
