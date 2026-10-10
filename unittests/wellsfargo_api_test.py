import unittest
from unittest.mock import MagicMock, call, patch

from selenium.webdriver import Keys

from src.brokerages.wellsfargo_api import (
    _parse_wellsfargo_account,
    _replace_field_value,
    _wellsfargo_authenticated,
)


class WellsFargoCredentialsTest(unittest.TestCase):
    def test_parses_documented_credentials(self) -> None:
        self.assertEqual(
            _parse_wellsfargo_account("username:password:1234"),
            ("username", "password", "1234"),
        )

    def test_password_may_contain_colons(self) -> None:
        self.assertEqual(
            _parse_wellsfargo_account("username:pass:word:1234"),
            ("username", "pass:word", "1234"),
        )

    def test_rejects_incomplete_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "USERNAME:PASSWORD:PHONE_LAST_FOUR"):
            _parse_wellsfargo_account("username:password")

    def test_replaces_browser_autofill_before_typing(self) -> None:
        element = MagicMock()
        element.get_attribute.return_value = "new-password"

        with patch("src.brokerages.wellsfargo_api.type_slowly") as type_slowly:
            _replace_field_value(element, "new-password")

        self.assertEqual(
            element.send_keys.call_args_list,
            [call(Keys.CONTROL, "a"), call(Keys.BACKSPACE)],
        )
        element.clear.assert_called_once_with()
        type_slowly.assert_called_once_with(element, "new-password")

    def test_detects_authenticated_account_page(self) -> None:
        driver = MagicMock()
        driver.find_elements.side_effect = [[], [MagicMock()]]

        self.assertTrue(_wellsfargo_authenticated(driver))

    def test_does_not_treat_2fa_page_as_authenticated(self) -> None:
        driver = MagicMock()
        driver.find_elements.return_value = []

        self.assertFalse(_wellsfargo_authenticated(driver))


if __name__ == "__main__":
    unittest.main()
