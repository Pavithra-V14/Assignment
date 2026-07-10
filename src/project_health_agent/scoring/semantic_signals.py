"""
Semantic, exemplar-based signal detection.

Replaces the old fixed BLOCKER_KEYWORDS / NEGATIVE_SENTIMENT_KEYWORDS /
theme-keyword-map substring-match lists that used to live in metrics.py and
aggregator.py. Those lists silently missed any comment that used a synonym
or phrasing the list's author hadn't anticipated ("held up", "stuck",
"can't proceed" never matched unless the exact substring "blocked" or
"pending" was typed) -- and extending coverage meant remembering to find
and edit a Python list buried in a scoring module.

This module instead defines each concept as a small set of natural-language
EXAMPLE SENTENCES (not keywords) and scores incoming text by similarity to
those examples, using two complementary, fully deterministic techniques
(no network call, no model variance, so scoring stays auditable like the
rest of metrics.py):

  1. TF-IDF + cosine similarity ("semantic_score") -- used for classification
     questions ("is this comment a blocker?", "is this comment negative in
     tone?"). It scores a whole sentence against a whole exemplar sentence,
     so a paraphrase ("still haven't gotten sign-off") scores close to the
     exemplar ("waiting for sign-off") even though no single token is a
     literal keyword match.

  2. BM25 ("bm25_score") -- used for search/ranking questions ("which of
     these N candidate themes does this driver text best match?"). BM25 is
     the standard information-retrieval ranking function: it is still
     lexical (no embeddings/model download required), but it ranks against
     a whole small corpus of exemplar phrasings per theme with proper
     term-frequency / inverse-document-frequency weighting, which is far
     more forgiving of new wording than a literal `keyword in text` check.

To extend coverage for a new phrasing of "blocked", "negative sentiment",
or a new recurring theme: add another EXAMPLE SENTENCE to the relevant list
below -- do not add a keyword. A short natural sentence generalizes to many
future phrasings a bare keyword never will, and needs no code change
elsewhere (metrics.py / aggregator.py only ever call the functions here).

Thresholds are centralized in config.SEMANTIC_THRESHOLDS so they're tunable
the same way SIGNAL_WEIGHTS / BAND_THRESHOLDS are, without touching logic.
"""
import re
from functools import lru_cache

from rank_bm25 import BM25Okapi
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from project_health_agent.core.config import SEMANTIC_THRESHOLDS

# --- Exemplar sentences (extend these, not keyword lists) -------------------

BLOCKER_EXEMPLARS = [
    "This item is still pending and has not been completed yet.",
    "We are waiting for sign-off or approval before we can proceed.",
    "This task is blocked and cannot move forward right now.",
    "Work has been delayed because of a dependency on another team.",
    "This issue needs to be escalated for resolution.",
    "We still need to schedule a meeting to resolve this open item.",
    "This activity has been impacted by other parallel workstreams.",
    "Field or data mapping work remains incomplete.",
    "We have not yet received the required information or file from the client.",
    "This deliverable is stuck waiting on an external party.",
]

NEGATIVE_SENTIMENT_EXEMPLARS = [
    "The client is frustrated and unhappy with our progress.",
    "This has been escalated because the client is upset.",
    "The client raised serious concerns about the delivery.",
    "This same issue keeps repeating week after week with no progress.",
    "The client says this is still not resolved, again.",
    "This is urgent and needs immediate attention.",
    "There is a significant risk to the timeline here.",
    "This remains unresolved despite repeated follow-up.",
    "There is a large gap between what was promised and delivered.",
    "The client expressed dissatisfaction with the team's responsiveness.",
]

# theme label -> a few natural-language phrasings of that theme. Used only
# as a BM25 search index (see match_themes below); the label itself is the
# human-readable string surfaced in reports, unchanged from before.
THEME_EXEMPLARS = {
    "field/data mapping pending": [
        "Field mapping between source and target systems is still in progress.",
        "Data mapping work needs to be finalized before we can proceed.",
        "Mapping fields for inbound and outbound integration remains open.",
    ],
    "workshop scheduling / delays": [
        "The workshop session has been delayed and needs to be rescheduled.",
        "We are waiting to schedule the next requirements workshop.",
        "Onsite workshop dates keep slipping and pushing other work back.",
    ],
    "client sample/data delivery pending": [
        "We are still waiting on the client to send sample data files.",
        "The client has not yet delivered the required sample data.",
        "Sample records from the client remain outstanding.",
    ],
    "meeting scheduling needed": [
        "We still need to schedule a meeting with the client on this topic.",
        "Getting time on the client's calendar for this discussion has been difficult.",
        "This needs to go on the calendar so we can align with stakeholders.",
    ],
    "sign-off pending": [
        "We are waiting on client sign-off before moving forward.",
        "This deliverable still needs formal approval and sign-off from the client.",
    ],
    "critical-path slippage": [
        "A critical-path task with zero float is currently slipping.",
        "This schedule delay is on the critical path and threatens the go-live date.",
        "Zero-float critical tasks are behind schedule and eating into contingency.",
    ],
}


def _normalize(text: str) -> str:
    text = (text or "").lower()
    return re.sub(r"[^a-z0-9@._\s]", " ", text)


def _tokenize(text: str) -> list:
    return [w for w in _normalize(text).split() if w]


def semantic_score(text: str, exemplars: tuple) -> float:
    """Max cosine similarity of `text` against any single exemplar sentence.

    The TF-IDF vocabulary is fit fresh on (exemplars + this one text) for
    every call, rather than fit once on the exemplars alone and reused. A
    vocabulary fit only on ~10 short exemplar sentences is small enough that
    a single shared word (e.g. "need") can dominate the cosine score for an
    unrelated sentence; including the candidate text in the same fit gives
    the vectorizer a fuller picture of that text's own wording before
    scoring it, which is a meaningfully better (if slightly more expensive —
    negligible at PM-comment volumes) approximation of semantic relevance."""
    if not text or not text.strip():
        return 0.0
    corpus = list(exemplars) + [text]
    vec = TfidfVectorizer(preprocessor=_normalize, stop_words="english", ngram_range=(1, 2))
    matrix = vec.fit_transform(corpus)
    ex_matrix = matrix[: len(exemplars)]
    text_vec = matrix[len(exemplars) :]
    sims = cosine_similarity(text_vec, ex_matrix)
    return float(sims.max())


def is_blocker_text(text: str) -> bool:
    return semantic_score(text, tuple(BLOCKER_EXEMPLARS)) >= SEMANTIC_THRESHOLDS["blocker_similarity_min"]


def is_negative_sentiment_text(text: str) -> bool:
    return semantic_score(text, tuple(NEGATIVE_SENTIMENT_EXEMPLARS)) >= SEMANTIC_THRESHOLDS["sentiment_similarity_min"]


@lru_cache(maxsize=1)
def _theme_bm25_index():
    flat_labels, flat_docs = [], []
    for label, sentences in THEME_EXEMPLARS.items():
        for s in sentences:
            flat_labels.append(label)
            flat_docs.append(s)
    tokenized = [_tokenize(d) for d in flat_docs]
    return flat_labels, BM25Okapi(tokenized)


def match_themes(text: str) -> list:
    """BM25-rank `text` against every theme's exemplar phrasings; return the
    theme label(s) whose best-matching exemplar scores above threshold.
    This is the keyword-style *search* case referenced in this module's
    docstring, so BM25 (not TF-IDF cosine) is used here on purpose."""
    if not text:
        return []
    labels, bm25 = _theme_bm25_index()
    scores = bm25.get_scores(_tokenize(text))
    best_per_theme: dict[str, float] = {}
    for label, score in zip(labels, scores):
        if score > best_per_theme.get(label, 0.0):
            best_per_theme[label] = score
    threshold = SEMANTIC_THRESHOLDS["theme_bm25_min"]
    return sorted(label for label, score in best_per_theme.items() if score >= threshold)
