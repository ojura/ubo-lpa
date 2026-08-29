#!/usr/bin/env python3
"""Contract tests for the Chrome extension-diagnostics gate."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe import (  # noqa: E402
    EXPECTED_UBO_MANIFEST_WARNINGS,
    unexpected_extension_diagnostics,
)

UBO = "a" * 32
COMPANION = "b" * 32
STRANGER = "c" * 32


def record(*, install=(), manifest=(), runtime=()) -> dict:
    return {
        "installWarnings": list(install),
        "manifestErrors": list(manifest),
        "runtimeErrors": list(runtime),
    }


class DiagnosticContractTests(unittest.TestCase):
    def problems(self, info: dict, *, required: bool = True) -> dict:
        return unexpected_extension_diagnostics(info, UBO, COMPANION, required)

    def test_windows_requires_exact_manifest_pair(self) -> None:
        info = {
            UBO: record(manifest=EXPECTED_UBO_MANIFEST_WARNINGS),
            COMPANION: record(),
        }
        self.assertEqual(self.problems(info), {})

    def test_missing_expected_pair_fails_windows(self) -> None:
        problems = self.problems({UBO: record(), COMPANION: record()})
        self.assertIn(f"{UBO}.warnings", problems)

    def test_linux_allows_no_warnings_or_exact_pair(self) -> None:
        empty = {UBO: record(), COMPANION: record()}
        expected = {
            UBO: record(install=EXPECTED_UBO_MANIFEST_WARNINGS),
            COMPANION: record(),
        }
        self.assertEqual(self.problems(empty, required=False), {})
        self.assertEqual(self.problems(expected, required=False), {})

    def test_duplicate_messages_across_sources_are_deduplicated(self) -> None:
        info = {
            UBO: record(
                install=EXPECTED_UBO_MANIFEST_WARNINGS,
                manifest=EXPECTED_UBO_MANIFEST_WARNINGS,
            ),
            COMPANION: record(),
        }
        self.assertEqual(self.problems(info), {})

    def test_extra_ubo_warning_is_rejected(self) -> None:
        info = {
            UBO: record(manifest=[*EXPECTED_UBO_MANIFEST_WARNINGS, "extra"]),
            COMPANION: record(),
        }
        self.assertIn(f"{UBO}.warnings", self.problems(info))

    def test_companion_warning_is_rejected(self) -> None:
        info = {
            UBO: record(manifest=EXPECTED_UBO_MANIFEST_WARNINGS),
            COMPANION: record(manifest=["unexpected companion warning"]),
        }
        self.assertIn(f"{COMPANION}.warnings", self.problems(info))

    def test_runtime_errors_are_never_allowed(self) -> None:
        info = {
            UBO: record(
                manifest=EXPECTED_UBO_MANIFEST_WARNINGS,
                runtime=["background failed"],
            ),
            COMPANION: record(runtime=["worker failed"]),
        }
        problems = self.problems(info)
        self.assertEqual(problems[f"{UBO}.runtimeErrors"], ["background failed"])
        self.assertEqual(problems[f"{COMPANION}.runtimeErrors"], ["worker failed"])

    def test_unrelated_extension_diagnostics_are_ignored(self) -> None:
        info = {
            UBO: record(manifest=EXPECTED_UBO_MANIFEST_WARNINGS),
            COMPANION: record(),
            STRANGER: record(manifest=["not ours"], runtime=["not ours either"]),
        }
        self.assertEqual(self.problems(info), {})


class EmbeddedProbeTests(unittest.TestCase):
    def test_readable_probe_matches_embedded_harness_copy(self) -> None:
        here = Path(__file__).resolve().parent
        harness = (here / "setup-ubo-wine-harness.sh").read_text(encoding="utf-8")
        marker = "cat >\"$WORK_DIR/probe.py\" <<'PYPROBE'\n"
        embedded = harness.split(marker, 1)[1].split("\nPYPROBE\n", 1)[0] + "\n"
        self.assertEqual(embedded, (here / "probe.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
