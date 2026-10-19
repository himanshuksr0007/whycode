"""Answers "why" questions from the graph.

Each claim in an answer is tagged:
  [EVIDENCE]  — came straight from the graph or the text index
  [INFERENCE] — came from a heuristic (fuzzy entity match, guessed
                supersession). Treat accordingly.
Anything else doesn't get said. If confidence is under the threshold,
the answer is a refusal with some leads, not a guess.

The traversal (Cypher + node ids touched) is returned with the answer
so you can check the confidence score yourself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .linking import extract_candidates, link_entities

ENTITY_LABEL = {"File": "File:Structural", "Symbol": "Symbol:Structural"}
ENTITY_KEY = {"File": "path", "Symbol": "key"}

# Pure-function confidence weights (unit-testable).
WEIGHTS = {
    "has_decision": 0.30,
    "commit_linked": 0.10,
    "discussion": 0.20,      # PR or issue discussion attached
    "doc": 0.15,             # design doc mention
    "person": 0.10,          # identifiable author
    "corroborated": 0.10,    # 3+ independent evidence kinds
    "recent": 0.05,          # decision within 2 years
}


def score_confidence(flags: dict) -> tuple[float, dict]:
    """flags: subset of WEIGHTS keys -> bool. Returns (score, breakdown)."""
    breakdown = {k: (WEIGHTS[k] if flags.get(k) else 0.0) for k in WEIGHTS}
    return round(sum(breakdown.values()), 2), breakdown


def _band(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


@dataclass
class Answer:
    question: str
    status: str  # "answered" | "refused"
    summary: str = ""
    evidence_chain: list = field(default_factory=list)  # dicts w/ tag,text,ref,date
    supersession: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    person_to_ask: str | None = None
    confidence: float = 0.0
    confidence_breakdown: dict = field(default_factory=dict)
    traversal: dict = field(default_factory=dict)
    suggestions: list = field(default_factory=list)
    entity: dict | None = None
    verbalizer: str = "template"
    decision_status: dict = field(default_factory=dict)  # decision id -> active|superseded|contested

    def to_dict(self) -> dict:
        """Machine-readable form: categorical confidence is the primary
        signal, the numeric score rides along for audit."""
        return {
            "question": self.question,
            "status": self.status,
            "confidence": _band(self.confidence),
            "confidence_score": self.confidence,
            "confidence_breakdown": self.confidence_breakdown,
            "summary": self.summary,
            "evidence": [
                {"tag": e["tag"], "text": e["text"],
                 "ref": e["ref"], "date": e.get("date", "")}
                for e in self.evidence_chain
            ],
            "supersession": self.supersession,
            "decision_status": self.decision_status,
            "person_to_ask": self.person_to_ask,
            "observations": self.observations,
            "suggestions": self.suggestions,
            "traversal": self.traversal,
            "verbalizer": self.verbalizer,
        }


EVIDENCE_CYPHER = """
MATCH (e:{label} {{{key}: $ekey}})
OPTIONAL MATCH (e)<-[:ABOUT]-(d:Decision:Intent)
OPTIONAL MATCH (d)-[:RECORDED_IN]->(c:Commit:Structural)
OPTIONAL MATCH (c)<-[:AUTHORED]-(a:Person:Structural)
OPTIONAL MATCH (d)-[:BASED_ON]->(pr:PullRequest:Structural)
OPTIONAL MATCH (d)-[:BASED_ON]->(i:Issue:Structural)
OPTIONAL MATCH (doc:DocFile:Structural)-[:MENTIONS]->(e)
OPTIONAL MATCH (d)-[s:SUPERSEDES]->(old:Decision:Intent)
RETURN e, d, c, a, pr, i, doc, s, old
"""


def _fetch_entity_graph(driver, database, entity):
    label = ENTITY_LABEL[entity["kind"]]
    key = ENTITY_KEY[entity["kind"]]
    query = EVIDENCE_CYPHER.format(label=label, key=key)
    with driver.session(database=database) as session:
        return list(session.run(query, ekey=entity["key"])), query


def _assemble(rows):
    """Group rows by decision id."""
    decisions = {}
    entity_node = None
    for r in rows:
        e, d = r["e"], r["d"]
        if e is not None and entity_node is None:
            entity_node = dict(e)
        if d is None:
            continue
        did = d["id"]
        dec = decisions.setdefault(did, {
            "node": dict(d), "commit": None, "author": None,
            "prs": {}, "issues": {}, "docs": set(),
            "supersedes": [],
        })
        if r["c"] is not None and dec["commit"] is None:
            dec["commit"] = dict(r["c"])
        if r["a"] is not None and dec["author"] is None:
            dec["author"] = dict(r["a"])
        if r["pr"] is not None:
            dec["prs"][r["pr"]["id"]] = dict(r["pr"])
        if r["i"] is not None:
            dec["issues"][r["i"]["id"]] = dict(r["i"])
        if r["doc"] is not None:
            dec["docs"].add(r["doc"]["path"])
        if r["s"] is not None and r["old"] is not None:
            edge = (r["old"]["id"], r["s"]["method"],
                    r["s"].get("confidence", 0))
            if edge not in dec["supersedes"]:
                dec["supersedes"].append(edge)
    return entity_node, decisions


def _pr_number(pid: str) -> str:
    # "encode:httpx:pr:123" -> "123" (also tolerates the old "repo#123")
    return pid.split(":pr:")[-1] if ":pr:" in pid else pid.split("#")[-1]


def _issue_number(iid: str) -> str:
    return (iid.split(":issue:")[-1] if ":issue:" in iid
            else iid.split("#")[-1])


def _github_url(kind: str, repo: str, ref: str) -> str:
    base = f"https://github.com/{repo}"
    if kind == "commit":
        return f"{base}/commit/{ref}"
    if kind == "pr":
        # ref looks like "encode:httpx:pr:123"
        number = ref.split(":pr:")[-1] if ":pr:" in ref else ref.split("#")[-1]
        return f"{base}/pull/{number}"
    if kind == "issue":
        number = (ref.split(":issue:")[-1] if ":issue:" in ref
                  else ref.split("#")[-1])
        return f"{base}/issues/{number}"
    return base


def _is_recent(date_str: str) -> bool:
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days < 730
    except Exception:
        return False


def answer_question(question, driver, rawctx, settings) -> Answer:
    candidates = extract_candidates(question)
    entities = link_entities(driver, settings.neo4j_database, candidates)

    ans = Answer(question=question)
    ans.traversal["entity_candidates"] = candidates
    ans.traversal["linked_entities"] = entities

    if not entities:
        # Nothing in the graph: check the raw-context index for leads.
        leads = rawctx.search(question) if rawctx else []
        ans.status = "refused"
        ans.summary = (
            "I can't link your question to anything in the graph, so I won't "
            "guess. No Decision node exists for it."
        )
        ans.suggestions = [l["title"] or l["ref"] for l in leads[:5]]
        ans.traversal["leads_from_rawctx"] = [
            {"ref": l["ref"], "snippet": l["snippet"]} for l in leads[:3]
        ]
        return ans

    entity = entities[0]
    ans.entity = entity
    rows, query = _fetch_entity_graph(
        driver, settings.neo4j_database, entity)
    ans.traversal["cypher"] = re.sub(r"\s+", " ", query).strip()
    _, decisions = _assemble(rows)

    # Superseded decisions: anything targeted by a SUPERSEDES edge.
    superseded_ids = set()
    sup_edges = []
    for did, dec in decisions.items():
        for old_id, method, conf in dec["supersedes"]:
            superseded_ids.add(old_id)
            sup_edges.append({
                "older": old_id, "newer": did,
                "method": method, "confidence": conf,
                "tag": "EVIDENCE" if method == "explicit" else "INFERENCE",
            })
    live = {did: d for did, d in decisions.items()
            if did not in superseded_ids}
    if not live:
        live = decisions  # supersession data incomplete; say so downstream

    primary = None
    if live:
        primary = sorted(live.values(),
                         key=lambda d: d["node"].get("date", ""))[-1]

    # Observations about this entity (leads, never answers).
    with driver.session(database=settings.neo4j_database) as session:
        label = ENTITY_LABEL[entity["kind"]]
        key = ENTITY_KEY[entity["kind"]]
        obs_rows = session.run(
            f"MATCH (e:{label} {{{key}: $ekey}})<-[:ABOUT]-(o:Observation:Intent) "
            "RETURN o.summary AS s LIMIT 5",
            ekey=entity["key"],
        )
        ans.observations = [r["s"] for r in obs_rows]

    if primary is None:
        ans.status = "refused"
        ans.summary = (
            "The graph knows this code but holds no recorded decision about "
            "it. Per the evidence gate, I won't invent one."
        )
        if ans.observations:
            ans.summary += " Closest leads: " + "; ".join(ans.observations[:2])
        return ans

    d = primary["node"]
    repo = settings.github_repo
    chain = []
    tag_entity = ("INFERENCE" if entity["match_type"] == "fuzzy"
                  else "EVIDENCE")
    chain.append({
        "tag": "EVIDENCE",
        "text": f"Decision ({d.get('kind')}): {d.get('summary')}",
        "ref": d["id"], "date": d.get("date", ""),
    })
    if primary["commit"]:
        c = primary["commit"]
        chain.append({
            "tag": "EVIDENCE",
            "text": f"Committed as {c['sha'][:8]}: {c.get('subject','')}",
            "ref": _github_url("commit", repo, c["sha"]),
            "date": c.get("date", ""),
        })
    for pid, pr in primary["prs"].items():
        chain.append({
            "tag": "EVIDENCE",
            "text": f"Discussed in PR #{_pr_number(pid)}: {pr.get('title','')}",
            "ref": _github_url("pr", repo, pid),
            "date": pr.get("merged_at", ""),
        })
    for iid, iss in primary["issues"].items():
        chain.append({
            "tag": "EVIDENCE",
            "text": f"Linked issue #{_issue_number(iid)}: {iss.get('title','')}",
            "ref": _github_url("issue", repo, iid),
            "date": iss.get("closed_at", ""),
        })
    for doc_path in sorted(primary["docs"]):
        chain.append({
            "tag": "EVIDENCE",
            "text": f"Mentioned in design doc: {doc_path}",
            "ref": doc_path, "date": "",
        })
    if entity["match_type"] == "fuzzy":
        chain.append({
            "tag": "INFERENCE",
            "text": f"Entity linked by fuzzy match ({entity['label']}); "
                    "verify it is the code you meant.",
            "ref": entity["key"], "date": "",
        })

    # Person to ask: author of the primary commit, else most frequent author.
    person = (primary["author"] or {}).get("login")
    if not person:
        with driver.session(database=settings.neo4j_database) as session:
            row = session.run(
                f"""
                MATCH (e:{label} {{{key}: $ekey}})<-[:ABOUT]-(dd:Decision)
                      -[:RECORDED_IN]->(cc:Commit)<-[:AUTHORED]-(pp:Person)
                RETURN pp.login AS login, count(*) AS n
                ORDER BY n DESC LIMIT 1
                """,
                ekey=entity["key"],
            ).single()
            person = row["login"] if row else None
    ans.person_to_ask = person

    kinds = set()
    if primary["commit"]:
        kinds.add("commit")
    if primary["prs"] or primary["issues"]:
        kinds.add("discussion")
    if primary["docs"]:
        kinds.add("doc")
    if person:
        kinds.add("person")
    flags = {
        "has_decision": True,
        "commit_linked": "commit" in kinds,
        "discussion": "discussion" in kinds,
        "doc": "doc" in kinds,
        "person": "person" in kinds,
        "corroborated": len(kinds) >= 3,
        "recent": _is_recent(d.get("date", "")),
    }
    score, breakdown = score_confidence(flags)
    if entity["match_type"] == "fuzzy":
        score = min(score, 0.6)  # fuzzy links can never be high confidence
    ans.confidence = score
    ans.confidence_breakdown = breakdown
    ans.evidence_chain = chain
    ans.supersession = [
        {**e,
         "older_summary": decisions.get(e["older"], {}).get("node", {}).get("summary", ""),
         "newer_summary": decisions.get(e["newer"], {}).get("node", {}).get("summary", "")}
        for e in sup_edges
    ]
    ans.traversal["decision_ids"] = list(decisions.keys())
    # Decision status: superseded (explicitly replaced), contested (a later
    # decision probably replaced it, heuristic), or active.
    status_of = {}
    for e in sup_edges:
        if e["method"] == "explicit":
            status_of[e["older"]] = "superseded"
        else:
            status_of.setdefault(e["older"], "contested")
    ans.decision_status = {
        did: status_of.get(did, "active") for did in decisions
    }

    if score < settings.confidence_threshold:
        ans.status = "refused"
        ans.summary = (
            f"Found a possible decision but confidence is {score} "
            f"({_band(score)}), below the {settings.confidence_threshold} "
            "threshold. I won't present it as an answer."
        )
        return ans

    ans.status = "answered"
    sup_note = ""
    if sup_edges:
        sup_note = (f" Note: {len(sup_edges)} earlier decision(s) on this "
                    "code were superseded (see below).")
    primary_status = ans.decision_status.get(
        d["id"], "active") if primary else "active"
    status_note = ("" if primary_status == "active"
                   else f" Status: {primary_status}.")
    ans.summary = (f"{d.get('summary')} "
                   f"[decision: {d.get('kind')}, {d.get('date','')[:10]}]."
                   f"{status_note}{sup_note}")
    return ans


def render_markdown(ans: Answer) -> str:
    L = [f"## Why: {ans.question}", ""]
    if ans.status == "refused":
        L.append(f"**Refused** (confidence {ans.confidence}, "
                 f"threshold applies). {ans.summary}")
        if ans.observations:
            L += ["", "### Leads (observations, not answers)"]
            L += [f"- [EVIDENCE] {o}" for o in ans.observations]
        if ans.suggestions:
            L += ["", "### Maybe you meant"]
            L += [f"- {s}" for s in ans.suggestions[:5]]
        return "\n".join(L)

    L.append(f"**Answer** (confidence {ans.confidence} — {_band(ans.confidence)})")
    L.append(ans.summary)
    L += ["", "### Evidence chain"]
    for e in ans.evidence_chain:
        date = f" ({e['date'][:10]})" if e.get("date") else ""
        L.append(f"- [{e['tag']}] {e['text']}{date} — {e['ref']}")
    if ans.supersession:
        L += ["", "### Supersession"]
        for s in ans.supersession:
            L.append(
                f"- [{s['tag']}] superseded ({s['method']}, "
                f"conf {s['confidence']}): \"{s['older_summary']}\" "
                f"→ replaced by \"{s['newer_summary']}\""
            )
    if ans.person_to_ask:
        L += ["", f"### Person to ask\n- [EVIDENCE] {ans.person_to_ask}"]
    L += ["", "### Confidence breakdown"]
    for k, v in ans.confidence_breakdown.items():
        if v:
            L.append(f"- {k}: +{v}")
    L += ["", "### Audit trail",
          f"Entities linked: {ans.traversal.get('linked_entities')}",
          f"Cypher: `{ans.traversal.get('cypher','')}`",
          f"Decisions traversed: {ans.traversal.get('decision_ids')}",
          f"Verbalizer: {ans.verbalizer}"]
    return "\n".join(L)


def maybe_verbalize(points: list[str], settings) -> tuple[str, str]:
    """Optional LLM polish over template points. Returns (text, verbalizer)."""
    if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
        return "\n".join(f"- {p}" for p in points), "template"
    try:
        import requests
        resp = requests.post(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={
                "model": settings.llm_model,
                "messages": [
                    {"role": "system",
                     "content": "Rewrite the evidence points as a concise "
                                "answer. Do not add facts. Keep [EVIDENCE] / "
                                "[INFERENCE] tags exactly where they are."},
                    {"role": "user", "content": "\n".join(points)},
                ],
                "temperature": 0.1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return text, f"llm:{settings.llm_model}"
    except Exception:
        return "\n".join(f"- {p}" for p in points), "template(llm-failed)"
