"""Command line interface: ingest | ask | stats | wipe | demo"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from . import __version__
from .config import Settings


def _driver(settings: Settings):
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )


def collect_docs(repo_path: str, limit: int = 60) -> list[dict]:
    """Find design-ish docs in the repo (bounded)."""
    out = subprocess.run(
        ["git", "-C", repo_path, "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True,
    )
    docs = []
    for line in out.stdout.split("\n"):
        low = line.lower()
        if (low.startswith(("docs/", "doc/", "adr/", "design/", "rfc/"))
                or low.endswith((".md", ".rst"))
                and any(h in low for h in
                        ("adr", "design", "decision", "rationale",
                         "architecture", "proposal"))):
            docs.append(line)
        if len(docs) >= limit:
            break
    result = []
    for path in docs:
        full = os.path.join(repo_path, path)
        try:
            with open(full, errors="replace") as f:
                text = f.read(20000)
        except OSError:
            continue
        result.append({"path": path,
                       "title": path.rsplit("/", 1)[-1], "text": text})
    return result


def cmd_ingest(args, settings: Settings) -> int:
    from .graph.builder import GraphBuilder
    from .ingest import git_history
    from .ingest.github_api import GitHubClient
    from .ingest.intent import (classify_commit, classify_pr,
                                derive_observations, supersession_edges)
    from .store.rawctx import RawContext

    repo_url = args.repo or settings.repo_url
    if args.repo_path:
        repo_path = args.repo_path
    else:
        repo_path = git_history.ensure_clone(repo_url, settings.clone_dir)

    commits = git_history.stream_commits(repo_path, args.max_commits)

    decisions, file_stats = [], {}
    for c in commits:
        for f in c.files:
            st = file_stats.setdefault(f.path, {"commits": 0, "decisions": 0})
            st["commits"] += 1
        d = classify_commit(c)
        if d is not None:  # the gate: no evidence -> no Decision
            decisions.append(d)
            for f in d.about_files:
                file_stats.setdefault(f, {"commits": 0, "decisions": 0})
                file_stats[f]["decisions"] += 1
    print(f"decisions: {len(decisions)} / {len(commits)} commits")

    prs, issues = [], []
    if not args.skip_github:
        gh = GitHubClient(settings.github_token)
        if "/" not in settings.github_repo:
            print("set GITHUB_REPO=owner/name to fetch PRs/issues")
        else:
            print("fetching PRs ...")
            prs = list(gh.iter_prs(settings.repo_owner, settings.repo_name,
                                   args.max_prs))
            print(f"  {len(prs)} PRs")
            print("fetching issues ...")
            issues = list(gh.iter_issues(settings.repo_owner,
                                         settings.repo_name, args.max_issues))
            print(f"  {len(issues)} issues")
            for pr in prs:
                d = classify_pr(pr)
                if d is not None:
                    decisions.append(d)
            print(f"decisions after PRs: {len(decisions)}")

    observations = derive_observations(file_stats)
    print(f"observations: {len(observations)}")
    # Sequence numbers (0 = oldest) so supersession direction survives
    # 1-second git timestamp ties.
    order = {f"d:{c.sha}": i for i, c in enumerate(reversed(commits))}
    edges = supersession_edges(decisions, order=order)
    print(f"supersession edges: {len(edges)}")

    docs = collect_docs(repo_path)
    print(f"docs: {len(docs)}")

    # Raw-context layer: index full texts (not bulk-stored in Neo4j).
    rawctx = RawContext(settings.rawctx_path)
    for c in commits:
        rawctx.add(f"commit:{c.sha}", "commit", c.sha, c.subject,
                   f"{c.subject}\n{c.body}\nfiles: "
                   + ", ".join(f.path for f in c.files[:40]))
    for p in prs:
        rawctx.add(f"pr:{p.number}", "pr", str(p.number), p.title,
                   p.body + "\n" + "\n".join(cm["body"]
                                             for cm in p.comments[:10]))
    for i in issues:
        rawctx.add(f"issue:{i.number}", "issue", str(i.number), i.title,
                   i.body + "\n" + "\n".join(cm["body"]
                                             for cm in i.comments[:10]))
    for d in docs:
        rawctx.add(f"doc:{d['path']}", "doc", d["path"], d["title"], d["text"])
    rawctx.commit()
    rawctx.close()
    print("raw-context index written")

    driver = _driver(settings)
    try:
        gb = GraphBuilder(driver, settings.neo4j_database)
        gb.init_schema()
        gb.write_commits(commits)
        gb.write_github(prs, issues, settings.github_repo)
        gb.write_docs(docs)
        gb.write_intent(decisions, observations)
        gb.link_fixes(decisions)
        gb.apply_supersession(edges)
        # Doc MENTIONS links: doc text contains a known file path.
        known = list(file_stats.keys())
        mentions = []
        for d in docs:
            for fp in known:
                if len(fp) > 6 and fp in d["text"]:
                    mentions.append({"doc": d["path"], "file": fp})
        if mentions:
            with driver.session(database=settings.neo4j_database) as s:
                s.run(
                    "UNWIND $rows AS r "
                    "MATCH (d:DocFile:Structural {path: r.doc}), "
                    "(f:File:Structural {path: r.file}) "
                    "MERGE (d)-[:MENTIONS]->(f)",
                    rows=mentions[:5000],
                )
            print(f"doc mentions: {len(mentions)}")
        print("graph stats:", gb.stats())
    finally:
        driver.close()
    print("ingest complete")
    return 0


def cmd_ask(args, settings: Settings) -> int:
    from .retrieval.qa import (answer_question, maybe_verbalize,
                              render_markdown)
    from .store.rawctx import RawContext

    driver = _driver(settings)
    try:
        rawctx = RawContext(settings.rawctx_path)
        ans = answer_question(args.question, driver, rawctx, settings)
        rawctx.close()
    finally:
        driver.close()
    if ans.status == "answered":
        points = [f"[{e['tag']}] {e['text']}" for e in ans.evidence_chain]
        text, verb = maybe_verbalize(points, settings)
        ans.verbalizer = verb
        if verb.startswith("llm"):
            # LLM rewrite replaces the summary; evidence chain stays verbatim.
            ans.summary = text
    print(render_markdown(ans))
    return 0


def cmd_stats(args, settings: Settings) -> int:
    from .graph.builder import GraphBuilder
    driver = _driver(settings)
    try:
        print(GraphBuilder(driver, settings.neo4j_database).stats())
    finally:
        driver.close()
    return 0


def cmd_wipe(args, settings: Settings) -> int:
    from .graph.builder import GraphBuilder
    if not args.yes:
        print("refusing without --yes")
        return 1
    driver = _driver(settings)
    try:
        GraphBuilder(driver, settings.neo4j_database).wipe()
    finally:
        driver.close()
    try:
        os.remove(settings.rawctx_path)
    except OSError:
        pass
    print("wiped graph and raw-context index")
    return 0


def cmd_demo(args, settings: Settings) -> int:
    """Small bounded ingest + sample questions, for a quick end-to-end demo."""
    args.max_commits = 150
    args.max_prs = 30
    args.max_issues = 30
    args.skip_github = False
    args.repo = args.repo or settings.repo_url
    args.repo_path = None
    rc = cmd_ingest(args, settings)
    if rc != 0:
        return rc
    print("\nTry e.g.:")
    print('  whycode ask "why does flask use blinker for signals?"')
    print('  whycode ask "why was the dev server reloader changed?"')
    return 0


def main(argv=None) -> int:
    settings = Settings.from_env()
    ap = argparse.ArgumentParser(
        prog="whycode", description="Why The Code Is Like This")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="ingest a repo into Neo4j")
    p.add_argument("--repo", default=None, help="git URL (default: GITHUB_REPO)")
    p.add_argument("--repo-path", default=None, help="use a local clone")
    p.add_argument("--max-commits", type=int, default=settings.max_commits)
    p.add_argument("--max-prs", type=int, default=settings.max_prs)
    p.add_argument("--max-issues", type=int, default=settings.max_issues)
    p.add_argument("--skip-github", action="store_true")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("ask", help="ask a why-question")
    p.add_argument("question")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("stats", help="graph stats")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("wipe", help="delete everything ingested")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_wipe)

    p = sub.add_parser("demo", help="tiny ingest + sample questions")
    p.add_argument("--repo", default=None)
    p.set_defaults(fn=cmd_demo)

    args = ap.parse_args(argv)
    return args.fn(args, settings)


if __name__ == "__main__":
    sys.exit(main())
