import asyncio
import csv
import datetime
import glob
import json
import math
import os
import random
import re
import traceback

from discord.ext.commands import Bot
from dotenv import load_dotenv
import pyotp
import pytz
import zendriver as uc
from zendriver import KeyPressEvent, cdp
from zendriver.core.keys import KeyEvents, SpecialKeys

from src.helper_api import (
    Brokerage,
    StockOrder,
    get_otp_from_discord,
    mask_string,
    print_all_holdings,
    print_and_discord,
)

# Controls detailed logging
COOKIES_PATH = "creds"

# --- URL Constants ---
LOGIN_URL = "https://digital.fidelity.com/prgw/digital/login/full-page?AuthRedUrl=https://digital.fidelity.com/ftgw/digital/portfolio/summary"
POSITIONS_URL = "https://digital.fidelity.com/ftgw/digital/portfolio/positions"
TRADE_URL = "https://digital.fidelity.com/ftgw/digital/trade-equity/index/orderEntry"


class FidelityBrokerage(Brokerage):
    """Brokerage state plus the accounts discovered from Fidelity's trade UI."""

    def __init__(self) -> None:
        super().__init__("Fidelity")
        self.fidelity_accounts: list[dict[str, str]] = []


def log(message: str) -> None:
    """Write opt-in Fidelity diagnostics without exposing credentials."""
    if os.getenv("FIDELITY_DEBUG", "false").lower() == "true":
        print(f"[FIDELITY DEBUG] {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}")


def create_creds_folder() -> None:
    os.makedirs(COOKIES_PATH, exist_ok=True)


async def fidelity_error(error: str, page=None, discord_loop=None, browser=None):
    print(f"Fidelity Error: {error}")
    log(f"Error encountered: {error}")
    await _record_fidelity_error(error, page)

    if discord_loop:
        print_and_discord(f"Fidelity Error: {error}", discord_loop)

    if browser:
        try:
            await browser.stop()
        except:
            pass


async def _record_fidelity_error(error: str, page=None) -> str | None:
    """Persist a Fidelity failure and optional browser artifacts.

    The default directory is under the shared ``volumes/db`` mount so errors
    survive container replacement. Override it with ``FIDELITY_ERROR_LOG_DIR``.
    """
    error_dir = os.getenv("FIDELITY_ERROR_LOG_DIR", os.path.join("volumes", "db", "fidelity_errors"))
    if not os.path.isabs(error_dir):
        error_dir = os.path.abspath(error_dir)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    try:
        os.makedirs(error_dir, exist_ok=True)
        current_url = await get_current_url(page) if page else ""
        log_path = os.path.join(error_dir, f"fidelity_error_{timestamp}.log")
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(f"time: {datetime.datetime.now().isoformat()}\n")
            handle.write(f"url: {current_url or 'unknown'}\n")
            handle.write(f"error: {error}\n\n")
            handle.write(traceback.format_exc())

        if page:
            try:
                await page.save_screenshot(filename=os.path.join(error_dir, f"fidelity_error_{timestamp}.png"))
            except Exception as exc:
                log(f"Failed to save Fidelity screenshot: {type(exc).__name__}")
            try:
                content = await page.get_content()
                with open(os.path.join(error_dir, f"fidelity_error_{timestamp}.html"), "w", encoding="utf-8") as handle:
                    handle.write(content)
            except Exception as exc:
                log(f"Failed to save Fidelity HTML: {type(exc).__name__}")
        log(f"Fidelity error artifacts saved to {log_path}")
        return log_path
    except Exception as exc:
        print(f"Failed to save Fidelity error log: {exc}")
        return None


async def get_current_url(page):
    try:
        return await page.evaluate("window.location.href")
    except:
        return ""


async def _settle_page(page) -> None:
    """Let Fidelity's SPA update without failing on transient wait timeouts."""
    await page.sleep(0.25)
    try:
        await page.wait_for_ready_state("complete", timeout=10)
    except Exception as exc:
        log(f"Page ready-state wait was non-fatal: {type(exc).__name__}")
    try:
        await page.wait()
    except Exception as exc:
        log(f"Page event wait was non-fatal: {type(exc).__name__}")
    await page.sleep(0.25)


async def _select_first(page, selectors: tuple[str, ...], timeout: float = 1):
    """Return the first matching selector while tolerating browser timeouts."""
    for selector in selectors:
        try:
            element = await page.select(selector, timeout=timeout)
        except Exception:
            element = None
        if element:
            return element
    return None


async def _click_button_by_text(page, phrases: tuple[str, ...]) -> bool:
    """Click a visible download-like control, including nested component DOM."""
    phrase_json = json.dumps(phrases)
    return bool(
        await page.evaluate(
            f"""
            (phrases => {{
                const roots = [document];
                const candidates = [];
                // Fidelity has shipped both iframe-based and shadow-DOM versions
                // of the positions grid. Walk accessible component roots instead
                // of limiting the search to the top-level document.
                for (let index = 0; index < roots.length; index++) {{
                    const root = roots[index];
                    candidates.push(...root.querySelectorAll(
                        'button, a, [role="button"], [role="menuitem"], input[type="button"], input[type="submit"]'
                    ));
                    for (const element of root.querySelectorAll('*')) {{
                        if (element.shadowRoot) roots.push(element.shadowRoot);
                    }}
                    for (const frame of root.querySelectorAll('iframe, frame')) {{
                        try {{
                            if (frame.contentDocument) roots.push(frame.contentDocument);
                        }} catch (_) {{ /* Cross-origin frames are intentionally skipped. */ }}
                    }}
                }}
                const visible = element => Boolean(
                    element && !element.disabled &&
                    (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
                );
                for (const element of candidates) {{
                    if (!visible(element)) continue;
                    const text = (
                        element.innerText || element.value ||
                        element.getAttribute('aria-label') ||
                        element.getAttribute('title') || ''
                    )
                        .trim()
                        .toLowerCase();
                    if (phrases.some(phrase => text.includes(phrase))) {{
                        element.scrollIntoView({{block: 'center', inline: 'center'}});
                        element.click();
                        return true;
                    }}
                }}
                return false;
            }})({phrase_json})
            """,
        )
    )


async def _click_component_control(page, selectors: tuple[str, ...]) -> bool:
    """Click a control matched in Fidelity's document or nested component DOM."""
    selector_json = json.dumps(selectors)
    try:
        return bool(
            await page.evaluate(
                f"""
                (selectors => {{
                    const roots = [document];
                    const clickableSelector =
                        'button, a, [role="button"], [role="menuitem"], input[type="button"], input[type="submit"]';
                    const visible = element => Boolean(
                        element && !element.disabled &&
                        (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
                    );

                    for (let index = 0; index < roots.length; index++) {{
                        const root = roots[index];
                        for (const element of root.querySelectorAll('*')) {{
                            if (element.shadowRoot) roots.push(element.shadowRoot);
                        }}
                        for (const frame of root.querySelectorAll('iframe, frame')) {{
                            try {{
                                if (frame.contentDocument) roots.push(frame.contentDocument);
                            }} catch (_) {{ /* Cross-origin frames are intentionally skipped. */ }}
                        }}

                        for (const selector of selectors) {{
                            for (const matched of root.querySelectorAll(selector)) {{
                                // Fidelity's pwefds-button is a custom-element host. The
                                // rendered button inside its shadow root owns the geometry.
                                const target = matched.matches(clickableSelector)
                                    ? matched
                                    : matched.shadowRoot?.querySelector(clickableSelector) ||
                                      matched.querySelector(clickableSelector) ||
                                      matched.closest(clickableSelector) ||
                                      matched;
                                // Closed-shadow custom-element hosts can report no
                                // box even though their internal button is rendered.
                                // Dispatching click on the host is still retargeted by
                                // the component, so only require geometry for a
                                // separately discovered inner/ancestor control.
                                if (target !== matched && !visible(target)) continue;
                                target.scrollIntoView({{block: 'center', inline: 'center'}});
                                target.click();
                                return true;
                            }}
                        }}
                    }}
                    return false;
                }})({selector_json})
                """,
            )
        )
    except Exception as exc:
        log(f"Component DOM click was unavailable: {type(exc).__name__}")
        return False


async def _click_fidelity_element(element) -> None:
    """Click a Fidelity control without requiring custom-host geometry."""
    try:
        # Invoke the DOM method directly on the CDP-resolved node. Zendriver's
        # Element.click() flashes the element first, which still requests a
        # bounding box and fails for Fidelity's closed-shadow custom host.
        await element.apply("(element) => { element.click(); return true; }")
        return
    except (AttributeError, TypeError):
        # Keep compatibility with older/fake element implementations.
        pass
    await element.scroll_into_view()
    await element.mouse_move()
    await element.mouse_click()


async def _click_positions_download(page) -> bool:
    """Open Fidelity's positions actions menu and start a CSV download."""
    native_menu = await _select_first(
        page,
        (
            "pwefds-button[data-testid='kebab-menu'] button",
            "pwefds-button[data-testid='kebab-menu'] > s-root button",
        ),
        timeout=2,
    )
    if native_menu:
        await _click_fidelity_element(native_menu)
        await page.sleep(0.25)
        native_download = await _select_first(page, ("#kebabmenuitem-download",), timeout=2)
        if native_download:
            await _click_fidelity_element(native_download)
            return True

    menu_selectors = (
        "pwefds-button[data-testid='kebab-menu'] button",
        "pwefds-button[data-testid='kebab-menu'] > s-root button",
        "#posweb-grid_top-kebab_popover-button",
        "[data-testid='kebab-menu']",
        "button[aria-label*='more' i]",
        "button[aria-label*='action' i]",
        "button[title*='more' i]",
        "[data-testid*='overflow' i]",
    )
    menu_opened = await _click_component_control(page, menu_selectors)
    if menu_opened:
        await page.sleep(0.25)
        if await _click_component_control(
            page,
            (
                "#kebabmenuitem-download",
                "[data-testid*='download' i]",
                "a[download]",
                "button[aria-label*='download' i]",
                "a[aria-label*='download' i]",
            ),
        ):
            return True
        if await _click_button_by_text(page, ("download", "export")):
            return True

    menu = await _select_first(
        page,
        menu_selectors,
        timeout=2,
    )
    if menu:
        await _click_fidelity_element(menu)
        await page.sleep(0.25)

        download = await _select_first(
            page,
            (
                "#kebabmenuitem-download",
                "[data-testid*='download' i]",
                "a[download]",
                "button[aria-label*='download' i]",
                "a[aria-label*='download' i]",
            ),
            timeout=2,
        )
        if download:
            await _click_fidelity_element(download)
            return True
        if await _click_button_by_text(page, ("download", "export")):
            return True

    # Some versions of the positions page expose Download directly, without an
    # overflow menu.
    direct_download_selectors = (
        "#posweb-grid_top-download",
        "[data-testid*='download' i]",
        "a[download]",
        "button[aria-label*='download' i]",
        "a[aria-label*='download' i]",
        "button[title*='download' i]",
    )
    if await _click_component_control(page, direct_download_selectors):
        return True
    download = await _select_first(
        page,
        direct_download_selectors,
        timeout=2,
    )
    if download:
        await _click_fidelity_element(download)
        return True
    return await _click_button_by_text(page, ("download", "export"))


async def _detect_2fa_challenge(page) -> str | None:
    """Identify Fidelity 2FA pages using attributes and visible copy."""
    return await page.evaluate("""
        (() => {
            const visible = element => Boolean(
                element && (element.offsetWidth || element.offsetHeight ||
                element.getClientRects().length)
            );
            const firstVisible = selectors => selectors
                .map(selector => document.querySelector(selector))
                .find(visible);
            const bodyText = (document.body?.innerText || '').toLowerCase();
            const controls = Array.from(document.querySelectorAll(
                'button, a[role="button"], input[type="button"], input[type="submit"]'
            ));
            const controlText = controls
                .map(element => (element.innerText || element.value || '').toLowerCase())
                .join(' ');

            if (firstVisible([
                '#dom-totp-security-code-input',
                'input[name*="totp" i]',
                'input[id*="totp" i]'
            ])) return 'totp';

            if (firstVisible([
                '#dom-otp-code-input',
                'input[autocomplete="one-time-code"]',
                'input[name*="security-code" i]',
                'input[name*="otp" i]',
                'input[id*="otp" i]'
            ])) return 'otp';

            if (
                document.querySelector('#dom-push-authenticator-header') ||
                bodyText.includes('notification to the fidelity') ||
                bodyText.includes('fidelity investments app') ||
                bodyText.includes('approve the notification') ||
                bodyText.includes('check your notifications')
            ) return 'push';

            if (
                document.querySelector('#dom-channel-list-header') ||
                controlText.includes('text me') ||
                controlText.includes('call me') ||
                controlText.includes('send a text')
            ) return 'channel';

            if (
                controlText.includes('try another way') ||
                controlText.includes('other ways to verify') ||
                controlText.includes('choose another method') ||
                bodyText.includes('select how to verify')
            ) return 'alternate';

            return null;
        })()
    """)


async def _trust_fidelity_device(page) -> None:
    await page.evaluate("""
        (() => {
            const checkbox = document.querySelector(
                '#dom-trust-device-checkbox, input[name*="trust" i], input[id*="trust" i]'
            );
            if (checkbox && !checkbox.checked) checkbox.click();
        })()
    """)


async def _wait_for_fidelity_login(page, timeout: float = 15) -> bool:
    """Wait for Fidelity to leave its login flow."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        current_url = await get_current_url(page)
        if _is_fidelity_portfolio_url(current_url):
            return True
        await page.sleep(0.5)
    return False


def _is_fidelity_auth_url(url: str) -> bool:
    """Return whether Fidelity redirected a protected request to authentication."""
    normalized = (url or "").casefold()
    return any(
        marker in normalized
        for marker in (
            "/prgw/digital/signin/",
            "/prgw/digital/login/",
            "/digital/login/",
        )
    )


def _is_fidelity_portfolio_url(url: str) -> bool:
    """Return whether *url* is an authenticated Fidelity portfolio route."""
    normalized = (url or "").casefold()
    return "/ftgw/digital/portfolio/" in normalized and not _is_fidelity_auth_url(normalized)


async def _verify_fidelity_session(page) -> bool:
    """Verify authentication against the protected positions endpoint."""
    await page.get(POSITIONS_URL)
    await _settle_page(page)
    current_url = await get_current_url(page)
    return "portfolio/positions" in current_url.casefold() and _is_fidelity_portfolio_url(current_url)


async def type_with_random_delay(element, text, min_delay=0.05, max_delay=0.15):
    """Type text with random delays between characters"""
    # Get the payloads for each character
    payloads = KeyEvents.from_text(text, KeyPressEvent.DOWN_AND_UP)

    for payload in payloads:
        await element._tab.send(cdp.input_.dispatch_key_event(**payload))
        await asyncio.sleep(random.uniform(min_delay, max_delay))


async def _start_fidelity_browser(browser_args, profile_path, *, docker_mode):
    """Start Chromium with its inner sandbox disabled only inside Docker."""
    return await uc.start(
        browser_args=browser_args,
        user_data_dir=profile_path,
        sandbox=not docker_mode,
    )


def fidelity_run(
    order_obj: StockOrder,
    bot_obj: Bot | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    *,
    docker_mode: bool = False,
) -> Brokerage | None:
    """Run the GUI-derived Zendriver Fidelity workflow for an auto-rsa order."""
    load_dotenv()
    print("Starting Fidelity run process...")
    log("fidelity_run initiated.")
    create_creds_folder()

    # Create a dedicated event loop for this run
    local_fidelity_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(local_fidelity_loop)

    credentials = os.getenv("FIDELITY", "").strip()
    if not credentials:
        print("Fidelity not found, skipping...")
        local_fidelity_loop.close()
        return None

    accounts_env = [account.strip() for account in credentials.split(",") if account.strip()]
    fidelity_brokerage_obj = FidelityBrokerage()

    action_to_perform = "_holdings" if order_obj.get_holdings() else "_transaction"

    try:
        populated_obj = local_fidelity_loop.run_until_complete(
            _async_fidelity_run_wrapper(
                accounts_env,
                fidelity_brokerage_obj,
                action_to_perform,
                bot_obj,
                loop,
                order_obj,
                docker_mode,
            )
        )

        if populated_obj:
            order_obj.set_logged_in(populated_obj, "fidelity")
        return populated_obj

    except Exception as e:
        print(f"Critical error in Fidelity run: {e}")
        traceback.print_exc()
        raise
    finally:
        try:
            local_fidelity_loop.close()
            log("Local event loop closed.")
        except Exception as e_loop:
            log(f"Error closing local loop: {e_loop}")


async def _async_fidelity_run_wrapper(
    accounts_env: list[str],
    brokerage_obj: FidelityBrokerage,
    action: str,
    bot_obj: Bot | None,
    discord_loop: asyncio.AbstractEventLoop | None,
    order_obj: StockOrder,
    docker_mode: bool = False,
) -> Brokerage:
    headless = os.getenv("HEADLESS", "true").lower() == "true"

    for acc_idx, account_cred_str in enumerate(accounts_env):
        account_name_key = f"Fidelity {acc_idx + 1}"
        browser = None
        page = None

        try:
            browser_args = []
            if docker_mode:
                browser_args.extend(
                    ["--disable-dev-shm-usage", "--disable-gpu", "--window-size=1920,1080"],
                )
            elif headless:
                browser_args.extend([
                    "--headless=new",
                    "--window-size=1920,1080",
                    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-site-isolation-trials",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-session-crashed-bubble",
                    "--disable-infobars",
                    "--disable-features=TranslateUI,VizDisplayCompositor",
                    "--no-first-run",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1920,1080",
                ])
            else:
                browser_args.extend(["--start-maximized", "--disable-session-crashed-bubble", "--disable-infobars"])

            profile_path = os.path.abspath(os.path.join(COOKIES_PATH, f"ZenFidelity_{acc_idx + 1}"))

            browser_args.append("--force-device-scale-factor=0.8")

            log(f"Starting browser for {account_name_key}...")
            browser = await _start_fidelity_browser(
                browser_args,
                profile_path,
                docker_mode=docker_mode,
            )
            page = await browser.get(LOGIN_URL) if not browser.tabs else await browser.tabs[0].get(LOGIN_URL)

            # Login
            success = await fidelity_login(page, account_cred_str, account_name_key, bot_obj, discord_loop)
            if not success:
                raise Exception("Login failed.")

            print(f"{account_name_key}: Verifying Fidelity session...")
            if not await _verify_fidelity_session(page):
                # Fidelity can briefly expose the portfolio summary from a stale
                # browser profile before a protected request redirects to the
                # current retail sign-in flow. Log in once more from that actual
                # redirect page instead of reporting a misleading download error.
                log(f"{account_name_key}: Protected positions page redirected to sign-in; retrying login.")
                print(f"{account_name_key}: Session was not authenticated; retrying sign-in...")
                success = await fidelity_login(page, account_cred_str, account_name_key, bot_obj, discord_loop)
                if not success or not await _verify_fidelity_session(page):
                    current_url = await get_current_url(page)
                    raise RuntimeError(
                        "Fidelity authentication did not persist for the positions page "
                        f"(current URL: {current_url or 'unknown'})"
                    )

            log(f"Login successful for {account_name_key}!")
            print(f"{account_name_key}: Fidelity session verified.")
            brokerage_obj.set_logged_in_object(account_name_key, browser)

            # Action
            if action == "_holdings":
                await fetch_holdings(page, brokerage_obj, account_name_key, discord_loop)
            elif action == "_transaction":
                # Ensure accounts are fetched before transaction
                await fetch_accounts(page, brokerage_obj, name=account_name_key, loop=discord_loop)
                await fidelity_transaction(page, brokerage_obj, order_obj, account_name_key, discord_loop)

        except Exception as e:
            await fidelity_error(f"Error in {account_name_key}: {e}", page, discord_loop, browser)
        finally:
            if browser:
                try:
                    await asyncio.sleep(2)
                    for tab in browser.tabs:
                        try:
                            await tab.close()
                        except:
                            pass
                    await asyncio.sleep(1)
                    await browser.stop()
                except:
                    pass

    return brokerage_obj


async def fidelity_login(page, account_cred_str, name, bot_obj, discord_loop):
    try:
        username, password_and_totp = account_cred_str.split(":", 1)
        password, totp_secret = password_and_totp.rsplit(":", 1)
    except ValueError:
        log(f"{name}: Fidelity credentials must contain a username and password.")
        return False

    log(f"Logging in {name}...")

    curr_url = await get_current_url(page)
    # Check if we are already logged in (must NOT be on the login page)
    if _is_fidelity_portfolio_url(curr_url):
        log("Already logged in.")
        return True

    try:
        await _settle_page(page)
        user_input = await _select_first(
            page,
            (
                "#dom-username-input",
                "input[name='username']",
                "#userId-input",
                "#userId",
                "input[autocomplete='username']",
                "input[type='email']",
            ),
            timeout=2,
        )
        if not user_input:
            raise RuntimeError("Fidelity username field was not found")
        await user_input.mouse_move()
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await user_input.mouse_click()
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await user_input.clear_input_by_deleting()
        await type_with_random_delay(user_input, username)

        pass_input = await _select_first(
            page,
            (
                "#dom-pswd-input",
                "#password",
                "input[name='password']",
                "input[autocomplete='current-password']",
                "input[type='password']",
            ),
            timeout=2,
        )
        if not pass_input:
            raise RuntimeError("Fidelity password field was not found")
        await pass_input.mouse_move()
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await pass_input.mouse_click()
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await pass_input.clear_input_by_deleting()
        await type_with_random_delay(pass_input, password)

        login_btn = await _select_first(
            page,
            (
                "#dom-login-button",
                "button[type='submit']",
                "input[type='submit']",
            ),
            timeout=2,
        )
        if login_btn:
            await login_btn.mouse_move()
            await asyncio.sleep(random.uniform(0.1, 0.3))
            await login_btn.mouse_click()
            await asyncio.sleep(random.uniform(0.1, 0.3))
        elif not await _click_button_by_text(page, ("log in", "sign in")):
            raise RuntimeError("Fidelity login button was not found")

        # Efficient wait loop for Redirection or 2FA
        log("Waiting for login result (Redirect or 2FA)...")
        start_time = datetime.datetime.now()

        # Extended wait for 2FA detection logic
        while (datetime.datetime.now() - start_time).seconds < 45:
            curr_url = await get_current_url(page)

            if _is_fidelity_portfolio_url(curr_url):
                log("Redirected to an authenticated portfolio page. Login complete.")
                return True

            challenge = await _detect_2fa_challenge(page)
            if challenge:
                log(f"2FA challenge detected ({challenge}). Initiating handler...")
                return await handle_2fa(page, bot_obj, discord_loop, totp_secret, name)

            await _settle_page(page)

        log("Login timed out or failed to redirect.")
        return False

    except Exception as e:
        log(f"Login exception: {e}")
        return False


async def handle_2fa(page, bot_obj, discord_loop, totp_secret, name):
    try:
        await _settle_page(page)
        challenge = await _detect_2fa_challenge(page)

        if challenge == "alternate":
            log("Alternate 2FA method selection detected.")
            if not await _click_button_by_text(
                page,
                ("try another way", "other ways to verify", "choose another method"),
            ):
                log("Unable to open Fidelity's alternate verification methods.")
                return False
            await _settle_page(page)
            challenge = await _detect_2fa_challenge(page)

        # -----------------------------------------------------
        # CASE 1: In-App Push Notification
        # -----------------------------------------------------
        # Look for header: "We'll send a notification to the Fidelity Investments app..."
        push_header = None
        try:
            push_header = await page.select("#dom-push-authenticator-header", timeout=1)
        except:
            push_header = None

        if push_header or challenge == "push":
            log("Push Notification 2FA detected.")

            # 1. Click 'Don't ask me again' if present
            await _trust_fidelity_device(page)
            await _settle_page(page)

            # 2. Click 'Send notification' button
            send_btn = await _select_first(
                page,
                (
                    "#dom-push-primary-button",
                    "button[id*='push'][id*='primary']",
                    "button[data-testid*='push']",
                ),
                timeout=2,
            )
            if send_btn:
                log("Clicking 'Send notification' button...")
                await send_btn.mouse_click()
            else:
                clicked = await _click_button_by_text(
                    page,
                    ("send notification", "send push", "notify me"),
                )
                if clicked:
                    log("Clicked push notification button by visible text.")

            # Some Fidelity variants send the push before this page renders.
            msg = f"{name}: Fidelity app approval required. Please approve within 2 minutes."
            log(msg)
            if bot_obj and discord_loop:
                print_and_discord(msg, discord_loop)
            log("Waiting for push approval...")
            if await _wait_for_fidelity_login(page, timeout=120):
                log("Push approved, redirected successfully.")
                return True
            log("Push notification timed out.")
            return False

        # -----------------------------------------------------
        # CASE 2: SMS/Call Channel Selection
        # -----------------------------------------------------
        # Look for header: "To verify it's you, we'll send a temporary code..."
        channel_header = None
        try:
            channel_header = await page.select("#dom-channel-list-header", timeout=1)
        except:
            channel_header = None

        if channel_header or challenge == "channel":
            log("SMS/Call Selection 2FA detected.")

            # Select "Text me the code"
            text_btn = await _select_first(
                page,
                (
                    "#dom-channel-list-primary-button",
                    "button[data-testid*='text']",
                    "button[id*='text']",
                ),
                timeout=2,
            )
            if text_btn:
                log("Clicking 'Text me the code'...")
                await text_btn.mouse_move()
                await text_btn.mouse_click()
            else:
                clicked = await _click_button_by_text(
                    page,
                    ("text me", "send a text", "text message"),
                )
                if not clicked:
                    clicked = await _click_button_by_text(
                        page,
                        ("call me", "phone call", "call with a code"),
                    )
                if not clicked:
                    log("No supported phone verification option was available.")
                    return False
            await _settle_page(page)
            challenge = await _detect_2fa_challenge(page)

        # -----------------------------------------------------
        # CASE 3: SMS Input Screen
        # -----------------------------------------------------
        # Can appear directly or after Case 2
        otp_input = await _select_first(
            page,
            (
                "#dom-otp-code-input",
                "input[autocomplete='one-time-code']",
                "input[name*='security-code']",
                "input[name*='otp']",
                "input[id*='otp']",
            ),
        )

        if otp_input and challenge == "otp":
            log("One-time security code input detected.")

            code = None
            if bot_obj and discord_loop:
                future = asyncio.run_coroutine_threadsafe(get_otp_from_discord(bot_obj, name, code_len=6, timeout=300, loop=discord_loop), discord_loop)
                code = await asyncio.wrap_future(future)
            else:
                code = await asyncio.get_event_loop().run_in_executor(None, input, f"Enter Fidelity security code for {name}: ")

            if code:
                log("Entering one-time security code...")
                await otp_input.clear_input_by_deleting()
                await otp_input.send_keys(code)

                # Click 'Don't ask me again'
                await _trust_fidelity_device(page)
                await page.sleep(0.25)

                # Click Submit
                submit_btn = await _select_first(
                    page,
                    (
                        "#dom-otp-code-submit-button",
                        "button[type='submit']",
                        "input[type='submit']",
                    ),
                    timeout=2,
                )
                if submit_btn:
                    await submit_btn.mouse_move()
                    await submit_btn.mouse_click()
                else:
                    await _click_button_by_text(page, ("submit", "continue", "verify"))
                return await _wait_for_fidelity_login(page)

            return False

        # -----------------------------------------------------
        # CASE 4: TOTP Authenticator (VIP Access / App Code)
        # -----------------------------------------------------
        auth_input = await _select_first(
            page,
            (
                "#dom-totp-security-code-input",
                "input[name*='totp']",
                "input[id*='totp']",
            ),
        )

        if auth_input and challenge == "totp":
            log("TOTP Authenticator detected.")
            code = None

            if totp_secret and totp_secret.lower() != "na":
                try:
                    totp = pyotp.TOTP(totp_secret.replace(" ", ""))
                    code = totp.now()
                    log("Generated TOTP code locally.")
                except Exception as e:
                    log(f"Failed to generate TOTP code: {e}")

            if not code:
                if bot_obj and discord_loop:
                    future = asyncio.run_coroutine_threadsafe(get_otp_from_discord(bot_obj, name, code_len=6, timeout=300, loop=discord_loop), discord_loop)
                    code = await asyncio.wrap_future(future)
                else:
                    code = await asyncio.get_event_loop().run_in_executor(None, input, f"Enter Fidelity TOTP for {name}: ")

            if code:
                await auth_input.mouse_move()
                await auth_input.mouse_click()
                await auth_input.send_keys(code)

                await _trust_fidelity_device(page)
                await page.sleep(0.25)

                continue_btn = await _select_first(
                    page,
                    (
                        "#dom-totp-code-continue-button",
                        "button[type='submit']",
                        "input[type='submit']",
                    ),
                    timeout=5,
                )
                if continue_btn:
                    await continue_btn.mouse_move()
                    await continue_btn.mouse_click()
                else:
                    await _click_button_by_text(page, ("continue", "verify", "submit"))
                return await _wait_for_fidelity_login(page)

    except Exception as e:
        log(f"2FA Error: {e}")
        traceback.print_exc()
    return False


async def set_download_path(page, path):
    try:
        await page.send(cdp.browser.set_download_behavior(behavior="allow", download_path=path, events_enabled=True))
    except Exception as e:
        log(f"Failed to set download path: {e}")


async def fetch_accounts(page, brokerage_obj, name, loop):
    """
    Captures accounts via UI scraping on the Trade page.
    """
    log("Fetching accounts info via UI...")
    found_accounts = []

    try:
        if TRADE_URL not in await get_current_url(page):
            await page.get(TRADE_URL)
            # ---------------------------------------------------------
            # NEW: ENSURE EXPANDED TICKET MODE
            # ---------------------------------------------------------
            log("Verifying Ticket Mode (Expanded vs Simplified)...")

            try:
                # Check if 'View simplified ticket' button is present
                # If present, it means we are CURRENTLY in Expanded Mode (Desired State)
                is_expanded_mode = await page.select("#show-fewer-trade-selections", timeout=1)

                if is_expanded_mode:
                    log("Expanded ticket mode detected (Active). Proceeding.")
                else:
                    log("Expanded ticket mode NOT detected. Looking for 'View expanded ticket' button...")

                    # Check for the 'View expanded ticket' button
                    expand_btn = await page.select("#show-more-trade-selections", timeout=5)

                    if expand_btn:
                        log("Found 'View expanded ticket' button. Clicking to switch modes...")
                        await expand_btn.scroll_into_view()
                        await expand_btn.mouse_move()
                        await expand_btn.mouse_click()
                        await page.select("#show-fewer-trade-selections", timeout=1)
                    else:
                        log("Warning: 'View expanded ticket' button not found. Layout might differ or already strictly enforced.")

            except Exception as e:
                log(f"Error ensuring Expanded Ticket Mode: {e}")
            # ---------------------------------------------------------

        dropdown_selector = "#dest-acct-dropdown"

        # Wait for dropdown
        await page.sleep(0.25)
        await page.wait_for_ready_state("complete", timeout=10)
        await page.wait()
        await page.sleep(0.25)

        found_dd = False
        for _ in range(20):
            if await page.evaluate(f'document.querySelector("{dropdown_selector}") !== null'):
                found_dd = True
                break
            await page.sleep(0.25)

        if found_dd:
            # Open dropdown to load list
            await page.evaluate(f'document.querySelector("{dropdown_selector}").click()')

            # Scrape items
            scraped_data = await page.evaluate("""
                (function() {
                    const list = document.getElementById("ett-acct-sel-list");
                    if (!list) return [];
                    const buttons = list.querySelectorAll('div[role="option"] button');
                    let results = [];
                    for (let btn of buttons) {
                        results.push(btn.innerText.trim());
                    }
                    return results;
                })();
            """)

            # Parse scraped strings "Nickname (Number)"
            for item in scraped_data:
                match = re.search(r"(.*?)\s*\((Z?\d+)\)", item)
                if match:
                    nickname = match.group(1).strip()
                    acct_num = match.group(2).strip()
                    found_accounts.append({"acctNum": acct_num, "name": nickname, "type": "Unknown", "desc": "Scraped"})
                    log(f"Captured Account (UI): {nickname} ({mask_string(acct_num)})")
        else:
            log("Trade dropdown not found for account fetch.")
    except Exception as e:
        log(f"UI Account Fetch Error: {e}")
        traceback.print_exc()

    brokerage_obj.fidelity_accounts = found_accounts
    log(f"Total Accounts Captured: {len(found_accounts)}")


async def fetch_holdings(page, brokerage_obj, name, loop):
    log("Fetching holdings via CSV...")
    download_dir = os.path.join(os.getcwd(), "temp_downloads")
    if not os.path.exists(download_dir):
        os.makedirs(download_dir)

    try:
        for f in glob.glob(os.path.join(download_dir, "*.csv")):
            os.remove(f)
    except:
        pass

    try:
        await set_download_path(page, download_dir)
        await page.get(POSITIONS_URL)
        await _settle_page(page)

        # The document's load event precedes the positions grid in Fidelity's
        # client-rendered app, especially in a resource-constrained container.
        # Poll long enough for the grid/action toolbar to be mounted.
        clicked_download = False
        for _ in range(20):
            if await _click_positions_download(page):
                clicked_download = True
                break
            await page.sleep(0.5)

        if not clicked_download:
            current_url = await get_current_url(page)
            msg = (
                "Could not find a Download or Export control after waiting for "
                f"Fidelity's positions page to render (current URL: {current_url or 'unknown'})"
            )
            raise RuntimeError(msg)

        log("Waiting for CSV download...")
        downloaded_file = None
        for _ in range(30):
            files = glob.glob(os.path.join(download_dir, "*.csv"))
            if files:
                files.sort(key=os.path.getmtime, reverse=True)
                downloaded_file = files[0]
                await asyncio.sleep(1)
                break
            await asyncio.sleep(1)

        if not downloaded_file:
            await _record_fidelity_error(f"{name}: Download timed out", page)
            print_and_discord(f"{name}: Download timed out.", loop)
            return

        account_totals = {}
        seen_accounts = set()

        with open(downloaded_file, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                normalized_row = {
                    str(column).strip().casefold(): value
                    for column, value in row.items()
                    if column is not None
                }
                acc_num = normalized_row.get("account number")
                if not acc_num or "and" in acc_num or str(acc_num).startswith("Y") or not normalized_row.get("symbol"):
                    continue

                acc_name = normalized_row.get("account name", "Current Portfolio")
                symbol = normalized_row.get("symbol")
                desc = normalized_row.get("description", "")

                def clean_num(v):
                    if not v:
                        return 0.0
                    try:
                        clean_v = str(v).replace("$", "").replace(",", "").replace("%", "").strip()
                        if clean_v in ["--", "n/a", "N/A", "-"]:
                            return 0.0
                        return float(clean_v or 0)
                    except ValueError:
                        return 0.0

                qty = clean_num(normalized_row.get("quantity"))
                last_price = clean_num(normalized_row.get("last price"))
                current_val = clean_num(normalized_row.get("current value"))

                if "Pending" in str(symbol) or "Pending" in desc:
                    pass
                if not symbol and "Cash" in desc:
                    symbol = "CASH"

                if acc_num not in seen_accounts:
                    brokerage_obj.set_account_number(name, acc_num)
                    brokerage_obj.set_account_type(name, acc_num, acc_name)
                    seen_accounts.add(acc_num)

                if acc_num not in account_totals:
                    account_totals[acc_num] = 0.0
                account_totals[acc_num] += current_val

                if symbol and (qty > 0 or current_val > 0):
                    if not symbol:
                        symbol = "OTHER"
                    brokerage_obj.set_holdings(name, acc_num, symbol, qty, last_price)

        for acc_num, total in account_totals.items():
            brokerage_obj.set_account_totals(name, acc_num, total)

        print_all_holdings(brokerage_obj, loop)

        try:
            f.close()
            os.remove(downloaded_file)
        except:
            pass

    except Exception as e:
        await _record_fidelity_error(f"{name} Holdings Error: {e}", page)
        print_and_discord(f"{name} Holdings Error: {e}", loop)
        traceback.print_exc()


async def _select_dropdown_option(page, dropdown_opener_selector, option_value, timeout=10):
    """
    Helper to select an option from a custom dropdown.
    """
    try:
        log(f"Clicking dropdown '{dropdown_opener_selector}' and selecting '{option_value}'.")
        await page.wait_for_ready_state("complete")

        # 1. Open the dropdown
        opener = await page.select(dropdown_opener_selector, timeout=timeout)
        await opener.scroll_into_view()
        await opener.mouse_click()

        await page.select("#ett-acct-sel-list", timeout=timeout)

        option = await page.find(option_value, best_match=True)
        await option.scroll_into_view()
        await option.mouse_click()

    except Exception as e:
        print(f"Error selecting option: {e}")
        raise Exception(f"Failed to select '{option_value}' from dropdown '{dropdown_opener_selector}'.")


async def fidelity_transaction(page, brokerage_obj, order_obj, name, loop):
    log("Starting transaction loop...")

    # Ensure accounts are populated
    if not hasattr(brokerage_obj, "fidelity_accounts") or not brokerage_obj.fidelity_accounts:
        log("Account list not populated, fetching...")
        await fetch_accounts(page, brokerage_obj, name, loop)

    if not hasattr(brokerage_obj, "fidelity_accounts") or not brokerage_obj.fidelity_accounts:
        log("Failed to fetch accounts or no accounts found. Aborting transaction.")
        return

    accounts_to_process = []
    # Note: StockOrder class doesn't usually carry a specific account number,
    # but we keep this check for compatibility if the object differs.
    if hasattr(order_obj, "account_number") and order_obj.account_number:
        accounts_to_process = [a for a in brokerage_obj.fidelity_accounts if a["acctNum"] == order_obj.account_number]
        if not accounts_to_process:
            print_and_discord(f"{name}: Specified account {order_obj.account_number} not found.", loop)
            return
    else:
        accounts_to_process = brokerage_obj.fidelity_accounts

    # --- Use Getters for StockOrder Object ---
    try:
        action_val = order_obj.get_action()
        quantity_val = order_obj.get_amount()
        is_dry_run = order_obj.get_dry()
        symbols = order_obj.get_stocks()
    except AttributeError:
        # Fallback if order_obj is not the expected class
        log("Warning: order_obj missing expected getters, trying attributes...")
        action_val = getattr(order_obj, "action", getattr(order_obj, "order_type", None))
        quantity_val = getattr(order_obj, "quantity", None)
        is_dry_run = getattr(order_obj, "dry_run", False)
        single_sym = getattr(order_obj, "symbol", None)
        symbols = [single_sym] if single_sym else []

    if not action_val or quantity_val is None or not symbols:
        log(f"Critical Error: Missing Order Details. Action: {action_val}, Qty: {quantity_val}, Symbols: {symbols}")
        return

    action_upper = action_val.upper()

    # Iterate through symbols to support list of stocks
    for symbol in symbols:
        log(f"Processing Symbol: {symbol} ({action_upper})")

        for account in accounts_to_process:
            acct_num = account["acctNum"]
            acct_name = account.get("name", "Unknown")
            masked_account = mask_string(acct_num)
            log(f"--- Processing Account: {acct_name} ({masked_account}) for {symbol} ---")
            print_and_discord(
                f"{name}: {action_upper}ing {quantity_val} of {symbol} in {acct_name} ({masked_account})",
                loop,
            )

            try:
                await page.get(TRADE_URL)
                await page.select("#previewOrderBtn", timeout=10)

                # ---------------------------------------------------------
                # NEW: ENSURE EXPANDED TICKET MODE
                # ---------------------------------------------------------
                log("Verifying Ticket Mode (Expanded vs Simplified)...")

                try:
                    # Check if 'View simplified ticket' button is present
                    # If present, it means we are CURRENTLY in Expanded Mode (Desired State)
                    is_expanded_mode = await page.select("#show-fewer-trade-selections", timeout=1)

                    if is_expanded_mode:
                        log("Expanded ticket mode detected (Active). Proceeding.")
                    else:
                        log("Expanded ticket mode NOT detected. Looking for 'View expanded ticket' button...")

                        # Check for the 'View expanded ticket' button
                        expand_btn = await page.select("#show-more-trade-selections", timeout=5)

                        if expand_btn:
                            log("Found 'View expanded ticket' button. Clicking to switch modes...")
                            await expand_btn.scroll_into_view()
                            await expand_btn.mouse_move()
                            await expand_btn.mouse_click()
                            await page.select("#show-fewer-trade-selections", timeout=1)
                        else:
                            log("Warning: 'View expanded ticket' button not found. Layout might differ or already strictly enforced.")

                except Exception as e:
                    log(f"Error ensuring Expanded Ticket Mode: {e}")
                # ---------------------------------------------------------

                dropdown_selector = "#dest-acct-dropdown"
                await _select_dropdown_option(page, dropdown_selector, acct_num)

                # ---------------------------------------------------------
                # EXTENDED HOURS LOGIC (Updated: Toggle Priority + Time Fallback)
                # ---------------------------------------------------------
                et_tz = pytz.timezone("US/Eastern")
                now_et = datetime.datetime.now(et_tz)
                current_time = now_et.time()
                is_extended_time = False

                toggle_row = None
                switch_root = None
                try:
                    toggle_row = await page.select(".eq-ticket__extended-hrs-toggle-row_dest", timeout=1)
                except:
                    pass

                toggle_element_exists = toggle_row is not None

                if toggle_element_exists:
                    log("Extended Hours Toggle Element DETECTED. Forcing Extended Hours Mode.")
                    is_extended_time = True

                    # 2. Check if toggle is ON using element properties
                    is_toggled_on = False
                    switch_root = await page.select(".eq-ticket__extendedhour-toggle", timeout=5)
                    if switch_root:
                        # Check if element has the 'pvd-switch--on' class
                        is_toggled_on = "pvd-switch--on" in switch_root.attrs.get("class", "")

                    if not is_toggled_on:
                        # Fallback: check aria-checked attribute on button
                        try:
                            toggle_btn = await page.select("#eq-ticket_extendedhour", timeout=2)
                            if toggle_btn and toggle_btn.attrs.get("aria-checked") == "true":
                                is_toggled_on = True
                        except:
                            pass

                    if is_toggled_on:
                        log("Extended Hours Switch is already ON.")
                    else:
                        log("Extended Hours Switch is OFF. Clicking to Enable...")
                        try:
                            # Try clicking the button ID first
                            btn_clicked = False
                            try:
                                toggle_btn = await page.select("#eq-ticket_extendedhour", timeout=2)
                                if toggle_btn is not None:
                                    await toggle_btn.mouse_move()
                                    await toggle_btn.mouse_click()
                                    btn_clicked = True
                            except:
                                pass

                            if not btn_clicked:
                                # Fallback to clicking the switch wrapper
                                try:
                                    switch_wrapper = await page.select(".eq-ticket__extendedhour-toggle", timeout=2)
                                    if switch_wrapper is not None:
                                        await switch_wrapper.mouse_move()
                                        await switch_wrapper.mouse_click()
                                except:
                                    pass

                            await page.sleep(0.25)
                            await page.wait_for_ready_state("complete", timeout=10)
                            await page.wait()
                            await page.sleep(0.25)
                        except Exception as e:
                            log(f"Error toggling Extended Hours switch: {e}")
                else:
                    log("Extended Hours Toggle Element NOT DETECTED. Proceeding with Normal Order logic.")

                current_price = 0.0

                log(f"Entering symbol: {symbol}")

                # Reverting to send_keys method per user request
                symbol_input = await page.select("#eq-ticket-dest-symbol")
                if symbol_input:
                    await symbol_input.send_keys(symbol)
                    # Correct way to press Enter
                    await symbol_input.send_keys(SpecialKeys.ENTER)
                await page.wait_for("#ett-more-less-quote-link", timeout=10)

                log("Waiting for price data via DOM parsing...")
                await page.sleep(0.5)
                await page.wait_for_ready_state("complete", timeout=10)
                await page.wait()
                await page.sleep(0.5)

                # Parse DOM for Price (Last, Bid, Ask)
                price_data = await page.evaluate("""
                    (function() {
                        function parsePrice(text) {
                            if (!text) return 0.0;
                            return parseFloat(text.replace(/[$,]/g, '').trim()) || 0.0;
                        }

                        let last = 0.0, bid = 0.0, ask = 0.0;

                        // Last Price
                        const lastEl = document.querySelector('.last-price');
                        if (lastEl) last = parsePrice(lastEl.innerText);

                        // Bid/Ask blocks
                        const blocks = document.querySelectorAll('.eq-ticket__quote--block');
                        for (let block of blocks) {
                            const title = block.querySelector('.block-title');
                            const num = block.querySelector('.number');
                            if (title && num) {
                                if (title.innerText.includes('Bid')) {
                                    bid = parsePrice(num.innerText);
                                } else if (title.innerText.includes('Ask')) {
                                    ask = parsePrice(num.innerText);
                                }
                            }
                        }
                        return { last: last, bid: bid, ask: ask };
                    })();
                """)

                last_price = price_data.get("last", 0.0)
                bid_price = price_data.get("bid", 0.0)
                ask_price = price_data.get("ask", 0.0)

                log(f"Scraped Prices - Last: {last_price}, Bid: {bid_price}, Ask: {ask_price}")

                # Determine Price to Use based on Action
                # BUY -> Use Ask (paying), Fallback to Last
                # SELL -> Use Bid (receiving), Fallback to Last
                if action_upper == "BUY":
                    if ask_price > 0:
                        current_price = ask_price
                    else:
                        current_price = last_price
                elif action_upper == "SELL":
                    if bid_price > 0:
                        current_price = bid_price
                    else:
                        current_price = last_price
                else:
                    current_price = last_price

                log(f"Selected Reference Price for {action_upper}: {current_price}")
                print_and_discord(f"{name}: Current price for {symbol} is ${current_price}", loop)

                log(f"Selecting Action: {action_upper}")

                # 1. Open the dropdown
                action_dropdown = await page.select("#dest-dropdownlist-button-action", timeout=10)
                await action_dropdown.scroll_into_view()
                await action_dropdown.mouse_move()
                await action_dropdown.mouse_click()

                # 2. Robust Selection via JavaScript
                # Fidelity uses specific IDs: #Action0 = Buy, #Action1 = Sell
                if action_upper == "BUY":
                    buy_option = await page.select("#Action0", timeout=10)
                    await buy_option.scroll_into_view()
                    await buy_option.mouse_move()
                    await buy_option.mouse_click()
                elif action_upper == "SELL":
                    sell_option = await page.select("#Action1", timeout=10)
                    await sell_option.scroll_into_view()
                    await sell_option.mouse_move()
                    await sell_option.mouse_click()

                log(f"Entering Quantity: {quantity_val}")
                qty_input = await page.select("#eqt-shared-quantity", timeout=10)
                if qty_input:
                    await qty_input.clear_input()
                    await qty_input.send_keys(str(quantity_val))

                order_type_to_use = "Market"
                limit_price_to_use = None

                # Check for Penny Stock Rule (Price < $1 often requires Limit on Fidelity)
                if current_price > 0 and current_price < 1.00 and action_upper == "BUY":
                    log("Price < $1.00. Forcing LIMIT order.")
                    order_type_to_use = "Limit"
                # ---------------------------------------------------------
                # EXTENDED HOURS ADJUSTMENTS (Moved logic kept here for order type)
                # ---------------------------------------------------------
                if is_extended_time:
                    # 2. Force LIMIT Order (Market is unavailable in Ext Hours)
                    if order_type_to_use != "Limit":
                        log("Extended Hours requires LIMIT order. Converting Market -> Limit.")
                        order_type_to_use = "Limit"

                    # 3. Calculate Limit Price (if user didn't provide one)
                    # We behave like a 'Market' order by setting a Limit at the current Ask/Bid
                    if not limit_price_to_use:
                        base_price = last_price

                        if action_upper == "BUY":
                            # Buying: Use Ask Price (or Last + 0.01 buffer)
                            base_price = ask_price if ask_price > 0 else last_price
                            # Fidelity Extended Hours Requirement: Max 2 decimal places
                            # Round UP (ceil) to nearest penny to ensure fill and meet requirement
                            limit_price_to_use = math.ceil(base_price * 100) / 100.0

                        elif action_upper == "SELL":
                            # Selling: Use Bid Price (or Last - 0.01 buffer)
                            base_price = bid_price if bid_price > 0 else last_price
                            # Fidelity Extended Hours Requirement: Max 2 decimal places
                            # Round DOWN (floor) to nearest penny
                            limit_price_to_use = math.floor(base_price * 100) / 100.0

                        log(f"Calculated Extended Hours Limit Price: {limit_price_to_use}")

                # ---------------------------------------------------------
                # ORDER TYPE SELECTION (Updated)
                # ---------------------------------------------------------
                log(f"Setting Order Type: {order_type_to_use}")

                # 1. Open the Order Type Dropdown
                type_dropdown = await page.select("#dest-dropdownlist-button-ordertype", timeout=10)
                await type_dropdown.mouse_move()
                await type_dropdown.mouse_click()

                # 2. Select the specific option using mouse_click
                # Mapping based on IDs: #Order-type0=Market, #Order-type1=Limit, #Order-type3=Stop Loss, #Order-type4=Stop Limit

                if order_type_to_use == "Limit":
                    # Robustly find 'Limit' option, prioritizing text match
                    # This handles both Standard (usually Type1) and Extended Hours (usually Type0)
                    target_id = await page.evaluate("""
                       (function() {
                           const options = document.querySelectorAll('div[role="option"]');
                           for (const opt of options) {
                               if (opt.innerText.trim() === 'Limit') return opt.id;
                           }
                           return null;
                       })();
                    """)

                    if target_id:
                        option = await page.select(f"#{target_id}", timeout=10)
                        if option:
                            await option.mouse_click()
                    else:
                        # Fallback if text search failed
                        fallback_id = "#Order-type0" if is_extended_time else "#Order-type1"
                        option = await page.select(fallback_id, timeout=10)
                        if option:
                            await option.mouse_click()
                elif order_type_to_use == "Stop Loss":
                    option = await page.select("#Order-type3", timeout=10)
                    await option.mouse_click()
                elif order_type_to_use == "Stop Limit":
                    option = await page.select("#Order-type4", timeout=10)
                    await option.mouse_click()
                else:
                    # Default to Market if "Market" or unknown
                    option = await page.select("#Order-type0", timeout=10)
                    await option.mouse_click()

                # 4. Handle Limit Price Input (Only if Limit was selected)
                if order_type_to_use == "Limit":
                    # Wait a moment for the Limit Price input to appear/become active
                    limit_input = await page.select("#eqt-mts-limit-price", timeout=10)
                    await limit_input.mouse_click()
                    await limit_input.focus()
                    await limit_input.clear_input_by_deleting()
                    if limit_price_to_use is None:
                        limit_price_to_use = current_price
                    await limit_input.send_keys(str(limit_price_to_use))
                    await page.mouse_click(0, 0)

                log("Previewing Order...")

                # Click Preview Button
                preview = await page.select("#previewOrderBtn", timeout=10)
                await preview.mouse_move()
                await preview.mouse_click()

                # Wait for potential error modal or success state
                await page.sleep(0.25)
                await page.wait_for_ready_state("complete", timeout=10)
                await page.wait()
                await page.sleep(0.25)

                # Check for "Place Order" button first (Success Indicator)
                place_order_btn = None
                try:
                    place_order_btn = await page.select("#placeOrderBtn", timeout=2)
                except:
                    place_order_btn = None

                preview_error = None

                if not place_order_btn:
                    log("Place Order button not found. Checking for Error Modal...")
                    # Search for the error content div directly
                    error_content = await page.select(".pvd-inline-alert__content", timeout=2)
                    if error_content:
                        # Found the error text container. Try to read it.
                        try:
                            preview_error = await page.evaluate("document.querySelector('.pvd-inline-alert__content').innerText")
                        except:
                            preview_error = "Error detected (Could not parse text)"
                    else:
                        # Fallback: Check if the modal dialog exists at all
                        modal_dialog = await page.select(".pvd-modal__dialog", timeout=2)
                        if modal_dialog:
                            preview_error = "Error Modal Detected (details could not be parsed)"

                if preview_error:
                    log(f"Preview Failed for {acct_num}: {preview_error}")
                    # Try to close modal
                    close_btn = await page.select(".pvd-modal__close-button", timeout=2)
                    if close_btn:
                        await close_btn.mouse_move()
                        await close_btn.mouse_click()
                    continue

                log("Preview Successful (No error modal detected).")

                if is_dry_run:
                    log(f"Dry Run Active. Order NOT placed for {masked_account}.")
                    print_and_discord(
                        f"{name} [{masked_account}]: [DRY RUN] Would {action_upper} {quantity_val} of {symbol} @ ~${limit_price_to_use or current_price}",
                        loop,
                    )
                else:
                    log(f"Placing Order for {masked_account}...")

                    place_order_btn = await page.select("#placeOrderBtn", timeout=10)

                    if place_order_btn:
                        try:
                            await place_order_btn.mouse_move()
                            await place_order_btn.mouse_click()

                            # Wait for confirmation
                            log("Order placed. Waiting for confirmation...")
                            confirmation_success = False

                            # Wait up to 10 seconds for confirmation indicators
                            for _ in range(20):
                                await page.sleep(0.5)

                                # Check for "Order Received" header/text or verified success indicators
                                success_check = await page.evaluate("""
                                    (function() {
                                        const bodyText = document.body.innerText;
                                        if (bodyText.includes('Order Received') || bodyText.includes('Confirmation')) return true;
                                        if (document.querySelector('.pvd-inline-alert__content--success')) return true;
                                        return false;
                                    })();
                                """)

                                if success_check:
                                    confirmation_success = True
                                    break

                                # Check if we got redirected to Confirmation page (URL change)
                                curr_url = await page.evaluate("window.location.href")
                                if "confirmation" in curr_url.lower():
                                    confirmation_success = True
                                    break

                            if confirmation_success:
                                msg = f"{name}: SUCCESS - Order for {quantity_val} {symbol} placed successfully in {acct_name} ({masked_account})"
                                log(msg)
                                print_and_discord(msg, loop)
                            else:
                                msg = f"{name}: WARNING - Order button clicked but confirmation not detected for {symbol} in {acct_name} ({masked_account}). Please verify manually."
                                log(msg)
                                print_and_discord(msg, loop)

                        except Exception as e:
                            log(f"Place order interactions failed: {e}")
                            print_and_discord(
                                f"{name}: FAILED to place order for {symbol} in {acct_name} ({masked_account}). Error: {e}",
                                loop,
                            )
                    else:
                        msg = f"{name}: ERROR - Place Order button not found for {symbol} in {acct_name}"
                        log(msg)
                        print_and_discord(msg, loop)

            except Exception as e:
                log(f"Exception processing account {acct_num}: {e}")
                traceback.print_exc()

    log("Transaction loop complete.")
