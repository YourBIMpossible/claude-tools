"""Secret-gate tests for manual ctxdex ingestion — CLI-level, same driver style
as the auto-index hook's suite: invoke ctxdex.py as a subprocess, assert exit
codes and what did/didn't reach an isolated test DB. Run: python test_ctxdex_gate.py
"""
import sqlite3
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

CTXDEX = str(Path(__file__).with_name("ctxdex.py"))
PROJECT = "ctxdex-gate-selftest"
DB = Path(__file__).parent / "data" / f"{PROJECT.replace('-', '-')}.db"

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" -- {detail}" if detail and not cond else ""))


def run(args_list):
    return subprocess.run(
        [sys.executable, CTXDEX, *args_list, "--project", PROJECT],
        capture_output=True, text=True, timeout=30,
    )


def db_count(like=None):
    if not DB.exists():
        return 0
    conn = sqlite3.connect(DB)
    if like:
        n = conn.execute("SELECT COUNT(*) FROM docs WHERE content LIKE ?", (f"%{like}%",)).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    conn.close()
    return n


def reset():
    run(["purge", "--all"])


SECRET_VECTORS = {
    "aws-access-key": "config dump\nAWS_KEY=AKIAIOSFODNN7EXAMPLE\nend",
    "github-pat-classic": "auth: ghp_" + "a1B2" * 9 + "\ndone",
    "github-pat-fine": "tok github_pat_" + "x" * 24 + " trailing",
    "bearer-token": "Authorization: Bearer " + "Ab1-._~" * 4 + "abcdefgh",
    "connection-string-url": "db at postgres://admin:hunter2secret@db.internal:5432/prod",
    "connection-string-odbc": "Server=sql01;Database=x;User Id=sa;Password=P@ss;",
    "pem-private-key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----",
}

BENIGN_VECTORS = {
    "prose-mentions": "The token count exceeded the password policy discussion in the API key rotation doc.",
    "short-akia-like": "identifier AKIA123 is not a real key shape",
    "plain-url": "see https://docs.example.com/path?q=1 for details",
}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ctxdex-gate-"))
    try:
        reset()

        # 1) each secret vector: single-file index rejected, non-zero exit, nothing persisted
        for name, content in SECRET_VECTORS.items():
            f = tmp / f"{name}.md"
            f.write_text(f"# doc\n\n{content}\n", encoding="utf-8")
            p = run(["index", str(f), "--source", "t"])
            check(f"1 {name}: non-zero exit", p.returncode != 0, f"rc={p.returncode}")
            check(f"1 {name}: reason printed", "sensitive" in (p.stdout + p.stderr).lower())
            check(f"1 {name}: nothing persisted", db_count() == 0, f"rows={db_count()}")
            reset()

        # 2) benign vectors index fine, exit 0
        for name, content in BENIGN_VECTORS.items():
            f = tmp / f"{name}.md"
            f.write_text(f"# doc\n\n{content}\n", encoding="utf-8")
            p = run(["index", str(f), "--source", "t"])
            check(f"2 {name}: exit 0", p.returncode == 0, f"rc={p.returncode} err={p.stderr[:120]}")
            check(f"2 {name}: persisted", db_count() > 0)
            reset()

        # 3) secret-bearing FILENAME denied even with benign content (direct file target
        #    bypasses the extension filter, so .env etc. must be name-denied)
        for fname in (".env", ".env.local", "id_rsa", "server.pem", "credentials.json"):
            f = tmp / fname
            f.write_text("PORT=8080\nDEBUG=true\n", encoding="utf-8")
            p = run(["index", str(f), "--source", "t"])
            check(f"3 {fname}: non-zero exit", p.returncode != 0, f"rc={p.returncode}")
            check(f"3 {fname}: nothing persisted", db_count() == 0)
            reset()

        # 4) directory: clean files indexed, secret file skipped with reason, non-zero exit
        d = tmp / "mixed"
        d.mkdir()
        (d / "clean.md").write_text("# ok\n\nnormal notes about deployment\n", encoding="utf-8")
        (d / "leaky.md").write_text("key AKIAIOSFODNN7EXAMPLE here\n", encoding="utf-8")
        p = run(["index", str(d), "--source", "t"])
        check("4 dir: non-zero exit (partial rejection)", p.returncode != 0, f"rc={p.returncode}")
        check("4 dir: clean file persisted", db_count("normal notes") > 0)
        check("4 dir: secret file NOT persisted", db_count("AKIAIOSFODNN7EXAMPLE") == 0)
        check("4 dir: skip reason printed", "sensitive" in (p.stdout + p.stderr).lower())
        reset()

        # 5) all-clean directory still exits 0
        d2 = tmp / "clean"
        d2.mkdir()
        (d2 / "a.md").write_text("# a\n\nalpha content\n", encoding="utf-8")
        (d2 / "b.md").write_text("# b\n\nbeta content\n", encoding="utf-8")
        p = run(["index", str(d2), "--source", "t"])
        check("5 clean dir: exit 0", p.returncode == 0, f"rc={p.returncode}")
        check("5 clean dir: both persisted", db_count() >= 2)
        reset()

    finally:
        reset()
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            DB.unlink(missing_ok=True)
        except OSError:
            pass

    passed = sum(1 for _, c in results if c)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
