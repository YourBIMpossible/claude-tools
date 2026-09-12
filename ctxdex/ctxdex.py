#!/usr/bin/env python3
"""Local FTS5 knowledge index — durable recall over content you've already paid to see once.

Usage:
    ctxdex index <path-or-url> [--source LABEL] [--project NAME]
    ctxdex search <query> [--source LABEL] [--project NAME] [--limit N]
    ctxdex stats [--project NAME]
    ctxdex sources [--project NAME]
    ctxdex purge [--project NAME] [--source LABEL] [--older-than DAYS] [--all]
"""

import argparse
import html.parser
import os
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DATA_DIR = Path(__file__).parent / "data"
CHUNK_MAX_BYTES = 4096
TTL_DAYS_DEFAULT = 14
TEXT_EXTS = {
    ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".cs", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".hpp", ".sql", ".ps1",
    ".sh", ".toml", ".ini", ".cfg", ".log", ".csv", ".html", ".xml",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "bin", "obj", "dist", "build"}

# --- secret gate (manual ingestion) ------------------------------------------
# Content is stored as plaintext SQLite, so secret-bearing input is rejected at
# the door: clear reason, non-zero exit, nothing written. Deliberately no
# --allow-sensitive escape hatch until a real need appears.
#
# ALIGNMENT NOTE: the content patterns are a deliberate duplicate of
# _OUTPUT_PATTERNS in claude-profile/hooks/ctxdex_autoindex.py (auto-capture's
# gate). The two policies must stay aligned — change one, change both, and keep
# their test vectors matching. Duplicated on purpose: this standalone repo must
# not depend on the hook at runtime.
SENSITIVE_CONTENT_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}=*"),
    re.compile(r"\w+://[^\s/:@]+:[^\s/@]+@"),  # scheme://user:pass@host
    re.compile(r"(?is)\bserver\s*=.*?;\s*password\s*="),  # ODBC-style connection string
]
# Known secret-file names are denied outright regardless of content — a direct
# file target bypasses the TEXT_EXTS extension filter, so .env / keys / creds
# need their own gate.
SENSITIVE_NAME_RE = re.compile(
    r"^\.env(\..+)?$|^id_(rsa|ed25519|ecdsa|dsa)(\..+)?$|\.(pem|key|pfx|p12|ppk)$|^credentials?(\..+)?$|^\.netrc$|^\.npmrc$|^\.pypirc$",
    re.I,
)


def sensitive_reason(name: str, text: str) -> str | None:
    """Why this content must not be persisted, or None if it may be."""
    if name and SENSITIVE_NAME_RE.search(name):
        return f"sensitive filename pattern: {name}"
    for pat in SENSITIVE_CONTENT_PATTERNS:
        if pat.search(text):
            return f"content matches sensitive pattern: {pat.pattern[:60]}"
    return None


def db_path(project: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", project)
    return DATA_DIR / f"{safe}.db"


def connect(project: str) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(project))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS docs (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            indexed_at INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            title, content, content='docs', content_rowid='id', tokenize='porter unicode61'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_trigram USING fts5(
            content, content='docs', content_rowid='id', tokenize='trigram'
        );
        CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
            INSERT INTO docs_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
            INSERT INTO docs_trigram(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
            INSERT INTO docs_fts(docs_fts, rowid, title, content) VALUES('delete', old.id, old.title, old.content);
            INSERT INTO docs_trigram(docs_trigram, rowid, content) VALUES('delete', old.id, old.content);
        END;
        """
    )
    conn.commit()
    return conn


def chunk_text(text: str, title_hint: str) -> list[tuple[str, str]]:
    """Split text into (title, content) chunks, preferring markdown headings."""
    text = text.strip()
    if not text:
        return []

    heading_re = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
    headings = list(heading_re.finditer(text))

    chunks: list[tuple[str, str]] = []
    if headings:
        for i, m in enumerate(headings):
            start = m.start()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            body = text[start:end].strip()
            title = m.group(2).strip()[:80]
            chunks.extend(_split_oversized(title, body))
        preamble = text[: headings[0].start()].strip()
        if preamble:
            chunks.insert(0, (f"{title_hint} (preamble)", preamble[:CHUNK_MAX_BYTES]))
    else:
        paragraphs = re.split(r"\n\s*\n", text)
        buf = ""
        idx = 1
        for para in paragraphs:
            if len(buf) + len(para) + 2 > CHUNK_MAX_BYTES and buf:
                chunks.append((f"{title_hint} (part {idx})", buf.strip()))
                idx += 1
                buf = ""
            buf += para + "\n\n"
        if buf.strip():
            chunks.append((f"{title_hint} (part {idx})", buf.strip()))

    return chunks


def _split_oversized(title: str, body: str) -> list[tuple[str, str]]:
    if len(body) <= CHUNK_MAX_BYTES:
        return [(title, body)]
    parts = []
    for i in range(0, len(body), CHUNK_MAX_BYTES):
        parts.append((f"{title} (cont. {i // CHUNK_MAX_BYTES + 1})", body[i : i + CHUNK_MAX_BYTES]))
    return parts


class _HTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "header", "footer"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "header", "footer") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def fetch_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "ctxdex/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read(50 * 1024 * 1024).decode("utf-8", errors="replace")
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    return "\n\n".join(parser.parts) if parser.parts else raw


def iter_source_files(path: Path):
    if path.is_file():
        yield path
        return
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            fp = Path(root) / f
            if fp.suffix.lower() in TEXT_EXTS:
                yield fp


def cmd_index(args) -> int:
    """Index the target. Returns 0, or 2 when anything was rejected by the
    secret gate (rejected files are skipped and never written; in a directory
    run the clean files still index). No sys.exit here — the auto-capture hook
    calls this in-process and must be able to fail open."""
    conn = connect(args.project)
    total_chunks = 0
    total_files = 0
    rejected = 0

    if args.target.startswith(("http://", "https://")):
        text = fetch_url(args.target)
        reason = sensitive_reason("", text)
        if reason:
            print(f"refused: {reason} ({args.target}) - nothing indexed", file=sys.stderr)
            conn.close()
            return 2
        source = args.source or "web"
        chunks = chunk_text(text, args.target)
        for title, body in chunks:
            conn.execute(
                "INSERT INTO docs(source, path, title, content, indexed_at) VALUES (?,?,?,?,?)",
                (source, args.target, title, body, int(time.time())),
            )
        total_chunks += len(chunks)
        total_files = 1
    else:
        target_path = Path(args.target).resolve()
        if not target_path.exists():
            print(f"error: path not found: {target_path}", file=sys.stderr)
            sys.exit(1)
        source = args.source or "local"
        for fp in iter_source_files(target_path):
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            reason = sensitive_reason(fp.name, text)
            if reason:
                print(f"refused: {reason} ({fp}) - not indexed", file=sys.stderr)
                rejected += 1
                continue
            chunks = chunk_text(text, fp.name)
            stored_path = args.path or str(fp)
            for title, body in chunks:
                conn.execute(
                    "INSERT INTO docs(source, path, title, content, indexed_at) VALUES (?,?,?,?,?)",
                    (source, stored_path, title, body, int(time.time())),
                )
            total_chunks += len(chunks)
            total_files += 1

    conn.commit()
    conn.close()
    print(f"indexed {total_files} file(s), {total_chunks} chunk(s) -> project '{args.project}', source '{args.source or ('web' if args.target.startswith('http') else 'local')}'")
    if rejected:
        print(f"refused {rejected} file(s) by the secret gate - see reasons above", file=sys.stderr)
        return 2
    return 0


def _rrf_merge(porter_hits: list[tuple[int, float]], trigram_hits: list[tuple[int, float]], k: int = 60) -> list[int]:
    scores: dict[int, float] = {}
    for rank, (doc_id, _) in enumerate(porter_hits):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (doc_id, _) in enumerate(trigram_hits):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: -x[1])]


def _fts_query_escape(query: str) -> str:
    """OR-join terms: an all-terms-required AND makes multi-word queries miss constantly."""
    terms = re.findall(r"\w+", query)
    return " OR ".join(f'"{t}"' for t in terms) if terms else f'"{query}"'


def cmd_search(args):
    conn = connect(args.project)
    q = _fts_query_escape(args.query)

    where_source = "AND d.source = ?" if args.source else ""
    params_extra = (args.source,) if args.source else ()

    try:
        porter_rows = conn.execute(
            f"""
            SELECT d.id, bm25(docs_fts) FROM docs_fts
            JOIN docs d ON d.id = docs_fts.rowid
            WHERE docs_fts MATCH ? {where_source}
            ORDER BY bm25(docs_fts) LIMIT 20
            """,
            (q, *params_extra),
        ).fetchall()
    except sqlite3.OperationalError:
        porter_rows = []

    # Trigram catches partial/misspelled tokens the porter index misses; needs >=3 chars.
    trigram_rows = []
    for term in {t for t in re.findall(r"\w+", args.query) if len(t) >= 3}:
        try:
            trigram_rows += conn.execute(
                f"""
                SELECT d.id, 0 FROM docs_trigram
                JOIN docs d ON d.id = docs_trigram.rowid
                WHERE docs_trigram MATCH ? {where_source}
                LIMIT 10
                """,
                (f'"{term}"', *params_extra),
            ).fetchall()
        except sqlite3.OperationalError:
            continue

    candidates = _rrf_merge(porter_rows, trigram_rows)[:20]

    if not candidates:
        print("no results")
        conn.close()
        return

    # Chunks containing every query term outrank partial matches — OR semantics alone
    # ranks a chunk matching one common term above one matching all of them.
    terms = [t.lower() for t in re.findall(r"\w+", args.query)]
    rows = {}
    for rank, doc_id in enumerate(candidates):
        row = conn.execute("SELECT source, path, title, content FROM docs WHERE id=?", (doc_id,)).fetchone()
        if row:
            haystack = (row[2] + " " + row[3]).lower()
            hits = sum(1 for t in terms if t in haystack)
            rows[doc_id] = (row, hits, rank)

    ranked_ids = sorted(rows, key=lambda i: (-rows[i][1], rows[i][2]))[: args.limit]

    for doc_id in ranked_ids:
        source, path, title, content = rows[doc_id][0]
        snippet = content[:1500].strip()
        print(f"--- [{source}] {title} ({path}) ---")
        print(snippet)
        print()

    conn.close()


def cmd_stats(args):
    conn = connect(args.project)
    total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    by_source = conn.execute("SELECT source, COUNT(*) FROM docs GROUP BY source ORDER BY 2 DESC").fetchall()
    size = db_path(args.project).stat().st_size if db_path(args.project).exists() else 0
    print(f"project: {args.project}")
    print(f"db: {db_path(args.project)} ({size / 1024:.1f} KB)")
    print(f"total chunks: {total}")
    for source, count in by_source:
        print(f"  {source}: {count}")
    conn.close()


def cmd_sources(args):
    conn = connect(args.project)
    rows = conn.execute("SELECT DISTINCT source FROM docs ORDER BY source").fetchall()
    for (source,) in rows:
        print(source)
    conn.close()


def cmd_purge(args):
    conn = connect(args.project)
    if args.all:
        conn.execute("DELETE FROM docs")
        print(f"purged all chunks for project '{args.project}'")
    else:
        # --source alone purges that whole source, no age filter (back-compat).
        # --older-than alone (or neither flag) purges by age only, default TTL.
        # Both together compose: source AND older-than.
        clauses = []
        params = []
        if args.source:
            clauses.append("source = ?")
            params.append(args.source)
        include_age = args.older_than is not None or not args.source
        older_than = TTL_DAYS_DEFAULT if args.older_than is None else args.older_than
        if include_age:
            clauses.append("indexed_at < ?")
            params.append(int(time.time()) - older_than * 86400)
        cur = conn.execute(f"DELETE FROM docs WHERE {' AND '.join(clauses)}", params)
        desc = []
        if args.source:
            desc.append(f"source '{args.source}'")
        if include_age:
            desc.append(f"older than {older_than} day(s)")
        print(f"purged {cur.rowcount} chunk(s) matching: {', '.join(desc)}")
    conn.commit()
    conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO docs_trigram(docs_trigram) VALUES('rebuild')")
    conn.commit()
    conn.close()


def default_project() -> str:
    return Path.cwd().name or "default"


def main():
    parser = argparse.ArgumentParser(prog="ctxdex", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="index a file, directory, or URL")
    p_index.add_argument("target")
    p_index.add_argument("--source", default=None, help="label for this content (default: local/web)")
    p_index.add_argument("--project", default=None, help="project namespace (default: cwd folder name)")
    p_index.add_argument("--path", default=None, help="override the stored path (for synthetic sources like bash://...)")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="search the index")
    p_search.add_argument("query")
    p_search.add_argument("--source", default=None)
    p_search.add_argument("--project", default=None)
    p_search.add_argument("--limit", type=int, default=2)
    p_search.set_defaults(func=cmd_search)

    p_stats = sub.add_parser("stats", help="show index stats")
    p_stats.add_argument("--project", default=None)
    p_stats.set_defaults(func=cmd_stats)

    p_sources = sub.add_parser("sources", help="list distinct sources")
    p_sources.add_argument("--project", default=None)
    p_sources.set_defaults(func=cmd_sources)

    p_purge = sub.add_parser("purge", help="purge chunks")
    p_purge.add_argument("--project", default=None)
    p_purge.add_argument("--source", default=None)
    p_purge.add_argument(
        "--older-than", type=int, default=None,
        help=f"purge older than N days (default {TTL_DAYS_DEFAULT}; only applied alongside --source if explicitly given)",
    )
    p_purge.add_argument("--all", action="store_true")
    p_purge.set_defaults(func=cmd_purge)

    args = parser.parse_args()
    if getattr(args, "project", None) is None:
        args.project = default_project()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
