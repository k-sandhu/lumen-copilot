"""Behavioral regressions for the QA ledger contract (issue #766)."""

import copy
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qa_tracking", ROOT / "scripts/qa_tracking.py"
)
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)
SHA = "a" * 40
OTHER = "b" * 40


class TrackingContract(unittest.TestCase):
    def setUp(self):
        self.catalog, self.cases = qa.load_definitions(ROOT)
        self.context = {
            "application": {"source_state": "clean", "images": []},
            "environment": {
                "label": "isolated test",
                "kind": "live_local",
                "browser": "Chrome test",
                "models": ["fake/model"],
                "database_schema": "test",
                "data": {},
                "configuration": {},
            },
        }
        self.run = qa.make_run(ROOT, "test-run", SHA, self.context, OTHER, False)

    def complete(self, case_id="QA-CHAT-002"):
        result = next(x for x in self.run["results"] if x["case_id"] == case_id)
        result.update(
            outcome="passed",
            reason="",
            residual_risk="",
            evidence=[
                {"kind": "observation", "description": "Visible zero-citation refusal"}
            ],
        )
        return result

    def test_preserves_all_imported_areas_and_proposed_gaps(self):
        entries = self.catalog["entries"]
        self.assertEqual(sum(x["kind"] == "existing_area" for x in entries), 52)
        self.assertEqual(sum(x["kind"] == "proposed_gap" for x in entries), 19)
        self.assertEqual(len({x["id"] for x in entries}), 71)

    def test_imported_text_is_checked_against_frozen_chat_snapshot(self):
        catalog = copy.deepcopy(self.catalog)
        catalog["entries"][0]["steps"] = "Invented replacement"
        with self.assertRaisesRegex(ValueError, "source|snapshot"):
            qa.validate_catalog(catalog, ROOT)
        catalog = copy.deepcopy(self.catalog)
        catalog["entries"][0]["kind"] = "existing_area"
        with self.assertRaisesRegex(ValueError, "source|snapshot"):
            qa.validate_catalog(catalog, ROOT)

    def test_valid_partial_run_round_trips_without_promoting_untested_cases(self):
        self.complete()
        with tempfile.TemporaryDirectory() as tmp:
            path = qa.write_run(Path(tmp), self.run, self.cases)
            saved = json.loads(path.read_text())
            self.assertEqual(sum(x["outcome"] == "passed" for x in saved["results"]), 1)
            self.assertTrue(any(x["outcome"] == "not_run" for x in saved["results"]))

    def test_append_only_record_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa.write_run(Path(tmp), self.run, self.cases)
            before = (Path(tmp) / "test-run.json").read_bytes()
            with self.assertRaises(FileExistsError):
                qa.write_run(Path(tmp), self.run, self.cases)
            self.assertEqual(before, (Path(tmp) / "test-run.json").read_bytes())

    def test_run_id_cannot_escape_ledger(self):
        for value in ("../escape", "a/escape", "a\\escape", "a..b", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bad = copy.deepcopy(self.run)
                bad["run_id"] = value
                qa.validate_run(bad, self.cases)

    def test_pass_without_evidence_is_rejected(self):
        self.complete()["evidence"] = []
        with self.assertRaisesRegex(ValueError, "evidence"):
            qa.validate_run(self.run, self.cases)

    def test_unknown_and_duplicate_case_results_are_rejected(self):
        bad = copy.deepcopy(self.run)
        bad["results"][0]["case_id"] = "QA-UNKNOWN-999"
        with self.assertRaises(ValueError):
            qa.validate_run(bad, self.cases)
        self.run["results"].append(copy.deepcopy(self.run["results"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            qa.validate_run(self.run, self.cases)

    def test_partial_skip_and_not_run_require_reason_and_risk(self):
        for outcome in ("partial", "skipped", "blocked", "not_run", "failed"):
            for field in ("reason", "residual_risk"):
                with self.subTest(outcome=outcome, field=field):
                    bad = copy.deepcopy(self.run)
                    bad["results"][0].update(
                        outcome=outcome,
                        reason="Not exercised",
                        residual_risk="Unverified",
                        evidence=[],
                    )
                    bad["results"][0][field] = ""
                    with self.assertRaises(ValueError):
                        qa.validate_run(bad, self.cases)

    def test_full_revisions_and_utc_execution_order_are_required(self):
        for field in ("application", "definitions"):
            bad = copy.deepcopy(self.run)
            bad[field]["commit"] = "main"
            with self.assertRaises(ValueError):
                qa.validate_run(bad, self.cases)
        bad = copy.deepcopy(self.run)
        bad["finished_at"] = "2000-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            qa.validate_run(bad, self.cases)
        bad["finished_at"] = "2030-01-01T00:00:00"
        with self.assertRaises(ValueError):
            qa.validate_run(bad, self.cases)

    def test_definition_snapshot_tampering_is_rejected(self):
        self.run["results"][0]["definition"]["expected"] = "Silently widened"
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            qa.validate_run(self.run, self.cases)

    def test_historical_definition_is_retained_and_labelled_changed(self):
        self.complete()
        changed = copy.deepcopy(self.cases)
        changed["QA-CHAT-002"]["expected"] = "New expectation"
        qa.validate_run(self.run, changed)
        report = qa.render_report([self.run], changed, app_commit=SHA)
        self.assertIn("changed definition", report)
        self.assertIn(SHA, report)

    def test_results_never_carry_across_application_revisions(self):
        self.complete()
        report = qa.render_report([self.run], self.cases, app_commit=OTHER)
        self.assertNotIn("| passed |", report)
        self.assertIn("No recorded results", report)

    def test_evidence_paths_and_credential_fields_are_rejected(self):
        result = self.complete()
        result["evidence"] = [
            {
                "kind": "artifact",
                "path": "../secret",
                "sha256": "a" * 64,
                "description": "bad path",
            }
        ]
        with self.assertRaises(ValueError):
            qa.validate_run(self.run, self.cases)
        result["evidence"] = [{"kind": "observation", "description": "valid"}]
        self.run["environment"]["configuration"]["api_key"] = "should-not-persist"
        with self.assertRaisesRegex(ValueError, "credential"):
            qa.validate_run(self.run, self.cases)

    def test_junit_uses_actual_cases_and_does_not_mark_skips_passed(self):
        xml = '<testsuites tests="3"><testsuite><testcase classname="a" name="ok"/><testcase classname="a" name="skip"><skipped/></testcase><testcase classname="a" name="bad"><failure type="AssertionError">private error text</failure></testcase></testsuite></testsuites>'
        parsed = qa.parse_junit(xml, "backend")
        self.assertEqual(
            parsed["counts"], {"passed": 1, "failed": 1, "errored": 0, "skipped": 1}
        )
        self.assertEqual(parsed["outcome"], "failed")
        self.assertNotIn("private error text", json.dumps(parsed))

    def test_empty_missing_and_all_skipped_suites_never_pass(self):
        for xml in (None, "<testsuites/>"):
            self.assertEqual(qa.parse_junit(xml, "backend")["outcome"], "blocked")
        xml = (
            '<testsuite tests="1"><testcase name="x"><skipped/></testcase></testsuite>'
        )
        self.assertEqual(qa.parse_junit(xml, "backend")["outcome"], "skipped")

    def test_junit_rejects_duplicate_cases_or_false_totals(self):
        for xml in (
            '<testsuite tests="9"><testcase name="x"/></testsuite>',
            '<testsuite><testcase name="x"/><testcase name="x"/></testsuite>',
            '<testsuite tests="1" failures="1"><testcase name="x"/></testsuite>',
        ):
            with self.assertRaises(ValueError):
                qa.parse_junit(xml, "backend")

    def test_http_smoke_distinguishes_blocked_wrong_status_and_valid_json(self):
        good = qa.evaluate_probe(
            "QA-AUTH-001",
            [
                (401, "application/problem+json", {"status": 401}),
                (401, "application/problem+json", {"status": 401}),
            ],
        )
        self.assertEqual(good["outcome"], "passed")
        self.assertEqual(qa.evaluate_probe("QA-AUTH-001", [None])["outcome"], "blocked")
        self.assertEqual(
            qa.evaluate_probe("QA-NAV-001", [(200, "text/html", {})])["outcome"],
            "failed",
        )
        self.assertEqual(
            qa.evaluate_probe("QA-NAV-001", [(404, "text/html", {})])["outcome"],
            "failed",
        )
        self.assertEqual(
            qa.evaluate_probe(
                "QA-NAV-001", [(404, "application/json", {"detail": "Not Found"})]
            )["outcome"],
            "passed",
        )
        ready = {"status": "ready", "dependencies": [{"name": "postgres", "ok": False}]}
        self.assertEqual(
            qa.evaluate_probe("QA-RUNTIME-001", [(200, "application/json", ready)])[
                "outcome"
            ],
            "failed",
        )

    def test_catalog_and_case_id_changes_are_not_silent(self):
        catalog = copy.deepcopy(self.catalog)
        catalog["entries"].append(copy.deepcopy(catalog["entries"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            qa.validate_catalog(catalog, ROOT)
        base = {"id": "QA-OLD-001", "title": "Original"}
        with self.assertRaises(ValueError):
            qa.validate_evolution({"QA-OLD-001": base}, {})
        with self.assertRaises(ValueError):
            qa.validate_evolution(
                {"QA-OLD-001": base},
                {"QA-OLD-001": {"id": "QA-OLD-001", "title": "Reused"}},
            )

    def test_suite_record_is_valid_without_inventing_catalog_case_passes(self):
        suite = copy.deepcopy(self.run)
        suite["kind"] = "suite"
        suite.pop("results")
        suite["suite"] = qa.parse_junit(
            '<testsuite tests="1"><testcase name="ok"/></testsuite>', "tracking"
        )
        qa.validate_run(suite, self.cases)
        report = qa.render_report([suite], self.cases)
        self.assertIn("tracking", report)
        self.assertNotIn("QA-CHAT-002 | passed", report)

    def test_committed_ledger_modification_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(root), *args], text=True
                ).strip()

            git("init", "--quiet")
            ledger = root / "qa/runs/old.json"
            ledger.parent.mkdir(parents=True)
            ledger.write_text("{}")
            git("add", ".")
            git(
                "-c",
                "user.name=QA",
                "-c",
                "user.email=qa@example.test",
                "commit",
                "--quiet",
                "-m",
                "test: initial run",
            )
            base = git("rev-parse", "HEAD")
            (root / "qa/runs/new.json").write_text("{}")
            self.assertFalse(qa.definition_tree_dirty(root))
            (root / "case-change.txt").write_text("new test data")
            self.assertTrue(qa.definition_tree_dirty(root))
            ledger.write_text('{"changed": true}')
            git("add", ".")
            git(
                "-c",
                "user.name=QA",
                "-c",
                "user.email=qa@example.test",
                "commit",
                "--quiet",
                "-m",
                "test: forbidden rewrite",
            )
            with self.assertRaisesRegex(ValueError, "modified/deleted"):
                qa.validate_history(root, base)

    def test_environment_variants_and_dirty_definitions_remain_visible(self):
        self.complete()
        self.run["definitions"]["dirty"] = True
        report = qa.render_report([self.run], self.cases, app_commit=SHA)
        self.assertIn("fake/model", report)
        self.assertIn("(dirty)", report)
        self.assertIn("context ", report)


if __name__ == "__main__":
    unittest.main()
