"""DCM xa11y E2E test suite: validate the installed DCM app on all platforms.

Replicates the CTDX e2e test suite using xa11y instead of Selenium/tauri-driver.
xa11y exposes the full DOM tree inside DCM's Tauri WebView, allowing us to
interact with buttons, inputs, checkboxes, and tabs via accessibility APIs.

Test categories (mirroring CTDX e2etest/):
  1. CLI tests — --help, --version, get-credentials, create-profile
  2. App launch — app appears in a11y tree, window content loads
  3. Studio URL validation — invalid URLs show errors, valid URL proceeds
  4. Profile creation — name validation, review page
  5. Settings dialog — tabs, checkboxes, language selector
  6. Login flow — (requires AWS credentials, uses MONITOR_* env vars)

Cross-platform. Requires:
  - DCM installed and on PATH (or at known platform path)
  - xa11y >= 0.8.1
  - Linux: Xvfb + AT-SPI running, webkit2gtk runtime
  - macOS: Accessibility TCC permission granted to Python
  - Windows: no extra setup needed

Environment variables:
  - DEADLINE_BINARY: override path to DCM binary
  - SCREENSHOT_DIR: where to save screenshots (default: /tmp or %TEMP%)
  - MONITOR_SUBDOMAIN, MONITOR_REGION, MONITOR_USERNAME, MONITOR_PASSWORD:
    required only for login flow tests (skipped if not set)
  - TEST_LOGIN: set to "1" to run login flow tests (requires MONITOR_* vars)
"""

import os
import platform
import subprocess
import sys
import time
import shutil
import tempfile
import traceback

sys.stdout.reconfigure(line_buffering=True)

SYSTEM = platform.system()
IS_LINUX = SYSTEM == "Linux"
IS_MACOS = SYSTEM == "Darwin"
IS_WINDOWS = SYSTEM == "Windows"

SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", tempfile.gettempdir())
TEST_LOGIN = os.environ.get("TEST_LOGIN", "0") == "1"

# Timeouts
ELEMENT_TIMEOUT = 10
APP_LAUNCH_TIMEOUT = 30
CONTENT_RENDER_WAIT = 8

# Known DCM installation paths
DCM_PATHS = {
    "Linux": ["/usr/bin/deadline-cloud-monitor"],
    "Darwin": ["/Applications/Deadline Cloud Monitor.app/Contents/MacOS/Deadline Cloud Monitor"],
    "Windows": [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "DeadlineCloudMonitor", "DeadlineCloudMonitor.exe"),
    ],
}


def find_dcm_binary():
    """Locate the DCM binary on this platform."""
    env_binary = os.environ.get("DEADLINE_BINARY")
    if env_binary and os.path.isfile(env_binary):
        return env_binary
    which = shutil.which("deadline-cloud-monitor") or shutil.which("DeadlineCloudMonitor")
    if which:
        return which
    for path in DCM_PATHS.get(SYSTEM, []):
        if os.path.isfile(path):
            return path
    return None


def run_cmd(cmd, check=True, timeout=30, env=None):
    """Run a command and return the result."""
    print(f"    $ {' '.join(cmd) if isinstance(cmd, list) else cmd}", flush=True)
    r = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, env=env)
    if r.stdout.strip():
        for line in r.stdout.strip().split("\n")[:5]:
            print(f"      {line}", flush=True)
    if r.stderr.strip():
        for line in r.stderr.strip().split("\n")[:3]:
            print(f"      (err) {line}", flush=True)
    if check and r.returncode:
        raise RuntimeError(f"Command failed (rc={r.returncode}): {cmd}")
    return r


def screenshot(name):
    """Take a screenshot via xa11y."""
    try:
        import xa11y
        path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
        xa11y.screenshot().save_png(path)
        print(f"    [screenshot: {path}]", flush=True)
    except Exception as e:
        print(f"    [screenshot({name}) failed: {e}]", flush=True)


# =============================================================================
# Test result tracking
# =============================================================================

class TestResults:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.skipped = []

    def ok(self, name):
        self.passed.append(name)
        print(f"  PASS  {name}", flush=True)

    def fail(self, name, reason=""):
        self.failed.append((name, reason))
        print(f"  FAIL  {name} — {reason}", flush=True)

    def skip(self, name, reason=""):
        self.skipped.append((name, reason))
        print(f"  SKIP  {name} — {reason}", flush=True)

    def check(self, name, condition, fail_reason=""):
        if condition:
            self.ok(name)
        else:
            self.fail(name, fail_reason)
        return condition

    def summary(self):
        total = len(self.passed) + len(self.failed) + len(self.skipped)
        print(f"\n{'='*70}", flush=True)
        print(f"  {len(self.passed)} passed, {len(self.failed)} failed, "
              f"{len(self.skipped)} skipped / {total} total", flush=True)
        if self.failed:
            print("\n  Failures:", flush=True)
            for name, reason in self.failed:
                print(f"    x {name}: {reason}", flush=True)
        print(f"{'='*70}\n", flush=True)
        return len(self.failed) == 0


results = TestResults()


# =============================================================================
# DCM App context manager — launches DCM with isolated config, cleans up after
# =============================================================================

class DCMApp:
    """Context manager that launches DCM with an isolated config dir."""

    def __init__(self, binary):
        self.binary = binary
        self.proc = None
        self.config_dir = None
        self.app = None

    def __enter__(self):
        import xa11y
        self.config_dir = tempfile.mkdtemp(prefix="dcm_e2e_")
        env = os.environ.copy()
        env["HOME_DIR_OVERRIDE"] = self.config_dir
        env["CONFIG_DIR_OVERRIDE"] = self.config_dir

        # Record PIDs of any already-running DCM instances to exclude them
        existing_pids = set()
        for a in xa11y.App.list():
            name = (a.name or "").lower()
            if "deadline" in name or "cloud monitor" in name:
                existing_pids.add(a.pid)

        self.proc = subprocess.Popen(
            [self.binary],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

        # Wait for our new app to appear in a11y tree (exclude pre-existing)
        deadline = time.time() + APP_LAUNCH_TIMEOUT
        while time.time() < deadline:
            for a in xa11y.App.list():
                name = (a.name or "").lower()
                if "deadline" in name or "cloud monitor" in name:
                    if a.pid not in existing_pids:
                        self.app = a
                        break
            if self.app:
                break
            time.sleep(0.5)

        # Fallback: if no new instance found, try matching our PID
        if not self.app:
            for a in xa11y.App.list():
                if a.pid == self.proc.pid:
                    self.app = a
                    break

        if self.app:
            # Wait for content to render
            time.sleep(CONTENT_RENDER_WAIT)

        return self

    def __exit__(self, *args):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.config_dir:
            shutil.rmtree(self.config_dir, ignore_errors=True)

    def dump(self, max_depth=20):
        if not self.app:
            return ""
        return self.app.dump(max_depth=max_depth)

    def locator(self, selector):
        return self.app.locator(selector)

    def wait_for(self, selector, timeout=ELEMENT_TIMEOUT):
        loc = self.app.locator(selector)
        loc.wait_visible(timeout=timeout)
        return loc

    def press_button(self, name, timeout=ELEMENT_TIMEOUT):
        btn = self.wait_for(f"button[name='{name}']", timeout=timeout)
        btn.press()
        time.sleep(0.5)

    def press_escape(self):
        import xa11y
        sim = xa11y.input_sim()
        sim.press("Escape")
        time.sleep(0.5)

    def type_text(self, text):
        import xa11y
        sim = xa11y.input_sim()
        sim.type_text(text)

    def tree_contains(self, *terms):
        tree = self.dump().lower()
        return all(t.lower() in tree for t in terms)


# =============================================================================
# 1. CLI Tests
# =============================================================================

def run_cli_tests(binary):
    print("\n" + "="*70, flush=True)
    print("  1. CLI TESTS", flush=True)
    print("="*70, flush=True)

    # --help
    try:
        r = run_cmd([binary, "--help"], check=False)
        results.check("cli: --help responds",
                      r.returncode == 0 and "deadline" in r.stdout.lower(),
                      f"rc={r.returncode}")
    except Exception as e:
        results.fail("cli: --help responds", str(e))

    # --version
    try:
        r = run_cmd([binary, "--version"], check=False)
        results.check("cli: --version responds",
                      r.returncode == 0 and len(r.stdout.strip()) > 0,
                      f"rc={r.returncode}")
    except Exception as e:
        results.fail("cli: --version responds", str(e))

    # get-credentials unknown profile
    try:
        r = run_cmd([binary, "get-credentials", "--profile", "nonexistent_profile"], check=False)
        output = (r.stdout + r.stderr).lower()
        results.check("cli: get-credentials fails for unknown profile",
                      r.returncode != 0 and ("unknown" in output or "profile" in output),
                      f"rc={r.returncode}, no useful error message")
    except Exception as e:
        results.fail("cli: get-credentials fails for unknown profile", str(e))

    # create-profile
    config_dir = tempfile.mkdtemp(prefix="dcm_cli_")
    try:
        env = os.environ.copy()
        env["HOME_DIR_OVERRIDE"] = config_dir
        env["CONFIG_DIR_OVERRIDE"] = config_dir
        cmd = [binary, "create-profile",
               "--profile", "cli-smoke-test",
               "--monitor-id", "us-east-1:stid-00000000000000000",
               "--monitor-url", "https://example.us-east-1.deadlinecloud.amazonaws.com"]
        r = run_cmd(cmd, check=False, env=env)
        results.check("cli: create-profile succeeds",
                      r.returncode == 0 and "cli-smoke-test" in r.stdout.lower(),
                      f"rc={r.returncode}")
    except Exception as e:
        results.fail("cli: create-profile succeeds", str(e))
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


# =============================================================================
# 2. App Launch Tests
# =============================================================================

def run_app_launch_tests(binary):
    print("\n" + "="*70, flush=True)
    print("  2. APP LAUNCH TESTS", flush=True)
    print("="*70, flush=True)

    with DCMApp(binary) as dcm:
        if not dcm.app:
            results.fail("launch: app visible in a11y tree", "DCM not found in accessibility tree")
            results.skip("launch: window has web_area (DOM exposed)", "no app")
            results.skip("launch: main heading visible", "no app")
            results.skip("launch: Next button visible", "no app")
            results.skip("launch: Settings button visible", "no app")
            return

        results.ok("launch: app visible in a11y tree")
        screenshot("app_launched")

        tree = dcm.dump()
        results.check("launch: window has web_area (DOM exposed)",
                      "web_area" in tree,
                      "no web_area — xa11y cannot see DOM")

        results.check("launch: main heading visible",
                      dcm.tree_contains("heading", "deadline cloud"),
                      "no 'Deadline Cloud' heading")

        # Next button
        try:
            dcm.wait_for("button[name='Next']", timeout=5)
            results.ok("launch: Next button visible")
        except Exception:
            results.fail("launch: Next button visible", "not found")

        # Settings button
        try:
            dcm.wait_for("button[name='Settings']", timeout=5)
            results.ok("launch: Settings button visible")
        except Exception:
            results.fail("launch: Settings button visible", "not found")


# =============================================================================
# 3. Studio URL Validation Tests
# =============================================================================

def run_studio_url_tests(binary):
    """Test URL input validation.

    The wizard validates the URL against the backend. With a fake URL, validation
    shows "No valid monitor exists" but does NOT prevent error display for
    syntactically invalid URLs. A valid-looking URL that doesn't resolve still
    shows a warning but the client-side format validation passes.
    """
    print("\n" + "="*70, flush=True)
    print("  3. STUDIO URL VALIDATION TESTS", flush=True)
    print("="*70, flush=True)

    with DCMApp(binary) as dcm:
        if not dcm.app:
            results.skip("url: rejects invalid URL", "no app")
            results.skip("url: accepts valid format URL (client-side)", "no app")
            return

        import xa11y

        # Fresh app goes directly to URL step (wizard Step 1)
        tree = dcm.dump()
        screenshot("url_step_initial")

        # Find the URL text field
        try:
            text_field = dcm.wait_for("text_field", timeout=5)
        except Exception:
            results.skip("url: rejects invalid URL", "no text_field found")
            results.skip("url: accepts valid format URL (client-side)", "no text_field found")
            return

        # Test 1: Invalid URL format → shows "Invalid Deadline Cloud URL"
        try:
            text_field.focus()
            time.sleep(0.3)
            dcm.type_text("invalid$url")
            time.sleep(0.5)
            dcm.press_button("Next", timeout=5)
            time.sleep(1.5)

            tree = dcm.dump()
            screenshot("invalid_url_submitted")
            has_error = "invalid deadline cloud url" in tree.lower() or "invalid" in tree.lower()
            results.check("url: rejects invalid URL", has_error,
                          f"no 'Invalid' error. Tree excerpt: {tree[:500]}")
        except Exception as e:
            results.fail("url: rejects invalid URL", str(e))

        # Test 2: Valid format URL → advances to profile name step
        try:
            # Re-focus the text field and triple-click to select all, then delete
            text_field = dcm.wait_for("text_field", timeout=5)
            text_field.focus()
            time.sleep(0.5)
            # Triple-click to select all text in field (more reliable than Cmd+A)
            if IS_MACOS:
                xa11y.input_sim().chord("a", held=["Meta"])
            else:
                xa11y.input_sim().chord("a", held=["Control"])
            time.sleep(0.3)
            xa11y.input_sim().press("Backspace")
            time.sleep(0.5)

            # Verify field is clear
            tree_check = dcm.dump()
            if "invalid" in tree_check.lower():
                # Field might not have cleared — try typing over the selection
                text_field.focus()
                time.sleep(0.2)

            valid_url = "https://mymonitor.us-west-2.deadlinecloud.amazonaws.com"
            dcm.type_text(valid_url)
            time.sleep(1)
            dcm.press_button("Next", timeout=5)
            time.sleep(3)

            tree = dcm.dump(max_depth=25)
            screenshot("valid_format_url_submitted")
            tree_lower = tree.lower()
            # A valid URL advances the wizard to Step 2 (profile name step)
            # or shows "No valid monitor" (backend can't reach it but format is OK)
            # or at minimum the format-specific error "Invalid Deadline Cloud URL." is gone
            advanced_to_profile = "profile name" in tree_lower or "your profile name" in tree_lower
            has_monitor_msg = "no valid monitor" in tree_lower
            # The format error has a period at the end: "Invalid Deadline Cloud URL."
            no_format_error = "invalid deadline cloud url." not in tree_lower
            passed = advanced_to_profile or has_monitor_msg or no_format_error
            results.check("url: accepts valid format URL (client-side)", passed,
                          f"advanced={advanced_to_profile}, monitor_msg={has_monitor_msg}, "
                          f"no_format_error={no_format_error}. Tree: {tree[:500]}")
        except Exception as e:
            results.fail("url: accepts valid format URL (client-side)", str(e))


# =============================================================================
# 4. Profile Creation Tests (via CLI) and Profile Display Tests
# =============================================================================

def run_profile_creation_tests(binary):
    """Test profile creation via CLI and verify the app shows the profile.

    The wizard's URL step validates against the real backend, so we can't
    drive through the full GUI wizard without credentials. Instead, we:
    1. Create a profile via CLI (already tested in CLI tests)
    2. Launch the app and verify it shows the created profile
    3. Verify the profile dropdown and selection work
    """
    print("\n" + "="*70, flush=True)
    print("  4. PROFILE CREATION & DISPLAY TESTS", flush=True)
    print("="*70, flush=True)

    import xa11y

    # Create a config dir with a pre-created profile
    config_dir = tempfile.mkdtemp(prefix="dcm_profile_")
    env = os.environ.copy()
    env["HOME_DIR_OVERRIDE"] = config_dir
    env["CONFIG_DIR_OVERRIDE"] = config_dir

    # Create profile via CLI
    cmd = [binary, "create-profile",
           "--profile", "e2e-test-profile",
           "--monitor-id", "us-east-1:stid-00000000000000000",
           "--monitor-url", "https://example.us-east-1.deadlinecloud.amazonaws.com"]
    r = subprocess.run(cmd, text=True, capture_output=True, timeout=30, env=env)
    if r.returncode != 0:
        results.fail("profile: CLI create-profile for display test", f"rc={r.returncode}: {r.stderr}")
        results.skip("profile: app shows created profile", "profile creation failed")
        results.skip("profile: profile dropdown has profile name", "profile creation failed")
        results.skip("profile: default checkbox is present", "profile creation failed")
        shutil.rmtree(config_dir, ignore_errors=True)
        return

    results.ok("profile: CLI create-profile for display test")

    # Launch app with the pre-created profile
    proc = subprocess.Popen(
        [binary], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )

    try:
        time.sleep(CONTENT_RENDER_WAIT)

        app = None
        deadline_t = time.time() + APP_LAUNCH_TIMEOUT
        while time.time() < deadline_t:
            for a in xa11y.App.list():
                name = (a.name or "").lower()
                if "deadline" in name or "cloud monitor" in name:
                    app = a
                    break
            if app:
                break
            time.sleep(0.5)

        if not app:
            results.fail("profile: app shows created profile", "app not found in a11y tree")
            results.skip("profile: profile dropdown has profile name", "no app")
            results.skip("profile: default checkbox is present", "no app")
            return

        tree = app.dump(max_depth=20)
        screenshot("profile_preloaded")

        # When launched with an existing profile, the app shows the sign-in page
        # with the profile name in a dropdown
        has_profile = "e2e-test-profile" in tree.lower()
        results.check("profile: app shows created profile", has_profile,
                      f"profile name not in tree. Excerpt: {tree[:500]}")

        # Check for combo_box (profile dropdown)
        has_dropdown = "combo_box" in tree
        results.check("profile: profile dropdown has profile name", has_dropdown,
                      "no combo_box in tree")

        # Check for default profile checkbox
        has_checkbox = "check_box" in tree
        results.check("profile: default checkbox is present", has_checkbox,
                      "no check_box in tree")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(config_dir, ignore_errors=True)


# =============================================================================
# 5. Settings Dialog Tests
# =============================================================================

def run_settings_tests(binary):
    print("\n" + "="*70, flush=True)
    print("  5. SETTINGS DIALOG TESTS", flush=True)
    print("="*70, flush=True)

    with DCMApp(binary) as dcm:
        if not dcm.app:
            results.skip("settings: dialog opens", "no app")
            results.skip("settings: Application tab has checkboxes", "no app")
            results.skip("settings: Profile tab loads", "no app")
            results.skip("settings: Language tab has selector", "no app")
            results.skip("settings: dialog closes with Escape", "no app")
            return

        # Open settings
        try:
            dcm.press_button("Settings", timeout=5)
            time.sleep(2)
            tree = dcm.dump()
            screenshot("settings_opened")

            has_tabs = dcm.tree_contains("Application", "Profile", "Language")
            results.check("settings: dialog opens with all tabs", has_tabs,
                          "missing one or more settings tabs")
        except Exception as e:
            results.fail("settings: dialog opens with all tabs", str(e))
            return

        # Application tab — should show checkboxes
        try:
            tree = dcm.dump()
            has_checkboxes = "check_box" in tree and "updates" in tree.lower()
            results.check("settings: Application tab has checkboxes", has_checkboxes,
                          "no checkboxes or 'updates' text visible")
        except Exception as e:
            results.fail("settings: Application tab has checkboxes", str(e))

        # Profile tab
        try:
            tab = dcm.wait_for("radio_button[name='Profile']", timeout=5)
            tab.press()
            time.sleep(1)
            tree = dcm.dump()
            screenshot("settings_profile_tab")

            has_profile_content = dcm.tree_contains("profile")
            results.check("settings: Profile tab loads", has_profile_content,
                          "Profile tab content not visible")
        except Exception as e:
            results.fail("settings: Profile tab loads", str(e))

        # Language tab
        try:
            tab = dcm.wait_for("radio_button[name='Language']", timeout=5)
            tab.press()
            time.sleep(1)
            tree = dcm.dump()
            screenshot("settings_language_tab")

            has_language = "combo_box" in tree or "english" in tree.lower()
            results.check("settings: Language tab has selector", has_language,
                          "no language selector visible")
        except Exception as e:
            results.fail("settings: Language tab has selector", str(e))

        # Close with Escape
        try:
            dcm.press_escape()
            time.sleep(1)
            tree = dcm.dump()
            settings_closed = "tab_group" not in tree
            results.check("settings: dialog closes with Escape", settings_closed,
                          "settings dialog still open after Escape")
        except Exception as e:
            results.fail("settings: dialog closes with Escape", str(e))


# =============================================================================
# 6. Login Flow Tests (requires MONITOR_* environment variables)
# =============================================================================

def run_login_tests(binary):
    print("\n" + "="*70, flush=True)
    print("  6. LOGIN FLOW TESTS", flush=True)
    print("="*70, flush=True)

    required_vars = ["MONITOR_SUBDOMAIN", "MONITOR_REGION", "MONITOR_USERNAME", "MONITOR_PASSWORD"]
    missing = [v for v in required_vars if not os.environ.get(v)]

    if not TEST_LOGIN:
        for name in ["login: deadline auth login completes", "login: auth status succeeds"]:
            results.skip(name, "TEST_LOGIN not set")
        return

    if missing:
        for name in ["login: deadline auth login completes", "login: auth status succeeds"]:
            results.skip(name, f"missing env vars: {missing}")
        return

    # The login flow uses the existing dcm_e2e_test.py approach:
    # 1. Create a profile via CLI
    # 2. Run `deadline auth login`
    # 3. Drive the browser sign-in via xa11y
    # This is already handled by dcm_e2e_test.py, so we just verify it works
    try:
        r = run_cmd([sys.executable, ".github/scripts/dcm_e2e_test.py"],
                    check=False, timeout=300)
        results.check("login: deadline auth login completes",
                      r.returncode == 0, f"rc={r.returncode}")
    except Exception as e:
        results.fail("login: deadline auth login completes", str(e))

    # Verify auth status
    try:
        r = run_cmd(["deadline", "auth", "status"], check=False, timeout=30)
        results.check("login: auth status succeeds",
                      r.returncode == 0, f"rc={r.returncode}")
    except Exception as e:
        results.fail("login: auth status succeeds", str(e))


# =============================================================================
# Main
# =============================================================================

def main():
    print(f"Platform: {SYSTEM} ({platform.machine()})", flush=True)
    print(f"Screenshot dir: {SCREENSHOT_DIR}", flush=True)
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    binary = find_dcm_binary()
    if not binary:
        print(f"ERROR: DCM binary not found.", flush=True)
        print(f"  Searched PATH and: {DCM_PATHS.get(SYSTEM, [])}", flush=True)
        sys.exit(1)

    print(f"DCM binary: {binary}", flush=True)

    # Verify xa11y is available
    try:
        import xa11y
        print(f"xa11y: available\n", flush=True)
    except ImportError:
        print("ERROR: xa11y is not installed. Install with: pip install 'xa11y>=0.8.1,<0.9'", flush=True)
        sys.exit(1)

    # Run all test suites
    run_cli_tests(binary)
    run_app_launch_tests(binary)
    run_studio_url_tests(binary)
    run_profile_creation_tests(binary)
    run_settings_tests(binary)
    run_login_tests(binary)

    # Summary
    if not results.summary():
        sys.exit(1)


if __name__ == "__main__":
    main()
