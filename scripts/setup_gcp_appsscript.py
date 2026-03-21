"""
GCP project creation and Apps Script API enablement via Playwright.
Uses existing Chrome profile (satoru@chillaxy.jp / Profile 34) for authentication.
"""
import time
import json

def run():
    from playwright.sync_api import sync_playwright

    chrome_user_data = "/Users/s/Library/Application Support/Google/Chrome"

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=chrome_user_data,
            channel="chrome",
            headless=False,
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--profile-directory=Profile 34",
            ],
            viewport={"width": 1280, "height": 900},
        )

        page = browser.pages[0] if browser.pages else browser.new_page()

        # Step 1: Go to project creation page
        print("Step 1: Navigating to GCP project creation...")
        page.goto("https://console.cloud.google.com/projectcreate", wait_until="networkidle", timeout=30000)
        time.sleep(5)
        page.screenshot(path="/tmp/gcp_step1.png")
        print("Screenshot: /tmp/gcp_step1.png")

        # Check if we're logged in (look for login page indicators)
        if "accounts.google.com" in page.url:
            print("ERROR: Not logged in. Need to use a different approach.")
            page.screenshot(path="/tmp/gcp_login_needed.png")
            browser.close()
            return

        # Fill project name
        print("Filling project name...")
        try:
            # Wait for any input to appear
            page.wait_for_selector('input', timeout=10000)
            time.sleep(2)

            # GCP console project name input
            inputs = page.locator('input[type="text"]')
            filled = False
            for i in range(inputs.count()):
                inp = inputs.nth(i)
                if inp.is_visible():
                    inp.click()
                    inp.fill("")
                    time.sleep(0.5)
                    inp.fill("workspace-mcp")
                    filled = True
                    print(f"  Filled input #{i}")
                    break

            if not filled:
                print("  No visible text input found, trying broader search...")
                page.locator('input').first.fill("workspace-mcp")
        except Exception as e:
            print(f"  Error filling name: {e}")

        time.sleep(2)
        page.screenshot(path="/tmp/gcp_step1b.png")
        print("Screenshot: /tmp/gcp_step1b.png")

        # Click Create
        print("Clicking Create...")
        try:
            create_btn = page.locator('button:has-text("作成"), button:has-text("Create")')
            create_btn.first.click()
            print("  Clicked Create")
            # Wait for project creation
            time.sleep(10)
        except Exception as e:
            print(f"  Error: {e}")

        page.screenshot(path="/tmp/gcp_step2.png")
        print("Screenshot: /tmp/gcp_step2.png")

        # Step 2: Select the new project
        print("\nStep 2: Selecting workspace-mcp project...")
        # Navigate to project selector - use the API to find project ID
        page.goto("https://console.cloud.google.com/cloud-resource-manager", wait_until="networkidle", timeout=30000)
        time.sleep(5)
        page.screenshot(path="/tmp/gcp_step2b.png")

        # Step 3: Enable Apps Script API on the new project
        print("\nStep 3: Enabling APIs...")

        apis = [
            ("script.googleapis.com", "Apps Script API"),
            ("drive.googleapis.com", "Drive API"),
            ("sheets.googleapis.com", "Sheets API"),
            ("calendar-json.googleapis.com", "Calendar API"),
            ("gmail.googleapis.com", "Gmail API"),
            ("people.googleapis.com", "People API"),
            ("chat.googleapis.com", "Chat API"),
            ("tasks.googleapis.com", "Tasks API"),
            ("docs.googleapis.com", "Docs API"),
            ("slides.googleapis.com", "Slides API"),
            ("forms.googleapis.com", "Forms API"),
        ]

        for api_id, api_name in apis:
            print(f"  Enabling {api_name}...")
            page.goto(
                f"https://console.cloud.google.com/apis/library/{api_id}",
                wait_until="networkidle",
                timeout=30000,
            )
            time.sleep(3)

            try:
                enable_btn = page.locator('button:has-text("有効にする"), button:has-text("Enable")')
                if enable_btn.count() > 0 and enable_btn.first.is_visible():
                    enable_btn.first.click()
                    print(f"    -> Enabled")
                    time.sleep(5)
                else:
                    # Check if already enabled
                    manage_btn = page.locator('button:has-text("管理"), button:has-text("Manage")')
                    if manage_btn.count() > 0:
                        print(f"    -> Already enabled")
                    else:
                        print(f"    -> Button not found")
                        page.screenshot(path=f"/tmp/gcp_api_{api_id}.png")
            except Exception as e:
                print(f"    -> Error: {e}")

        page.screenshot(path="/tmp/gcp_step3.png")
        print("Screenshot: /tmp/gcp_step3.png")

        # Step 4: OAuth consent screen
        print("\nStep 4: OAuth consent screen...")
        page.goto("https://console.cloud.google.com/apis/credentials/consent", wait_until="networkidle", timeout=30000)
        time.sleep(5)
        page.screenshot(path="/tmp/gcp_step4.png")
        print("Screenshot: /tmp/gcp_step4.png")

        # Check if consent screen needs setup - look for "External" radio or user type selection
        try:
            external_radio = page.locator('text=外部, text=External')
            if external_radio.count() > 0:
                external_radio.first.click()
                time.sleep(1)
                create_btn = page.locator('button:has-text("作成"), button:has-text("Create")')
                if create_btn.count() > 0:
                    create_btn.first.click()
                    time.sleep(5)
                    page.screenshot(path="/tmp/gcp_step4b.png")
        except Exception as e:
            print(f"  Consent screen setup: {e}")

        # Step 5: Create OAuth client
        print("\nStep 5: Creating OAuth client credentials...")
        page.goto("https://console.cloud.google.com/apis/credentials", wait_until="networkidle", timeout=30000)
        time.sleep(3)
        page.screenshot(path="/tmp/gcp_step5.png")
        print("Screenshot: /tmp/gcp_step5.png")

        print("\n=== Automated steps complete ===")
        print("Check /tmp/gcp_step*.png for progress")
        print("Press Enter to close browser...")

        # Keep browser open for inspection
        try:
            input()
        except EOFError:
            time.sleep(300)

        browser.close()

if __name__ == "__main__":
    run()
