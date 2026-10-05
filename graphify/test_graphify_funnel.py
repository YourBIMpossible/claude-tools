"""Tests for graphify_funnel.py. All fixtures are SYNTHETIC: fake transcripts, a fake repo
with an empty graph file, and a stub resolver written into a temp dir.

Run: python test_graphify_funnel.py
"""
from __future__ import annotations

import io
import json
import math
import os
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import graphify_funnel as gf

T0 = datetime(2026, 1, 12, 10, 0, tzinfo=timezone.utc)   # synthetic clock
SINCE, UNTIL = T0 - timedelta(days=7), T0 + timedelta(days=1)

STUB_RESOLVER = textwrap.dedent('''
    """SYNTHETIC stub of resolve_graph: a graph exists only under <ROOT>/repo/backend."""
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent

    def _git_toplevel(start):
        for d in (start, *start.parents):
            if (d / ".git").exists():
                return d
        return None

    def _main_checkout(top):
        return None

    def resolve(cwd):
        repo = ROOT / "repo"
        here = Path(cwd).resolve()
        if here == repo or repo in here.parents:
            return {"graph": str(repo / "backend" / "graphify-out" / "graph.json"),
                    "graph_root": str(repo / "backend"), "source": "repo-subdir",
                    "local_root": str(repo / "backend")}
        return None
''')


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class Transcript:
    """Builds one synthetic session transcript (and optional subagent files)."""

    def __init__(self, cwd: Path, start: datetime = T0):
        self.cwd, self.t, self.lines, self.n = str(cwd), start, [], 0
        self.subagents: dict[str, tuple[str, "Transcript"]] = {}

    def _rec(self, **kw) -> None:
        self.t += timedelta(seconds=1)
        self.lines.append({"timestamp": iso(self.t), "cwd": self.cwd, **kw})

    def hint(self) -> "Transcript":
        self._rec(type="attachment", attachment={
            "type": "hook_additional_context", "hookEvent": "SessionStart",
            "content": ["<graphify-graph>\nsynthetic\n</graphify-graph>"]})
        return self

    def tool(self, name: str, inp: dict, output: str = "ok", is_error: bool = False,
             tool_id: str | None = None) -> "Transcript":
        self.n += 1
        tid = tool_id or f"toolu_{id(self)}_{self.n}"
        self._rec(type="assistant", message={"content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]})
        self._rec(type="user", message={"content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": output}]})
        return self

    def reads(self, files: list[Path]) -> "Transcript":
        for f in files:
            self.tool("Read", {"file_path": str(f)})
        return self

    def greps(self, n: int, path: Path | None = None) -> "Transcript":
        for i in range(n):
            self.tool("Grep", {"pattern": f"sym{i}", **({"path": str(path)} if path else {})})
        return self

    def query(self, question: str, output: str = "NODE x [src=a.py]", is_error: bool = False,
              extra: str = "") -> "Transcript":
        return self.tool("Bash", {"command": f'{extra}python "/x/query_reranked.py" "{question}" '
                                             f'--graph "/x/graph.json"'}, output, is_error)

    def subagent(self, agent_id: str, agent_type: str, sub: "Transcript") -> "Transcript":
        self.subagents[agent_id] = (agent_type, sub)
        return self

    def write(self, projects: Path, project: str, session_id: str) -> Path:
        d = projects / project
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{session_id}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in self.lines) + "\n", encoding="utf-8")
        for agent_id, (agent_type, sub) in self.subagents.items():
            sd = d / session_id / "subagents"
            sd.mkdir(parents=True, exist_ok=True)
            (sd / f"agent-{agent_id}.jsonl").write_text(
                "\n".join(json.dumps(r) for r in sub.lines) + "\n", encoding="utf-8")
            (sd / f"agent-{agent_id}.meta.json").write_text(
                json.dumps({"agentType": agent_type}), encoding="utf-8")
        return path


class FunnelCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name).resolve()
        self.repo = self.root / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "backend" / "graphify-out").mkdir(parents=True)
        (self.repo / "backend" / "graphify-out" / "graph.json").write_text("{}", encoding="utf-8")
        self.other = self.root / "other"
        (self.other / ".git").mkdir(parents=True)
        (self.root / "resolve_graph.py").write_text(STUB_RESOLVER, encoding="utf-8")
        self.resolver = gf.load_resolver(self.root)
        self.projects = self.root / "projects"
        self.log = self.root / "queries.log"
        self.log.write_text("", encoding="utf-8")
        self.cfg: dict = {"skill_scripts": str(self.root), "targets": [{"repo": str(self.repo)}],
                          # fixtures live in the temp dir, so keep that rule off here
                          "funnel": {"exclude_sessions": {}, "exclude_temp_cwd": False}}

    def backend_files(self, n: int) -> list[Path]:
        return [self.repo / "backend" / f"m{i}.py" for i in range(n)]

    def log_record(self, question: str, at: datetime, rerank: bool | None = True,
                   duration: float = 900.0, nodes: int = 5) -> None:
        rec = {"ts": at.isoformat(), "kind": "query", "question": question,
               "nodes_returned": nodes, "duration_ms": duration}
        if rerank is not None:
            rec.update(rerank=rerank, via="wrapper")
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def run_funnel(self, log: Path | None = None) -> dict:
        scan = gf.discover_sessions(self.projects, SINCE, UNTIL)
        records = gf.load_log(self.log if log is None else log, SINCE, UNTIL)
        cfg = gf.parse_config(self.cfg, self.root)
        return gf.analyse(scan, records, self.resolver, cfg, SINCE, UNTIL)

    def write_cfg(self, cfg: dict, folder: Path | None = None) -> Path:
        path = (folder or self.root) / "graphify.local.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path


class Routing(FunnelCase):
    def test_graph_backed_exploration_without_query_is_routing_fail(self):
        sub = Transcript(self.repo).greps(3).reads(self.backend_files(3))
        Transcript(self.repo).tool("Agent", {"subagent_type": "Explore", "prompt": "find X"}) \
            .subagent("a1", "Explore", sub).write(self.projects, "p", "s-bypass")
        out = self.run_funnel()
        self.assertEqual(out["routing"]["verdict"], gf.FAIL)
        self.assertEqual(len(out["routing"]["bypassed"]), 1)
        self.assertIn("Explore:a1", out["routing"]["bypassed"][0])
        # Zero queries must not read as a measurement pass or "no demand".
        self.assertEqual(out["measurement"]["verdict"], gf.INSUFFICIENT)
        self.assertEqual(out["execution"]["verdict"], gf.INSUFFICIENT)

    def test_post_hint_bypass_links_its_transcript(self):
        self.cfg["funnel"]["hint_live_since"] = iso(T0 - timedelta(days=1))
        path = Transcript(self.repo).hint().greps(6).reads(self.backend_files(3)) \
            .write(self.projects, "p", "s-post")
        after = self.run_funnel()["routing"]["after_hint_live"]
        self.assertEqual((after["exploration_units"], after["used_graphify"]), (1, 0))
        self.assertIn(f"transcript={path}", after["bypassed"][0])

    def test_subagent_query_routes_and_matches_log(self):
        sub = Transcript(self.repo, T0 + timedelta(minutes=1)).query("who calls sync").greps(2) \
            .reads(self.backend_files(2))
        Transcript(self.repo).hint().subagent("a2", "Explore", sub).write(self.projects, "p", "s-ok")
        self.log_record("who calls sync", T0 + timedelta(minutes=1, seconds=2))
        out = self.run_funnel()
        self.assertEqual(out["routing"]["verdict"], gf.PASS)
        self.assertEqual(out["execution"]["verdict"], gf.PASS)
        self.assertEqual(out["execution"]["logged"], 1)
        self.assertEqual(out["measurement"]["reranked"]["n"], 1)

    def test_no_exploration_is_insufficient_not_pass(self):
        Transcript(self.repo).tool("Read", {"file_path": str(self.backend_files(1)[0])}) \
            .write(self.projects, "p", "s-small")
        out = self.run_funnel()
        self.assertEqual(out["routing"]["verdict"], gf.INSUFFICIENT)
        self.assertEqual(out["routing"]["eligibility_unknown_sessions"], 1)

    def test_lookups_outside_graph_coverage_are_not_eligible(self):
        frontend = [self.repo / "frontend" / f"c{i}.tsx" for i in range(5)]
        Transcript(self.repo).greps(8).reads(frontend).write(self.projects, "p", "s-front")
        self.assertEqual(self.run_funnel()["routing"]["exploration_units"], 0)

    def test_deleted_worktree_paths_count_as_covered(self):
        lane = self.repo / ".claude" / "worktrees" / "gone" / "backend"
        files = [lane / f"m{i}.py" for i in range(3)]
        Transcript(self.repo).greps(6).reads(files).write(self.projects, "p", "s-lane")
        self.assertEqual(self.run_funnel()["routing"]["exploration_units"], 1)


class Execution(FunnelCase):
    def test_shell_failure_before_wrapper_recovers_on_retry(self):
        # Shape of the acceptance run: Git Bash `cd` on a backslash path failed, the retry worked.
        Transcript(self.repo).query("q1", output="cd: reposub: No such file or directory", is_error=True,
                                    extra="cd repo\\sub && ") \
            .query("q1").write(self.projects, "p", "s-retry")
        self.log_record("q1", T0 + timedelta(seconds=4))
        ex = self.run_funnel()["execution"]
        self.assertEqual(ex["verdict"], gf.PASS)
        self.assertEqual(ex["by_status"], {"shell-error": 1, "ok": 1})
        self.assertEqual(ex["unrecovered_shell_errors"], [])
        self.assertEqual(len(ex["recovered_shell_errors"]), 1)
        self.assertIn("No such file or directory", ex["recovered_shell_errors"][0])
        self.assertIn("cd repo", ex["recovered_shell_errors"][0])

    def test_completed_wrapper_call_without_log_record_fails(self):
        Transcript(self.repo).query("lost").write(self.projects, "p", "s-unlogged")
        ex = self.run_funnel()["execution"]
        self.assertEqual(ex["verdict"], gf.FAIL)
        self.assertEqual(len(ex["unlogged_ok_wrapper_calls"]), 1)

    def test_wrapper_error_fails(self):
        Transcript(self.repo).query("bad", output="graphify query failed (exit 1)", is_error=True) \
            .write(self.projects, "p", "s-err")
        ex = self.run_funnel()["execution"]
        self.assertEqual(ex["verdict"], gf.FAIL)
        self.assertEqual(len(ex["wrapper_errors"]), 1)

    def test_test_runs_are_not_calls(self):
        Transcript(self.repo) \
            .tool("Bash", {"command": "python test_query_reranked.py"}) \
            .query("redirected", extra="GRAPHIFY_QUERY_LOG=/tmp/x.log ") \
            .write(self.projects, "p", "s-tests")
        ex = self.run_funnel()["execution"]
        self.assertEqual(ex["calls"], 0)
        self.assertEqual(ex["test_alt_log_calls"], 1)


class OrganicSeparation(FunnelCase):
    def test_excluded_session_stays_out_of_every_stage(self):
        Transcript(self.repo).query("smoke").greps(6).reads(self.backend_files(3)) \
            .write(self.projects, "p", "s-smoke")
        self.log_record("smoke", T0 + timedelta(seconds=2))
        self.cfg["funnel"]["exclude_sessions"] = {"s-smoke": "synthetic smoke run"}
        out = self.run_funnel()
        self.assertEqual(out["sessions"]["organic"], 0)
        self.assertEqual(out["execution"]["calls"], 0)
        self.assertEqual(out["measurement"]["organic_records"], 0)
        self.assertEqual(out["measurement"]["excluded_session_records"], 1)
        self.assertEqual(out["measurement"]["unattributed_records"], 0)

    def test_unmatched_log_records_are_unattributed_not_organic(self):
        self.log_record("manual cli run", T0)
        out = self.run_funnel()
        self.assertEqual(out["measurement"]["organic_records"], 0)
        self.assertEqual(out["measurement"]["unattributed_records"], 1)

    def test_session_copied_into_two_projects_counts_once(self):
        t = Transcript(self.repo).greps(2)
        t.write(self.projects, "p1", "s-dup")
        t.greps(1).write(self.projects, "p2", "s-dup")
        self.assertEqual(self.run_funnel()["sessions"]["total"], 1)

    def test_repeated_tool_ids_are_counted_once(self):
        t = Transcript(self.repo)
        for _ in range(3):
            t.tool("Grep", {"pattern": "x"}, tool_id="toolu_same")
        path = t.write(self.projects, "p", "s-rep")
        self.assertEqual(gf.load_session(path).units["main"].searches, 1)


class Availability(FunnelCase):
    def test_missing_hint_after_go_live_fails(self):
        self.cfg["funnel"]["hint_live_since"] = iso(T0 - timedelta(days=1))
        Transcript(self.repo).greps(1).write(self.projects, "p", "s-nohint")
        av = self.run_funnel()["availability"]
        self.assertEqual(av["verdict"], gf.FAIL)
        self.assertEqual(len(av["hint_missing"]), 1)

    def test_pre_hint_sessions_are_no_evidence_for_delivery(self):
        self.cfg["funnel"]["hint_live_since"] = iso(T0 + timedelta(days=1))
        Transcript(self.repo).greps(1).write(self.projects, "p", "s-early")
        av = self.run_funnel()["availability"]
        self.assertEqual(av["graph_resolution"], gf.PASS)
        self.assertEqual(av["hint_delivery"], gf.INSUFFICIENT)
        self.assertEqual(av["verdict"], gf.INSUFFICIENT)
        self.assertEqual((av["graph_backed_pre_hint"], av["graph_backed_post_hint"]), (1, 0))

    def test_hint_seen_after_go_live_passes(self):
        self.cfg["funnel"]["hint_live_since"] = iso(T0 - timedelta(days=1))
        Transcript(self.repo).hint().greps(1).write(self.projects, "p", "s-hinted")
        Transcript(self.repo, T0 - timedelta(days=2)).greps(1).write(self.projects, "p", "s-before")
        av = self.run_funnel()["availability"]
        self.assertEqual((av["graph_resolution"], av["hint_delivery"], av["verdict"]),
                         (gf.PASS, gf.PASS, gf.PASS))
        row = av["by_repo"][str(self.repo)]
        self.assertEqual((row["graph_pre"], row["hinted_pre"], row["graph_post"], row["hinted_post"]),
                         (1, 0, 1, 1))

    def test_expected_repo_without_graph_fails(self):
        self.cfg["targets"].append({"repo": str(self.other)})
        Transcript(self.other).greps(1).write(self.projects, "p", "s-other")
        av = self.run_funnel()["availability"]
        self.assertEqual(av["verdict"], gf.FAIL)
        self.assertEqual(len(av["expected_graph_missing"]), 1)

    def test_no_graph_backed_sessions_is_insufficient(self):
        Transcript(self.root).greps(1).write(self.projects, "p", "s-nograph")
        self.assertEqual(self.run_funnel()["availability"]["verdict"], gf.INSUFFICIENT)


class Measurement(FunnelCase):
    def _queries(self, n: int, duration: float) -> None:
        t = Transcript(self.repo)
        for i in range(n):
            t.query(f"q{i}")
            self.log_record(f"q{i}", t.t, duration=duration)
        t.write(self.projects, "p", "s-many")

    def test_sample_threshold(self):
        self._queries(gf.MIN_RERANKED - 1, 800)
        self.assertEqual(self.run_funnel()["measurement"]["verdict"], gf.INSUFFICIENT)

    def test_pass_within_latency_ceiling(self):
        self._queries(gf.MIN_RERANKED, 800)
        self.assertEqual(self.run_funnel()["measurement"]["verdict"], gf.PASS)

    def test_fail_above_latency_ceiling(self):
        self._queries(gf.MIN_RERANKED, 3500)
        self.assertEqual(self.run_funnel()["measurement"]["verdict"], gf.FAIL)

    def test_bare_cli_record_is_not_the_stock_arm(self):
        t = Transcript(self.repo)
        t.tool("Bash", {"command": 'graphify query "bare q"'}, output="NODE x [src=a.py]")
        self.log_record("bare q", t.t, rerank=None, duration=600)
        t.query("wrapped q")
        self.log_record("wrapped q", t.t, rerank=False, duration=1500)
        t.write(self.projects, "p", "s-arms")
        m = self.run_funnel()["measurement"]
        self.assertEqual((m["reranked"]["n"], m["stock"]["n"], m["unlabelled_bare_cli"]["n"]), (0, 1, 1))
        self.assertEqual(m["stock"]["median_ms"], 1500)


class TempDirRule(FunnelCase):
    def test_temp_dir_sessions_excluded_by_default(self):
        Transcript(self.repo).greps(1).write(self.projects, "p", "s-tmp")
        del self.cfg["funnel"]["exclude_temp_cwd"]
        out = self.run_funnel()
        self.assertEqual(out["sessions"]["organic"], 0)
        self.assertEqual(out["sessions"]["excluded"][0]["reason"], "cwd in system temp dir")


class Cli(FunnelCase):
    def test_main_writes_report_and_summary(self):
        Transcript(self.repo).greps(6).reads(self.backend_files(3)).write(self.projects, "p", "s-cli")
        cfg = dict(self.cfg, skill_scripts=str(self.root), query_log=str(self.log))
        cfg_path = self.root / "graphify.local.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        out_md, out_json = self.root / "r.md", self.root / "r.json"
        rc = gf.main(["--config", str(cfg_path), "--projects", str(self.projects),
                      "--until", iso(UNTIL), "--days", "8", "--out", str(out_md), "--json", str(out_json)])
        self.assertEqual(rc, 0)
        self.assertIn("| 2 Routing | FAIL |", out_md.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(out_json.read_text(encoding="utf-8"))["routing"]["verdict"], gf.FAIL)

    def test_missing_skill_scripts_is_a_clean_error(self):
        cfg_path = self.root / "graphify.local.json"
        cfg_path.write_text("{}", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(gf.main(["--config", str(cfg_path), "--projects", str(self.projects)]), 2)
        self.assertIn("skill_scripts is missing", err.getvalue())


class UntimedLatency(FunnelCase):
    """M1: a reranked record without a numeric, finite duration_ms is untimed, never 0 ms."""

    UNTIMED = {"absent": ..., "null": None, "string": "9000", "true": True, "false": False,
               "nan": math.nan, "inf": math.inf, "negative": -5}

    def _raw(self, question: str, at: datetime, duration) -> None:
        rec = {"ts": at.isoformat(), "kind": "query", "question": question, "nodes_returned": 5,
               "rerank": True, "via": "wrapper"}
        if duration is not ...:
            rec["duration_ms"] = duration
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")      # NaN/Infinity tokens: json.loads accepts them

    def _session(self, timed: int, timed_ms: float, untimed: list) -> dict:
        t = Transcript(self.repo)
        for i in range(timed):
            t.query(f"t{i}")
            self._raw(f"t{i}", t.t, timed_ms)
        for i, d in enumerate(untimed):
            t.query(f"u{i}")
            self._raw(f"u{i}", t.t, d)
        t.write(self.projects, "p", "s-lat")
        return self.run_funnel()["measurement"]

    def test_each_untimed_form_is_insufficient(self):
        for label, d in self.UNTIMED.items():
            with self.subTest(duration=label):
                self.log.write_text("", encoding="utf-8")
                m = self._session(0, 0, [d] * gf.MIN_RERANKED)
                self.assertEqual(m["verdict"], gf.INSUFFICIENT)
                self.assertEqual((m["reranked"]["n"], m["reranked"]["timed"], m["reranked"]["untimed"]),
                                 (gf.MIN_RERANKED, 0, gf.MIN_RERANKED))
                self.assertIsNone(m["reranked"]["median_ms"])

    def test_untimed_records_do_not_fill_the_sample(self):
        m = self._session(gf.MIN_RERANKED - 1, 800, list(self.UNTIMED.values()))
        self.assertEqual(m["verdict"], gf.INSUFFICIENT)
        self.assertEqual(m["reranked"]["timed"], gf.MIN_RERANKED - 1)

    def test_untimed_records_do_not_pull_latency_down(self):
        m = self._session(gf.MIN_RERANKED, 3500, [None] * (3 * gf.MIN_RERANKED))
        self.assertEqual(m["verdict"], gf.FAIL)
        self.assertEqual(m["reranked"]["median_ms"], 3500)

    def test_incomplete_log_is_insufficient_even_with_timed_records(self):
        t = Transcript(self.repo)
        for i in range(gf.MIN_RERANKED):
            t.query(f"t{i}")
            self._raw(f"t{i}", t.t, 800)
        t.write(self.projects, "p", "s-lat")
        real_iter = gf.iter_json_lines

        def cut_short(path, stats):
            # Every record is read, then the read fails: a partial log is still no evidence.
            yield from real_iter(path, stats)
            if path == self.log:
                stats.error = "OSError: read interrupted"
        with mock.patch.object(gf, "iter_json_lines", side_effect=cut_short):
            out = self.run_funnel()
        m = out["measurement"]
        self.assertEqual(m["reranked"]["timed"], gf.MIN_RERANKED)
        self.assertEqual((m["verdict"], m["log_status"]), (gf.INSUFFICIENT, "unreadable"))
        self.assertIn("read interrupted", out["inputs"]["query_log"]["error"])
        self.assertIn("query log unreadable", gf.render(out))

    def test_unopenable_log_is_reported(self):
        real_open = Path.open

        def denied(path, *a, **k):
            if path == self.log:
                raise PermissionError(13, "denied")
            return real_open(path, *a, **k)
        with mock.patch.object(Path, "open", autospec=True, side_effect=denied):
            out = self.run_funnel()
        self.assertEqual(out["inputs"]["query_log"]["status"], "unreadable")
        self.assertIn("PermissionError", out["inputs"]["query_log"]["error"])


class ConfigPaths(FunnelCase):
    """M2: config paths resolve against the config folder; scan-only targets count."""

    def test_relative_paths_resolve_against_config_folder(self):
        Transcript(self.other).greps(1).write(self.projects, "p", "s-other")
        cfg = dict(self.cfg, skill_scripts="..", query_log="../queries.log",
                   targets=[{"repo": "../repo"}, {"repo": "../other"}])
        cfg_path = self.write_cfg(cfg, self.root / "conf")
        out_json = self.root / "r.json"
        self.assertNotEqual(Path.cwd().resolve(), self.root / "conf")
        rc = gf.main(["--config", str(cfg_path), "--projects", str(self.projects),
                      "--until", iso(UNTIL), "--days", "8", "--out", str(self.root / "r.md"),
                      "--json", str(out_json)])
        self.assertEqual(rc, 0)
        out = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(out["availability"]["verdict"], gf.FAIL)
        self.assertEqual(len(out["availability"]["expected_graph_missing"]), 1)
        self.assertEqual(out["inputs"]["query_log"]["status"], "ok")
        self.assertEqual(Path(out["inputs"]["query_log"]["path"]), self.log)

    def test_scan_only_target_names_an_expected_repo(self):
        (self.other / "src").mkdir()
        Transcript(self.other).greps(1).write(self.projects, "p", "s-other")
        for scan in (str(self.other / "src"), "other/src"):
            with self.subTest(scan=scan):
                self.cfg["targets"] = [{"repo": str(self.repo)}, {"scan": scan}]
                av = self.run_funnel()["availability"]
                self.assertEqual(av["verdict"], gf.FAIL)
                self.assertEqual(len(av["expected_graph_missing"]), 1)

    def test_empty_repo_falls_back_to_scan(self):
        self.cfg["targets"] = [{"scan": "other", "repo": ""}]
        self.assertEqual(gf.parse_config(self.cfg, self.root).targets, (str(self.other),))


class ConfigValidation(FunnelCase):
    """L2: every key the funnel reads is validated before any analysis."""

    def errors(self, cfg: object = None, **overrides) -> list[str]:
        with self.assertRaises(gf.ConfigError) as ctx:
            gf.parse_config(dict(self.cfg, **overrides) if cfg is None else cfg, self.root)
        return ctx.exception.errors

    def test_bad_values_are_all_reported(self):
        errs = self.errors(query_log=5, targets=[{"repo": "<path-to-repo>"}, "x", {}],
                           funnel={"hint_live_since": "2026-13-01", "exclude_temp_cwd": "yes",
                                   "exclude_sessions": "s-1", "hint_since": "2026-01-01"})
        text = "\n".join(errs)
        for needle in ("query_log must be a non-empty string", "targets[0].repo still holds a template",
                       "targets[1] must be a JSON object", "targets[2] needs a scan or repo",
                       "funnel.hint_live_since must be an ISO-8601", "funnel.exclude_temp_cwd must be true",
                       "funnel.exclude_sessions must be an object", "funnel.hint_since is not a known key"):
            self.assertIn(needle, text)
        self.assertEqual(len(errs), 8)

    def test_exclude_session_entries_are_checked(self):
        self.assertIn("non-empty string reason", self.errors(funnel={"exclude_sessions": {"s-1": ""}})[0])
        self.assertIn("non-string or empty session id", self.errors(funnel={"exclude_sessions": [1]})[0])

    def test_shape_errors(self):
        no_scripts = {k: v for k, v in self.cfg.items() if k != "skill_scripts"}
        self.assertIn("skill_scripts is missing", self.errors(no_scripts)[0])
        self.assertIn("funnel must be a JSON object", self.errors(funnel=[])[0])
        self.assertIn("targets must be a JSON array", self.errors(targets={})[0])
        self.assertIn("config must be a JSON object", self.errors([])[0])

    @unittest.skipUnless(os.name == "nt", "drive- and root-relative paths are Windows forms")
    def test_drive_and_root_relative_paths_rejected(self):
        for bad in ("C:repo", "\\repo"):
            with self.subTest(path=bad):
                self.assertIn("drive- or root-relative", self.errors(targets=[{"repo": bad}])[0])

    def test_list_form_of_exclude_sessions(self):
        self.cfg["funnel"]["exclude_sessions"] = ["s-smoke"]
        Transcript(self.repo).greps(1).write(self.projects, "p", "s-smoke")
        out = self.run_funnel()
        self.assertEqual(out["sessions"]["excluded"],
                         [{"session": "s-smoke", "reason": "listed in funnel.exclude_sessions"}])

    def test_cli_rejects_invalid_config_before_analysis(self):
        cfg_path = self.write_cfg(dict(self.cfg, funnel={"exclude_temp_cwd": 1, "bogus": True}))
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(gf, "discover_sessions") as discover:
            rc = gf.main(["--config", str(cfg_path), "--projects", str(self.projects)])
        self.assertEqual(rc, 2)
        discover.assert_not_called()
        for needle in ("config invalid", "funnel.bogus is not a known key",
                       "funnel.exclude_temp_cwd must be true or false"):
            self.assertIn(needle, err.getvalue())


class InputVisibility(FunnelCase):
    """L1/L3/L4: inputs that could not be read are counted and shown, never silent."""

    def assert_problem(self, out: dict, needle: str) -> None:
        self.assertTrue(any(needle in p for p in out["inputs"]["problems"]), out["inputs"]["problems"])
        report = gf.render(out)
        self.assertIn("Input problems", report)
        self.assertIn(needle, report)

    def test_clean_inputs_report_no_problems(self):
        Transcript(self.repo).greps(1).write(self.projects, "p", "s-ok")
        out = self.run_funnel()
        self.assertEqual(out["inputs"]["problems"], [])
        self.assertNotIn("Input problems", gf.render(out))

    def test_missing_unconfigured_and_non_file_log(self):
        out = self.run_funnel(log=self.root / "nope.log")
        self.assertEqual(out["inputs"]["query_log"]["status"], "missing")
        self.assert_problem(out, "query log missing")
        scan = gf.discover_sessions(self.projects, SINCE, UNTIL)
        out = gf.analyse(scan, gf.load_log(None, SINCE, UNTIL), self.resolver,
                         gf.parse_config(self.cfg, self.root), SINCE, UNTIL)
        self.assert_problem(out, "query log not configured")
        self.assertEqual(self.run_funnel(log=self.root)["inputs"]["query_log"]["status"], "not-a-file")

    def test_corrupt_log_lines_are_counted(self):
        t = Transcript(self.repo).query("q0")
        self.log_record("q0", t.t)
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n[1, 2]\n\n")
        t.write(self.projects, "p", "s-q")
        out = self.run_funnel()
        self.assertEqual(out["inputs"]["query_log"]["corrupt_lines"], 2)
        self.assertEqual(out["inputs"]["query_log"]["records_in_window"], 1)
        self.assert_problem(out, "query log has 2 corrupt line(s)")

    def test_missing_projects_folder(self):
        out = self.run_funnel()
        self.assertTrue(out["inputs"]["projects_dir_missing"])
        self.assert_problem(out, "transcript projects folder missing")

    def test_corrupt_undated_and_unreadable_transcripts(self):
        path = Transcript(self.repo).greps(1).write(self.projects, "p", "s-ok")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("{garbage\n")
        (self.projects / "p" / "s-undated.jsonl").write_text(json.dumps({"type": "user"}) + "\n",
                                                            encoding="utf-8")
        Transcript(self.repo).greps(1).write(self.projects, "p", "s-locked")
        real_open = Path.open

        def locked(p, *a, **k):
            if p.name == "s-locked.jsonl":
                raise PermissionError(13, "denied")
            return real_open(p, *a, **k)
        with mock.patch.object(Path, "open", autospec=True, side_effect=locked):
            out = self.run_funnel()
        inp = out["inputs"]
        self.assertEqual(inp["transcript_corrupt_lines"], 1)
        self.assertEqual(len(inp["transcripts_undated"]), 2)      # no timestamp, and unreadable
        self.assertEqual(len(inp["transcripts_unreadable"]), 1)
        self.assertIn("PermissionError", inp["transcripts_unreadable"][0])
        self.assertEqual(out["sessions"]["total"], 1)
        for needle in ("1 corrupt transcript line(s)", "1 transcript file(s) could not be read",
                       "2 transcript(s) had no timestamped record"):
            self.assert_problem(out, needle)

    def test_unstatable_transcript(self):
        Transcript(self.repo).greps(1).write(self.projects, "p", "s-gone")
        real_stat = Path.stat

        def gone(p, *a, **k):
            if p.name == "s-gone.jsonl":
                raise FileNotFoundError(2, "vanished")
            return real_stat(p, *a, **k)
        with mock.patch.object(Path, "stat", autospec=True, side_effect=gone):
            out = self.run_funnel()
        self.assertEqual(len(out["inputs"]["transcripts_unstatable"]), 1)
        self.assert_problem(out, "1 transcript(s) could not be stat'ed")

    def test_subagent_metadata_problems(self):
        t = Transcript(self.repo)
        for agent in ("missing", "badjson", "notype", "fine"):
            t.subagent(agent, "Explore", Transcript(self.repo).greps(1))
        t.write(self.projects, "p", "s-meta")
        sub = self.projects / "p" / "s-meta" / "subagents"
        (sub / "agent-missing.meta.json").unlink()
        (sub / "agent-badjson.meta.json").write_text("{nope", encoding="utf-8")
        (sub / "agent-notype.meta.json").write_text("{}", encoding="utf-8")
        out = self.run_funnel()
        meta = "\n".join(out["inputs"]["subagent_metadata_problems"])
        for needle in ("agent-missing.jsonl: metadata missing",
                       "agent-badjson.jsonl: metadata is not valid JSON",
                       "agent-notype.jsonl: metadata has no agentType"):
            self.assertIn(needle, meta)
        self.assertEqual(len(out["inputs"]["subagent_metadata_problems"]), 3)
        self.assert_problem(out, "3 subagent(s) without readable metadata")


if __name__ == "__main__":
    unittest.main()
