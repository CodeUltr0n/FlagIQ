"""
FlagIQ — Evidence-Gated Decision Engine.

Takes a creation-time IssueReport (strictly title + body only) and a Flagsmith
strategy variant, extracts deterministic evidence signals, and emits a decision.

Experimental validity guarantee:
  Outcome and post-close metadata (real_resolution, has_linked_pr, comment_count,
  labels) are NEVER inspected prior to assigning `decision`.
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Sequence
import yaml
from .schemas import IssueReport, DecisionResult, DecisionType

_POLICY_PATH = Path(__file__).parent.parent / "config" / "policy.yaml"
_policy: dict | None = None

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "in", "on", "to", "of", "for", "with",
    "and", "or", "when", "not", "it", "this", "that", "from", "by", "be",
    "as", "at", "after", "before", "into", "using", "issue", "bug", "error",
})

_TRACE_RE = re.compile(
    r"(traceback \(most recent|stack ?trace|exception:|error:|at line \d+|"
    r"\.(py|js|ts|tsx|jsx|go|rs):\d+)",
    re.IGNORECASE,
)
_REPRO_RE = re.compile(
    r"(steps to reproduce|reproduction|repro|expected behavior|actual behavior|"
    r"minimal reproduction|environment|version|python \d|node v?\d|macos|ubuntu|windows)",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r"(\?\s*$|^(how|can|is it possible|where|why|what|does|should)\b|"
    r"\b(how to|how do i|documentation|docs|example|configure|config|setting|usage)\b)",
    re.IGNORECASE,
)
_BUG_RE = re.compile(
    r"\b(crash|crashes|segfault|regression|fails|failing|broken|breaks|panic|"
    r"deadlock|memory leak|500|typeerror|referenceerror|attributeerror|"
    r"keyerror|valueerror|runtimeerror|nullpointer|undefined is not)\b",
    re.IGNORECASE,
)
_DUP_PHRASE_RE = re.compile(r"\b(duplicate of|same as)\s+#?\d+", re.IGNORECASE)
_ERR_SIG_RE = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:Error|Exception)|SIGSEGV|ECONNREFUSED|ENOENT|HTTP\s*5\d\d)\b"
)


def _load_policy() -> dict:
    global _policy
    if _policy is None:
        with open(_POLICY_PATH) as f:
            _policy = yaml.safe_load(f)
    return _policy


def _tokenize(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9_]{3,}", text.lower()))
    return tokens - _STOPWORDS


def _extract_triage_signals(
    issue: IssueReport,
    corpus: Sequence[IssueReport] | None = None,
) -> dict:
    """
    Extract creation-time signals strictly from issue.title, issue.body, and
    peer issue titles in the same repo. Never reads post-close outcomes.
    """
    title = (issue.title or "").strip()
    body = (issue.body or "").strip()
    combined = f"{title}\n{body}"

    has_code_block = "```" in body
    has_trace = bool(_TRACE_RE.search(combined))
    has_repro = bool(_REPRO_RE.search(combined))
    is_question = bool(_QUESTION_RE.search(title) or _QUESTION_RE.search(body[:250]))
    bug_matches = set(m.group(0).lower() for m in _BUG_RE.finditer(combined))
    err_sigs = sorted(set(m.group(1) for m in _ERR_SIG_RE.finditer(combined)))

    repro_score = 0.0
    evidence: list[str] = []

    if err_sigs:
        evidence.append(f"Normalized error signatures: {', '.join(err_sigs[:3])}")
    if has_code_block or has_trace:
        repro_score += 0.35
        evidence.append("Code block or stack trace present")
    if has_repro:
        repro_score += 0.25
        evidence.append("Reproduction steps / environment info provided")
    if len(body) >= 180:
        repro_score += 0.25
        evidence.append(f"Detailed report ({len(body)} chars)")
    elif len(body) >= 60:
        repro_score += 0.10
        evidence.append(f"Brief report ({len(body)} chars)")
    if len(title.split()) >= 5:
        repro_score += 0.15
    repro_score = min(repro_score, 1.0)

    question_score = 0.0
    if is_question:
        question_score += 0.45
        evidence.append("Question / configuration phrasing detected")
    if not has_trace and not bug_matches:
        question_score += 0.25
    if 60 <= len(body) <= 1000:
        question_score += 0.20
    if re.search(r"\b(setting|option|config|docs|guide|example)\b", combined, re.IGNORECASE):
        question_score += 0.10
    question_score = min(question_score, 1.0)

    bug_severity = 0.0
    if bug_matches:
        bug_severity += min(0.50, 0.25 * len(bug_matches))
        evidence.append(f"Failure signatures: {', '.join(sorted(bug_matches)[:3])}")
    if has_trace:
        bug_severity += 0.30
    if has_repro or has_code_block:
        bug_severity += 0.20
    bug_severity = min(bug_severity, 1.0)

    dup_similarity = 0.0
    matched_peer = None
    if _DUP_PHRASE_RE.search(combined) or title.lower().startswith("duplicate"):
        dup_similarity = 0.90
        evidence.append("Explicit duplicate reference in text")
    elif corpus:
        src_tokens = _tokenize(title)
        if len(src_tokens) >= 3:
            for peer in corpus:
                if peer.identity == issue.identity or peer.repo != issue.repo:
                    continue
                # Strict temporal causality: never match issues created at/after this issue
                if peer.created_at and issue.created_at:
                    if peer.created_at >= issue.created_at:
                        continue
                elif peer.number and issue.number and peer.number >= issue.number:
                    continue
                peer_tokens = _tokenize(peer.title or "")
                if not peer_tokens:
                    continue
                jaccard = len(src_tokens & peer_tokens) / len(src_tokens | peer_tokens)
                if jaccard > dup_similarity:
                    dup_similarity = jaccard
                    matched_peer = peer.identity
            if dup_similarity >= 0.45 and matched_peer:
                evidence.append(f"Similar prior issue {matched_peer} (Jaccard {dup_similarity:.2f})")

    if not evidence:
        evidence.append("Minimal context: no stack trace, repro steps, or clear question")

    return {
        "repro_score": round(repro_score, 3),
        "question_score": round(question_score, 3),
        "bug_severity": round(bug_severity, 3),
        "dup_similarity": round(dup_similarity, 3),
        "evidence": evidence,
    }


def _compute_confidence(issue: IssueReport) -> float:
    """Public helper returning creation-time evidence score (0.0 – 1.0)."""
    return _extract_triage_signals(issue)["repro_score"]


def run_triage(
    issue: IssueReport,
    strategy: str,
    corpus: Sequence[IssueReport] | None = None,
) -> DecisionResult:
    """
    Apply the given Flagsmith strategy variant to creation-time issue text
    and return an evidence-backed DecisionResult.
    """
    policy = _load_policy()
    if strategy not in policy["strategies"]:
        strategy = "answer_first"
    thresholds = policy["strategies"][strategy]
    cost_cfg = policy.get("cost_model", {})

    signals = _extract_triage_signals(issue, corpus=corpus)
    confidence = signals["repro_score"]
    question_score = signals["question_score"]
    bug_severity = signals["bug_severity"]
    dup_similarity = signals["dup_similarity"]

    # Determine decision strictly from creation-time text signals & Flagsmith strategy.
    # Outcome / post-close fields are never referenced before `decision` is assigned.
    decision: DecisionType
    if dup_similarity >= thresholds["duplicate_threshold"]:
        decision = "DUPLICATE"
    elif strategy == "answer_first":
        if bug_severity >= thresholds["escalate_bug_threshold"] and question_score < 0.45:
            decision = "ESCALATE"
        elif confidence < thresholds["clarify_max_confidence"] and question_score < 0.35:
            decision = "CLARIFY"
        elif question_score >= 0.35 or confidence >= thresholds["answer_threshold"]:
            decision = "ANSWER"
        else:
            decision = "CLARIFY"
    else:
        if bug_severity >= thresholds["escalate_bug_threshold"] and confidence >= 0.45:
            decision = "ESCALATE"
        elif confidence < thresholds["clarify_max_confidence"] and question_score < 0.55:
            decision = "CLARIFY"
        elif (question_score >= 0.45 and bug_severity < 0.55) or (
            confidence >= thresholds["answer_threshold"] and bug_severity < 0.50
        ):
            decision = "ANSWER"
        else:
            decision = "CLARIFY"

    if strategy == "clarify_first" and decision == "CLARIFY":
        signals["evidence"].append(
            "Guardrail triggered: refused unverified guess — requesting repro steps / stack trace"
        )

    # Post-decision evaluation against ground truth (strictly after decision is locked)
    correct = decision == issue.real_resolution
    false_escalation = decision == "ESCALATE" and issue.real_resolution != "ESCALATE"
    false_answer = decision == "ANSWER" and issue.real_resolution == "ESCALATE"

    if false_escalation:
        net_value = -float(cost_cfg.get("false_escalation_usd", 45.0))
    elif false_answer:
        net_value = -float(cost_cfg.get("false_answer_usd", 25.0))
    elif correct:
        net_value = float(cost_cfg.get("correct_decision_usd", 15.0))
    else:
        net_value = -10.0

    return DecisionResult(
        identity=issue.identity,
        strategy=strategy,
        decision=decision,
        confidence=confidence,
        real_resolution=issue.real_resolution,
        correct=correct,
        false_escalation=false_escalation,
        false_answer=false_answer,
        evidence=signals["evidence"],
        net_value_usd=net_value,
    )
