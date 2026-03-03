"""
LLM prompt templates for structured extraction.

Design decisions:
- System prompt enforces JSON-only output with an explicit schema.
- Few-shot examples are real Enron-style emails to ground the model.
- Rules are explicit about what to extract and what NOT to extract
  (hallucination guardrails).
- The prompt is versioned — any change to the prompt generates a new
  hash, which flows into the extraction_version tag for reproducibility.
"""

from __future__ import annotations

import hashlib
import json

# ──────────────────────────────────────────────────────────────
# System Prompt
# ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a precise information extraction system analyzing corporate emails. Given an email with its metadata, extract structured knowledge as JSON.

EXTRACT:
1. **Entities** — People, Organizations, Projects, Topics, Documents, Meetings mentioned.
2. **Claims** — Relationships between entities with supporting evidence.

OUTPUT SCHEMA (strict JSON, no markdown):
{
  "entities": [
    {
      "name": "Full Entity Name",
      "type": "Person|Organization|Project|Topic|Document|Meeting",
      "aliases": ["alternate names", "abbreviations"],
      "properties": {"role": "...", "department": "...", "title": "..."}
    }
  ],
  "claims": [
    {
      "type": "WORKS_AT|REPORTS_TO|PARTICIPATES_IN|DISCUSSES|DECIDED|MENTIONS|SENT_TO|REFERENCES_DOC|SCHEDULED",
      "subject": "entity_name (must match an entity above)",
      "object": "entity_name (must match an entity above)",
      "properties": {"role": "...", "status": "proposed|confirmed|reversed"},
      "evidence_excerpt": "exact verbatim quote from the email body",
      "confidence": 0.0
    }
  ]
}

RULES:
1. Only extract facts explicitly stated or strongly implied in the email.
2. `evidence_excerpt` MUST be an exact verbatim substring of the email body. Do not paraphrase.
3. Confidence scoring:
   - 0.9–1.0: Explicitly stated facts ("I am the VP of Trading")
   - 0.7–0.8: Strongly implied ("As we discussed in our team meeting" implies PARTICIPATES_IN)
   - 0.5–0.6: Weakly implied, contextual inference
4. Do NOT hallucinate entities or relationships not supported by the text.
5. For people, include email address as an alias if visible.
6. Resolve "I", "me" to the email sender when clear from context.
7. If the email body is empty or contains only a forwarded message, return {"entities": [], "claims": []}.
8. SENT_TO claims should only be extracted for meaningful exchanges, not routine forwards.
9. DISCUSSES claims link the email (represented as its subject line) to a Topic entity.

Return ONLY valid JSON. No explanation, no markdown fences."""

# ──────────────────────────────────────────────────────────────
# Few-Shot Examples
# ──────────────────────────────────────────────────────────────

FEW_SHOT_EXAMPLES = [
    {
        "role": "user",
        "content": """METADATA:
From: ken.lay@enron.com
To: all.employees@enron.com
Date: 2001-08-14
Subject: Organizational Announcement

BODY:
I am pleased to announce that Jeff Skilling will be joining our executive team as President and COO effective immediately. Jeff brings tremendous experience from McKinsey and will report directly to me. Please join me in welcoming him to his new role.

Ken Lay
Chairman and CEO, Enron Corp.""",
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "entities": [
                    {
                        "name": "Kenneth Lay",
                        "type": "Person",
                        "aliases": ["Ken Lay", "ken.lay@enron.com"],
                        "properties": {"role": "Chairman and CEO", "department": "Executive"},
                    },
                    {
                        "name": "Jeff Skilling",
                        "type": "Person",
                        "aliases": [],
                        "properties": {"role": "President and COO", "department": "Executive"},
                    },
                    {
                        "name": "Enron Corp",
                        "type": "Organization",
                        "aliases": ["Enron"],
                        "properties": {"type": "company"},
                    },
                    {
                        "name": "McKinsey",
                        "type": "Organization",
                        "aliases": [],
                        "properties": {"type": "external_company"},
                    },
                ],
                "claims": [
                    {
                        "type": "WORKS_AT",
                        "subject": "Kenneth Lay",
                        "object": "Enron Corp",
                        "properties": {"role": "Chairman and CEO"},
                        "evidence_excerpt": "Ken Lay\nChairman and CEO, Enron Corp.",
                        "confidence": 0.95,
                    },
                    {
                        "type": "WORKS_AT",
                        "subject": "Jeff Skilling",
                        "object": "Enron Corp",
                        "properties": {"role": "President and COO"},
                        "evidence_excerpt": "Jeff Skilling will be joining our executive team as President and COO",
                        "confidence": 0.95,
                    },
                    {
                        "type": "REPORTS_TO",
                        "subject": "Jeff Skilling",
                        "object": "Kenneth Lay",
                        "properties": {},
                        "evidence_excerpt": "will report directly to me",
                        "confidence": 0.9,
                    },
                ],
            },
            indent=None,
        ),
    },
    {
        "role": "user",
        "content": """METADATA:
From: sally.beck@enron.com
To: vince.kaminski@enron.com
Date: 2001-03-15
Subject: Re: Risk Assessment Model Review

BODY:
Vince,

Thanks for the update on the VaR model review. I've shared the preliminary results with the Risk Management Committee. They want to schedule a meeting next Tuesday to discuss the findings.

Can you prepare a summary document covering the key assumptions? I think we should also loop in John Lavorato from Trading since some of the positions affect his book.

Thanks,
Sally""",
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "entities": [
                    {
                        "name": "Sally Beck",
                        "type": "Person",
                        "aliases": ["sally.beck@enron.com"],
                        "properties": {},
                    },
                    {
                        "name": "Vince Kaminski",
                        "type": "Person",
                        "aliases": ["vince.kaminski@enron.com"],
                        "properties": {},
                    },
                    {
                        "name": "John Lavorato",
                        "type": "Person",
                        "aliases": [],
                        "properties": {"department": "Trading"},
                    },
                    {
                        "name": "Risk Management Committee",
                        "type": "Organization",
                        "aliases": [],
                        "properties": {"type": "internal_dept"},
                    },
                    {
                        "name": "VaR Model Review",
                        "type": "Project",
                        "aliases": ["Risk Assessment Model Review"],
                        "properties": {},
                    },
                    {
                        "name": "Risk Management",
                        "type": "Topic",
                        "aliases": [],
                        "properties": {},
                    },
                ],
                "claims": [
                    {
                        "type": "PARTICIPATES_IN",
                        "subject": "Vince Kaminski",
                        "object": "VaR Model Review",
                        "properties": {},
                        "evidence_excerpt": "Thanks for the update on the VaR model review",
                        "confidence": 0.85,
                    },
                    {
                        "type": "PARTICIPATES_IN",
                        "subject": "Sally Beck",
                        "object": "VaR Model Review",
                        "properties": {},
                        "evidence_excerpt": "I've shared the preliminary results with the Risk Management Committee",
                        "confidence": 0.8,
                    },
                    {
                        "type": "DISCUSSES",
                        "subject": "Sally Beck",
                        "object": "Risk Management",
                        "properties": {},
                        "evidence_excerpt": "VaR model review",
                        "confidence": 0.8,
                    },
                    {
                        "type": "WORKS_AT",
                        "subject": "John Lavorato",
                        "object": "Risk Management Committee",
                        "properties": {"department": "Trading"},
                        "evidence_excerpt": "loop in John Lavorato from Trading",
                        "confidence": 0.7,
                    },
                ],
            },
            indent=None,
        ),
    },
]


# ──────────────────────────────────────────────────────────────
# User Prompt Template
# ──────────────────────────────────────────────────────────────


def build_user_prompt(
    sender: str,
    recipients: list[str],
    date: str,
    subject: str,
    body: str,
    thread_context: str | None = None,
) -> str:
    """
    Build the user message for extraction.

    Includes email metadata and body. Optionally includes thread context
    (summary of prior messages) to help resolve references.
    """
    parts = [
        "METADATA:",
        f"From: {sender}",
        f"To: {', '.join(recipients[:10])}",  # cap recipients to save tokens
        f"Date: {date}",
        f"Subject: {subject}",
    ]

    if thread_context:
        parts.append(f"\nTHREAD CONTEXT (prior messages summary):\n{thread_context}")

    parts.append(f"\nBODY:\n{body}")

    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────
# Prompt Versioning
# ──────────────────────────────────────────────────────────────


def get_prompt_hash() -> str:
    """Generate a stable hash of the current prompt configuration."""
    content = SYSTEM_PROMPT + json.dumps(FEW_SHOT_EXAMPLES, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def get_version_tag(model_name: str) -> str:
    """Generate a version tag for this extraction configuration."""
    return f"v1.0_{model_name}_{get_prompt_hash()}"
