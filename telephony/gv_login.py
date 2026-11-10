"""
Automated Google sign-in for Google Voice worker profiles.

A worker's Chrome profile can come up in one of three shapes, and each needs
different handling:

  * the dialer is already there       → nothing to do
  * the voice.google.com landing page → never signed in; run the sign-in form
  * anything else                     → classify; only a Voice product-ban
                                        page drops cookies. A dead Chrome or
                                        signed-out form keeps the account.

Everything here is synchronous Selenium meant to be called through
run_in_executor. Selectors deliberately avoid Google's generated class names
(`VfPpkd-LgbsSe…` changes without notice) and key off ids, name attributes and
visible text in both English and Russian, because the sign-in UI follows the
profile's locale.

Two details that are easy to get wrong and are handled explicitly:
  * the landing page's "Sign in" link carries target="_blank", so the form
    opens in a SECOND tab that has to be switched to, then folded back into a
    single tab — the rest of the codebase assumes one window.
  * the identifier page keeps a hidden password input in the DOM (and the
    password page a hidden identifier one), so which step we are on can only be
    told from element *visibility*, never from mere presence.
"""
import random
import time
from typing import Callable, Optional, Sequence

from loguru import logger
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from infra import totp as totp_mod

DIALER_URL = "https://voice.google.com/u/0/calls"
LANDING_URL = "https://voice.google.com/"

# ── Page states ───────────────────────────────────────────────────────────────
DIALER = "dialer"           # signed in, Google Voice app is up
LANDING = "landing"         # marketing page → not signed in
IDENTIFIER = "identifier"   # email step of the sign-in form
PASSWORD = "password"       # password step of the sign-in form
CHOOSER = "chooser"         # "Choose an account"
CHALLENGE = "challenge"     # 2FA / captcha / anything a human must answer
BANNED = "banned"           # Google disabled the account
UNKNOWN = "unknown"         # in transit, or a page we do not recognise

# ── Sign-in outcomes ──────────────────────────────────────────────────────────
OK = "ok"
BAD_PASSWORD = "bad_password"
UNKNOWN_EMAIL = "unknown_email"
CHALLENGE_REQUIRED = "challenge_required"
CAPTCHA = "captcha"
RATE_LIMITED = "rate_limited"
DISABLED = "disabled"
TIMEOUT = "timeout"
NO_FORM = "no_form"
NO_DIALER = "no_dialer"
ACCOUNT_CHOOSER = "account_chooser"  # saved-account picker — wipe cookies, don't click
ERROR = "error"

# Outcomes where retrying with the same credentials is pointless or harmful:
# repeating a wrong password is how an account gets locked.
FATAL_OUTCOMES = frozenset(
    {BAD_PASSWORD, UNKNOWN_EMAIL, CHALLENGE_REQUIRED, CAPTCHA, RATE_LIMITED, DISABLED}
)

# Kept in sync with PhoneClient.LOGIN_UI_SELECTORS; duplicated to avoid a
# circular import, and overridable by the caller.
DEFAULT_DIALER_SELECTORS = (
    "gv-make-call-panel",
    "gv-call-sidebar",
    "gv-call-as-banner",
    ".did-display",
    "[gv-a11y-grid]",
    "button[aria-label='Make a call']",
    "button[aria-label='Dialpad']",
)

# Text from the signed-out voice.google.com landing page.
LANDING_TEXT_MARKERS = (
    "a professional-grade phone plan",
    "phone plan that's easy to use",
    "stay connected and save time",
    "flexible cloud phone system",
)

SIGNIN_LINK_SELECTORS = (
    "a[aria-label*='Sign in' i]",
    "a[href*='accountchooser' i]",
    "a[href*='servicelogin' i]",
    "a[href*='accounts.google.com']",
)

# Signed-out marketing pages for Voice live on two hosts: voice.google.com and
# workspace.google.com/products/voice. Both carry a "Sign in" control and the
# same sign-in flow — the only difference is the URL, which is why a profile
# that landed on the workspace page used to classify as UNKNOWN and stall.
WORKSPACE_VOICE_MARKERS = (
    "workspace.google.com/products/voice",
    "workspace.google.com/intl/",
)

IDENTIFIER_SELECTORS = (
    "input#identifierId",
    "input[name='identifier']",
    "input[autocomplete='username']",
    # NEVER a bare input[type=email]: Google's recovery-email field is also
    # type=email, and matching it here made the 3rd step look like the
    # identifier page — the primary address was typed (or nothing was) and
    # the recovery address was never submitted.
    "input[type='email']:not([name='knowledgePreregisteredEmailResponse'])"
    ":not(#knowledge-preregistered-email-response)",
)

PASSWORD_SELECTORS = (
    "input[name='Passwd']",
    "input[autocomplete='current-password']",
    "input[type='password']",
)

CHOOSER_SELECTORS = (
    "div[data-identifier]",
    "li[data-identifier]",
    "[data-email]",
)

# "Confirm your recovery email" challenge. Google shows it as a list of
# selectable options (data-action="selectchallenge"); the recovery-email one
# is ALWAYS data-challengetype="12". Phone/SMS is type 13/6/etc and listed
# first. NEVER click Try another way / phone / generic selectchallenge while
# the type-12 row is on the page.
RECOVERY_CHALLENGE_OPTION = (
    "[data-action='selectchallenge'][data-challengetype='12']",
    "div[data-action='selectchallenge'][data-challengetype='12']",
    "[data-challengetype='12'][data-action='selectchallenge']",
    "[jsname='EBHGs'][data-challengetype='12']",
)
# Email-only copy. Do NOT use "подтвердите резервный" — that also matches
# "подтвердите резервный номер телефона" and we used to click the phone tile.
RECOVERY_CHALLENGE_TEXTS = (
    "резервный адрес электронной почты",
    "резервный адрес эл. почты",
    "резервный адрес эл почты",
    "резервную почту",
    "резервной почты",
    "резервный адрес",
    "подтвердите резервный адрес",
    "подтвердите резервную почту",
    "введите резервный адрес",
    "укажите резервный адрес",
    "recovery email",
    "backup email",
    "confirm your recovery email",
    "confirm recovery email",
    "verify your recovery email",
    "enter your recovery email",
    "enter the email you added",
    "email you added as a recovery",
)
RECOVERY_PHONE_TEXTS = (
    "телефон", "телефона", "телефону",
    "phone", "sms", "text message",
    "номер телефона", "резервный номер",
    "recovery phone", "backup phone",
    "authenticator", "приложение google",
    "passkey", "ключ доступа",
)
RECOVERY_URL_MARKERS = (
    "knowledgepreregisteredemail",
    "/challenge/kpe",
    "idvrecoveryemail",
    "challengetype=12",
)
RECOVERY_EMAIL_INPUT = (
    "input[name='knowledgePreregisteredEmailResponse']",
    "input#knowledge-preregistered-email-response",
    "input[aria-label*='recovery email' i]",
    "input[aria-label*='backup email' i]",
    "input[aria-label*='резервный адрес' i]",
    "input[aria-label*='резервн' i][aria-label*='почт' i]",
)

# ── 2FA: authenticator (TOTP) and backup codes ───────────────────────────────
TOTP_INPUT = (
    "input#totpPin",
    "input[name='totpPin']",
)
BACKUP_CODE_INPUT = (
    "input#backupCodePin",
    "input[name='backupCodePin']",
)
TOTP_URL_MARKERS = ("/challenge/totp", "challengetype=6")
BACKUP_CODE_URL_MARKERS = ("/challenge/bc",)
TOTP_PAGE_TEXT_MARKERS = (
    "google authenticator",
    "authenticator app",
    "приложение google authenticator",
    "приложение authenticator",
    "код из приложения",
)
# SMS prompt copy — an autocomplete='one-time-code' input on a page with this
# copy is the SMS step, NOT the authenticator, and must not get a TOTP code.
SMS_PAGE_TEXT_MARKERS = (
    "text message", "sms", "по sms", "в смс", "текстовое сообщение",
)
# Method-picker rows (selectchallenge) we may click when we hold the material.
TOTP_TILE_TEXTS = (
    "google authenticator",
    "authenticator app",
    "authenticator",
    "приложение google authenticator",
    "приложение authenticator",
    "код из приложения",
)
BACKUP_CODE_TILE_TEXTS = (
    "backup code",
    "backup codes",
    "резервный код",
    "резервные коды",
    "8-значный код",
    "8-значные коды",
    "один из резервных кодов",
    "код резервной копии",
)
# Google rejected the code we typed.
WRONG_CODE_TEXTS = (
    "wrong code",
    "incorrect code",
    "invalid code",
    "wrong. try again",
    "неверный код",
    "неправильный код",
    "код введен неверно",
    "неверный. повторите попытку",
)
# "Google prompt / check your phone" screens — Try another way leads to the
# method picker. Never matched on the recovery picker (handled separately).
TWO_FA_PROMPT_MARKERS = (
    "2-step verification",
    "двухэтапная аутентификация",
    "двухфакторная аутентификация",
    "check your phone",
    "проверьте телефон",
    "verify it's you",
    "подтвердите, что это вы",
    "tap yes",
    "нажмите «да»",
)

# After the password is accepted, the form can land on the marketing page.
# From there we navigate to the dialer ONCE per grace window — the Voice SPA
# needs several uninterrupted seconds to boot, and re-navigating every loop
# (the old behaviour) aborts the page load each time, so the dialer never
# renders and the run ends in a timeout.
DIALER_NAV_GRACE_SEC = 15.0
DIALER_NAV_MAX = 4

NEXT_TEXTS = ("далее", "next", "продолжить", "continue", "войти", "sign in")
ANOTHER_ACCOUNT_TEXTS = (
    "use another account", "использовать другой аккаунт", "другой аккаунт",
    "сменить аккаунт", "add account", "добавить аккаунт",
)
# Post-login interstitials that are safe to dismiss (passkey nags, tips).
SKIP_TEXTS = (
    "не сейчас", "not now", "skip", "пропустить", "later", "позже",
    "cancel", "отмена", "no thanks", "нет, спасибо",
    "remind me later", "напомнить позже", "not now, thanks",
    "skip for now", "пропустить пока", "не надо",
    "not right now", "не сейчас, спасибо",
)
# Passkey *login* (existing key) — fall back to password instead of hanging
# on a hardware prompt. Never Continue / Продолжить.
TRY_ANOTHER_TEXTS = (
    "try another way", "попробовать другой способ", "другой способ",
    "use your password", "использовать пароль", "войти с паролем",
    "try another method", "использовать другой способ",
)
# Optional passkey *enrollment* speedbump (Sign in faster / Входите в аккаунт
# быстрее). Distinct from a passkey *login* challenge — skip with Not now,
# never Continue (that starts creating a passkey).
PASSKEY_ENROLL_URL_MARKERS = (
    "passkeyenrollment", "speedbump/passkey", "speedbump/idvpasskey",
)
PASSKEY_ENROLL_TEXT_MARKERS = (
    "sign in faster",
    "входите в аккаунт быстрее",
    "make it easier to sign in",
    "упростите вход",
    "only create a passkey",
    "создавайте ключ доступа, только если",
    "create a passkey",
    "создать ключ доступа",
    "with passkeys, your device",
    "если у вас настроены ключи доступа",
    "skip passkey",
    "пропустить ключ доступа",
    "passkeys are easier",
    "ключи доступа упрощают",
    "turn on passkeys",
    "включите ключи доступа",
    "save a passkey",
    "сохраните ключ доступа",
)
# Google identity action-bar jsnames on this speedbump (stable across locales).
PASSKEY_SKIP_JSNAME = "eBSUOb"       # secondary: Not now / Не сейчас
PASSKEY_CONTINUE_JSNAME = "Njthtb"   # primary: Continue / Продолжить — do not click
# "Stay signed in?" — saying yes is what keeps the profile usable.
STAY_TEXTS = ("да", "yes", "ок", "ok", "готово", "done", "confirm")
STAY_PAGE_MARKERS = (
    "stay signed in", "не выходить из аккаунта", "остаться в аккаунте",
)

ERROR_MARKERS = (
    (BAD_PASSWORD, (
        "wrong password", "incorrect password",
        "неверный пароль", "неправильный пароль",
    )),
    (UNKNOWN_EMAIL, (
        "couldn't find your google account", "couldn't find your account",
        "не удалось найти аккаунт google", "аккаунт google не найден",
        "enter a valid email", "введите действительный адрес",
    )),
    (RATE_LIMITED, (
        "too many failed attempts", "try again later",
        "слишком много попыток", "повторите попытку позже",
        "unusual activity", "необычная активность",
    )),
    (DISABLED, (
        "account has been disabled", "account disabled",
        "аккаунт отключен", "аккаунт заблокирован",
        "unable to access a google product",
        "your access to this google product has been suspended",
        "не удается получить доступ к продукту google",
        "не удаётся получить доступ к продукту google",
    )),
)

CHALLENGE_TEXT_MARKERS = (
    ("2fa", ("2-step verification", "двухэтапная аутентификация",
             "двухфакторная")),
    ("code", ("enter the code", "введите код", "verification code",
              "код подтверждения", "get a verification code")),
    ("device", ("check your phone", "проверьте телефон", "tap yes",
                "открой приложение")),
    ("identity", ("verify it's you", "подтвердите, что это вы",
                  "confirm your recovery")),
    ("captcha", ("i'm not a robot", "я не робот", "captcha", "капча",
                 "recaptcha", "reCAPTCHA")),
    ("passkey", ("use your passkey", "используйте ключ доступа")),
    ("choose", ("choose how you want to sign in",
                "выберите способ входа")),
)

CAPTCHA_TEXT_MARKERS = (
    "i'm not a robot", "i am not a robot", "im not a robot",
    "я не робот",
    "verify you are not a robot",
    "подтвердите, что вы не робот",
    "подтвердите что вы не робот",
    "unusual traffic from your computer",
    "необычный трафик с вашего компьютера",
    "select all images",
    "выберите все изображения",
)
# Path / query on accounts.google.com when the form itself is the robot check.
CAPTCHA_URL_MARKERS = (
    "/challenge/recaptcha",
    "signin/challenge/recaptcha",
    "flow=recaptcha",
    "idvrecaptcha",
    "idv/recaptcha",
    "speedbump/captcha",
)

# Real Voice product-suspension copy. Google redirects a banned session
# off the dialer onto a help page. Heading on the legacy page (40039):
# "Unable to access a Google product". Newer Voice-specific page (16053145):
# "Unable to access Google Voice" + "Your access to Google Voice has been
# suspended". Do NOT use generic phrases like "account is suspended" or
# "couldn't load" — those fire on outages and wipe live accounts.
BAN_TEXT_MARKERS = (
    "unable to access google voice",
    "unable to access a google product",
    "your access to google voice has been suspended",
    "your access to this google product has been suspended",
    "google voice service has been suspended",
    "if you've been redirected to this page from google voice",
    "if you've been redirected to this page from a particular product",
    "не удается получить доступ к google voice",
    "не удаётся получить доступ к google voice",
    "не удается получить доступ к продукту google",
    "не удаётся получить доступ к продукту google",
    "доступ к google voice приостановлен",
    "ваш доступ к google voice приостановлен",
    "доступ к этому продукту google приостановлен",
    "ваш доступ к этому продукту google приостановлен",
)

# Only Google's own help hosts. `p=grandcentral` on a random URL is not a ban.
BAN_URL_MARKERS = (
    "support.google.com/accounts/answer/40039",
    "support.google.com/voice/answer/16053145",
    "support.google.com/accounts/contact/suspended",
)


def is_voice_ban_page(url: str, text: str = "") -> bool:
    """True only when Google itself suspended Voice for this account.

    Real signals:
      * redirect to support.google.com/accounts/answer/40039?p=grandcentral
      * redirect to support.google.com/voice/answer/16053145
      * appeal form support.google.com/accounts/contact/suspended?p=grandcentral
      * the distinctive 'Unable to access a Google product / Google Voice' copy

    Missing dialer, missing number, chrome:// NTP, DNS interstitial, and
    the workspace marketing page are NOT a ban.
    """
    u = (url or "").lower()
    if "support.google.com" in u:
        if any(m in u for m in BAN_URL_MARKERS):
            return True
        if "p=grandcentral" in u:
            return True
    t = (text or "").lower()
    if not t:
        return False
    return any(m in t for m in BAN_TEXT_MARKERS)


def is_passkey_enrollment_page(url: str, text: str = "") -> bool:
    """True for the optional 'Sign in faster' passkey-enrollment nag.

    Not the same as a passkey *login* challenge ('use your passkey').
    """
    u = (url or "").lower()
    if any(m in u for m in PASSKEY_ENROLL_URL_MARKERS):
        return True
    t = (text or "").lower()
    return any(m in t for m in PASSKEY_ENROLL_TEXT_MARKERS)


def _jitter(lo: float = 0.03, hi: float = 0.09) -> float:
    return random.uniform(lo, hi)


class GoogleSignIn:
    """Drives the Google sign-in form for one worker's browser."""

    def __init__(
        self,
        driver,
        dialer_selectors: Optional[Sequence[str]] = None,
        worker_id="?",
        log: Optional[Callable[[str], None]] = None,
    ):
        self.driver = driver
        self.dialer_selectors = tuple(dialer_selectors or DEFAULT_DIALER_SELECTORS)
        self.worker_id = worker_id
        self._log = log or (
            lambda m: logger.info(f"Worker {worker_id}: sign-in {m}")
        )

    # ── low-level helpers ─────────────────────────────────────────────────────

    def _url(self) -> str:
        try:
            return (self.driver.current_url or "").lower()
        except Exception:
            return ""

    def page_text(self) -> str:
        """Visible copy, including open shadow roots (the identity UI lives there)."""
        try:
            txt = self.driver.execute_script(
                """
                const walk = (root, parts) => {
                    if (!root) return parts;
                    try {
                        if (root.body && root.body.innerText)
                            parts.push(root.body.innerText);
                        else if (root.innerText)
                            parts.push(root.innerText);
                    } catch (e) {}
                    let kids;
                    try { kids = (root.querySelectorAll && root.querySelectorAll('*'))
                        || []; } catch (e) { return parts; }
                    for (const el of kids) {
                        if (el.shadowRoot) walk(el.shadowRoot, parts);
                    }
                    return parts;
                };
                return walk(document, []).join('\\n');
                """
            )
        except Exception:
            try:
                txt = self.driver.execute_script(
                    "return document.body ? document.body.innerText : ''"
                )
            except Exception:
                return ""
        return (txt or "").lower()

    def _visible(self, selectors: Sequence[str]):
        """First displayed element matching any selector, else None."""
        for sel in selectors:
            try:
                for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if el.is_displayed():
                            return el
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def _present(self, selectors: Sequence[str]) -> bool:
        for sel in selectors:
            try:
                if self.driver.find_elements(By.CSS_SELECTOR, sel):
                    return True
            except Exception:
                continue
        return False

    def _button_by_text(self, texts: Sequence[str]):
        """Find a visible clickable whose label matches one of `texts`.

        Short labels (3 chars or fewer — 'да', 'ok', 'yes') must match
        exactly, otherwise 'да' would steal the 'Далее' (Next) button.
        Walks open shadow roots: Google's identity Next lives in a web
        component, so a light-DOM query misses it.
        """
        try:
            return self.driver.execute_script(
                """
                const wanted = arguments[0];
                const walk = (root, acc) => {
                    if (!root || !root.querySelectorAll) return acc;
                    try {
                        acc.push(...root.querySelectorAll(
                            'button, [role="button"], [role="link"], div[jsname]'
                        ));
                    } catch (e) {}
                    let kids;
                    try { kids = root.querySelectorAll('*'); } catch (e) { return acc; }
                    for (const el of kids) {
                        if (el.shadowRoot) walk(el.shadowRoot, acc);
                    }
                    return acc;
                };
                const nodes = walk(document, []);
                for (const b of nodes) {
                    const t = (b.innerText || b.textContent || '')
                        .trim().toLowerCase();
                    if (!t || t.length > 40) continue;
                    const hit = wanted.some(w => {
                        const ww = (w || '').toLowerCase();
                        if (!ww) return false;
                        if (ww.length <= 3) return t === ww;
                        return t === ww || t.startsWith(ww);
                    });
                    if (!hit) continue;
                    const r = b.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return b;
                }
                return null;
                """,
                list(texts),
            )
        except Exception:
            return None

    def _click_next(self) -> bool:
        """Click Google's Next/Далее, including inside shadow DOM.

        Must not fall back to leftover #identifierNext / #passwordNext from
        earlier steps — those stay in the DOM and a click on them does nothing
        on the recovery-email page.
        """
        try:
            btn = self.driver.execute_script(
                """
                const wanted = arguments[0];
                const walk = (root, acc) => {
                    if (!root || !root.querySelectorAll) return acc;
                    try {
                        acc.push(...root.querySelectorAll(
                            'button, [role="button"], div[jsname="LgbsSe"]'
                        ));
                    } catch (e) {}
                    let kids;
                    try { kids = root.querySelectorAll('*'); } catch (e) { return acc; }
                    for (const el of kids) {
                        if (el.shadowRoot) walk(el.shadowRoot, acc);
                    }
                    return acc;
                };
                const nodes = walk(document, []);
                for (const b of nodes) {
                    const t = (b.innerText || b.textContent || '')
                        .trim().toLowerCase();
                    if (!wanted.some(w => t === w || t.startsWith(w))) continue;
                    const r = b.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return b;
                }
                return null;
                """,
                list(NEXT_TEXTS),
            )
        except Exception:
            btn = self._button_by_text(NEXT_TEXTS)
        if btn is None:
            self._log("Next/Далее button not found")
            return False
        return self._click(btn)

    def _click(self, el) -> bool:
        if el is None:
            return False
        try:
            el.click()
            return True
        except Exception:
            # Overlays and Google's animated buttons intercept real clicks.
            try:
                self.driver.execute_script("arguments[0].click();", el)
                return True
            except Exception as e:
                self._log(f"click failed: {e}")
                return False

    def _type(self, el, text: str) -> bool:
        """Type like a human — Google's form listens for real key events, so
        setting .value through JS would leave its model empty."""
        try:
            el.click()
        except Exception:
            pass
        try:
            el.clear()
        except Exception:
            pass
        try:
            for ch in text:
                el.send_keys(ch)
                time.sleep(_jitter())
            return True
        except Exception as e:
            self._log(f"typing failed: {e}")
            return False

    # ── state detection ───────────────────────────────────────────────────────

    def state(self) -> str:
        """Classify the current page. Visibility-based, so hidden decoy inputs
        on Google's form never confuse the email and password steps."""
        url = self._url()

        # Ban URL first: support.google.com/accounts/answer/40039?p=grandcentral
        # has no sign-in fields, and used to fall through as UNKNOWN while
        # the post-password loop kept navigating to the dialer for ~2 minutes.
        if is_voice_ban_page(url):
            return BANNED

        if "voice.google.com" in url and self._present(self.dialer_selectors):
            return DIALER

        # Recovery (picker or input) MUST be classified before identifier:
        # the recovery field is also input[type=email] and used to steal this
        # page into the identifier step, so the backup address was never typed.
        if self._is_recovery_challenge():
            return CHALLENGE

        if self._visible(PASSWORD_SELECTORS) is not None:
            return PASSWORD
        if self._visible(IDENTIFIER_SELECTORS) is not None:
            return IDENTIFIER

        text = self.page_text()
        if is_voice_ban_page(url, text):
            return BANNED

        # Passkey *enrollment* (Sign in faster) sits on top of leftover
        # chooser DOM. Classifying CHOOSER first is why we kept clicking the
        # account tile and never pressed Not now.
        if is_passkey_enrollment_page(url, text):
            return CHALLENGE

        if self._present(CHOOSER_SELECTORS):
            return CHOOSER

        if "voice.google.com" in url:
            if any(m in text for m in LANDING_TEXT_MARKERS):
                return LANDING
            if self._visible(SIGNIN_LINK_SELECTORS) is not None:
                return LANDING
            # UNKNOWN — the SPA is still painting. Only here do we wait.
            return UNKNOWN

        # The workspace.google.com Voice product page is a signed-out landing
        # too: it has a "Sign in" control and runs the same form. Treat it as
        # LANDING so the sign-in starts instead of stalling on UNKNOWN.
        # Ban copy on this host was already caught above.
        if any(m in url for m in WORKSPACE_VOICE_MARKERS):
            return LANDING

        # Away from voice.google.com there is no SPA to wait out: a page we do
        # not recognise is conclusive right now, so don't burn the probe budget.
        if "accounts.google.com" in url:
            return CHALLENGE
        return UNKNOWN

    def challenge_reason(self) -> str:
        text = self.page_text()
        for label, markers in CHALLENGE_TEXT_MARKERS:
            if any(m in text for m in markers):
                return f"{label} challenge — needs a human"
        return f"unrecognised Google prompt @ {self._url()[:120]}"

    def _is_captcha(self) -> bool:
        """True when Google is showing a reCAPTCHA / 'I'm not a robot' check.

        After the email step this often sits on top of the identifier page
        (iframe), so `state()` still says IDENTIFIER — we have to look for
        the widget itself, not the page class. The floating grecaptcha-badge
        is NOT a captcha (it is 70×60 and used to false-trigger).
        """
        url = self._url()
        if any(m in url for m in CAPTCHA_URL_MARKERS):
            return True
        if self._captcha_widget_present():
            return True
        if self._captcha_iframe_present():
            return True
        text = self.page_text()
        return any(m in text for m in CAPTCHA_TEXT_MARKERS)

    def _captcha_widget_present(self) -> bool:
        try:
            return bool(self.driver.execute_script(
                """
                const isBadge = (el) => {
                    let n = el;
                    for (let i = 0; i < 5 && n; i++) {
                        const c = ((n.className && n.className.toString()) || '');
                        const id = n.id || '';
                        if (c.indexOf('grecaptcha-badge') !== -1
                            || id.indexOf('grecaptcha-badge') !== -1)
                            return true;
                        n = n.parentElement;
                    }
                    return false;
                };
                const blobOf = (n) => (
                    (n.src || (n.getAttribute && n.getAttribute('src')) || '')
                    + ' ' + (n.id || '') + ' ' + (n.className || '')
                    + ' ' + (n.title || '') + ' ' + (n.name || '')
                ).toLowerCase();
                const isCaptchaBlob = (b) => (
                    b.indexOf('recaptcha') !== -1
                    || b.indexOf('hcaptcha') !== -1
                    || b.indexOf('recaptcha.net') !== -1
                );
                const bigEnough = (n) => {
                    const r = n.getBoundingClientRect();
                    // Checkbox widget ~302×78; image challenge much taller.
                    // Badge is ~70×60 — reject that.
                    return (r.width >= 240 && r.height >= 60)
                        || (r.width >= 140 && r.height >= 140);
                };
                const walk = (root) => {
                    if (!root || !root.querySelectorAll) return false;
                    let nodes;
                    try {
                        nodes = root.querySelectorAll(
                            'iframe, .g-recaptcha, #recaptcha, [data-sitekey],'
                            + ' textarea[name="g-recaptcha-response"]'
                        );
                    } catch (e) { nodes = []; }
                    for (const n of nodes) {
                        if (isBadge(n)) continue;
                        const blob = blobOf(n);
                        const tag = (n.tagName || '').toLowerCase();
                        if (tag === 'textarea') continue;
                        if (!isCaptchaBlob(blob) && !n.getAttribute('data-sitekey'))
                            continue;
                        if (bigEnough(n)) return true;
                    }
                    let kids;
                    try { kids = root.querySelectorAll('*'); } catch (e) { return false; }
                    for (const el of kids) {
                        if (el.shadowRoot && walk(el.shadowRoot)) return true;
                    }
                    return false;
                };
                return walk(document);
                """,
            ))
        except Exception:
            return False

    def _captcha_iframe_present(self) -> bool:
        """Selenium can see cross-origin iframes the JS walk sometimes misses."""
        try:
            frames = self.driver.find_elements(By.TAG_NAME, "iframe")
        except Exception:
            return False
        for fr in frames:
            try:
                src = (
                    (fr.get_attribute("src") or "")
                    + " " + (fr.get_attribute("title") or "")
                    + " " + (fr.get_attribute("name") or "")
                ).lower()
                cls = (fr.get_attribute("class") or "").lower()
                if "grecaptcha-badge" in cls:
                    continue
                if "recaptcha" not in src and "hcaptcha" not in src:
                    continue
                size = fr.size or {}
                w, h = float(size.get("width") or 0), float(size.get("height") or 0)
                if (w >= 240 and h >= 60) or (w >= 140 and h >= 140):
                    return True
            except Exception:
                continue
        return False

    # ── recovery-email challenge ──────────────────────────────────────────────

    # Click-in-JS finder: ONLY the recovery-EMAIL row (challengetype 12).
    # Never a phone tile, never Try another way, never a generic selectchallenge.
    _FIND_RECOVERY_EMAIL_TILE_JS = """
        const clickIt = arguments[1] === true;
        const TYPE12 =
            '[data-action="selectchallenge"][data-challengetype="12"],'
            + '[jsname="EBHGs"][data-action="selectchallenge"][data-challengetype="12"],'
            + '[jsname="EBHGs"][data-challengetype="12"]';
        const walk = (root, acc) => {
            if (!root || !root.querySelectorAll) return acc;
            try { acc.push(...root.querySelectorAll(TYPE12)); } catch (e) {}
            let kids;
            try { kids = root.querySelectorAll('*'); } catch (e) { return acc; }
            for (const el of kids) {
                if (el.shadowRoot) walk(el.shadowRoot, acc);
            }
            return acc;
        };
        const labelOf = (el) => ((el.innerText || el.textContent || '') + ' '
            + ((el.getAttribute && el.getAttribute('aria-label')) || ''))
            .toLowerCase().replace(/\\s+/g, ' ').trim();
        const asType12Row = (el) => {
            if (!el) return null;
            const row = el.closest
                ? (el.closest('[data-action="selectchallenge"][data-challengetype="12"]')
                   || el.closest('[data-challengetype="12"]'))
                : el;
            if (!row) return null;
            const typ = (row.getAttribute && row.getAttribute('data-challengetype')) || '';
            if (typ !== '12') return null;
            const action = (row.getAttribute && row.getAttribute('data-action')) || '';
            if (action && action !== 'selectchallenge') return null;
            const t = labelOf(row);
            if (t && (t.indexOf('phone') !== -1 || t.indexOf('sms') !== -1
                || t.indexOf('телефон') !== -1)
                && t.indexOf('email') === -1 && t.indexOf('почт') === -1
                && t.indexOf('mail') === -1) {
                return null;
            }
            return row;
        };
        const pick = (doc) => {
            if (!doc) return null;
            const nodes = walk(doc, []);
            for (const n of nodes) {
                const row = asType12Row(n);
                if (row) return row;
            }
            return null;
        };
        let hit = pick(document);
        if (!hit) {
            const frames = document.querySelectorAll('iframe');
            for (const f of frames) {
                try { hit = pick(f.contentDocument); } catch (e) {}
                if (hit) break;
            }
        }
        if (!hit) return null;
        if (clickIt) {
            try { hit.click(); return true; } catch (e) {}
            try {
                hit.dispatchEvent(new MouseEvent('click', {
                    bubbles: true, cancelable: true, view: window
                }));
                return true;
            } catch (e) { return false; }
        }
        return hit;
        """

    def _is_recovery_challenge(self) -> bool:
        """True on the 'confirm your recovery email' picker or its input page."""
        url = self._url()
        if any(m in url for m in RECOVERY_URL_MARKERS):
            return True
        return (
            self._find_recovery_input() is not None
            or bool(self._find_recovery_tile())
        )

    def _recovery_picker_on_screen(self) -> bool:
        """True on the 'choose how to verify' list.

        The only legal click on this screen is challengetype=12
        (Confirm your recovery email). Try another way / Not now / phone
        must never be clicked here.
        """
        return bool(self._find_recovery_tile()) or self._selectchallenge_tiles_present()

    def _selectchallenge_tiles_present(self) -> bool:
        """True when Google's method picker (selectchallenge rows) is on screen."""
        try:
            return bool(self.driver.execute_script(
                """
                const walk = (root) => {
                    if (!root || !root.querySelector) return false;
                    if (root.querySelector('[data-action="selectchallenge"]'))
                        return true;
                    let kids;
                    try { kids = root.querySelectorAll('*'); } catch (e) { return false; }
                    for (const el of kids) {
                        if (el.shadowRoot && walk(el.shadowRoot)) return true;
                    }
                    return false;
                };
                if (walk(document)) return true;
                const frames = document.querySelectorAll('iframe');
                for (const f of frames) {
                    try { if (walk(f.contentDocument)) return true; } catch (e) {}
                }
                return false;
                """
            ))
        except Exception:
            return False

    def _find_recovery_tile(self):
        """ONLY 'Confirm your recovery email' — data-challengetype=12.

        Never the phone/SMS row, never Try another way.
        """
        try:
            hit = self.driver.execute_script(
                self._FIND_RECOVERY_EMAIL_TILE_JS,
                list(RECOVERY_CHALLENGE_TEXTS),
                False,
            )
            if hit:
                return hit
        except Exception:
            pass
        return self._visible(RECOVERY_CHALLENGE_OPTION)

    def _click_recovery_email_tile(self) -> bool:
        """Click type-12 inside the page so the WebElement cannot go stale."""
        try:
            clicked = self.driver.execute_script(
                self._FIND_RECOVERY_EMAIL_TILE_JS,
                list(RECOVERY_CHALLENGE_TEXTS),
                True,
            )
        except Exception as e:
            self._log(f"recovery-email tile click failed: {e}")
            return False
        if clicked:
            self._log("clicked Confirm your recovery email (challengetype=12)")
            return True
        tile = self._visible(RECOVERY_CHALLENGE_OPTION)
        if tile is None:
            return False
        self._log("clicked Confirm your recovery email (selector fallback)")
        return self._click(tile)

    def _find_recovery_input(self):
        """The backup-address input, even if Selenium thinks it is hidden."""
        try:
            hit = self.driver.execute_script(
                """
                const sels = arguments[0];
                const walk = (root) => {
                    if (!root) return null;
                    for (const s of sels) {
                        try {
                            const el = root.querySelector(s);
                            if (el) return el;
                        } catch (e) {}
                    }
                    let kids;
                    try { kids = root.querySelectorAll('*'); } catch (e) { return null; }
                    for (const el of kids) {
                        if (el.shadowRoot) {
                            const hit = walk(el.shadowRoot);
                            if (hit) return hit;
                        }
                    }
                    return null;
                };
                return walk(document);
                """,
                list(RECOVERY_EMAIL_INPUT),
            )
            if hit:
                return hit
        except Exception:
            pass
        return self._visible(RECOVERY_EMAIL_INPUT)

    def answer_recovery_email(self, recovery_email: str) -> bool:
        """Type the backup address on the 'confirm recovery email' step.

        Handles both shapes: the picker (click the recovery tile first, which
        reveals the input) and the input page itself. Returns True once the
        address has been submitted with the form's own Next/Далее button.
        """
        if not recovery_email:
            return False

        # If we are on the picker, open the recovery-email option first.
        box = self._find_recovery_input()
        if box is None:
            if not self._click_recovery_email_tile():
                self._log("recovery-email challenge is on screen but type-12 tile was not clicked")
                return False
            self._log("opening Confirm your recovery email (challengetype=12)")
            deadline = time.time() + 8.0
            box = None
            while time.time() < deadline:
                box = self._find_recovery_input()
                if box is not None:
                    break
                time.sleep(0.25)
            if box is None:
                self._log("recovery-email input never appeared")
                return False

        self._log("entering the recovery email")
        if not self._type(box, recovery_email):
            return False
        time.sleep(0.4)

        self._log("clicking Next after recovery email")
        clicked = self._click_next()
        if not clicked:
            try:
                box.send_keys(Keys.RETURN)
                self._log("Next button missed — sent Enter")
            except Exception as e:
                self._log(f"could not submit recovery email: {e}")
                return False

        # Only treat it as submitted once the recovery form actually leaves.
        deadline = time.time() + 6.0
        while time.time() < deadline:
            if (
                self._find_recovery_input() is None
                and self._find_recovery_tile() is None
            ):
                return True
            time.sleep(0.25)
        self._log("recovery form still on screen after Next")
        return False

    # ── 2FA: authenticator (TOTP) / backup codes ─────────────────────────────

    def _is_totp_page(self) -> bool:
        url = self._url()
        if any(m in url for m in TOTP_URL_MARKERS):
            return True
        if self._visible(TOTP_INPUT) is not None:
            return True
        text = self.page_text()
        if any(m in text for m in SMS_PAGE_TEXT_MARKERS):
            return False
        return any(m in text for m in TOTP_PAGE_TEXT_MARKERS)

    def _find_totp_input(self):
        """The authenticator code field — never the SMS one."""
        box = self._visible(TOTP_INPUT)
        if box is not None:
            return box
        if not self._is_totp_page():
            return None
        return self._visible((
            "input[autocomplete='one-time-code']",
            "input[type='tel']",
        ))

    def _find_backup_code_input(self):
        box = self._visible(BACKUP_CODE_INPUT)
        if box is not None:
            return box
        if any(m in self._url() for m in BACKUP_CODE_URL_MARKERS):
            return self._visible((
                "input[autocomplete='one-time-code']",
                "input[type='text']",
                "input[type='tel']",
            ))
        return None

    def _wrong_code_shown(self) -> bool:
        text = self.page_text()
        return any(m in text for m in WRONG_CODE_TEXTS)

    def _click_method_tile(self, texts: Sequence[str]) -> bool:
        """Click a selectchallenge row whose label matches `texts`.

        Used on the 2FA method picker for the authenticator / backup-code
        options. Type 12 (recovery email) is handled by answer_recovery_email;
        phone/SMS rows are never clicked here.
        """
        try:
            clicked = self.driver.execute_script(
                """
                const wanted = arguments[0];
                const phoneish = ['phone', 'sms', 'text message', 'телефон',
                                  'смс', 'звонок', 'вызов'];
                const walk = (root, acc) => {
                    if (!root || !root.querySelectorAll) return acc;
                    try {
                        acc.push(...root.querySelectorAll(
                            '[data-action="selectchallenge"]'
                        ));
                    } catch (e) {}
                    let kids;
                    try { kids = root.querySelectorAll('*'); } catch (e) { return acc; }
                    for (const el of kids) {
                        if (el.shadowRoot) walk(el.shadowRoot, acc);
                    }
                    return acc;
                };
                const labelOf = (el) => ((el.innerText || el.textContent || '') + ' '
                    + ((el.getAttribute && el.getAttribute('aria-label')) || ''))
                    .toLowerCase().replace(/\\s+/g, ' ').trim();
                const fire = (el) => {
                    try { el.click(); return true; } catch (e) {}
                    try {
                        el.dispatchEvent(new MouseEvent('click', {
                            bubbles: true, cancelable: true, view: window
                        }));
                        return true;
                    } catch (e) { return false; }
                };
                const nodes = walk(document, []);
                for (const n of nodes) {
                    const t = labelOf(n);
                    if (!t) continue;
                    if (!wanted.some(w => t.indexOf(w) !== -1)) continue;
                    if (phoneish.some(b => t.indexOf(b) !== -1)
                        && t.indexOf('authenticator') === -1) continue;
                    if (fire(n)) return true;
                }
                return false;
                """,
                list(texts),
            )
        except Exception as e:
            self._log(f"method tile click failed: {e}")
            return False
        return bool(clicked)

    def handle_2fa(
        self,
        totp_seed: Optional[str],
        backup_codes: Sequence[str],
        backup_code_used: Optional[Callable[[str], None]],
        fa: dict,
    ) -> str:
        """Drive one 2FA step — authenticator code, backup code, method picker.

        `fa` is the caller's per-run state dict; on a fatal rejection it sets
        fa["terminal"] = (outcome, detail). Returns "acted" (something was
        clicked/typed — keep looping), "terminal", or "no" (nothing on this
        screen for us — fall back to the recovery-email / human paths).
        """
        if not totp_seed and not backup_codes:
            return "no"

        # 1. Authenticator code input on screen.
        if totp_seed:
            box = self._find_totp_input()
            if box is not None:
                if fa["totp_failures"] >= 3:
                    fa["terminal"] = (
                        CHALLENGE_REQUIRED,
                        "Google rejected the authenticator code 3 times",
                    )
                    return "terminal"
                wrong = self._wrong_code_shown()
                if wrong and fa.get("last_totp"):
                    fa["totp_failures"] += 1
                    fa["last_totp"] = None
                    self._log(
                        f"authenticator code rejected ({fa['totp_failures']}/3)"
                    )
                try:
                    code, remaining = totp_mod.totp_now(totp_seed)
                except Exception:
                    fa["terminal"] = (CHALLENGE_REQUIRED, "invalid TOTP seed")
                    return "terminal"
                if fa.get("last_totp") == code and not wrong:
                    # This window's code was already submitted — wait it out.
                    return "acted"
                if remaining < 3:
                    # Too close to the window edge: Google may validate it
                    # after the roll-over and call it wrong.
                    time.sleep(remaining + 1.0)
                    code, remaining = totp_mod.totp_now(totp_seed)
                self._log("entering 2FA code (Google Authenticator)")
                if not self._type(box, code):
                    return "acted"
                time.sleep(0.3)
                if not self._click_next():
                    try:
                        box.send_keys(Keys.RETURN)
                    except Exception:
                        pass
                fa["last_totp"] = code
                time.sleep(2.0)
                return "acted"

        # 2. Backup-code input on screen.
        box = self._find_backup_code_input()
        if box is not None and backup_codes:
            if fa.get("last_bc") and self._wrong_code_shown():
                fa["bc_failures"] += 1
                self._log(f"backup code rejected ({fa['bc_failures']}/3)")
                fa["last_bc"] = None
            if fa["bc_failures"] >= 3:
                fa["terminal"] = (
                    CHALLENGE_REQUIRED,
                    "Google rejected the backup codes — sign in by hand once",
                )
                return "terminal"
            if fa.get("last_bc"):
                # Submitted, no error painted — the page is in transit.
                return "acted"
            candidates = fa.setdefault("bc_candidates", list(backup_codes))
            if fa["bc_index"] >= len(candidates):
                fa["terminal"] = (CHALLENGE_REQUIRED, "backup codes exhausted")
                return "terminal"
            code = candidates[fa["bc_index"]]
            fa["bc_index"] += 1
            self._log("entering a backup code")
            if self._type(box, code):
                time.sleep(0.3)
                if not self._click_next():
                    try:
                        box.send_keys(Keys.RETURN)
                    except Exception:
                        pass
                fa["last_bc"] = code
                # Burned either way: Google never takes the same code twice,
                # accepted or rejected — record it right at submit time so a
                # page transition cannot lose the bookkeeping.
                if backup_code_used:
                    try:
                        backup_code_used(code)
                    except Exception:
                        pass
                time.sleep(2.0)
            return "acted"

        # 3. Method picker: take the option we hold material for. Type 12
        # (recovery email) is left for the caller's recovery path.
        if self._selectchallenge_tiles_present():
            if totp_seed and self._click_method_tile(TOTP_TILE_TEXTS):
                self._log("chose Google Authenticator on the method picker")
                time.sleep(1.5)
                return "acted"
            if backup_codes and self._click_method_tile(BACKUP_CODE_TILE_TEXTS):
                self._log("chose backup codes on the method picker")
                time.sleep(1.5)
                return "acted"
            return "no"

        # 4. A bare prompt ("check your phone") or a code page we cannot
        # fill — Try another way leads to the picker above.
        text = self.page_text()
        if (
            any(m in text for m in TWO_FA_PROMPT_MARKERS)
            or box is not None
            or (not totp_seed and self._find_totp_input() is not None)
        ):
            if (
                not self._is_recovery_challenge()
                and not self._recovery_picker_on_screen()
            ):
                btn = self._button_by_text(TRY_ANOTHER_TEXTS)
                if btn is not None and self._click(btn):
                    self._log("opening the 2FA method list (Try another way)")
                    time.sleep(1.5)
                    return "acted"
        return "no"

    def error_outcome(self) -> Optional[tuple]:
        """(outcome, detail) when Google rejected our input, else None."""
        text = self.page_text()
        for outcome, markers in ERROR_MARKERS:
            for m in markers:
                if m in text:
                    return outcome, m
        return None

    # ── individual steps ──────────────────────────────────────────────────────

    def _switch_to_new_tab(self, before: set, timeout: float = 12.0) -> bool:
        """The landing page's Sign in link is target="_blank"."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                handles = set(self.driver.window_handles)
            except Exception:
                return False
            fresh = handles - before
            if fresh:
                try:
                    self.driver.switch_to.window(fresh.pop())
                    self._log("switched to the sign-in tab")
                    return True
                except Exception:
                    return False
            time.sleep(0.25)
        return False

    def _stop_loads(self):
        """Abort an in-flight navigation without waiting out page-load timeout.

        Selenium queues execute_script / close behind the previous get(), which
        is the ~20s stall after the dialer already painted. CDP stopLoading
        does not wait.
        """
        try:
            self.driver.execute_cdp_cmd("Page.stopLoading", {})
        except Exception:
            pass
        try:
            self.driver.set_page_load_timeout(1)
        except Exception:
            pass

    def consolidate_tabs(self):
        """Fold everything back into one tab — the rest of the code base has no
        notion of window handles and would operate on a stale one.

        Extra tabs are often still loading. Selenium waits for the previous
        page-load to finish before execute_script/close, which is the ~25s
        stall after a successful sign-in. Stop the load via CDP first.
        """
        self._stop_loads()
        try:
            handles = list(self.driver.window_handles)
        except Exception:
            try:
                self.driver.set_page_load_timeout(25)
            except Exception:
                pass
            return
        if len(handles) <= 1:
            try:
                self.driver.set_page_load_timeout(25)
            except Exception:
                pass
            return
        try:
            keep = self.driver.current_window_handle
        except Exception:
            keep = handles[0]
        try:
            for h in handles:
                if h == keep:
                    continue
                try:
                    self.driver.switch_to.window(h)
                    try:
                        self.driver.execute_script(
                            "try { window.stop(); } catch (e) {}"
                        )
                    except Exception:
                        pass
                    try:
                        self.driver.close()
                    except Exception:
                        pass
                except Exception:
                    pass
            try:
                self.driver.switch_to.window(keep)
            except Exception:
                try:
                    self.driver.switch_to.window(self.driver.window_handles[0])
                except Exception:
                    pass
        finally:
            try:
                self.driver.set_page_load_timeout(25)
            except Exception:
                pass

    def _goto(self, url: str):
        """Navigate without waiting out a stuck previous page-load (~25s)."""
        self._stop_loads()
        try:
            self.driver.set_page_load_timeout(8)
        except Exception:
            pass
        try:
            self.driver.get(url)
        except Exception as e:
            # Timeout is expected on the Voice SPA — the document often never
            # reaches 'complete'. The page is still going; don't treat it as
            # a failed navigation.
            msg = str(e).lower()
            if "timeout" not in msg and "timed out" not in msg:
                self._log(f"navigation to {url} failed: {e}")

    def click_sign_in(self) -> bool:
        try:
            before = set(self.driver.window_handles)
        except Exception:
            before = set()
        url_before = self._url()

        el = self._visible(SIGNIN_LINK_SELECTORS) or self._button_by_text(
            ("sign in", "войти", "вход")
        )
        if el is None:
            return False
        self._log("clicking Sign in on the landing page")
        if not self._click(el):
            return False
        if self._switch_to_new_tab(before):
            return True
        # No second tab: some landing variants sign in in the SAME tab, which
        # also counts. But if the URL never left the landing page either, the
        # click was swallowed (popup blocker, stale element) — say so instead
        # of letting the caller click again until the timeout.
        for _ in range(20):
            time.sleep(0.25)
            u = self._url()
            if u != url_before and "voice.google.com" not in u:
                self._log("sign-in opened in the same tab")
                return True
        self._log("Sign in click opened nothing")
        return False

    def submit_email(self, email: str) -> bool:
        el = self._visible(IDENTIFIER_SELECTORS)
        if el is None:
            return False
        self._log(f"entering account {email}")
        if not self._type(el, email):
            return False
        if not self._click_next():
            btn = self._visible(("#identifierNext button", "#identifierNext"))
            if not self._click(btn):
                try:
                    el.send_keys(Keys.RETURN)
                except Exception:
                    return False
        return True

    def submit_password(self, password: str) -> bool:
        el = self._visible(PASSWORD_SELECTORS)
        if el is None:
            return False
        self._log("entering password")
        if not self._type(el, password):
            return False
        if not self._click_next():
            btn = self._visible(("#passwordNext button", "#passwordNext"))
            if not self._click(btn):
                try:
                    el.send_keys(Keys.RETURN)
                except Exception:
                    return False
        return True

    def pick_account(self, email: str) -> bool:
        """On the chooser, take the wanted account or fall back to a fresh one.

        Click happens inside the same script that finds the node — returning
        a WebElement and clicking it from Python is what produced
        stale-element failures after the chooser re-rendered.
        """
        try:
            picked = self.driver.execute_script(
                """
                const want = (arguments[0] || '').toLowerCase();
                const walk = (root, acc) => {
                    if (!root || !root.querySelectorAll) return acc;
                    try {
                        acc.push(...root.querySelectorAll(
                            '[data-identifier], [data-email], [data-emailaddress]'
                        ));
                    } catch (e) {}
                    let kids;
                    try { kids = root.querySelectorAll('*'); } catch (e) { return acc; }
                    for (const el of kids) {
                        if (el.shadowRoot) walk(el.shadowRoot, acc);
                    }
                    return acc;
                };
                const fire = (n) => {
                    const target = (n.closest && (
                        n.closest('[role="link"], [role="button"], li, div[data-identifier]')
                    )) || n;
                    try { target.click(); return true; } catch (e) {}
                    try {
                        target.dispatchEvent(new MouseEvent('click', {
                            bubbles: true, cancelable: true, view: window
                        }));
                        return true;
                    } catch (e) { return false; }
                };
                const nodes = walk(document, []);
                for (const n of nodes) {
                    const id = (
                        n.getAttribute('data-identifier') ||
                        n.getAttribute('data-email') ||
                        n.getAttribute('data-emailaddress') ||
                        n.innerText || ''
                    ).toLowerCase();
                    if (id && (id === want || id.indexOf(want) !== -1)) {
                        if (fire(n)) return true;
                    }
                }
                return false;
                """,
                email,
            )
        except Exception:
            picked = False
        if picked:
            self._log(f"picking {email} from the account chooser")
            return True

        other = self._button_by_text(ANOTHER_ACCOUNT_TEXTS)
        if other is not None:
            self._log("choosing 'use another account'")
            return self._click(other)
        return False

    def _is_passkey_enrollment(self) -> bool:
        return is_passkey_enrollment_page(self._url(), self.page_text())

    def _is_skippable_auth_nag(self) -> bool:
        """True when Not now / Try another way can finish this screen.

        Covers passkey *enrollment* and passkey *login* (fall back to password).
        Real 2FA / SMS / device-tap / recovery-email picker stay False.
        """
        if self._recovery_picker_on_screen() or self._is_recovery_challenge():
            return False
        if self._is_passkey_enrollment():
            return True
        text = self.page_text()
        stay = any(m in text for m in STAY_PAGE_MARKERS)
        passkeyish = any(m in text for m in (
            "passkey", "ключ доступа", "ключи доступа",
            "sign in faster", "входите в аккаунт быстрее",
        ))
        skip_visible = any(m in text for m in (
            "not now", "не сейчас", "try another way",
            "попробовать другой способ", "use your password",
            "использовать пароль",
        ))
        if stay and skip_visible:
            return True
        if passkeyish and skip_visible:
            return True
        return self._has_passkey_skip_button()

    def _has_passkey_skip_button(self) -> bool:
        try:
            return bool(self.driver.execute_script(
                """
                const skipJs = arguments[0];
                const skipWords = arguments[1] || [];
                const walk = (root) => {
                    if (!root || !root.querySelector) return false;
                    if (root.querySelector('[jsname="' + skipJs + '"]')) return true;
                    const bars = root.querySelectorAll
                        ? root.querySelectorAll('[data-secondary-action-label]') : [];
                    for (const bar of bars) {
                        const lab = (bar.getAttribute('data-secondary-action-label')
                            || '').toLowerCase();
                        if (skipWords.some(w => lab.indexOf(w) !== -1)) return true;
                    }
                    let kids;
                    try { kids = root.querySelectorAll('*'); } catch (e) { return false; }
                    for (const el of kids) {
                        if (el.shadowRoot && walk(el.shadowRoot)) return true;
                    }
                    return false;
                };
                return walk(document);
                """,
                PASSKEY_SKIP_JSNAME,
                list(SKIP_TEXTS),
            ))
        except Exception:
            return False

    def _try_another_way(self) -> bool:
        """On a passkey *login* prompt, pick password instead of a hardware key.

        NEVER on the recovery/method picker: that page also has a
        'Try another way' footer, and clicking it is what skipped
        'Confirm your recovery email' (challengetype=12) on every agent.
        """
        if self._recovery_picker_on_screen() or self._is_recovery_challenge():
            self._log(
                "Try another way ignored — Confirm your recovery email "
                "(challengetype=12) must be chosen; nothing else on this screen"
            )
            return False
        btn = self._button_by_text(TRY_ANOTHER_TEXTS)
        if btn is None:
            return False
        if self._click(btn):
            self._log("trying another sign-in method (password)")
            return True
        return False

    def _skip_passkey_enrollment(self) -> bool:
        """Click Not now / Не сейчас. Never Continue — that creates a passkey.

        Find-and-click is one script so a Loading overlay cannot stale the
        node between lookup and click. Walks shadow roots and same-origin
        iframes: the identity speedbump is a web component.
        """
        if self._recovery_picker_on_screen() or self._is_recovery_challenge():
            return False
        try:
            clicked = self.driver.execute_script(
                """
                const skipWords = arguments[0];
                const skipJs = arguments[1] || 'eBSUOb';
                const continueJs = arguments[2] || 'Njthtb';
                const enrollWords = arguments[3] || [];
                const norm = (s) => (s || '').toLowerCase()
                    .replace(/[\\u00a0\\u202f]/g, ' ')
                    .replace(/ё/g, 'е')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const isContinue = (el) => {
                    if (!el) return false;
                    const js = (el.getAttribute && el.getAttribute('jsname')) || '';
                    if (js === continueJs) return true;
                    if (el.closest && el.closest('[jsname="' + continueJs + '"]')) return true;
                    const t = norm((el.innerText || el.textContent || '') + ' ' +
                        (el.getAttribute && el.getAttribute('aria-label') || ''));
                    return t === 'continue' || t === 'продолжить' ||
                           t.startsWith('continue') || t.startsWith('продолжить');
                };
                const labelOf = (el) => norm(
                    (el.innerText || el.textContent || '') + ' ' +
                    ((el.getAttribute && (el.getAttribute('aria-label') ||
                        el.getAttribute('data-secondary-action-label'))) || '')
                );
                const isSkipLabel = (t) => skipWords.some(w => {
                    const ww = norm(w);
                    return t === ww || t.startsWith(ww) || t.indexOf(ww) !== -1;
                });
                const fire = (el) => {
                    if (!el || isContinue(el)) return false;
                    try { el.click(); return true; } catch (e) {}
                    try {
                        el.dispatchEvent(new MouseEvent('click', {
                            bubbles: true, cancelable: true, view: window
                        }));
                        return true;
                    } catch (e) { return false; }
                };
                const clickable = (host) => {
                    if (!host) return null;
                    if (host.matches && host.matches('button, [role="button"], [role="link"]'))
                        return host;
                    return (host.querySelector &&
                            host.querySelector('button, [role="button"], [role="link"]')) || host;
                };
                const walk = (root, acc) => {
                    if (!root || !root.querySelectorAll) return acc;
                    try {
                        acc.push(...root.querySelectorAll(
                            'button, [role="button"], [role="link"], div[jsname], span'
                        ));
                    } catch (e) {}
                    let kids;
                    try { kids = root.querySelectorAll('*'); } catch (e) { return acc; }
                    for (const el of kids) {
                        if (el.shadowRoot) walk(el.shadowRoot, acc);
                    }
                    return acc;
                };
                const search = (doc) => {
                    if (!doc) return false;
                    const href = ((doc.defaultView && doc.defaultView.location &&
                        doc.defaultView.location.href) || location.href || '').toLowerCase();
                    const body = ((doc.body && doc.body.innerText) || '').toLowerCase();
                    const enrollPage = href.indexOf('passkey') !== -1 ||
                        href.indexOf('speedbump') !== -1 ||
                        enrollWords.some(w => body.indexOf(w) !== -1);
                    const named = doc.querySelector('[jsname="' + skipJs + '"]');
                    if (named) {
                        const lab = labelOf(named);
                        if ((enrollPage || isSkipLabel(lab)) &&
                            fire(clickable(named) || named)) return true;
                    }
                    const bars = doc.querySelectorAll('[data-secondary-action-label]');
                    for (const bar of bars) {
                        const label = (bar.getAttribute('data-secondary-action-label') || '')
                            .toLowerCase();
                        if (!skipWords.some(w => label.indexOf(w) !== -1)) continue;
                        const slot = bar.querySelector('[jsname="' + skipJs + '"]');
                        const b = clickable(slot) || (() => {
                            const buttons = bar.querySelectorAll('button');
                            return buttons.length ? buttons[buttons.length - 1] : null;
                        })();
                        if (fire(b)) return true;
                    }
                    if (!enrollPage) return false;
                    for (const d of doc.querySelectorAll(
                        '[role="dialog"], [role="alertdialog"], [aria-modal="true"]'
                    )) {
                        const btns = d.querySelectorAll('button, [role="button"], [role="link"]');
                        for (const b of btns) {
                            if (isContinue(b)) continue;
                            if (isSkipLabel(labelOf(b)) && fire(clickable(b) || b)) return true;
                        }
                    }
                    const nodes = walk(doc, []);
                    for (const b of nodes) {
                        if (isContinue(b)) continue;
                        const t = labelOf(b);
                        if (!t || t.length > 80) continue;
                        if (!isSkipLabel(t)) continue;
                        const r = b.getBoundingClientRect();
                        if (r.width < 2 && r.height < 2) continue;
                        if (fire(clickable(b) || b)) return true;
                    }
                    return false;
                };
                if (search(document)) return true;
                const frames = document.querySelectorAll('iframe');
                for (const f of frames) {
                    try {
                        if (search(f.contentDocument)) return true;
                    } catch (e) {}
                }
                return false;
                """,
                list(SKIP_TEXTS),
                PASSKEY_SKIP_JSNAME,
                PASSKEY_CONTINUE_JSNAME,
                list(PASSKEY_ENROLL_TEXT_MARKERS),
            )
        except Exception:
            clicked = False
        if clicked:
            self._log("skipping passkey enrollment (Not now)")
            return True
        if self._skip_passkey_in_frames():
            self._log("skipping passkey enrollment (Not now, iframe)")
            return True
        if self._skip_passkey_xpath():
            self._log("skipping passkey enrollment (Not now, xpath)")
            return True
        if self._is_passkey_enrollment() and self._skip_passkey_escape():
            self._log("skipping passkey enrollment (Escape)")
            return True
        return False

    def _skip_passkey_in_frames(self) -> bool:
        """The identity speedbump is often inside an accounts.google.com iframe."""
        try:
            frames = self.driver.find_elements(By.TAG_NAME, "iframe")
        except Exception:
            return False
        clicked = False
        for fr in frames:
            try:
                self.driver.switch_to.frame(fr)
                clicked = self._skip_passkey_js_only()
            except Exception:
                clicked = False
            try:
                self.driver.switch_to.default_content()
            except Exception:
                pass
            if clicked:
                return True
        return False

    def _skip_passkey_js_only(self) -> bool:
        """Click Not now in the CURRENT frame (after switch_to.frame)."""
        try:
            return bool(self.driver.execute_script(
                """
                const skipJs = arguments[0];
                const continueJs = arguments[1];
                const words = arguments[2] || [];
                const fire = (el) => {
                    if (!el) return false;
                    const js = (el.getAttribute && el.getAttribute('jsname')) || '';
                    if (js === continueJs) return false;
                    try { el.click(); return true; } catch (e) { return false; }
                };
                const named = document.querySelector('[jsname="' + skipJs + '"]');
                if (named) {
                    const t = (
                        (named.innerText || named.textContent || '') + ' ' +
                        (named.getAttribute('aria-label') || '')
                    ).toLowerCase();
                    const href = (location.href || '').toLowerCase();
                    const enroll = href.indexOf('passkey') !== -1 ||
                        href.indexOf('speedbump') !== -1;
                    if ((enroll || words.some(w => t.indexOf(w) !== -1)) && fire(named))
                        return true;
                }
                const nodes = document.querySelectorAll(
                    'button, [role="button"], [role="link"], [aria-label]'
                );
                for (const b of nodes) {
                    const t = (
                        (b.innerText || b.textContent || '') + ' ' +
                        (b.getAttribute('aria-label') || '')
                    ).toLowerCase();
                    if (words.some(w => t.indexOf(w) !== -1) && fire(b)) return true;
                }
                return false;
                """,
                PASSKEY_SKIP_JSNAME,
                PASSKEY_CONTINUE_JSNAME,
                list(SKIP_TEXTS),
            ))
        except Exception:
            return False

    def _skip_passkey_xpath(self) -> bool:
        """Selenium XPath fallback when the JS walker misses a closed widget."""
        xpaths = (
            "//*[@jsname='eBSUOb']",
            "//*[@aria-label='Not now' or @aria-label='Не сейчас'"
            " or @aria-label='Skip' or @aria-label='Пропустить']",
            "//button[contains(., 'Not now') or contains(., 'Не сейчас')"
            " or contains(., 'не сейчас')]",
            "//*[@role='button'][contains(., 'Not now')"
            " or contains(., 'Не сейчас')]",
            "//*[@data-secondary-action-label]//*[@jsname='eBSUOb']",
        )
        enroll = self._is_passkey_enrollment()
        for xp in xpaths:
            try:
                for el in self.driver.find_elements(By.XPATH, xp):
                    try:
                        js = (el.get_attribute("jsname") or "")
                        if js == PASSKEY_CONTINUE_JSNAME:
                            continue
                        if js == PASSKEY_SKIP_JSNAME and not enroll:
                            lab = (
                                (el.text or "") + " " +
                                (el.get_attribute("aria-label") or "")
                            ).lower()
                            if not any(w in lab for w in SKIP_TEXTS):
                                continue
                    except Exception:
                        pass
                    if self._click(el):
                        return True
            except Exception:
                continue
        return False

    def _skip_passkey_escape(self) -> bool:
        """Optional enrollment nags are often dismissed with Escape."""
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.ESCAPE)
            return True
        except Exception:
            return False

    def dismiss_interstitial(self) -> bool:
        """Clear passkey nags and 'Stay signed in?' so they can't stall us."""
        # Recovery / method picker: the only click allowed is type 12,
        # handled by answer_recovery_email — never Not now / Try another way.
        if self._recovery_picker_on_screen() or self._is_recovery_challenge():
            return False
        # Try the skip button first even when the heading hasn't painted —
        # jsname eBSUOb is locale-independent and is how Not now is missed.
        if self._skip_passkey_enrollment():
            return True
        if self._try_another_way():
            return True
        text = self.page_text()
        if any(m in text for m in STAY_PAGE_MARKERS):
            btn = self._button_by_text(STAY_TEXTS)
            if btn is not None:
                self._log("answering 'stay signed in'")
                return self._click(btn)
        if is_passkey_enrollment_page(self._url(), text):
            return self._skip_passkey_enrollment()
        return False

    # ── driver ────────────────────────────────────────────────────────────────

    def run(
        self,
        email: str,
        password: str,
        timeout: float = 90.0,
        recovery_email: Optional[str] = None,
        cookie_wiper: Optional[Callable[[], bool]] = None,
        totp_seed: Optional[str] = None,
        backup_codes: Optional[Sequence[str]] = None,
        backup_code_used: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Sign `email` in, from whatever page the browser is currently on.

        `recovery_email` is used only if Google asks to confirm the backup
        address (the 'confirm your recovery email' challenge); without it that
        challenge is reported as needing a human.

        `totp_seed` (base32) answers the Google Authenticator 2FA step with a
        locally generated code; `backup_codes` (already filtered to unused
        ones) answers the backup-code step. When a backup code is accepted,
        `backup_code_used(code)` fires so the caller never submits it twice.

        `cookie_wiper` is called once if the saved-account picker appears.
        We never click a chooser tile — leftover Gaia cookies are dropped
        so the form starts from a blank identifier page.
        """
        deadline = time.time() + timeout
        emails_sent = 0
        passwords_sent = 0
        recovery_sent = 0
        recovery_misses = 0
        last_state = None
        idle_rounds = 0
        dialer_navs = 0
        last_dialer_nav = 0.0
        signin_failures = 0
        signin_retried = False
        chooser_wipes = 0
        passkey_skips = 0
        two_fa = {
            "totp_failures": 0,
            "bc_failures": 0,
            "bc_index": 0,
            "last_totp": None,
            "last_bc": None,
            "terminal": None,
        }

        while time.time() < deadline:
            try:
                st = self.state()
            except Exception as e:
                return self._done(ERROR, f"page probe failed: {e}")

            if st != last_state:
                self._log(f"state={st}")
                last_state, idle_rounds = st, 0

            if st == DIALER:
                # Don't close extra tabs here — driver.close() on a still-loading
                # sign-in tab used to block ~25s (page-load timeout) after the
                # dialer was already up. The caller consolidates afterwards.
                return self._done(OK, "dialer reached")

            if st == BANNED:
                return self._done(DISABLED, "Google disabled this account")

            # Captcha after the email step (or anywhere in the form) is not
            # something we can solve. Stop immediately so we don't re-type the
            # address until UNKNOWN_EMAIL / timeout, and so the operator sees
            # a clear "this account has captcha" mark.
            if self._is_captcha():
                self._log("CAPTCHA — this account requires a robot check")
                return self._done(
                    CAPTCHA, "reCAPTCHA / robot check — sign in by hand once"
                )

            # 2FA BEFORE the recovery picker: the method picker also carries
            # selectchallenge rows, so the recovery block below would claim
            # the page (and click type 12 or give up) before we ever reach
            # the authenticator / backup-code option.
            if totp_seed or backup_codes:
                action = self.handle_2fa(
                    totp_seed, backup_codes or (), backup_code_used, two_fa
                )
                if action == "terminal":
                    outcome, detail = two_fa["terminal"]
                    return self._done(outcome, detail)
                if action == "acted":
                    time.sleep(0.4)
                    continue

            # Recovery email FIRST. This picker also has a 'Try another way'
            # footer — clicking that before type 12 is what made every agent
            # skip Confirm your recovery email. While any selectchallenge
            # row is on screen, the only legal click is type 12.
            if self._is_recovery_challenge() or self._recovery_picker_on_screen():
                if not recovery_email:
                    return self._done(
                        CHALLENGE_REQUIRED,
                        "recovery email requested but none was supplied",
                    )
                if recovery_sent >= 2:
                    return self._done(
                        CHALLENGE_REQUIRED,
                        "recovery email step kept coming back",
                    )
                if self.answer_recovery_email(recovery_email):
                    recovery_sent += 1
                    recovery_misses = 0
                    time.sleep(2.0)
                    continue
                recovery_misses += 1
                if recovery_misses >= 8:
                    return self._done(
                        CHALLENGE_REQUIRED,
                        "could not fill the recovery-email prompt",
                    )
                time.sleep(0.4)
                continue

            # Passkey enrollment overlays the chooser / post-password page
            # (and sometimes IDENTIFIER/PASSWORD). Click Not now / Не сейчас
            # on every state — otherwise we keep driving the form behind it.
            # Try another way is passkey-login only; the recovery picker
            # was already handled above.
            if passkey_skips < 16:
                skipped = self._skip_passkey_enrollment()
                if not skipped:
                    skipped = self._try_another_way()
                if skipped:
                    passkey_skips += 1
                    time.sleep(0.7)
                    continue

            # Errors are answers to our own input, so only look once we typed.
            if emails_sent or passwords_sent or recovery_sent:
                err = self.error_outcome()
                if err:
                    return self._done(*err)

            if st == PASSWORD:
                if passwords_sent >= 2:
                    return self._done(
                        BAD_PASSWORD, "password step kept coming back"
                    )
                if self.submit_password(password):
                    passwords_sent += 1
                    # The post-password redirect chain (accounts.google.com →
                    # voice.google.com auth endpoint → dialer) is what sets the
                    # Voice session cookies. Give it a full grace window before
                    # navigating anywhere ourselves — an early driver.get()
                    # aborts that chain and the session never sticks.
                    last_dialer_nav = time.time()
                    time.sleep(2.0)

            elif st == IDENTIFIER:
                if emails_sent >= 2:
                    return self._done(
                        UNKNOWN_EMAIL, "email step kept coming back"
                    )
                if self.submit_email(email):
                    emails_sent += 1
                    time.sleep(2.0)

            elif st == CHOOSER:
                # Never click a saved-account tile. The picker means leftover
                # Gaia cookies — drop them and type the email on a blank form.
                if self.dismiss_interstitial():
                    time.sleep(0.7)
                    continue
                if chooser_wipes < 1 and cookie_wiper is not None:
                    self._log(
                        "account picker on screen — clearing cookies, "
                        "not picking an account"
                    )
                    try:
                        wiped = bool(cookie_wiper())
                    except Exception as e:
                        self._log(f"chooser cookie wipe failed: {e}")
                        wiped = False
                    chooser_wipes += 1
                    if wiped:
                        emails_sent = 0
                        passwords_sent = 0
                        recovery_sent = 0
                        recovery_misses = 0
                        passkey_skips = 0
                        signin_retried = False
                        dialer_navs = 0
                        time.sleep(1.0)
                        continue
                return self._done(
                    ACCOUNT_CHOOSER,
                    "account picker on screen — cookies must be cleared",
                )

            elif st == LANDING:
                if passwords_sent:
                    # Already authenticated: the form's continue= landed on
                    # the marketing page, so go straight to the dialer. One
                    # navigation per grace window — see DIALER_NAV_GRACE_SEC.
                    now = time.time()
                    if now - last_dialer_nav >= DIALER_NAV_GRACE_SEC:
                        if dialer_navs >= DIALER_NAV_MAX:
                            # The landing still offers a Sign-in control after
                            # a completed password step, so the session truly
                            # did not stick. The Google session itself exists
                            # now, though — one more pass through the form can
                            # land straight on the dialer.
                            if not signin_retried and self.click_sign_in():
                                self._log(
                                    "session did not stick — one more "
                                    "sign-in pass"
                                )
                                signin_retried = True
                                dialer_navs = 0
                                emails_sent = 0
                                passwords_sent = 0
                                last_dialer_nav = time.time()
                                time.sleep(1.5)
                                continue
                            return self._done(
                                NO_DIALER,
                                "Voice keeps showing the landing page after "
                                "sign-in — the session did not stick",
                            )
                        self._log("signed in — opening the dialer")
                        self._goto(DIALER_URL)
                        dialer_navs += 1
                        last_dialer_nav = now
                    time.sleep(1.0)
                else:
                    if self.click_sign_in():
                        signin_failures = 0
                    else:
                        signin_failures += 1
                        if signin_failures >= 3:
                            return self._done(
                                NO_FORM, "no Sign in control on the landing page"
                            )
                    time.sleep(1.5)

            elif st == CHALLENGE:
                if self._is_captcha():
                    self._log("CAPTCHA — this account requires a robot check")
                    return self._done(
                        CAPTCHA, "reCAPTCHA / robot check — sign in by hand once"
                    )
                # Passkey enrollment / "stay signed in" have no fillable field.
                # The enrollment page paints under a Loading overlay — a single
                # miss must not be treated as a human challenge.
                if self.dismiss_interstitial():
                    time.sleep(1.0)
                    continue
                skippable = self._is_skippable_auth_nag() or any(
                    m in self.page_text() for m in STAY_PAGE_MARKERS
                )
                if skippable:
                    idle_rounds += 1
                    if idle_rounds < 15:
                        time.sleep(0.8)
                        continue
                    return self._done(
                        CHALLENGE_REQUIRED,
                        "could not dismiss passkey enrollment / stay-signed-in",
                    )
                reason = self.challenge_reason()
                if "captcha" in reason.lower():
                    self._log("CAPTCHA — this account requires a robot check")
                    return self._done(CAPTCHA, reason)
                return self._done(CHALLENGE_REQUIRED, reason)

            else:  # UNKNOWN — usually mid-navigation
                if not self.dismiss_interstitial():
                    idle_rounds += 1
                    if idle_rounds >= 12:
                        # Distinguish "never reached the form" from "the
                        # password went through but Voice never opened" — the
                        # operator's next move differs.
                        return self._done(
                            NO_DIALER if passwords_sent else NO_FORM,
                            f"stuck on an unrecognised page @ {self._url()[:120]}",
                        )
                time.sleep(1.0)

            time.sleep(0.4)

        return self._done(TIMEOUT, f"sign-in unfinished after {timeout:.0f}s")

    def _done(self, outcome: str, detail: str) -> dict:
        if outcome == OK:
            self._log(f"SUCCESS — {detail}")
        else:
            self._log(f"FAILED ({outcome}) — {detail}")
        return {"outcome": outcome, "detail": detail, "url": self._url()[:200]}
