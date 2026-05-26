"""Playwright probe: confirm every picker preset lands on a trading weekday.

Manual diagnostic — NOT wired into CI. Run after frontend changes that
touch ``ticker-range-picker`` or ``lean-engine.component.ts`` date
logic. The probe loads ``http://localhost:4200/engine`` in a headless
chromium, snapshots the two date inputs after pressing each preset
button, and prints the day-of-week so a weekend regression jumps out.

Why this exists
---------------
The sidecar validator at ``PythonDataService/app/routers/lean_sidecar.py``
rejects weekend ``start_ms_utc`` / ``end_ms_utc`` with HTTP 422
("start_ms_utc resolves to <date> which is not a trading day"). The
Engine Lab form has multiple code paths that derive a start date by
raw arithmetic (``today - days``, ``last - 30``) — every one of those
needs to walk back to the most recent weekday before publishing into
the picker's model signal. Unit tests cover each call site in
isolation; this probe is the end-to-end guard that catches "a new
preset was added without the weekday wrap" or "a new picker variant
forgot to call the shared util".

Run
---
Requires the dev stack (``./restart.sh``) and a Python env with
``playwright`` installed + ``playwright install chromium`` completed::

    python scripts/probe_engine_lab_presets.py

Expected output (all entries on Mon-Fri)::

    initial              -> [('2026-04-24', 'Fri'), ('2026-05-22', 'Fri')]
    after '7D'           -> [('2026-05-18', 'Mon'), ('2026-05-25', 'Mon')]
    after '1M'           -> [('2026-04-24', 'Fri'), ('2026-05-25', 'Mon')]
    ...

Any 'Sat' or 'Sun' is a regression of the picker's weekend-skip
invariant.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(timezone_id="America/New_York")
        page = await context.new_page()

        await page.goto("http://localhost:4200/engine", wait_until="networkidle")
        await page.wait_for_selector('input[type="date"]', timeout=15000)

        async def snapshot(tag: str) -> None:
            inputs = await page.query_selector_all('input[type="date"]')
            vals = [await el.input_value() for el in inputs]
            named = [(v, _dow(v)) for v in vals]
            print(f"{tag:20s} -> {named}")

        await snapshot("initial")

        for label in ["7D", "1M", "3M", "6M", "1Y", "2Y"]:
            try:
                btn = page.locator(".preset-btn", has_text=label).first
                await btn.scroll_into_view_if_needed()
                await btn.click(force=True)
                await page.wait_for_timeout(150)
                await snapshot(f"after '{label}'")
            except Exception as e:
                print(f"after '{label}'        -> click failed: {e}")

        await browser.close()


def _dow(iso: str) -> str:
    try:
        return dt.date.fromisoformat(iso).strftime("%a")
    except Exception:
        return "?"


if __name__ == "__main__":
    asyncio.run(main())
