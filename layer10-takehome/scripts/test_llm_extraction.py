"""
Live LLM extraction test — calls Groq (Llama 3.3 70B) and prints results.
Tests the full extraction pipeline: email → LLM → validation → structured output.
"""

import asyncio
import json
import sys
import os

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extraction.extractor import Extractor, EmailForExtraction
from extraction.prompts import SYSTEM_PROMPT, build_user_prompt, FEW_SHOT_EXAMPLES
from extraction.extractor import LLMClient
from config import get_settings

EMAILS = [
    EmailForExtraction(
        message_id="test-001@enron.com",
        sender="jeffrey.skilling@enron.com",
        recipients=["kenneth.lay@enron.com", "andrew.fastow@enron.com"],
        date="2001-10-10",
        subject="Q3 Results & Raptor Exposure",
        body="""Ken, Andy,

I've reviewed the Q3 numbers with the trading desk. The Raptor SPEs are
significantly undercapitalized — we're looking at a $544M mark-to-market
loss that needs to be recognized this quarter.

Andy, your LJM vehicles have absorbed about $300M but that's not enough.
Arthur Andersen has signed off on the FAS 140 treatment, but I'm concerned
about disclosure.

We need to decide before the Q3 earnings call on October 16th whether to
restate or take a one-time charge. Ken, you should be the one to decide —
you're the Chairman and this falls under your authority.

Let's meet Thursday in the boardroom at 2pm.

Jeff Skilling
CEO, Enron Corporation""",
    ),
    EmailForExtraction(
        message_id="test-002@enron.com",
        sender="kenneth.lay@enron.com",
        recipients=["all.employees@enron.com"],
        date="2001-10-12",
        subject="Confidence in Enron's Future",
        body="""To all Enron employees,

I want to reassure everyone that Enron is in the strongest financial position
in its history. Our stock, which is currently trading around $33, is an
excellent buying opportunity.

Jeff Skilling, our CEO, and Andy Fastow, our CFO, have worked tirelessly
to build value across our trading, broadband, and international businesses.

Enron's core businesses are fundamentally sound. The Dabhol Power project
in India and our European operations under Rebecca Mark continue to grow.

I personally have been buying shares and remain committed to this company.

Ken Lay
Chairman & CEO, Enron Corporation""",
    ),
]

async def test_extraction():
    settings = get_settings()
    print(f"\n{'='*60}")
    print(f"  Layer10 LLM Extraction Test")
    print(f"  Primary model : {settings.groq_model}")
    print(f"  Provider      : Groq ({settings.groq_base_url})")
    print(f"{'='*60}\n")

    extractor = Extractor()

    for i, email in enumerate(EMAILS, 1):
        print(f"\n{'─'*60}")
        print(f"  Email {i}/{len(EMAILS)}: \"{email.subject}\"")
        print(f"  From: {email.sender}")
        print(f"  To  : {', '.join(email.recipients)}")
        print(f"{'─'*60}")

        result = await extractor.extract_email(email)

        if result is None or not result.is_valid:
            print(f"  ❌ EXTRACTION FAILED")
            if result:
                print(f"     Errors: {result.errors}")
            continue

        ext = result.extraction
        print(f"\n  ✅ Extraction successful")
        print(f"  📦 Entities found : {len(ext.entities)}")
        print(f"  🔗 Claims found   : {len(ext.claims)}")
        print(f"  ⚠️  Evidence dropped: {result.dropped_count}")

        print(f"\n  ENTITIES:")
        for e in ext.entities:
            aliases = f"  [aliases: {', '.join(e.aliases)}]" if e.aliases else ""
            print(f"    • [{e.type.value}] {e.name}{aliases}")
            if e.properties:
                for k, v in e.properties.items():
                    print(f"        {k}: {v}")

        print(f"\n  CLAIMS:")
        for c in ext.claims:
            print(f"    • {c.subject} --[{c.type.value}]--> {c.object}")
            print(f"      confidence={c.confidence:.2f}  valid_from={c.properties.get('valid_from', 'N/A')}")
            excerpt_short = c.evidence_excerpt[:80].replace('\n', ' ')
            print(f"      evidence: \"{excerpt_short}\"")

    print(f"\n{'='*60}")
    print("  EXTRACTION STATS")
    print(f"{'='*60}")
    stats = extractor.stats.as_dict()
    for k, v in stats.items():
        print(f"  {k:<30} {v}")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    asyncio.run(test_extraction())
