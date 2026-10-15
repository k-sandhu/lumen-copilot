"""Dependency-free QA definitions, immutable run records and version queries (#766)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")
HASH = re.compile(r"^[0-9a-f]{64}$")
IDENT = re.compile(r"^QA-[A-Z0-9]+-[0-9]{3}$")
OUTCOMES = {"passed", "failed", "partial", "blocked", "skipped", "not_run"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def fingerprint(value):
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def relative_path(value):
    require(
        nonempty(value) and "\\" not in value,
        "evidence/reference path must be relative",
    )
    path = PurePosixPath(value)
    require(
        not path.is_absolute() and ".." not in path.parts and ":" not in value,
        "evidence/reference path must stay inside its artifact root",
    )
    return path


def validate_catalog(catalog, root):
    require(catalog.get("schema_version") == 1, "unsupported catalog schema")
    source = catalog["source"]
    snapshot = root / relative_path(source["snapshot_path"])
    require(
        file_hash(snapshot) == source["snapshot_sha256"],
        "source snapshot fingerprint differs",
    )
    original = []
    kind = None
    for line in snapshot.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("Likely gaps"):
            kind = "proposed_gap"
        elif line.startswith("These are expected passing conditions"):
            kind = "existing_area"
        if line.startswith("|") and not re.match(r"^\|\s*(Test|---)", line):
            values = tuple(x.strip() for x in line.split("|")[1:-1])
            if len(values) == 3:
                original.append((kind, *values))
    require(len(original) == 71, "source snapshot must retain all 71 imported entries")
    seen, imported = set(), []
    for entry in catalog["entries"]:
        ident = entry["id"]
        require(bool(IDENT.fullmatch(ident)), "invalid catalog ID")
        require(ident not in seen, "duplicate catalog ID")
        seen.add(ident)
        require(
            entry["kind"] in {"existing_area", "proposed_gap"}, "invalid catalog kind"
        )
        require(entry["status"] in {"active", "retired"}, "invalid catalog lifecycle")
        require(
            all(nonempty(entry.get(k)) for k in ("title", "steps", "expected")),
            "catalog text must be nonempty",
        )
        if entry.get("origin", "chat") == "chat":
            imported.append(
                (entry["kind"], entry["title"], entry["steps"], entry["expected"])
            )
    require(
        Counter(imported) == Counter(original),
        "catalog text differs from source snapshot",
    )


def load_definitions(root=ROOT):
    catalog = load_json(root / "docs/qa/catalog.json")
    validate_catalog(catalog, root)
    area_ids = {x["id"] for x in catalog["entries"]}
    document = load_json(root / "docs/qa/cases.json")
    require(document.get("schema_version") == 1, "unsupported cases schema")
    cases = {}
    for case in document["cases"]:
        ident = case["id"]
        require(
            bool(IDENT.fullmatch(ident)) and ident not in cases,
            "invalid/duplicate case ID",
        )
        require(case["status"] in {"active", "retired"}, "invalid case lifecycle")
        require(
            nonempty(case["title"]) and nonempty(case["expected"]),
            "empty case definition",
        )
        require(
            case["steps"] and all(nonempty(x) for x in case["steps"]),
            "case needs steps",
        )
        require(
            case["areas"] and set(case["areas"]) <= area_ids, "unknown catalog area"
        )
        require(
            case["method"] in {"manual_browser", "http_smoke"}, "unknown case method"
        )
        for reference in case["references"]:
            require(
                (root / relative_path(reference)).is_file(), "missing case reference"
            )
        cases[ident] = case
    require(cases, "case catalog cannot be empty")
    return catalog, cases


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value):
    require(isinstance(value, str), "timestamp must be a UTC string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(
        parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0,
        "timestamp must include UTC timezone",
    )
    return parsed


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def make_run(root, run_id, app_commit, context, definition_commit, definition_dirty):
    _, cases = load_definitions(root)
    application = dict(context["application"], commit=app_commit)
    result = {
        "schema_version": 1,
        "kind": "cases",
        "run_id": run_id,
        "started_at": now(),
        "finished_at": now(),
        "application": application,
        "definitions": {
            "commit": definition_commit,
            "dirty": definition_dirty,
            "catalog_sha256": file_hash(root / "docs/qa/catalog.json"),
            "cases_sha256": file_hash(root / "docs/qa/cases.json"),
        },
        "environment": context["environment"],
        "ci": None,
        "notes": "",
        "results": [
            {
                "case_id": case["id"],
                "definition": case,
                "definition_sha256": fingerprint(case),
                "outcome": "not_run",
                "reason": "Not exercised in this run.",
                "residual_risk": "The documented behavior remains unverified.",
                "evidence": [],
                "bugs": [],
            }
            for case in cases.values()
            if case["status"] == "active"
        ],
    }
    validate_run(result, cases)
    return result


def check_credentials(value):
    if isinstance(value, dict):
        for key, child in value.items():
            require(
                not re.search(
                    r"password|secret|api.?key|authorization|cookie|(^|_)token($|_)",
                    key,
                    re.I,
                ),
                "credential field cannot be stored in QA metadata",
            )
            check_credentials(child)
    elif isinstance(value, list):
        for child in value:
            check_credentials(child)


def validate_run(run, cases):
    require(run.get("schema_version") == 1, "unsupported run schema")
    ident = run["run_id"]
    require(
        isinstance(ident, str)
        and bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,100}", ident))
        and ".." not in ident,
        "invalid run ID",
    )
    require(
        timestamp(run["finished_at"]) >= timestamp(run["started_at"]),
        "finish timestamp precedes start",
    )
    for name in ("application", "definitions"):
        require(
            isinstance(run[name]["commit"], str)
            and bool(SHA.fullmatch(run[name]["commit"])),
            "full lowercase commit SHA required",
        )
    require(
        type(run["definitions"]["dirty"]) is bool, "definition dirty state required"
    )
    for key in ("catalog_sha256", "cases_sha256"):
        require(
            isinstance(run["definitions"][key], str)
            and bool(HASH.fullmatch(run["definitions"][key])),
            "definition fingerprint required",
        )
    app = run["application"]
    require(
        app["source_state"] in {"clean", "modified", "unknown"}, "invalid source state"
    )
    services = set()
    for image in app["images"]:
        require(
            nonempty(image["service"]) and image["service"] not in services,
            "duplicate/empty deployed service",
        )
        services.add(image["service"])
        require(
            bool(re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"])),
            "immutable deployed image digest required",
        )
    environment = run["environment"]
    require(
        nonempty(environment["label"])
        and environment["kind"] in {"live_local", "ci_offline", "live_ci"},
        "explicit environment kind and label required",
    )
    require(
        environment["browser"] is None or nonempty(environment["browser"]),
        "invalid browser",
    )
    require(
        isinstance(environment["models"], list)
        and all(nonempty(x) for x in environment["models"]),
        "invalid model context",
    )
    require(
        environment["database_schema"] is None
        or nonempty(environment["database_schema"]),
        "invalid database schema context",
    )
    for field in ("data", "configuration"):
        require(isinstance(environment[field], dict), "context must be an object")
        check_credentials(environment[field])
    if run.get("ci") is not None:
        ci = run["ci"]
        require(
            bool(
                re.fullmatch(
                    r"https://github\.com/[^/]+/[^/]+/actions/runs/[0-9]+", ci["url"]
                )
            ),
            "unsigned GitHub run URL required",
        )
        require(
            nonempty(ci["workflow"]) and nonempty(str(ci["attempt"])),
            "CI context required",
        )
    if run["kind"] == "suite":
        suite = run["suite"]
        require(
            suite["outcome"] in {"passed", "failed", "skipped", "blocked"},
            "invalid suite outcome",
        )
        counts = Counter(x["outcome"] for x in suite["tests"])
        require(
            len({x["test_id"] for x in suite["tests"]}) == len(suite["tests"]),
            "duplicate suite result",
        )
        require(
            all(
                x["outcome"] in {"passed", "failed", "errored", "skipped"}
                for x in suite["tests"]
            ),
            "invalid suite test outcome",
        )
        expected = {k: counts[k] for k in ("passed", "failed", "errored", "skipped")}
        require(suite["counts"] == expected, "suite count mismatch")
        actual = (
            "blocked"
            if not suite["tests"]
            else "failed"
            if counts["failed"] + counts["errored"]
            else "skipped"
            if counts["skipped"] == len(suite["tests"])
            else "passed"
        )
        require(suite["outcome"] == actual, "suite outcome contradicts test results")
        require(
            actual == "passed" or nonempty(suite["reason"]),
            "suite limitation reason required",
        )
        return
    require(
        run["kind"] == "cases" and run["results"], "run needs concrete case results"
    )
    seen = set()
    for result in run["results"]:
        case_id = result["case_id"]
        require(case_id in cases, "unknown case ID")
        require(case_id not in seen, "duplicate case result")
        seen.add(case_id)
        require(
            result["definition"]["id"] == case_id, "definition belongs to another case"
        )
        require(
            fingerprint(result["definition"]) == result["definition_sha256"],
            "case definition fingerprint differs",
        )
        outcome = result["outcome"]
        require(outcome in OUTCOMES, "unknown outcome")
        if outcome == "passed":
            require(result["evidence"], "passing result requires evidence")
        else:
            require(
                nonempty(result["reason"]) and nonempty(result["residual_risk"]),
                "non-pass requires reason and residual risk",
            )
        for evidence in result["evidence"]:
            require(nonempty(evidence["description"]), "evidence description required")
            require(
                evidence["kind"] in {"observation", "artifact"}, "invalid evidence kind"
            )
            if evidence["kind"] == "artifact":
                relative_path(evidence["path"])
                require(
                    bool(HASH.fullmatch(evidence["sha256"])),
                    "artifact fingerprint required",
                )
        check_credentials(result["evidence"])
        for bug in result["bugs"]:
            require(
                bool(
                    re.fullmatch(r"https://github\.com/[^/]+/[^/]+/issues/[0-9]+", bug)
                ),
                "bug must link to an issue",
            )


def write_json_exclusive(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return path


def write_run(directory, run, cases):
    validate_run(run, cases)
    return write_json_exclusive(directory / (run["run_id"] + ".json"), run)


def parse_junit(xml, suite_name):
    tests = []
    if xml:
        root = ET.fromstring(xml)
        require(root.tag in {"testsuite", "testsuites"}, "unsupported JUnit root")
        for case in root.iter("testcase"):
            ident = f"{suite_name}::{case.get('classname', '')}::{case.get('name', '')}"
            require(nonempty(case.get("name")), "JUnit test name required")
            children = [x.tag for x in case if x.tag in {"failure", "error", "skipped"}]
            require(len(children) <= 1, "conflicting JUnit outcomes")
            outcome = {
                "failure": "failed",
                "error": "errored",
                "skipped": "skipped",
            }.get(children[0] if children else "", "passed")
            tests.append({"test_id": ident, "outcome": outcome})
        require(
            len({x["test_id"] for x in tests}) == len(tests), "duplicate JUnit case"
        )
        if root.get("tests") is not None:
            require(
                int(root.get("tests")) == len(tests),
                "JUnit declared test total differs",
            )
    counts = Counter(x["outcome"] for x in tests)
    if xml:
        for attribute, outcome_key in (
            ("failures", "failed"),
            ("errors", "errored"),
            ("skipped", "skipped"),
        ):
            if root.get(attribute) is not None:
                require(
                    int(root.get(attribute)) == counts[outcome_key],
                    "JUnit declared outcome total differs",
                )
    outcome = (
        "blocked"
        if not tests
        else "failed"
        if counts["failed"] + counts["errored"]
        else "skipped"
        if counts["skipped"] == len(tests)
        else "passed"
    )
    return {
        "name": suite_name,
        "outcome": outcome,
        "reason": {
            "blocked": "JUnit absent or empty; no executed test evidence.",
            "skipped": "Every collected test was skipped.",
            "failed": "JUnit contains failed or errored tests.",
            "passed": "",
        }[outcome],
        "counts": {k: counts[k] for k in ("passed", "failed", "errored", "skipped")},
        "tests": tests,
    }


def validate_evolution(previous, current):
    for ident, entry in previous.items():
        require(ident in current, f"removed stable ID {ident}; retire it instead")
        require(entry["title"] == current[ident]["title"], f"reused stable ID {ident}")


def validate_history(root, base):
    git(root, "rev-parse", "--verify", base + "^{commit}")
    changed = git(root, "diff", "--name-status", base, "HEAD", "--", "qa/runs")
    for line in changed.splitlines():
        require(
            line.startswith("A\t"),
            "existing QA run modified/deleted; append a correction",
        )
    for path, key in (
        ("docs/qa/catalog.json", "entries"),
        ("docs/qa/cases.json", "cases"),
    ):
        prior = subprocess.run(
            ["git", "-C", str(root), "show", base + ":" + path],
            capture_output=True,
            text=True,
        )
        if prior.returncode == 0:
            previous = {x["id"]: x for x in json.loads(prior.stdout)[key]}
            current = {x["id"]: x for x in load_json(root / path)[key]}
            validate_evolution(previous, current)


def markdown(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(runs, cases, app_commit=None, case_filter=None):
    lines = [
        "# Version-linked QA results",
        "",
        "Scoped case/suite outcomes; no automatic catalog-area pass claims.",
        "",
        "| Run | Application commit | Test-definition commit | Case or suite | Result | Definition | Environment |",
        "|---|---|---|---|---|---|---|",
    ]
    count = 0
    for run in sorted(runs, key=lambda x: x["finished_at"], reverse=True):
        if app_commit and run["application"]["commit"] != app_commit:
            continue
        if run["kind"] == "suite":
            if case_filter:
                continue
            entries = [
                (run["suite"]["name"], run["suite"]["outcome"], "automated suite")
            ]
        else:
            entries = []
            for result in run["results"]:
                if case_filter and case_filter != result["case_id"]:
                    continue
                current = cases[result["case_id"]]
                state = (
                    "current definition"
                    if fingerprint(current) == result["definition_sha256"]
                    else "changed definition"
                )
                entries.append((result["case_id"], result["outcome"], state))
        for name, outcome, definition in entries:
            count += 1
            app = (
                run["application"]["commit"]
                + " ("
                + run["application"]["source_state"]
                + ")"
            )
            definitions = run["definitions"]["commit"] + (
                " (dirty)" if run["definitions"]["dirty"] else ""
            )
            context = run["environment"]
            environment = context["label"] + "; " + ", ".join(context["models"])
            environment += "; context " + fingerprint(context)[:12]
            row = [
                run["run_id"],
                app,
                definitions,
                name,
                outcome,
                definition,
                environment,
            ]
            lines.append("| " + " | ".join(markdown(x) for x in row) + " |")
    if not count:
        lines.extend(
            ["", "No recorded results match this exact application revision/case."]
        )
    return "\n".join(lines) + "\n"


def evaluate_probe(case_id, responses):
    if any(response is None for response in responses):
        return {
            "outcome": "blocked",
            "reason": "Local HTTP dependency could not be reached.",
            "residual_risk": "Expected response remains unverified.",
            "evidence": [
                {"kind": "observation", "description": "Connection/timeout failure"}
            ],
        }
    expected = {
        "QA-AUTH-001": 401,
        "QA-INPUT-001": 422,
        "QA-NAV-001": 404,
        "QA-RUNTIME-001": 200,
    }[case_id]
    valid = len(responses) == (2 if case_id == "QA-AUTH-001" else 1)
    for status, content_type, body in responses:
        valid = (
            valid
            and status == expected
            and (content_type == "application/json" or content_type.endswith("+json"))
        )
        valid = valid and isinstance(body, dict)
        if case_id == "QA-RUNTIME-001":
            dependencies = (
                body.get("dependencies", []) if isinstance(body, dict) else []
            )
            valid = (
                valid
                and body.get("status") == "ready"
                and bool(dependencies)
                and all(
                    isinstance(x, dict) and x.get("ok") is True for x in dependencies
                )
            )
        else:
            valid = valid and body.get("status") == expected
    return {
        "outcome": "passed" if valid else "failed",
        "reason": ""
        if valid
        else "Response status, JSON problem or dependency readiness differed.",
        "residual_risk": ""
        if valid
        else "The case's expected boundary behavior is not established.",
        "evidence": [
            {
                "kind": "observation",
                "description": "Unauthenticated loopback HTTP probes",
                "responses": [
                    {"status": x[0], "content_type": x[1]} for x in responses
                ],
            }
        ],
    }


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_smoke(run, base_url):
    parsed = urllib.parse.urlsplit(base_url)
    require(
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"},
        "http-smoke only targets an explicit loopback HTTP origin",
    )
    endpoints = {
        "QA-RUNTIME-001": [("/health/ready", None)],
        "QA-AUTH-001": [("/api/v1/auth/me", None), ("/api/v1/documents", None)],
        "QA-INPUT-001": [("/api/v1/auth/login", b"{}")],
        "QA-NAV-001": [("/api/v1/qa-route-does-not-exist", None)],
    }
    opener = urllib.request.build_opener(NoRedirects)
    for result in run["results"]:
        if result["case_id"] not in endpoints:
            continue
        responses = []
        for path, data in endpoints[result["case_id"]]:
            request = urllib.request.Request(
                base_url.rstrip("/") + path,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            try:
                try:
                    response = opener.open(request, timeout=8)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    content_type = response.headers.get_content_type()
                    try:
                        body = json.loads(response.read(1024 * 1024))
                    except (ValueError, UnicodeDecodeError):
                        body = {}
                    responses.append((response.status, content_type, body))
            except (OSError, urllib.error.URLError):
                responses.append(None)
        result.update(evaluate_probe(result["case_id"], responses))
    run["finished_at"] = now()
    return run


def ci_metadata():
    if not os.environ.get("GITHUB_RUN_ID"):
        return None
    return {
        "url": f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
        "workflow": os.environ.get("GITHUB_WORKFLOW", "CI"),
        "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument(
        "--base", help="Base commit for append-only/ID evolution checks"
    )
    sub.add_parser("catalog")
    for name in ("begin", "http-smoke"):
        command = sub.add_parser(name)
        command.add_argument("--run-id", required=True)
        command.add_argument("--app-commit", required=True)
        command.add_argument("--context", required=True, type=Path)
        command.add_argument("--output", required=True, type=Path)
        if name == "http-smoke":
            command.add_argument("--base-url", default="http://localhost:47180")
    record = sub.add_parser("record")
    record.add_argument("--input", required=True, type=Path)
    report = sub.add_parser("report")
    report.add_argument("--runs-dir", type=Path)
    report.add_argument("--app-commit")
    report.add_argument("--case")
    suite = sub.add_parser("suite")
    suite.add_argument("--suite", required=True)
    suite.add_argument("--junit", required=True, type=Path)
    suite.add_argument("--run-id", required=True)
    suite.add_argument("--output", required=True, type=Path)
    suite.add_argument("--started-at-file", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    catalog, cases = load_definitions(root)
    if args.command == "catalog":
        print(
            "# QA catalog\n\n52 imported areas and 19 proposed gaps; headings are not test passes.\n"
        )
        for entry in catalog["entries"]:
            print(f"- {entry['id']} ({entry['kind']}): {entry['title']}")
        return 0
    if args.command == "validate":
        for path in (root / "qa/runs").glob("*.json"):
            run = load_json(path)
            validate_run(run, cases)
            require(path.stem == run["run_id"], "ledger filename must match run ID")
        if args.base:
            validate_history(root, args.base)
        print(
            f"Valid: {len(catalog['entries'])} catalog entries; {len(cases)} concrete cases."
        )
        return 0
    if args.command == "record":
        run = load_json(args.input)
        print(write_run(root / "qa/runs", run, cases))
        return 0
    if args.command == "report":
        if args.app_commit:
            require(
                bool(SHA.fullmatch(args.app_commit)),
                "query requires an exact full commit SHA",
            )
        if args.case:
            require(args.case in cases, "unknown case filter")
        runs = [
            load_json(p) for p in (args.runs_dir or root / "qa/runs").rglob("*.json")
        ]
        for run in runs:
            validate_run(run, cases)
        print(render_report(runs, cases, args.app_commit, args.case), end="")
        return 0
    commit = git(root, "rev-parse", "HEAD")
    dirty = bool(git(root, "status", "--porcelain"))
    if args.command == "suite":
        context = {
            "application": {
                "source_state": "modified" if dirty else "clean",
                "images": [],
            },
            "environment": {
                "label": f"CI/offline {args.suite}",
                "kind": "ci_offline",
                "browser": None,
                "models": ["deterministic fakes; RUN_LIVE=0"],
                "database_schema": None,
                "data": {},
                "configuration": {},
            },
        }
        run = make_run(root, args.run_id, commit, context, commit, dirty)
        run["kind"] = "suite"
        run.pop("results")
        xml = args.junit.read_text(encoding="utf-8") if args.junit.is_file() else None
        run["suite"] = parse_junit(xml, args.suite)
        run["suite"]["report_sha256"] = (
            file_hash(args.junit) if xml is not None else None
        )
        if args.started_at_file and args.started_at_file.is_file():
            run["started_at"] = args.started_at_file.read_text(encoding="utf-8").strip()
        run["ci"] = ci_metadata()
        run["finished_at"] = now()
        validate_run(run, cases)
        write_json_exclusive(args.output, run)
        summary = render_report([run], cases)
        print(summary, end="")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(summary)
        return 1 if run["suite"]["outcome"] in {"blocked", "failed"} else 0
    require(
        bool(SHA.fullmatch(args.app_commit)),
        "application commit must be full lowercase SHA",
    )
    git(root, "rev-parse", "--verify", args.app_commit + "^{commit}")
    run = make_run(
        root, args.run_id, args.app_commit, load_json(args.context), commit, dirty
    )
    if args.command == "http-smoke":
        http_smoke(run, args.base_url)
    validate_run(run, cases)
    write_json_exclusive(args.output, run)
    print(args.output)
    return int(any(x["outcome"] in {"failed", "blocked"} for x in run["results"]))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        ValueError,
        KeyError,
        OSError,
        ET.ParseError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"QA tracking error: {error}", file=sys.stderr)
        sys.exit(2)
