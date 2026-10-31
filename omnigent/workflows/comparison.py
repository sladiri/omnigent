"""Design approval and evidence comparisons owned by the Compare workflow."""

from __future__ import annotations

import copy
import json
import re
from typing import Any, TypeAlias

Json: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
NAME = "compare-review-fix"
ANALYSIS_STAGES = {"explore", "compare-results"}
DESIGN_FIELDS = (
    "description",
    "benefits",
    "disadvantages",
    "complexity",
    "maintenance",
    "risks",
    "assumptions",
    "questions",
)


def enabled(state: Json) -> bool:
    return state.get("definition", {}).get("workflow") == NAME


def analysis_stage(state: Json, stage: str) -> None:
    state.pop("parallel", None)
    state.update(current=stage, status="ready", pending=None, attempt=1, presented=None)


def brief(state: Json) -> str:
    definition = state["definition"]
    common = (
        f"Stage: {state['current']}\nTask: {definition['task']}\n"
        f"Repository: {definition['repository']}\nBaseline: {definition['baseline']}\n"
        "Read the installed compare-solutions skill. Use its exploration, fair-test, and "
        "comparison guidance only. This controller requires approval BEFORE implementation "
        "and final selection even with one viable design. Do not create or enable a goal. "
        "Do not implement, run candidate tests, edit files, or delegate. Read repository "
        "instructions and relevant code. Shell access is limited to the exact read commands "
        "listed below; use sys_os_read for files.\n"
        "Return JSON: stage, outcome (COMPLETE/NEEDS_INPUT/BLOCKED), evidence (nonempty), "
        "findings: [], resolved: []. NEEDS_INPUT/BLOCKED also needs question.\n"
        f"User answer: {state.get('answer', '')}\n"
    )
    if state["current"] == "explore":
        return common + (
            "Explore materially different designs; do not meet a fixed count. No candidate "
            "has been implemented or tested for this proposal round. Distinguish expected "
            "benefits from measurements. COMPLETE also needs designs: a nonempty list of "
            "objects with stable lowercase kebab-case id and nonempty text fields "
            f"{', '.join(DESIGN_FIELDS)}. Use 'None identified' where appropriate. "
            "Also supply checks: the shared acceptance test matrix, and recommendation: "
            "{id, reasons}. Preserve IDs for existing designs; never reuse an old ID for "
            "an unrelated design. Material revisions must be described explicitly.\n"
            f"Initial candidate suggestions: {json.dumps(definition.get('candidates', []))}\n"
            f"Previous proposals: {json.dumps(state.get('designs', {}))}\n"
            f"Revision request: {state.get('design_feedback', '')}"
        )
    return common + (
        "Compare actual results for EVERY proposed design, including unapproved, rejected, "
        "or failed designs. Use the recorded evidence; do not claim unrun tests passed. "
        "Separate measured results from judgment. COMPLETE also needs results: a list of "
        "{id, behavior, tradeoffs, limitations, evidence}, where evidence is a list of "
        "completed stage keys for that design (empty for unimplemented designs). Cite "
        "the implementation and both final-b axes for verified designs. "
        "recommendation: {id, reasons} must name "
        "a verified design, or id: null if none succeeded. Explain all exclusions.\n"
        f"Proposals and implementation decisions: {json.dumps(state['designs'])}\n"
        f"Candidates: {json.dumps(state['candidates'])}\n"
        f"Verified revisions: {json.dumps(state['verified'])}\n"
        f"Rejected or unsuccessful: {json.dumps(state.get('rejected', {}))}\n"
        f"Measured stage evidence: {json.dumps(state['completed'])}\n"
        f"Incomplete stage evidence: {json.dumps(state.get('incomplete', {}))}"
    )


def _text(value: Json, fields: tuple[str, ...]) -> None:
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in fields):
        raise ValueError("Comparison needs nonempty text: " + ", ".join(fields))


def _recommendation(report: Json, eligible: set[str]) -> None:
    recommendation = report.get("recommendation")
    if not isinstance(recommendation, dict):
        raise ValueError("Comparison needs a recommendation with reasons")
    _text(recommendation, ("reasons",))
    if recommendation.get("id") not in (eligible or {None}):
        raise ValueError("Recommendation must identify an eligible design")


def accept(state: Json, report: Json) -> None:
    """Validate a trusted worker report before entering either human gate."""
    if state["current"] == "explore":
        designs = report.get("designs")
        if not isinstance(designs, list) or not designs:
            raise ValueError("Propose at least one viable design or report BLOCKED")
        ids: set[str] = set()
        for design in designs:
            if not isinstance(design, dict):
                raise ValueError("Designs must be objects")
            identifier = design.get("id")
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier)
                or identifier in ids
            ):
                raise ValueError("Design IDs must be unique lowercase kebab-case")
            _text(design, DESIGN_FIELDS)
            ids.add(identifier)
        _text(report, ("checks",))
        _recommendation(report, ids)
        revision = state.get("designs", {}).get("revision", 0) + 1
        state["designs"] = {
            "revision": revision,
            "proposals": [
                {"id": d["id"], **{key: d[key] for key in DESIGN_FIELDS}} for d in designs
            ],
            "checks": report["checks"],
            "recommendation": report["recommendation"],
            "rejected": {},
            "approved": [],
            "decisions": [],
        }
        state.update(status="waiting-design-selection", current=None, presented=None)
    else:
        results = report.get("results")
        proposed = {d["id"] for d in state["designs"]["proposals"]}
        if not isinstance(results, list) or len(results) != len(proposed):
            raise ValueError("Results must account for every proposed design")
        seen = set()
        for row in results:
            if not isinstance(row, dict) or row.get("id") not in proposed or row["id"] in seen:
                raise ValueError("Result IDs must match the proposed designs")
            identifier = row["id"]
            seen.add(identifier)
            _text(row, ("behavior", "tradeoffs", "limitations"))
            evidence = row.get("evidence")
            if not isinstance(evidence, list) or any(
                not isinstance(key, str)
                or not key.startswith(identifier + "/")
                or key not in state["completed"]
                for key in evidence
            ):
                raise ValueError("Results must reference recorded evidence for that design")
            if identifier in state["verified"] and not {
                identifier + "/implement",
                identifier + "/final-b-standards",
                identifier + "/final-b-spec",
            }.issubset(evidence):
                raise ValueError(
                    "Verified results must cite implementation and both final reviews"
                )
        _recommendation(report, set(state["verified"]))
        state["result_comparison"] = {
            "results": results,
            "recommendation": report["recommendation"],
        }
        state.update(status="waiting-final-selection", current=None, presented=None)
    state["question"] = (
        "Call workflow_present, show its comparison, and wait for the user's decision."
    )


def command(state: Json, text: str) -> bool:
    """Accept only explicit revision-bound design decisions; never infer approval."""
    if text == "/workflow cancel":
        if state["status"] not in {
            "ready",
            "waiting-design-selection",
            "waiting-final-selection",
            "waiting-design-revision",
            "waiting-user",
            "blocked",
        }:
            raise ValueError("Cancel at a stopped decision gate, before publication")
        if state.get("pending") or state.get("parallel", {}).get("pending"):
            raise ValueError("Stop and reconcile active workers before cancelling")
        state.update(status="cancelled", question=None)
        state.setdefault("decisions", []).append(
            {"command": text, "revision": state.get("designs", {}).get("revision")}
        )
        return True
    if not text.startswith("/workflow designs "):
        return False
    parts = text.removeprefix("/workflow designs ").split()
    designs = state.get("designs", {})
    if len(parts) < 2 or not designs or parts[0] != str(designs.get("revision")):
        raise ValueError("Use the current proposal revision shown by workflow_present")
    action, values = parts[1], parts[2:]
    if state.get("pending") or state.get("parallel", {}).get("pending"):
        raise ValueError("Wait for or stop and reconcile the active workers first")
    if action == "revise":
        if not values or state["status"] not in {
            "waiting-design-selection",
            "waiting-final-selection",
            "waiting-design-revision",
            "waiting-user",
            "blocked",
        }:
            raise ValueError("Revision needs feedback at a stopped decision gate")
        designs["decisions"].append({"command": text})
        state.setdefault("design_history", []).append(
            copy.deepcopy(
                {
                    key: state.get(key)
                    for key in (
                        "designs",
                        "selected",
                        "candidates",
                        "completed",
                        "verified",
                        "rejected",
                        "result_comparison",
                        "incomplete",
                    )
                }
            )
        )
        state.update(design_feedback=" ".join(values), answer="", selected=None)
        analysis_stage(state, "explore")
        return True
    if state["status"] != "waiting-design-selection" or state.get("presented") != "designs":
        raise ValueError("Present the current proposals before choosing designs")
    proposed = {d["id"] for d in designs["proposals"]}
    available = proposed - designs["rejected"].keys()
    ids = sorted(available) if action == "all" and not values else values
    if (
        action not in {"all", "select", "reject"}
        or (action == "all" and values)
        or not ids
        or len(ids) != len(set(ids))
        or not set(ids) <= available
    ):
        raise ValueError("Choose all or list available design IDs with select/reject")
    designs["decisions"].append({"command": text})
    if action == "reject":
        designs["rejected"].update(dict.fromkeys(ids, "Rejected by user before implementation"))
        state["presented"] = None
        return True
    designs["approved"] = ids
    definition = state["definition"]
    definition["run_id"] = f"{state['workflow_id']}-designs-{designs['revision']}"
    definition["candidates"] = [
        {"id": d["id"], "task": json.dumps(d) + "\nShared acceptance checks: " + designs["checks"]}
        for d in designs["proposals"]
        if d["id"] in ids
    ]
    state.update(
        status="prepare",
        candidates=[],
        completed={},
        verified={},
        findings={},
        fix_attempts={},
        rejected={},
        incomplete={},
        selected=None,
        question=None,
        presented=None,
    )
    state.pop("result_comparison", None)
    state.pop("publication_input", None)
    return True


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def render(state: Json) -> str:
    """Render the persisted comparison and exact commands shown in the Web UI."""
    designs = state["designs"]
    revision = designs["revision"]
    if state["status"] == "waiting-design-selection":
        rows = [
            f"Design proposals — revision {revision}",
            "",
            "Expected tradeoffs only. No candidate for this proposal round "
            "has been implemented or tested.",
            "",
        ]
        for design in designs["proposals"]:
            rows.append(f"**{design['id']} — {design['description']}**")
            rows.append("")
            for field in DESIGN_FIELDS[1:]:
                rows.append(f"- {field.capitalize()}: {design[field]}")
            rows.append("")
        rows.extend(["Shared acceptance checks: " + designs["checks"], ""])
        rows.extend(f"{key}: {reason}" for key, reason in designs["rejected"].items())
        rows.append("")
        recommendation = designs["recommendation"]
        rows.append(
            f"Recommendation (before testing): {recommendation['id']} — "
            f"{recommendation['reasons']}"
        )
        rows.append("")
        rows.append(
            f"Wait for your decision. Send `/workflow designs {revision} all`, "
            f"`/workflow designs {revision} select <id> <id>`, "
            f"`/workflow designs {revision} reject <id> <id>`, "
            f"`/workflow designs {revision} revise <feedback>`, or `/workflow cancel`. "
            "Design approval permits implementation only; "
            "it is not final selection or publication approval."
        )
    else:
        rows = [
            "Implementation results",
            "",
            "Recorded evidence is listed separately from the comparison judgment.",
            "",
            "| ID | Status | Behavior | Tradeoffs | Limitations |",
            "| --- | --- | --- | --- | --- |",
        ]
        for result in state["result_comparison"]["results"]:
            identifier = result["id"]
            status = (
                "Verified " + state["verified"][identifier]
                if identifier in state["verified"]
                else state.get("rejected", {}).get(identifier)
                or designs["rejected"].get(identifier)
                or "Not approved for implementation"
            )
            rows.append(
                "| "
                + " | ".join(
                    _cell(v)
                    for v in (
                        identifier,
                        status,
                        result["behavior"],
                        result["tradeoffs"],
                        result["limitations"],
                    )
                )
                + " |"
            )
        rows.extend(["", "Recorded evidence:", ""])
        for result in state["result_comparison"]["results"]:
            for key in result["evidence"]:
                evidence = state["completed"][key]
                rows.append(f"{key} ({evidence['revision']}): {evidence['evidence']}")
        for key, evidence in state.get("incomplete", {}).items():
            rows.append(
                f"Incomplete {key}: {evidence.get('evidence', '')}; {evidence.get('question', '')}"
            )
        rows.append("")
        recommendation = state["result_comparison"]["recommendation"]
        rows.append(
            f"Recommendation: {recommendation['id'] or 'none'} — {recommendation['reasons']}"
        )
        rows.append("")
        rows.append(
            "Wait for final selection: `/workflow select <id>`. "
            f"Or `/workflow designs {revision} revise <feedback>` or `/workflow cancel`. "
            "Final selection preserves the original publication permission; "
            "it does not add authorization."
        )
    return "\n".join(rows)
