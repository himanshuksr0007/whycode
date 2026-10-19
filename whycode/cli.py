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
    from .evidence_ids import (issue_id, pr_id, repo_key,
                               review_comment_id, decision_id_for_commit)
    from .graph.builder import GraphBuilder
    from .ingest import git_history
    from .ingest.github_api import GitHubClient
    from .ingest.intent import (classify_commit, classify_pr,
                                derive_observations, supersession_edges)
    from .ingest.relevance import is_high_signal, score_pr
    from .ingest.state import IngestState
    from .store.rawctx import RawContext

    rk = repo_key(settings.github_repo)
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
        d = classify_commit(c, rk)
        if d is not None:  # the gate: no evidence -> no Decision
            decisions.append(d)
            for f in d.about_files:
                file_stats.setdefault(f, {"commits": 0, "decisions": 0})
                file_stats[f]["decisions"] += 1
    print(f"decisions: {len(decisions)} / {len(commits)} commits")

    prs, issues = [], []
    state = IngestState(settings.state_path)
    errors: list[str] = []
    if not args.skip_github:
        gh = GitHubClient(settings.github_token)
        if "/" not in settings.github_repo:
            print("set GITHUB_REPO=owner/name to fetch PRs/issues")
        else:
            print("fetching PRs ...")
            skip_prs = (set() if args.force
                        else state.completed_ids(settings.github_repo, "pr"))
            if skip_prs:
                print(f"  skipping {len(skip_prs)} already-ingested PRs")
            pr_scores: dict[int, float] = {}
            for pr in gh.iter_prs(settings.repo_owner, settings.repo_name,
                                  args.max_prs, skip_numbers=skip_prs):
                try:
                    state.save_raw(
                        pr_id(rk, pr.number),
                        f"GET /repos/{settings.github_repo}/pulls/{pr.number}",
                        pr.raw,
                    )
                    prs.append(pr)
                    pr_scores[pr.number] = score_pr(pr)
                    state.mark_complete(settings.github_repo, "pr", pr.number)
                except Exception as e:  # one bad PR must not kill the run
                    errors.append(f"pr #{pr.number}: {e}")
                    state.mark_failed(settings.github_repo, "pr",
                                      pr.number, str(e))
            n_hi = sum(1 for s in pr_scores.values() if is_high_signal(s))
            print(f"  {len(prs)} new PRs, {n_hi} high-signal")
            print("fetching issues ...")
            skip_issues = (set() if args.force
                           else state.completed_ids(settings.github_repo,
                                                     "issue"))
            for issue in gh.iter_issues(settings.repo_owner,
                                        settings.repo_name, args.max_issues,
                                        skip_numbers=skip_issues):
                try:
                    state.save_raw(
                        issue_id(rk, issue.number),
                        f"GET /repos/{settings.github_repo}"
                        f"/issues/{issue.number}",
                        issue.raw,
                    )
                    issues.append(issue)
                    state.mark_complete(settings.github_repo, "issue",
                                        issue.number)
                except Exception as e:
                    errors.append(f"issue #{issue.number}: {e}")
                    state.mark_failed(settings.github_repo, "issue",
                                      issue.number, str(e))
            print(f"  {len(issues)} new issues")
            state.commit()
            for pr in prs:
                d = classify_pr(pr, rk)
                if d is not None:
                    decisions.append(d)
            print(f"decisions after PRs: {len(decisions)}")

    observations = derive_observations(file_stats, rk)
    print(f"observations: {len(observations)}")
    # Sequence numbers (0 = oldest) so supersession direction survives
    # 1-second git timestamp ties.
    order = {decision_id_for_commit(rk, c.sha): i
             for i, c in enumerate(reversed(commits))}
    edges = supersession_edges(decisions, order=order, rk=rk)
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
        for cm in p.review_comments:
            rawctx.add(f"review_comment:{cm.id}", "review_comment",
                       str(cm.id), f"{cm.path}:{cm.line}",
                       f"{cm.path} line {cm.line} ({cm.side}): {cm.body}")
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
        gb.write_github(prs, issues, rk)
        gb.write_review_comments(prs, rk)
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

    # JSONL export of the normalized evidence (the component boundary) and
    # a small ingestion report, so reruns and debugging don't need Neo4j.
    import json as _json

    os.makedirs(os.path.dirname(settings.jsonl_path), exist_ok=True)
    n_jsonl = 0
    with open(settings.jsonl_path, "w") as fh:
        for c in commits:
            fh.write(_json.dumps(
                {"record_id": f"{rk}:commit:{c.sha}",
                 "record_type": "commit", "repo": settings.github_repo,
                 "payload": {"subject": c.subject, "author": c.author,
                             "date": c.date,
                             "files": [f.path for f in c.files]}}) + "\n")
            n_jsonl += 1
        for p in prs:
            fh.write(_json.dumps(
                {"record_id": pr_id(rk, p.number),
                 "record_type": "pull_request",
                 "repo": settings.github_repo, "pr_number": p.number,
                 "payload": {"title": p.title, "user": p.user,
                             "merged_at": p.merged_at}}) + "\n")
            n_jsonl += 1
        for i in issues:
            fh.write(_json.dumps(
                {"record_id": issue_id(rk, i.number),
                 "record_type": "issue", "repo": settings.github_repo,
                 "payload": {"title": i.title, "user": i.user}}) + "\n")
            n_jsonl += 1
        for cm in [rc for p in prs for rc in p.review_comments]:
            fh.write(_json.dumps(
                {"record_id": review_comment_id(rk, cm.id),
                 "record_type": "review_comment",
                 "repo": settings.github_repo,
                 "payload": {"path": cm.path, "line": cm.line,
                             "side": cm.side, "user": cm.user}}) + "\n")
            n_jsonl += 1
    with open(settings.report_path, "w") as fh:
        _json.dump({
            "repo": settings.github_repo,
            "commits": len(commits),
            "decisions": len(decisions),
            "observations": len(observations),
            "supersession_edges": len(edges),
            "prs_ingested": len(prs),
            "issues_ingested": len(issues),
            "review_comments": sum(len(p.review_comments) for p in prs),
            "errors": errors,
            "jsonl_records": n_jsonl,
            "jsonl_path": settings.jsonl_path,
        }, fh, indent=2)
    print(f"report: {settings.report_path} ({n_jsonl} records)")
    state.close()
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
    if args.as_json:
        import json as _json
        print(_json.dumps(ans.to_dict(), indent=2))
    else:
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
    print('  whycode ask "why are retries disabled by default in httpx?"')
    print('  whycode ask "why does httpx handle redirects this way?"')
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
    p.add_argument("--force", action="store_true",
                   help="re-fetch PRs/issues already marked complete")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("ask", help="ask a why-question")
    p.add_argument("question")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="machine-readable answer")
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
