import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.brokerages import fidelity_api


class FidelityRunTest(unittest.TestCase):
    def test_fidelity_run_maps_stock_order_to_gui_workflow(self) -> None:
        order = MagicMock()
        order.get_holdings.return_value = False
        populated = fidelity_api.FidelityBrokerage()
        wrapper = AsyncMock(return_value=populated)

        with (
            patch.dict(os.environ, {"FIDELITY": "user:password:NA"}, clear=False),
            patch.object(fidelity_api, "load_dotenv"),
            patch.object(fidelity_api, "create_creds_folder"),
            patch.object(fidelity_api, "_async_fidelity_run_wrapper", wrapper),
        ):
            result = fidelity_api.fidelity_run(order, docker_mode=True)

        self.assertIs(result, populated)
        order.set_logged_in.assert_called_once_with(populated, "fidelity")
        args = wrapper.await_args.args
        self.assertEqual(args[0], ["user:password:NA"])
        self.assertEqual(args[2], "_transaction")
        self.assertIs(args[5], order)
        self.assertTrue(args[6])

    def test_fidelity_run_skips_when_credentials_are_absent(self) -> None:
        order = MagicMock()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(fidelity_api, "load_dotenv"),
            patch.object(fidelity_api, "create_creds_folder"),
        ):
            result = fidelity_api.fidelity_run(order)

        self.assertIsNone(result)
        order.set_logged_in.assert_not_called()


class FidelityErrorLoggingTest(unittest.IsolatedAsyncioTestCase):
    async def test_error_log_is_written_to_configured_directory(self) -> None:
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="https://digital.fidelity.com/ftgw/digital/portfolio/positions")

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"FIDELITY_ERROR_LOG_DIR": directory},
            clear=False,
        ):
            log_path = await fidelity_api._record_fidelity_error("menu click failed", page)

            self.assertIsNotNone(log_path)
            content = Path(log_path).read_text(encoding="utf-8")
            self.assertIn("menu click failed", content)
            self.assertIn("portfolio/positions", content)


class FidelityBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def test_docker_browser_disables_chromium_inner_sandbox(self) -> None:
        browser = MagicMock()

        with patch.object(fidelity_api.uc, "start", AsyncMock(return_value=browser)) as start:
            result = await fidelity_api._start_fidelity_browser(
                ["--disable-dev-shm-usage"],
                "/tmp/fidelity-profile",
                docker_mode=True,
            )

        self.assertIs(result, browser)
        start.assert_awaited_once_with(
            browser_args=["--disable-dev-shm-usage"],
            user_data_dir="/tmp/fidelity-profile",
            sandbox=False,
        )

    def test_auth_url_recognizes_current_retail_signin(self) -> None:
        url = (
            "https://digital.fidelity.com/prgw/digital/signin/retail?"
            "AuthRedUrl=https:%2F%2Fdigital.fidelity.com%2Fftgw%2Fdigital%2Fportfolio%2Fpositions"
        )

        self.assertTrue(fidelity_api._is_fidelity_auth_url(url))

    def test_positions_url_is_an_authenticated_portfolio_route(self) -> None:
        self.assertTrue(fidelity_api._is_fidelity_portfolio_url(fidelity_api.POSITIONS_URL))

    async def test_login_waiter_accepts_positions_redirect_after_totp(self) -> None:
        page = MagicMock()
        page.sleep = AsyncMock()

        with patch.object(
            fidelity_api,
            "get_current_url",
            AsyncMock(return_value=fidelity_api.POSITIONS_URL),
        ):
            logged_in = await fidelity_api._wait_for_fidelity_login(page)

        self.assertTrue(logged_in)
        page.sleep.assert_not_awaited()

    async def test_session_verification_rejects_positions_signin_redirect(self) -> None:
        page = MagicMock()
        page.get = AsyncMock()
        page.sleep = AsyncMock()
        page.wait_for_ready_state = AsyncMock()
        page.wait = AsyncMock()

        with patch.object(
            fidelity_api,
            "get_current_url",
            AsyncMock(return_value="https://digital.fidelity.com/prgw/digital/signin/retail"),
        ):
            verified = await fidelity_api._verify_fidelity_session(page)

        self.assertFalse(verified)
        page.get.assert_awaited_once_with(fidelity_api.POSITIONS_URL)

    async def test_session_verification_accepts_protected_positions_page(self) -> None:
        page = MagicMock()
        page.get = AsyncMock()
        page.sleep = AsyncMock()
        page.wait_for_ready_state = AsyncMock()
        page.wait = AsyncMock()

        with patch.object(
            fidelity_api,
            "get_current_url",
            AsyncMock(return_value=fidelity_api.POSITIONS_URL),
        ):
            verified = await fidelity_api._verify_fidelity_session(page)

        self.assertTrue(verified)


class FidelityHoldingsTest(unittest.IsolatedAsyncioTestCase):
    async def test_fidelity_element_click_does_not_require_geometry(self) -> None:
        element = MagicMock()
        element.apply = AsyncMock(return_value=True)
        element.scroll_into_view = AsyncMock()
        element.mouse_move = AsyncMock()
        element.mouse_click = AsyncMock()

        await fidelity_api._click_fidelity_element(element)

        element.apply.assert_awaited_once()
        element.mouse_move.assert_not_awaited()
        element.mouse_click.assert_not_awaited()

    async def test_positions_download_clicks_custom_element_shadow_button(self) -> None:
        page = MagicMock()
        page.sleep = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[True, True])
        page.select = AsyncMock(side_effect=AssertionError("geometry fallback should not run"))

        result = await fidelity_api._click_positions_download(page)

        self.assertTrue(result)
        self.assertGreaterEqual(page.evaluate.await_count, 1)
        menu_script = page.evaluate.await_args_list[0].args[0]
        self.assertIn("matched.shadowRoot?.querySelector", menu_script)
        self.assertIn("matched.closest(clickableSelector) ||", menu_script)
        self.assertIn("pwefds-button[data-testid='kebab-menu'] button", menu_script)
        self.assertIn("#posweb-grid_top-kebab_popover-button", menu_script)

    async def test_positions_download_supports_legacy_positions_id(self) -> None:
        page = MagicMock()
        download = MagicMock()
        download.scroll_into_view = AsyncMock()
        download.mouse_move = AsyncMock()
        download.mouse_click = AsyncMock()

        async def select(selector: str, **_kwargs: object) -> object | None:
            if selector == "#posweb-grid_top-download":
                return download
            return None

        page.select = AsyncMock(side_effect=select)

        result = await fidelity_api._click_positions_download(page)

        self.assertTrue(result)
        download.mouse_click.assert_awaited_once()

    async def test_positions_download_searches_component_dom_by_accessible_label(self) -> None:
        page = MagicMock()
        page.sleep = AsyncMock()
        page.select = AsyncMock(return_value=None)
        page.evaluate = AsyncMock(return_value=True)

        result = await fidelity_api._click_positions_download(page)

        self.assertTrue(result)
        script = page.evaluate.await_args.args[0]
        self.assertIn("shadowRoot", script)
        self.assertIn("contentDocument", script)
        self.assertIn("aria-label", script)

    async def test_positions_download_supports_direct_download_control(self) -> None:
        page = MagicMock()
        page.sleep = AsyncMock()
        download = MagicMock()
        download.scroll_into_view = AsyncMock()
        download.mouse_move = AsyncMock()
        download.mouse_click = AsyncMock()

        async def select(selector: str, **_kwargs: object) -> object | None:
            if selector == "[data-testid*='download' i]":
                return download
            return None

        page.select = AsyncMock(side_effect=select)

        result = await fidelity_api._click_positions_download(page)

        self.assertTrue(result)
        download.mouse_click.assert_awaited_once()

    async def test_positions_download_ignores_svg_symbol_definition(self) -> None:
        """Do not treat Fidelity's non-rendered download icon as a button."""
        page = MagicMock()
        page.select = AsyncMock(return_value=None)
        page.evaluate = AsyncMock(return_value=False)

        result = await fidelity_api._click_positions_download(page)

        self.assertFalse(result)
        selectors = [call.args[0] for call in page.select.await_args_list]
        self.assertNotIn("[id*='download' i]", selectors)

    async def test_fetch_holdings_populates_brokerage_from_downloaded_csv(self) -> None:
        brokerage = fidelity_api.FidelityBrokerage()
        page = MagicMock()
        page.send = AsyncMock()
        page.get = AsyncMock()
        page.wait_for_ready_state = AsyncMock()
        page.wait = AsyncMock()
        page.sleep = AsyncMock()

        menu = MagicMock()
        menu.get_position = AsyncMock(return_value=(1, 1))
        menu.scroll_into_view = AsyncMock()
        menu.mouse_move = AsyncMock()
        menu.mouse_click = AsyncMock()

        download_button = MagicMock()
        download_button.get_position = AsyncMock(return_value=(1, 1))
        download_button.scroll_into_view = AsyncMock()
        download_button.mouse_move = AsyncMock()

        def write_download() -> None:
            Path("temp_downloads/positions.csv").write_text(
                "Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value\n"
                "Z12345678,Roth IRA,AAPL,APPLE INC,2,$150.00,$300.00\n",
                encoding="utf-8",
            )

        download_button.mouse_click = AsyncMock(side_effect=write_download)

        async def select(selector: str, **_kwargs: object) -> object:
            if selector == "[data-testid='kebab-menu']":
                return menu
            if selector == "#kebabmenuitem-download":
                return download_button
            raise AssertionError(f"Unexpected selector: {selector}")

        page.select = AsyncMock(side_effect=select)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(fidelity_api.asyncio, "sleep", AsyncMock()),
            patch.object(fidelity_api, "print_all_holdings") as print_holdings,
            patch.object(fidelity_api, "print_and_discord"),
        ):
            previous_directory = Path.cwd()
            os.chdir(directory)
            try:
                await fidelity_api.fetch_holdings(page, brokerage, "Fidelity 1", None)
            finally:
                os.chdir(previous_directory)

        self.assertEqual(brokerage.get_account_numbers("Fidelity 1"), ["Z12345678"])
        self.assertEqual(
            brokerage.get_holdings("Fidelity 1", "Z12345678")["AAPL"],
            {"quantity": 2.0, "price": 150.0, "total": 300.0},
        )
        self.assertEqual(brokerage.get_account_totals("Fidelity 1", "Z12345678"), 300.0)
        print_holdings.assert_called_once_with(brokerage, None)

    async def test_fetch_holdings_accepts_current_mixed_case_csv_headers(self) -> None:
        brokerage = fidelity_api.FidelityBrokerage()
        page = MagicMock()
        page.send = AsyncMock()
        page.get = AsyncMock()
        page.wait_for_ready_state = AsyncMock()
        page.wait = AsyncMock()
        page.sleep = AsyncMock()

        menu = MagicMock()
        menu.get_position = AsyncMock(return_value=(1, 1))
        menu.scroll_into_view = AsyncMock()
        menu.mouse_move = AsyncMock()
        menu.mouse_click = AsyncMock()
        download_button = MagicMock()
        download_button.get_position = AsyncMock(return_value=(1, 1))
        download_button.scroll_into_view = AsyncMock()
        download_button.mouse_move = AsyncMock()

        def write_download() -> None:
            Path("temp_downloads/positions.csv").write_text(
                "Account Number,Account Name,Symbol,Description,Quantity,Last price,Current value\n"
                "Z12345678,Roth IRA,AAPL,APPLE INC,2,$150.00,$300.00\n",
                encoding="utf-8",
            )

        download_button.mouse_click = AsyncMock(side_effect=write_download)
        page.select = AsyncMock(side_effect=[menu, download_button])

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(fidelity_api.asyncio, "sleep", AsyncMock()),
            patch.object(fidelity_api, "print_all_holdings"),
            patch.object(fidelity_api, "print_and_discord"),
        ):
            previous_directory = Path.cwd()
            os.chdir(directory)
            try:
                await fidelity_api.fetch_holdings(page, brokerage, "Fidelity 1", None)
            finally:
                os.chdir(previous_directory)

        self.assertEqual(
            brokerage.get_holdings("Fidelity 1", "Z12345678")["AAPL"],
            {"quantity": 2.0, "price": 150.0, "total": 300.0},
        )
        self.assertEqual(brokerage.get_account_totals("Fidelity 1", "Z12345678"), 300.0)


class FidelityTwoFactorTest(unittest.IsolatedAsyncioTestCase):
    async def test_login_uses_fallback_fields_when_legacy_selectors_timeout(self) -> None:
        page = MagicMock()
        page.sleep = AsyncMock()
        page.wait_for_ready_state = AsyncMock()
        page.wait = AsyncMock()
        username = MagicMock()
        username.mouse_move = AsyncMock()
        username.mouse_click = AsyncMock()
        username.clear_input_by_deleting = AsyncMock()
        password = MagicMock()
        password.mouse_move = AsyncMock()
        password.mouse_click = AsyncMock()
        password.clear_input_by_deleting = AsyncMock()
        submit = MagicMock()
        submit.mouse_move = AsyncMock()
        submit.mouse_click = AsyncMock()

        async def select(selector: str, **_kwargs: object) -> object:
            controls = {
                "input[name='username']": username,
                "input[name='password']": password,
                "button[type='submit']": submit,
            }
            if selector in controls:
                return controls[selector]
            raise TimeoutError(selector)

        page.select = AsyncMock(side_effect=select)
        page.evaluate = AsyncMock(
            side_effect=[
                "https://digital.fidelity.com/prgw/digital/login/full-page",
                "https://digital.fidelity.com/ftgw/digital/portfolio/summary",
            ],
        )

        with patch.object(fidelity_api, "type_with_random_delay", AsyncMock()) as type_text:
            result = await fidelity_api.fidelity_login(
                page,
                "user:password:NA",
                "Fidelity 1",
                None,
                None,
            )

        self.assertTrue(result)
        self.assertEqual(
            [call.args[1] for call in type_text.await_args_list],
            ["user", "password"],
        )
        submit.mouse_click.assert_awaited_once()

    async def test_settle_page_treats_spa_wait_timeouts_as_non_fatal(self) -> None:
        page = MagicMock()
        page.sleep = AsyncMock()
        page.wait_for_ready_state = AsyncMock(side_effect=TimeoutError)
        page.wait = AsyncMock(side_effect=TimeoutError)

        await fidelity_api._settle_page(page)

        self.assertEqual(page.sleep.await_count, 2)

    async def test_push_approval_without_legacy_element_ids_still_waits(self) -> None:
        page = MagicMock()
        page.select = AsyncMock(return_value=None)

        with (
            patch.object(fidelity_api, "_settle_page", AsyncMock()),
            patch.object(fidelity_api, "_detect_2fa_challenge", AsyncMock(return_value="push")),
            patch.object(fidelity_api, "_select_first", AsyncMock(return_value=None)),
            patch.object(fidelity_api, "_trust_fidelity_device", AsyncMock()),
            patch.object(fidelity_api, "_click_button_by_text", AsyncMock(return_value=False)),
            patch.object(fidelity_api, "_wait_for_fidelity_login", AsyncMock(return_value=True)) as wait_for_login,
        ):
            result = await fidelity_api.handle_2fa(page, None, None, None, "Fidelity 1")

        self.assertTrue(result)
        wait_for_login.assert_awaited_once_with(page, timeout=120)

    async def test_generic_one_time_code_input_is_submitted(self) -> None:
        page = MagicMock()
        page.select = AsyncMock(return_value=None)
        page.sleep = AsyncMock()
        otp_input = MagicMock()
        otp_input.clear_input_by_deleting = AsyncMock()
        otp_input.send_keys = AsyncMock()
        submit_button = MagicMock()
        submit_button.mouse_move = AsyncMock()
        submit_button.mouse_click = AsyncMock()
        event_loop = MagicMock()
        event_loop.run_in_executor = AsyncMock(return_value="123456")

        select_results = [otp_input, submit_button]
        with (
            patch.object(fidelity_api, "_settle_page", AsyncMock()),
            patch.object(fidelity_api, "_detect_2fa_challenge", AsyncMock(return_value="otp")),
            patch.object(fidelity_api, "_select_first", AsyncMock(side_effect=select_results)),
            patch.object(fidelity_api, "_trust_fidelity_device", AsyncMock()),
            patch.object(fidelity_api, "_wait_for_fidelity_login", AsyncMock(return_value=True)),
            patch.object(fidelity_api.asyncio, "get_event_loop", return_value=event_loop),
        ):
            result = await fidelity_api.handle_2fa(page, None, None, None, "Fidelity 1")

        self.assertTrue(result)
        otp_input.clear_input_by_deleting.assert_awaited_once()
        otp_input.send_keys.assert_awaited_once_with("123456")
        submit_button.mouse_click.assert_awaited_once()

    async def test_phone_call_can_be_used_when_text_is_unavailable(self) -> None:
        page = MagicMock()
        page.select = AsyncMock(return_value=None)
        page.sleep = AsyncMock()
        otp_input = MagicMock()
        otp_input.clear_input_by_deleting = AsyncMock()
        otp_input.send_keys = AsyncMock()
        submit_button = MagicMock()
        submit_button.mouse_move = AsyncMock()
        submit_button.mouse_click = AsyncMock()
        event_loop = MagicMock()
        event_loop.run_in_executor = AsyncMock(return_value="654321")

        with (
            patch.object(fidelity_api, "_settle_page", AsyncMock()),
            patch.object(
                fidelity_api,
                "_detect_2fa_challenge",
                AsyncMock(side_effect=["channel", "otp"]),
            ),
            patch.object(
                fidelity_api,
                "_select_first",
                AsyncMock(side_effect=[None, otp_input, submit_button]),
            ),
            patch.object(
                fidelity_api,
                "_click_button_by_text",
                AsyncMock(side_effect=[False, True]),
            ) as click_by_text,
            patch.object(fidelity_api, "_trust_fidelity_device", AsyncMock()),
            patch.object(fidelity_api, "_wait_for_fidelity_login", AsyncMock(return_value=True)),
            patch.object(fidelity_api.asyncio, "get_event_loop", return_value=event_loop),
        ):
            result = await fidelity_api.handle_2fa(page, None, None, None, "Fidelity 1")

        self.assertTrue(result)
        self.assertEqual(click_by_text.await_count, 2)
        otp_input.send_keys.assert_awaited_once_with("654321")


if __name__ == "__main__":
    unittest.main()
