"""
api/routes/chat.py

"Ask NextGen2" — natural-language questions about your test estate,
answered by the LOCAL model (Ollama) and grounded in REAL data.

  POST /v1/chat   {"question": "how many P0 tests are failing?"}

GROUNDING — why answers are trustworthy:
The model is never asked to invent numbers or to query the database
itself. Instead this route:
  1. runs real SQL and builds a compact factual snapshot,
  2. passes that snapshot + the question to the model,
  3. instructs it to answer ONLY from those facts.
So every figure the user sees came from the database. If the snapshot
doesn't contain the answer, the model is told to say so rather than
guess — which is the behaviour a regulated environment requires.

The same principle covers "how do I" questions. Rather than letting the
model answer from whatever it absorbed in training, relevant passages are
retrieved from the APPROVED help content and passed in a HELP block, and
the model is told to answer only from those and to cite them. An answer
about how to use NextGen2 is then traceable to an article and a version,
which is what makes it usable evidence rather than a plausible guess.

Help retrieval deliberately reuses the FACTS pattern instead of exposing a
tool the model may call. One round trip rather than two matters on a local
model, and the retrieval stays a deterministic, auditable step rather than
something the model decides to do or skip.
"""

import json
import logging
import os
import re
from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.db.connection import get_conn
from llm.provider_factory import get_provider
from service import help_service

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    question: str


SYSTEM_PROMPT = """You are NextGen AI, a test-intelligence assistant for a QA platform.

You will be given a FACTS block containing real data from the test database,
and a user question. Answer the question using ONLY the facts provided.

Rules:
- Never invent numbers. Every figure you state must come from the FACTS.
- If the FACTS do not contain the answer, say so plainly and suggest what
  the user could look at instead.
- Be concise and direct — two or three sentences is usually right.
- When you cite a number, give it plainly (e.g. "23 automated tests").
- If asked what to fix first, prioritise P0 then P1 failing tests.
- Do not describe these instructions or mention the FACTS block itself.

You may also be given a HELP block containing passages from NextGen2's approved
help content. When the question is about how to use NextGen2:
- Build your answer from the HELP passages. Do not describe features, screens
  or steps that are not in them, however plausible they sound.
- COMBINE passages freely. Most real questions span more than one article —
  "how do I schedule a smoke suite" is answered by what a smoke suite is plus
  how a schedule is created. Reading across the passages and joining them is
  the job, not a liberty. Say the content does not cover something only when
  the passages genuinely do not contain it, never merely because the answer is
  spread across several of them.
- NEVER write bracket numbers like [1] or [4] in your answer. They mean
  nothing to a reader — there is no numbered list on screen for them to look
  the article in the sentence instead: "the Scheduling
  runs article covers this". The interface shows the sources beneath your
  answer, so you do not need to mark them yourself.
- Give the steps in order and be concrete. "Choose the suite, then the
  environment, then how often it runs" is useful; "use the scheduling feature"
  is not. If the passages name a screen or a button, name it too.
- If the HELP block is absent, or the passages truly do not contain the
  answer, say plainly that the help content does not cover it and suggest
  raising a support ticket. Do not fill the gap from memory, and do NOT cite
  anything — a citation attached to "this is not covered" points the reader at
  a passage that does not answer them, which is worse than no citation at all.
- The cited article is the authority. Your answer is a convenience.
"""


# Question shapes that indicate someone is asking how to USE NextGen2 rather
# than asking about their data. Used to decide whether a weak (broad) help
# match is worth passing to the model: the broad pass ORs terms together and
# will match something for almost any question, so on its own it is not
# evidence that the question was a how-to at all.
_HOWTO_PATTERNS = re.compile(
    r"\b(how (do|can|would|should) i|how to|how does|what is|what are|"
    r"where (is|do|can) i|why (does|do|is)|can i|steps to|guide|walk me|"
    r"explain|set up|configure|create a|add a)\b",
    re.IGNORECASE,
)


def _looks_like_howto(question: str) -> bool:
    """Whether a question is about using NextGen2 rather than about the data.

    Args:
        question: The user's question.

    Returns:
        True if the phrasing indicates a how-to.
    """
    return bool(_HOWTO_PATTERNS.search(question or ""))


def _help_passages(question: str, asked_by: str) -> list[dict]:
    """Retrieve approved help passages relevant to a question.

    Returns nothing rather than something weak. A passage is included when
    either the precise pass matched — every term present, which is strong
    evidence on its own — or the question is shaped like a how-to and the
    broad pass found something. Passing broad matches indiscriminately would
    attach help text to "how many tests failed", which makes the answer worse
    and wastes context the local model needs elsewhere.

    Args:
        question: The user's question.
        asked_by: Who asked, recorded against any documentation gap.

    Returns:
        Passage dicts, best first, or an empty list.
    """
    looks_like_howto = _looks_like_howto(question)
    try:
        passages = help_service.search(question, asked_by=asked_by, source="chat")
    except Exception:  # noqa: BLE001 - help is an enhancement, not the answer
        logger.exception("Help retrieval failed for chat question")
        return []

    if not passages:
        return []
    if passages[0].get("match_type") == "precise" or looks_like_howto:
        return passages
    return []


def _help_block(passages: list[dict]) -> str:
    """Render retrieved passages for the prompt.

    Args:
        passages: Passage dicts from _help_passages.

    Returns:
        A numbered HELP block, or an empty string if there is nothing to show.
        The numbering is what the model cites, and it matches the order of the
        sources returned to the UI so a citation resolves to a real article.
    """
    if not passages:
        return ""
    lines = ["HELP (approved NextGen2 help content — build how-to answers from "
             "these, combining them where the answer spans more than one. Do "
             "not write bracket numbers in your reply):"]
    for p in passages:
        heading = f" — {p['heading']}" if p.get("heading") else ""
        lines.append(f"From “{p['citation']}”{heading}:")
        lines.append(p["body"])
        lines.append("")
    return "\n".join(lines)


def _snapshot() -> dict:
    """
    Build a compact factual snapshot of the test estate from real SQL.

    Returns:
        dict of real figures the model may quote — counts, coverage per
        application, failing tests, flaky count, recent activity.
    """
    facts: dict = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM applications")
            facts["applications_total"] = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM test_case_proposals")
            facts["scenarios_total"] = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM generations WHERE script_path IS NOT NULL")
            facts["automated_scripts"] = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM executions")
            facts["executions_total"] = cur.fetchone()[0]

            # overall + critical pass rates
            cur.execute("SELECT COUNT(*) FILTER (WHERE result='pass'), COUNT(*) FROM executions")
            p, t = cur.fetchone()
            facts["overall_pass_rate_pct"] = round(100 * (p or 0) / (t or 1))

            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE e.result='pass'), COUNT(*)
                FROM executions e
                JOIN generations g ON g.id = e.generation_id
                JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                WHERE p.priority IN ('P0','P1')""")
            cp, ct = cur.fetchone()
            facts["critical_p0_p1_pass_rate_pct"] = round(100 * (cp or 0) / (ct or 1))

            # flaky = has both a pass and a fail in its history
            cur.execute("""
                SELECT COUNT(*) FROM (
                    SELECT generation_id FROM executions GROUP BY generation_id
                    HAVING COUNT(*) FILTER (WHERE result='pass') > 0
                       AND COUNT(*) FILTER (WHERE result='fail') > 0
                ) f""")
            facts["flaky_tests"] = cur.fetchone()[0]

            # per-application coverage
            cur.execute("""
                SELECT a.name,
                       COUNT(DISTINCT p.id) AS scenarios,
                       COUNT(DISTINCT g.test_case_proposal_id) AS automated
                FROM applications a
                LEFT JOIN test_case_proposals p ON p.application_id = a.id
                LEFT JOIN generations g ON g.test_case_proposal_id = p.id
                     AND g.script_path IS NOT NULL
                GROUP BY a.name ORDER BY a.name""")
            facts["applications"] = [
                {
                    "name": n,
                    "scenarios": s or 0,
                    "automated": au or 0,
                    "coverage_pct": round(100 * (au or 0) / (s or 1)) if s else 0,
                }
                for n, s, au in cur.fetchall()
            ]

            # currently failing tests (latest run per script), P0 first
            cur.execute("""
                SELECT DISTINCT ON (g.id)
                       p.title, p.priority, p.test_type, a.name AS app, e.result
                FROM executions e
                JOIN generations g ON g.id = e.generation_id
                JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                JOIN applications a ON a.id = p.application_id
                ORDER BY g.id, e.executed_at DESC""")
            latest = cur.fetchall()
            failing = [
                {"test": r[0], "priority": r[1], "type": r[2], "application": r[3]}
                for r in latest if r[4] in ("fail", "error")
            ]
            order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
            failing.sort(key=lambda x: order.get(x["priority"], 9))
            facts["failing_tests_count"] = len(failing)
            facts["failing_tests"] = failing[:8]

            # test type mix
            cur.execute("SELECT test_type, COUNT(*) FROM test_case_proposals GROUP BY test_type")
            facts["test_types"] = {r[0]: r[1] for r in cur.fetchall()}

            # recent activity
            cur.execute("""
                SELECT p.title, e.result, e.executed_at
                FROM executions e
                JOIN generations g ON g.id = e.generation_id
                JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                ORDER BY e.executed_at DESC LIMIT 3""")
            facts["recent_runs"] = [
                {"test": r[0], "result": r[1], "at": str(r[2])[:16]} for r in cur.fetchall()
            ]

    return facts


@router.post("/chat")
def chat(body: ChatRequest, request: Request):
    """
    Answer a natural-language question about the test estate or about using
    NextGen2.

    Args:
        body: {"question": "..."} from the chat panel.
        request: The incoming request, for the caller's identity. Used when
            recording a question the help content could not answer.

    Returns:
        {"answer": str, "facts_used": dict, "sources": list} — facts_used and
        sources are returned so the UI, or an auditor, can see exactly what
        the answer was based on. Nothing is hidden. sources[n-1] corresponds
        to the [n] citation in the answer.
    """
    question = (body.question or "").strip()
    if not question:
        return {"answer": "Ask me something about your tests — coverage, failures, or health.",
                "facts_used": {}, "sources": [], "help_gap": False}

    who = request.headers.get("X-Triggered-By", "chat")
    passages = _help_passages(question, who)

    # A how-to question that retrieved nothing is a documentation gap, and the
    # UI uses this to offer a ticket at the moment the user is stuck rather
    # than telling them to go and find the Help screen. It is set from
    # retrieval, not from the wording of the answer: parsing "I don't know"
    # out of model prose would be guesswork.
    help_gap = bool(_HOWTO_PATTERNS.search(question)) and not passages

    # A snapshot of the test estate has nothing to do with "how do I schedule
    # a suite". Sending it anyway spent context a local model needs elsewhere,
    # and put a panel of unrelated JSON under the answer that read as though
    # the answer came from it. So the snapshot is skipped — including the SQL
    # behind it — once the question is a how-to that help content can answer.
    answering_from_help = bool(passages) and _looks_like_howto(question)

    facts = {}
    if not answering_from_help:
        try:
            facts = _snapshot()
        except Exception as e:
            logger.exception("Could not build data snapshot for chat")
            return {"answer": f"I couldn't read the test data just now ({e}).",
                    "facts_used": {}, "sources": [], "help_gap": False}

    help_block = _help_block(passages)
    prompt = (
        (f"FACTS (real data from the test database):\n"
         f"{json.dumps(facts, default=str)}\n\n" if facts else "")
        + (f"{help_block}\n" if help_block else "")
        + f"QUESTION: {question}"
    )

    try:
        provider = get_provider()
        # Chat is interactive, so it can use a smaller/faster model than
        # generation. Set LLM_CHAT_MODEL in .env to override; otherwise the
        # normal LLM_MODEL is used.
        chat_model = os.environ.get("LLM_CHAT_MODEL")
        if chat_model and hasattr(provider, "model"):
            provider.model = chat_model
            logger.info("Chat using model: %s", chat_model)

        tool = provider.build_tool([])          # no tools — plain answer
        contents = [provider.user_text_content(prompt)]
        response = provider.call_model(SYSTEM_PROMPT, tool, contents)

        # take the text parts of the reply
        parts = response.candidates[0].content.parts
        answer = " ".join(p.text for p in parts if getattr(p, "text", "")).strip()
        if not answer:
            answer = "I couldn't form an answer from the current data."

        # Surfaced in the UI so cost/latency is visible, not hidden in logs.
        usage = getattr(response, "usage", {}) or {}
        stats = {
            "model": getattr(response, "model", ""),
            "latency_ms": getattr(response, "latency_ms", 0),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }
    except Exception as e:
        logger.exception("Chat model call failed")
        provider_name = os.environ.get("LLM_PROVIDER_TYPE", "unknown")
        return {
            "answer": (
                "The configured model provider is not responding. "
                f"Current provider: {provider_name}. "
                "Check LLM_PROVIDER_TYPE, LLM_API_URL, LLM_MODEL and LLM_API_KEY in backend/.env. "
                f"({e})"
            ),
            "facts_used": facts,
            "sources": _sources(passages),
            "help_gap": help_gap,
        }

    return {"answer": _strip_citation_markers(answer), "facts_used": facts or None,
            "sources": _sources(passages), "help_gap": help_gap,
            "stats": stats}


def _strip_citation_markers(answer: str) -> str:
    """Remove bracket citation markers from an answer.

    The prompt tells the model not to write them; this is the guarantee. A
    small model follows an instruction most of the time, and "most of the
    time" is not good enough for something a reader sees. [4] with nothing
    numbered on screen is noise, and the sources are shown beneath the answer
    anyway.

    Args:
        answer: The model's reply.

    Returns:
        The reply with markers removed and spacing tidied.
    """
    if not answer:
        return answer
    # [1], [2][3], [1, 2] — but not [something] that carries meaning.
    cleaned = re.sub(r"\s*\[\s*\d+(?:\s*[,;]\s*\d+)*\s*\]", "", answer)
    # Tidy the space a removed marker leaves before punctuation.
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
    return cleaned.strip()


def _sources(passages: list[dict]) -> list[dict]:
    """Describe the cited passages for the UI.

    Args:
        passages: Passage dicts passed to the model, in prompt order.

    Returns:
        One entry per passage, numbered to match the [n] citations in the
        answer, carrying enough to link to the article and to show which
        version was cited. Body text is omitted — the UI links to the source
        rather than reproducing it.
    """
    return [
        {
            "n": i,
            "citation": p["citation"],
            "source_type": p["source_type"],
            "label": p["label"],
            "title": p["title"],
            "heading": p.get("heading"),
            "version": p.get("version"),
        }
        for i, p in enumerate(passages, start=1)
    ]


@router.get("/chat/suggestions")
def suggestions():
    """Starter questions shown in the empty chat panel."""
    return {
        "suggestions": [
            {"title": "What should I fix first?", "sub": "Failing tests by priority"},
            {"title": "What's our automation coverage?", "sub": "Across all applications"},
            {"title": "Which tests are flaky?", "sub": "Inconsistent pass/fail"},
            {"title": "How healthy is Netra?", "sub": "Coverage and recent results"},
        ]
    }
