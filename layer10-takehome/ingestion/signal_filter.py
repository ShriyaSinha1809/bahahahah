"""
Signal-based email pre-filter.

Runs BEFORE LLM extraction to cheaply eliminate low-value emails:
  1. Folder filter   — skip deleted_items, all_documents, notes_inbox
  2. Keyword filter  — require ≥2 Enron-signal keywords in subject+body
  3. Body length     — skip trivially short emails (< 50 chars)

This reduces LLM calls by ~70% while keeping all high-signal emails.
"""

from __future__ import annotations

from ingestion.parse_enron import RawEmail

# ──────────────────────────────────────────────────────────────
# Folder skip-list
# ──────────────────────────────────────────────────────────────

SKIP_FOLDERS = {
    "all_documents",
    "deleted_items",
    "notes_inbox",
    "contacts",
    "calendar",
    "straw",
}

# ──────────────────────────────────────────────────────────────
# High-signal keyword list
# ──────────────────────────────────────────────────────────────

SIGNAL_KEYWORDS: list[str] = [
    # Key people
    "skilling", "lay", "fastow", "causey", "kopper", "delainey",
    "whalley", "kean", "haedicke", "shankman", "kaminski",
    # Key entities
    "ljm", "raptor", "spe", "azurix", "dabhol", "blockbuster",
    "broadband", "ews", "ees", "enron", "andersen",
    # Financial / legal signals
    "mark-to-market", "mark to market", "restatement", "10-k", "10k",
    "earnings", "sec", "lawsuit", "settlement", "audit", "accounting",
    "board", "directors", "shareholders",
    # Decision / action signals
    "approve", "approved", "agreed", "decided", "signed",
    "merger", "acquisition", "deal", "contract", "transaction",
    "hired", "fired", "resigned", "promoted", "appointed",
    # Risk signals
    "loss", "exposure", "hedge", "risk", "write-down", "writedown",
    "collateral", "credit", "debt", "liability",
]

_LOWER_KEYWORDS = [kw.lower() for kw in SIGNAL_KEYWORDS]


# ──────────────────────────────────────────────────────────────
# Filter logic
# ──────────────────────────────────────────────────────────────


def is_high_signal(email: RawEmail, min_keyword_hits: int = 2) -> bool:
    """
    Return True if this email is worth sending to the LLM.

    Criteria (all must pass):
    - Not in a skip folder
    - Body is at least 50 characters
    - At least `min_keyword_hits` signal keywords appear in subject+body
    """
    # 1. Folder filter
    folder = email.folder_path.lower()
    for skip in SKIP_FOLDERS:
        if skip in folder:
            return False

    # 2. Length filter
    if len(email.body.strip()) < 50:
        return False

    # 3. Keyword filter
    text = (email.subject + " " + email.body).lower()
    hits = sum(1 for kw in _LOWER_KEYWORDS if kw in text)
    return hits >= min_keyword_hits


def filter_emails(
    emails: list[RawEmail],
    min_keyword_hits: int = 2,
) -> tuple[list[RawEmail], dict[str, int]]:
    """
    Filter a list of emails to high-signal ones.

    Returns (filtered_list, stats_dict).
    """
    passed, dropped_folder, dropped_length, dropped_signal = [], 0, 0, 0

    for em in emails:
        folder = em.folder_path.lower()
        if any(skip in folder for skip in SKIP_FOLDERS):
            dropped_folder += 1
            continue
        if len(em.body.strip()) < 50:
            dropped_length += 1
            continue
        text = (em.subject + " " + em.body).lower()
        hits = sum(1 for kw in _LOWER_KEYWORDS if kw in text)
        if hits < min_keyword_hits:
            dropped_signal += 1
            continue
        passed.append(em)

    stats = {
        "input": len(emails),
        "passed": len(passed),
        "dropped_folder": dropped_folder,
        "dropped_length": dropped_length,
        "dropped_signal": dropped_signal,
        "reduction_pct": round(100 * (1 - len(passed) / max(len(emails), 1)), 1),
    }
    return passed, stats
