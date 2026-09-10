"""Regression checks for the Settings "install requirements" panel.

Not everyone knows how to use a terminal, so Settings can check what Copita
needs and install missing pieces with one click. These tests cover the
state machine (installed/missing/installing/error), that it refuses to
re-trigger an install for something already installed or already in
progress, and that a "requires" dependency (e.g. FFmpeg needs Homebrew) is
enforced before a subprocess is ever started.
"""
import asyncio
import unittest
from unittest.mock import patch

from copita.core import requirements_checker as rc
from copita.core.requirements_checker import RequirementsManager


def _fake_check(installed, version="1.0", detail="ok"):
    return lambda: (installed, version, detail)


def _check_that_breaks_inside_a_running_loop():
    """Stands in for the real _check_playwright_chromium: Playwright's sync
    API raises immediately if it detects it's being called from a thread
    that already has an asyncio event loop running — exactly the situation
    every FastAPI/uvicorn request handler runs in. Like the real function,
    it catches that internally and reports "not installed" rather than
    raising — reproducing that exact (mis)behavior is what proves the fix
    (offloading the check to a plain executor thread with no loop) works."""
    try:
        asyncio.get_running_loop()
        return False, None, "Sync API inside asyncio loop"
    except RuntimeError:
        return True, "1.0", "ok"  # no loop on this thread — safe, as Playwright's sync API requires


class RequirementsStateTests(unittest.TestCase):
    def test_refresh_reflects_real_state_not_a_stale_cache(self):
        """If the user installs something themselves in a terminal, the panel
        must reflect that on the next refresh — not keep offering to install
        something that's already there."""
        state = {"installed": False}
        fake_req = {"id": "thing", "label": "Thing", "description": "d",
                    "check": lambda: (state["installed"], None, "x"),
                    "install_cmd": ["true"], "required": True}
        with patch.object(rc, "REQUIREMENTS", [fake_req]), \
             patch.object(rc, "_BY_ID", {"thing": fake_req}):
            manager = RequirementsManager()
            self.assertFalse(manager.get_all()[0]["installed"])
            state["installed"] = True
            manager.refresh_all()
            self.assertTrue(manager.get_all()[0]["installed"])

    def test_missing_requirement_reports_not_installed(self):
        fake_req = {"id": "thing", "label": "Thing", "description": "d",
                    "check": _fake_check(False), "install_cmd": ["true"], "required": True}
        with patch.object(rc, "REQUIREMENTS", [fake_req]), \
             patch.object(rc, "_BY_ID", {"thing": fake_req}):
            manager = RequirementsManager()
            state = manager.get_all()[0]
            self.assertFalse(state["installed"])
            self.assertEqual(state["status"], "missing")

    def test_installed_requirement_refuses_to_reinstall(self):
        """The user's explicit ask: once green/installed, don't re-trigger."""
        fake_req = {"id": "thing", "label": "Thing", "description": "d",
                    "check": _fake_check(True), "install_cmd": ["true"], "required": True}
        with patch.object(rc, "REQUIREMENTS", [fake_req]), \
             patch.object(rc, "_BY_ID", {"thing": fake_req}):
            manager = RequirementsManager()
            with self.assertRaisesRegex(ValueError, "already installed"):
                manager.validate_install("thing")

    def test_non_installable_requirement_is_refused(self):
        fake_req = {"id": "thing", "label": "Thing", "description": "d",
                    "check": _fake_check(False), "install_cmd": None, "required": True}
        with patch.object(rc, "REQUIREMENTS", [fake_req]), \
             patch.object(rc, "_BY_ID", {"thing": fake_req}):
            manager = RequirementsManager()
            with self.assertRaisesRegex(ValueError, "can't be installed"):
                manager.validate_install("thing")

    def test_dependency_requirement_must_be_installed_first(self):
        prereq = {"id": "prereq", "label": "Prereq", "description": "d",
                  "check": _fake_check(False), "install_cmd": None, "required": False}
        dependent = {"id": "thing", "label": "Thing", "description": "d",
                     "check": _fake_check(False), "install_cmd": ["true"],
                     "requires": "prereq", "required": True}
        with patch.object(rc, "REQUIREMENTS", [prereq, dependent]), \
             patch.object(rc, "_BY_ID", {"prereq": prereq, "thing": dependent}):
            manager = RequirementsManager()
            with self.assertRaisesRegex(ValueError, "Install Prereq first"):
                manager.validate_install("thing")


class RequirementsInstallTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_that_requires_no_running_loop_still_reports_correctly(self):
        """Regression: _refresh_one (direct, sync call) used to be called
        from async code that's always inside a running loop — which broke
        every check function that (like Playwright's) can't tolerate that.
        _refresh_one_async must run the check off-loop via an executor so
        this kind of check function works correctly."""
        fake_req = {"id": "thing", "label": "Thing", "description": "d",
                    "check": _check_that_breaks_inside_a_running_loop,
                    "install_cmd": ["true"], "required": True}
        with patch.object(rc, "REQUIREMENTS", [fake_req]), \
             patch.object(rc, "_BY_ID", {"thing": fake_req}):
            manager = RequirementsManager()  # __init__ calls the sync path — fine, no loop yet
            # The async path (what every request handler actually uses) must
            # succeed even though this test method itself runs inside a loop.
            await manager.refresh_all_async()
            self.assertTrue(manager.state["thing"]["installed"])
            self.assertEqual(manager.state["thing"]["status"], "installed")

    async def test_successful_install_transitions_to_installed_and_broadcasts(self):
        calls = {"n": 0}

        def check():
            # Not installed until the "install" has run once.
            calls["n"] += 1
            return (calls["n"] > 1, "2.0", "ok")

        fake_req = {"id": "thing", "label": "Thing", "description": "d",
                    "check": check, "install_cmd": ["true"], "required": True}
        events = []
        with patch.object(rc, "REQUIREMENTS", [fake_req]), \
             patch.object(rc, "_BY_ID", {"thing": fake_req}):
            manager = RequirementsManager()
            manager.register_listener(events.append)
            manager.validate_install("thing")  # should not raise — genuinely missing
            await manager.install("thing")

        statuses = [e["requirement"]["status"] for e in events]
        self.assertEqual(statuses[0], "installing")
        self.assertEqual(statuses[-1], "installed")

    async def test_failed_install_command_reports_error_status(self):
        fake_req = {"id": "thing", "label": "Thing", "description": "d",
                    "check": _fake_check(False), "install_cmd": ["false"], "required": True}
        events = []
        with patch.object(rc, "REQUIREMENTS", [fake_req]), \
             patch.object(rc, "_BY_ID", {"thing": fake_req}):
            manager = RequirementsManager()
            manager.register_listener(events.append)
            await manager.install("thing")

        self.assertEqual(events[-1]["requirement"]["status"], "error")
        self.assertIn("failed", events[-1]["requirement"]["detail"])

    async def test_start_install_actually_runs_without_an_external_reference(self):
        """Regression: the install endpoint used to call
        asyncio.create_task(manager.install(...)) directly and discard the
        result. Nothing else referenced that task, so asyncio (which only
        holds a *weak* reference to a task) could garbage-collect it before
        the event loop ever ran it — every click on Install silently did
        nothing, though the endpoint still returned 200 "started". This
        reproduces the real call path (fire-and-forget, like the HTTP
        handler does) and forces garbage collection to prove the task
        survives and actually completes."""
        import gc

        calls = {"n": 0}

        def check():
            calls["n"] += 1
            return (calls["n"] > 1, "2.0", "ok")

        fake_req = {"id": "thing", "label": "Thing", "description": "d",
                    "check": check, "install_cmd": ["true"], "required": True}
        with patch.object(rc, "REQUIREMENTS", [fake_req]), \
             patch.object(rc, "_BY_ID", {"thing": fake_req}):
            manager = RequirementsManager()
            manager.start_install("thing")  # no reference kept by the caller, on purpose
            gc.collect()
            for _ in range(50):
                await asyncio.sleep(0.05)
                if manager.state["thing"]["status"] == "installed":
                    break

        self.assertEqual(manager.state["thing"]["status"], "installed")


if __name__ == "__main__":
    unittest.main()
