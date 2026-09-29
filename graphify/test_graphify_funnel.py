"""Tests for graphify_funnel.py. All fixtures are SYNTHETIC: fake transcripts, a fake repo
with an empty graph file, and a stub resolver written into a temp dir.

Run: python test_graphify_funnel.py
"""
from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
        self.cfg: dict = {"targets": [{"repo": str(self.repo)}],
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

    def run_funnel(self) -> dict:
        sessions = gf.discover_sessions(self.projects, SINCE, UNTIL)
        records = gf.load_log(self.log, SINCE, UNTIL)
        return gf.analyse(sessions, records, self.resolver, self.cfg, SINCE, UNTIL)


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
        Transcript(self.repo).query("q1", output="cd: no such directory", is_error=True) \
            .query("q1").write(self.projects, "p", "s-retry")
        self.log_record("q1", T0 + timedelta(seconds=4))
        ex = self.run_funnel()["execution"]
        self.assertEqual(ex["verdict"], gf.PASS)
        self.assertEqual(ex["by_status"], {"shell-error": 1, "ok": 1})

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

    def test_hint_not_required_before_go_live(self):
        self.cfg["funnel"]["hint_live_since"] = iso(T0 + timedelta(days=1))
        Transcript(self.repo).greps(1).write(self.projects, "p", "s-early")
        self.assertEqual(self.run_funnel()["availability"]["verdict"], gf.PASS)

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
        self.assertEqual(gf.main(["--config", str(cfg_path), "--projects", str(self.projects)]), 2)


if __name__ == "__main__":
    unittest.main()
