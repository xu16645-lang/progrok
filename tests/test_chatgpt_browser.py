import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from chatgpt_browser import (
    ChatGPTBrowserRuntime,
    _action_clickable,
    _signup_state,
    _submit_for_element,
)


class _Button:
    def __init__(self, text):
        self.text = text
        self.clicked = False

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def text_content(self):
        return self.text

    def get_attribute(self, _name):
        return None

    def click(self):
        self.clicked = True


class _Form:
    def __init__(self, buttons):
        self.buttons = buttons

    def query_selector_all(self, _selector):
        return self.buttons


class _FormHandle:
    def __init__(self, form):
        self.form = form

    def as_element(self):
        return self.form


class _Input:
    def __init__(self, form):
        self.form = form

    def evaluate_handle(self, _script):
        return _FormHandle(self.form)


class SubmitForElementTests(unittest.TestCase):
    def test_chooses_exact_continue_instead_of_google_sso(self):
        google = _Button("Continue with Google")
        email_continue = _Button("Continue")
        email_input = _Input(_Form([google, email_continue]))

        clicked = _submit_for_element(
            object(), email_input, r"^continue$|^next$|^submit$"
        )

        self.assertTrue(clicked)
        self.assertTrue(email_continue.clicked)
        self.assertFalse(google.clicked)

    def test_busy_profile_submit_button_is_not_retried(self):
        button = _Button("Finish creating account")
        button.get_attribute = lambda name: "true" if name == "aria-busy" else None

        self.assertFalse(_action_clickable(button))


class BrowserRuntimeTests(unittest.TestCase):
    def test_matching_worker_browser_is_reused(self):
        runtime = ChatGPTBrowserRuntime()
        browser = object()
        proxy = {"server": "http://127.0.0.1:7890"}
        runtime.browser = browser
        runtime.headless = False
        runtime.proxy_key = runtime._key(proxy)
        runtime.using_camoufox = True

        actual, using_camoufox, reused = runtime.ensure(headless=False, proxy=proxy)

        self.assertIs(actual, browser)
        self.assertTrue(using_camoufox)
        self.assertTrue(reused)

    def test_proxy_key_includes_credentials(self):
        first = ChatGPTBrowserRuntime._key(
            {"server": "http://proxy", "username": "one", "password": "secret"}
        )
        second = ChatGPTBrowserRuntime._key(
            {"server": "http://proxy", "username": "two", "password": "secret"}
        )
        self.assertNotEqual(first, second)


class _VisibleInput:
    def is_visible(self):
        return True

    def get_attribute(self, name):
        return "6" if name == "maxlength" else None


class _ProfileAfterVerificationPage:
    url = "https://auth.openai.com/email-verification"

    def __init__(self):
        self.code = _VisibleInput()
        self.age = _VisibleInput()

    def inner_text(self, _selector):
        return "How old are you? Full name Age Finish creating account"

    def query_selector(self, selector):
        if selector == 'input[name="age"]':
            return self.age
        if selector == 'input[name="code"]':
            return self.code
        return None

    def query_selector_all(self, _selector):
        return []


class _BodyLocator:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class _DeactivatedAccountPage(_ProfileAfterVerificationPage):
    def __init__(self):
        super().__init__()
        self.code = None

    def locator(self, _selector):
        return _BodyLocator(
            "Authentication Error You do not have an account because it has "
            "been deleted or deactivated. error_code: account_deactivated"
        )

    def query_selector(self, _selector):
        return None


class SignupStateTests(unittest.TestCase):
    def test_profile_wins_when_verification_url_and_input_are_stale(self):
        self.assertEqual(_signup_state(_ProfileAfterVerificationPage()), "profile")

    def test_ready_page_is_logged_in(self):
        page = _ProfileAfterVerificationPage()
        page.url = "https://chatgpt.com/"
        page.query_selector = lambda _selector: None
        page.inner_text = lambda _selector: "你已准备就绪 ChatGPT 可能会出错"

        self.assertEqual(_signup_state(page), "logged_in")

    def test_deactivated_account_is_not_treated_as_verification_page(self):
        self.assertEqual(_signup_state(_DeactivatedAccountPage()), "account_deactivated")


if __name__ == "__main__":
    unittest.main()
