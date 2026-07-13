"""
Extract compliance gaps & framework scores from Intelligence turns (PDF spec §3).

Runs after chat completes — failures are logged only; never breaks the chat stream.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Optional

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.permissions import FeatureGate
from app.db.models import (
    ComplianceFramework,
    ComplianceGap,
    ComplianceScoreSnapshot,
    Organization,
    PlanType,
)
from app.services.compliance_gap_helpers import (
    GapSourceContext,
    apply_gap_source_context,
    build_gap_source_context,
)
from app.services.compliance_service import (
    DEFAULT_STARTER_SLUGS,
    FRAMEWORK_CATALOG,
    calculate_ai_score,
    calculate_risk_level,
    ensure_default_frameworks,
    framework_limit_for_plan,
)

logger = logging.getLogger(__name__)

COMPLIANCE_KEYWORDS = re.compile(
    r"\b(hipaa|fwa|compliance|audit|gap|risk|baa|business\s+associate|phi|"
    r"stark|kickback|fraud|waste|abuse|cms|oig|regulatory|safeguard|"
    r"breach|training|documentation|hipaa\s+security|45\s+cfr)\b",
    re.I,
)

EXTRACTION_PROMPT = """You are a healthcare compliance analyst. Read the user's question and the assistant's answer.
Identify concrete compliance GAPS (missing policies, missing BAAs, overdue training, weak controls, etc.)
and estimate per-framework scores (0-100).

Return ONLY valid JSON (no markdown):
{
  "framework_scores": {
    "hipaa": <number 0-100>,
    "fwa": <number 0-100>
  },
  "overall_ai_score": <number 0-100>,
  "risk_level": "low" | "medium" | "high" | "critical",
  "gaps_found": [
    {
      "title": "<short title>",
      "framework_slug": "hipaa" | "fwa" | "stark_law" | "anti_kickback" | "gdpr" | "reporting" | "documentation" | "training",
      "severity": "critical" | "high" | "medium" | "low",
      "description": "<one sentence>",
      "category": "<display name e.g. HIPAA>"
    }
  ],
  "recommendations": ["<action item>"]
}

Rules:
- Only include gaps that are clearly implied or stated in the conversation (max 5).
- If no gaps, return "gaps_found": [].
- framework_scores should include every framework you mention in gaps.
- Be conservative: do not invent violations without basis in the text.
- Assign framework_slug by topic: workforce training, breach/incident response, BAA, PHI access, risk analysis → hipaa; billing, coding, kickbacks, claims audits, FWA → fwa.
- If the document already defines a strong, compliant control (policy, timeline, review cadence), do not report it as a gap — only flag weak, missing, undocumented, or non-compliant controls."""

DOCUMENT_BATCH_EXTRACTION_PROMPT = """You are a Requi Health compliance auditor.
Extract ONLY material gaps that match the ALLOWED TOPICS supplied for this organization.
Do not invent soft improvements or out-of-scope controls.

Return ONLY valid JSON (no markdown):
{
  "framework_scores": { "<slug>": <number 0-100> },
  "gaps_found": [
    {
      "title": "<short title>",
      "framework_slug": "<slug from allowed topics>",
      "severity": "critical" | "high" | "medium",
      "description": "<one sentence with the explicit weak control from the text>",
      "category": "<framework display name>",
      "topic_key": "<one allowed topic_key>"
    }
  ],
  "recommendations": ["<action item>"]
}

Rules:
- One gap maximum per topic_key.
- Prefer precision: if unsure, omit the gap.
- Do not invent gaps without an explicit statement in THIS batch.
- Empty gaps_found is better than invented gaps."""

TOPIC_PROMPT_LINES: dict[str, str] = {
    "workforce_training": "workforce_training (hipaa) — late/infrequent HIPAA training",
    "risk_analysis": "risk_analysis (hipaa) — risk analysis less than annual",
    "breach_notification": "breach_notification (hipaa) — breach notice too slow",
    "baa": "baa (hipaa) — oral/missing written BAA",
    "fwa_audit_sampling": "fwa_audit_sampling (fwa) — claims audit sample too low",
    "sanctions": "sanctions (hipaa) — missing written sanctions policy",
    "session_timeout": "session_timeout (hipaa) — no auto-logoff / long idle timeout",
    "unique_user_ids": "unique_user_ids (hipaa) — shared logins",
    "authentication": "authentication (hipaa) — weak passwords / MFA optional",
    "encryption_at_rest": "encryption_at_rest (hipaa) — encryption at rest not required",
    "email_phi": "email_phi (hipaa) — unencrypted PHI email/messaging",
    "audit_log_retention": "audit_log_retention (hipaa) — audit logs retained too briefly",
    "backup_encryption": "backup_encryption (hipaa) — unencrypted backups",
    "contingency_dr": "contingency_dr (hipaa) — DR/contingency testing not required",
    "telehealth_baa": "telehealth_baa (hipaa) — consumer telehealth apps without BAA",
    "removable_media": "removable_media (hipaa) — unencrypted USB/portable media",
    "vendor_phi_destruction": "vendor_phi_destruction (hipaa) — no PHI return/destruction on vendor exit",
    "exclusion_screening": "exclusion_screening (fwa) — no OIG/GSA exclusion screening",
    "marketing_phi": "marketing_phi (hipaa) — marketing PHI with verbal consent only",
    "ai_governance": "ai_governance (hipaa) — no AI governance committee",
    "phi_disposal": "phi_disposal (hipaa) — PHI/clinical paper in regular trash",
    "stark_self_referral": "stark_self_referral (stark_law) — Stark self-referral / DHS ownership gaps",
    "aks_remuneration": "aks_remuneration (anti_kickback) — kickback/remuneration / missing safe harbor",
    "gdpr_lawful_basis": "gdpr_lawful_basis (gdpr) — missing lawful basis / DPA / transfer controls",
    "mandatory_reporting": "mandatory_reporting (reporting) — missing mandatory/regulatory reporting controls",
}


def _build_document_extraction_prompt(active_framework_slugs: set[str]) -> str:
    """Prompt limited to topics whose framework is active on the org."""
    lines = [
        TOPIC_PROMPT_LINES[key]
        for key, meta in DOCUMENT_GAP_TOPICS.items()
        if meta["framework_slug"] in active_framework_slugs and key in TOPIC_PROMPT_LINES
    ]
    if not lines:
        lines = [
            TOPIC_PROMPT_LINES[key]
            for key, meta in DOCUMENT_GAP_TOPICS.items()
            if meta["framework_slug"] in DEFAULT_STARTER_SLUGS and key in TOPIC_PROMPT_LINES
        ]
    allowed = "\n".join(f"- {line}" for line in lines)
    return (
        f"{DOCUMENT_BATCH_EXTRACTION_PROMPT}\n\n"
        f"ALLOWED TOPICS for this organization:\n{allowed}\n"
    )

LOGICAL_SECTION_SPLIT = re.compile(r"(?=Section \d+ —)", re.I)
CHUNK_SECTION_SPLIT = re.compile(r"(?=\[Section \d+ of \d+\])")

# Document upload gap extraction — fixed defaults (not env-configurable).
DOCUMENT_BATCH_SECTIONS = 4
DOCUMENT_BATCH_MAX_TOKENS = 4096

_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}

# Requi platform in-scope control topics for document gap extraction.
# Only gaps that match one of these topics are persisted.
DOCUMENT_GAP_TOPICS: dict[str, dict[str, Any]] = {
    "session_timeout": {
        "framework_slug": "hipaa",
        "patterns": (
            r"auto[- ]?logoff",
            r"session timeout",
            r"idle.{0,20}(timeout|logoff|lock)",
            r"60[- ]?minutes?.{0,30}(idle|logoff|timeout)",
        ),
    },
    "workforce_training": {
        "framework_slug": "hipaa",
        "patterns": (
            r"workforce.{0,20}train",
            r"hipaa.{0,20}train|train.{0,20}hipaa",
            r"training.{0,40}(every|years|days|infrequent|cadence|frequency)",
            r"(every|frequency).{0,40}training",
            r"every (two|2) years",
            r"90[- ]?days?.{0,40}train",
        ),
    },
    "risk_analysis": {
        "framework_slug": "hipaa",
        "patterns": (
            r"risk (analysis|assessment)",
            r"every (three|3) years",
            r"triennial",
        ),
    },
    "breach_notification": {
        "framework_slug": "hipaa",
        "patterns": (
            r"breach",
            r"90[- ]?days?.{0,40}breach|breach.{0,40}90[- ]?days?",
            r"notification timeline",
        ),
    },
    "baa": {
        "framework_slug": "hipaa",
        "patterns": (
            r"\bbaa\b",
            r"business associate",
            r"oral (baa|agreement)",
            r"informal vendor",
        ),
    },
    "fwa_audit_sampling": {
        "framework_slug": "fwa",
        "patterns": (
            r"\bfwa\b",
            r"audit (rate|sample|sampling)",
            r"0\.5\s*%",
            r"medicare.{0,30}audit",
            r"claims?.{0,20}audit",
        ),
    },
    "sanctions": {
        "framework_slug": "hipaa",
        "patterns": (r"sanction", r"discipline policy", r"written sanctions"),
    },
    "unique_user_ids": {
        "framework_slug": "hipaa",
        "patterns": (
            r"shared.{0,20}logins?",
            r"unique user",
            r"shared.*(id|account|credential|nursing)",
        ),
    },
    "authentication": {
        "framework_slug": "hipaa",
        "patterns": (r"\bmfa\b", r"password", r"6[- ]?char", r"authentication", r"multi[- ]?factor"),
    },
    "encryption_at_rest": {
        "framework_slug": "hipaa",
        "patterns": (
            r"encryption at rest",
            r"encrypt.{0,40}(at rest|storage|not required|remote|device)",
            r"no encryption requirement",
            r"unencrypted.{0,20}(storage|device|disk)",
        ),
    },
    "email_phi": {
        "framework_slug": "hipaa",
        "patterns": (r"email", r"messaging", r"unencrypted.{0,20}phi|phi.{0,20}email"),
    },
    "audit_log_retention": {
        "framework_slug": "hipaa",
        "patterns": (r"audit log", r"log retention", r"90[- ]?days?.{0,20}log|log.{0,20}90[- ]?days?"),
    },
    "backup_encryption": {
        "framework_slug": "hipaa",
        "patterns": (r"backup", r"unencrypted backup"),
    },
    "contingency_dr": {
        "framework_slug": "hipaa",
        "patterns": (r"disaster recovery", r"\bdr test", r"contingency", r"no required dr"),
    },
    "telehealth_baa": {
        "framework_slug": "hipaa",
        "patterns": (r"telehealth", r"zoom", r"facetime", r"consumer app", r"personal (communication|zoom)"),
    },
    "removable_media": {
        "framework_slug": "hipaa",
        "patterns": (r"\busb\b", r"removable media", r"portable (storage|media|drive)"),
    },
    "vendor_phi_destruction": {
        "framework_slug": "hipaa",
        "patterns": (
            r"vendor (termination|exit)",
            r"phi (return|destruction)",
            r"destruction certification",
            r"phi return timeline",
        ),
    },
    "exclusion_screening": {
        "framework_slug": "fwa",
        "patterns": (r"\boig\b", r"\bgsa\b", r"exclusion screen", r"leie", r"sam\.gov"),
    },
    "marketing_phi": {
        "framework_slug": "hipaa",
        "patterns": (r"marketing", r"fundraising", r"verbal consent"),
    },
    "ai_governance": {
        "framework_slug": "hipaa",
        "patterns": (
            r"ai governance",
            r"ai committee",
            r"artificial intelligence",
            r"\bno ai\b",
            r"no.{0,20}ai (governance|committee)",
        ),
    },
    "phi_disposal": {
        "framework_slug": "hipaa",
        "patterns": (
            r"regular trash",
            r"paper phi",
            r"phi.{0,20}dispos",
            r"clinical[- ]adjacent.{0,20}(trash|dispos)",
            r"shred.{0,20}phi|phi.{0,20}shred",
        ),
    },
    # Optional frameworks — only extracted when the org has them active
    "stark_self_referral": {
        "framework_slug": "stark_law",
        "patterns": (
            r"stark",
            r"self[- ]referral",
            r"designated health service",
            r"\bdhs\b",
            r"physician.{0,30}ownership",
        ),
    },
    "aks_remuneration": {
        "framework_slug": "anti_kickback",
        "patterns": (
            r"anti[- ]?kickback",
            r"\baks\b",
            r"remuneration",
            r"kickback",
            r"inducement",
            r"safe harbor",
        ),
    },
    "gdpr_lawful_basis": {
        "framework_slug": "gdpr",
        "patterns": (
            r"\bgdpr\b",
            r"lawful basis",
            r"data subject",
            r"\bdpa\b",
            r"data processing agreement",
            r"cross[- ]border",
        ),
    },
    "mandatory_reporting": {
        "framework_slug": "reporting",
        "patterns": (
            r"mandatory report",
            r"incident report",
            r"cms.{0,20}report",
            r"regulatory reporting",
            r"reportable event",
        ),
    },
}

_OUT_OF_SCOPE_GAP = re.compile(
    r"\b("
    r"irb|institutional review|"
    r"international (data )?transfer|"
    r"policy acknowledgment|"
    r"vendor review (timing|frequency|cadence|process)|"
    r"corrective action plan|"
    r"\bcap\b|"
    r"printed cop|"
    r"administrative forms?|"
    r"low[- ]risk administrative|"
    r"compliance files?|"
    r"record retention|"
    r"quality improvement|"
    r"documentation polish|"
    r"specificity"
    r")\b",
    re.I,
)

_GAP_TITLE_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "without",
        "no",
        "not",
        "lack",
        "lacking",
        "missing",
        "absent",
        "inadequate",
        "insufficient",
        "undefined",
        "unclear",
        "weak",
        "poor",
        "limited",
        "optional",
        "delayed",
        "infrequent",
        "requirement",
        "requirements",
        "policy",
        "policies",
        "process",
        "processes",
        "procedure",
        "procedures",
        "control",
        "controls",
        "explicit",
        "clear",
        "defined",
        "specific",
        "specificity",
        "mention",
        "mentioned",
    }
)


def _should_analyze(user_message: str, assistant_message: str, *, has_documents: bool) -> bool:
    if has_documents:
        return True
    combined = f"{user_message}\n{assistant_message}"
    return bool(COMPLIANCE_KEYWORDS.search(combined))


def _parse_json_from_text(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _mock_analysis(user_message: str, assistant_message: str) -> dict:
    """Deterministic gaps for MOCK_CHAT_STREAM / offline testing."""
    msg = (user_message or "").lower()
    gaps: list[dict] = []
    scores = {"hipaa": 88.0, "fwa": 82.0}

    if "baa" in msg or "business associate" in msg:
        gaps.append(
            {
                "title": "Missing or incomplete Business Associate Agreement",
                "framework_slug": "hipaa",
                "severity": "high",
                "description": "Identified from compliance review question about BAAs.",
                "category": "HIPAA",
            }
        )
        scores["hipaa"] = 72.0
    if "risk assessment" in msg or "risk analysis" in msg:
        gaps.append(
            {
                "title": "HIPAA security risk assessment needs update",
                "framework_slug": "hipaa",
                "severity": "medium",
                "description": "Risk analysis documentation gap discussed in chat.",
                "category": "HIPAA",
            }
        )
        scores["hipaa"] = min(scores["hipaa"], 75.0)
    if "training" in msg and ("overdue" in msg or "annual" in msg or "workforce" in msg):
        gaps.append(
            {
                "title": "Workforce HIPAA training documentation gap",
                "framework_slug": "hipaa",
                "severity": "medium",
                "description": "Training compliance mentioned in user query.",
                "category": "Training",
            }
        )
    if "fwa" in msg or "fraud" in msg or "billing" in msg:
        gaps.append(
            {
                "title": "FWA billing compliance control review needed",
                "framework_slug": "fwa",
                "severity": "medium",
                "description": "FWA-related topic in conversation.",
                "category": "FWA",
            }
        )
        scores["fwa"] = 76.0

    if not gaps and COMPLIANCE_KEYWORDS.search(user_message or ""):
        gaps.append(
            {
                "title": "Follow-up compliance documentation recommended",
                "framework_slug": "hipaa",
                "severity": "low",
                "description": "General compliance topic — confirm evidence in policies.",
                "category": "HIPAA",
            }
        )

    overall = calculate_ai_score({k: float(v) for k, v in scores.items()})
    risk = calculate_risk_level(overall, 0.0)
    return {
        "framework_scores": scores,
        "overall_ai_score": overall,
        "risk_level": risk,
        "gaps_found": gaps[:5],
        "recommendations": [
            "Review open gaps on the Compliance dashboard.",
            "Upload supporting policies to Documents and re-run analysis.",
        ],
    }


def _normalize_gap_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _gap_topic_tokens(title: str, description: str = "") -> frozenset[str]:
    """Significant tokens used to detect near-duplicate gap topics."""
    text = f"{title or ''} {description or ''}".lower()
    tokens = re.findall(r"[a-z0-9]{3,}", text)
    return frozenset(t for t in tokens if t not in _GAP_TITLE_STOPWORDS)


def _is_near_duplicate_gap(a: dict, b: dict) -> bool:
    """True when two gaps describe the same control topic with different wording."""
    ta = _gap_topic_tokens(a.get("title") or "", a.get("description") or "")
    tb = _gap_topic_tokens(b.get("title") or "", b.get("description") or "")
    if not ta or not tb:
        return False
    slug_a = (a.get("framework_slug") or "hipaa").strip().lower()
    slug_b = (b.get("framework_slug") or "hipaa").strip().lower()
    # Same or closely related buckets (training/documentation often map under hipaa)
    related = {slug_a, slug_b}
    if len(related) > 1 and not related <= {"hipaa", "training", "documentation", "breach"}:
        if "fwa" in related and "hipaa" in related:
            return False
    overlap = len(ta & tb)
    if overlap == 0:
        return False
    union = len(ta | tb)
    jaccard = overlap / union if union else 0.0
    if jaccard >= 0.45:
        return True
    # One title is essentially a subset of the other (rephrased duplicate)
    smaller, larger = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if smaller and len(smaller & larger) / len(smaller) >= 0.7 and overlap >= 2:
        return True
    return False


def _prefer_gap(existing: dict, challenger: dict) -> dict:
    """Keep the higher-severity / more specific gap when merging duplicates."""
    se = _SEVERITY_RANK.get((existing.get("severity") or "").lower(), 0)
    sc = _SEVERITY_RANK.get((challenger.get("severity") or "").lower(), 0)
    if sc > se:
        return challenger
    if sc < se:
        return existing
    # Prefer longer, more specific description
    if len((challenger.get("description") or "")) > len((existing.get("description") or "")):
        return challenger
    return existing


def _classify_document_gap_topic(
    item: dict,
    *,
    active_framework_slugs: Optional[set[str]] = None,
) -> Optional[str]:
    """Map a gap to a Requi-allowed topic key, or None if out of platform scope."""
    if not isinstance(item, dict):
        return None

    active = active_framework_slugs
    hinted = (item.get("topic_key") or "").strip().lower()
    if hinted in DOCUMENT_GAP_TOPICS:
        fw = DOCUMENT_GAP_TOPICS[hinted]["framework_slug"]
        if active is None or fw in active:
            return hinted

    text = f"{item.get('title') or ''} {item.get('description') or ''}".lower()

    # Score each allowlisted topic by how many patterns match.
    best_key: Optional[str] = None
    best_hits = 0
    for key, meta in DOCUMENT_GAP_TOPICS.items():
        if active is not None and meta["framework_slug"] not in active:
            continue
        hits = 0
        for pattern in meta["patterns"]:
            if re.search(pattern, text, re.I):
                hits += 1
        if hits > best_hits:
            best_hits = hits
            best_key = key

    if best_hits == 0:
        return None

    # Out-of-scope noise that only weakly touches an allowed keyword
    if _OUT_OF_SCOPE_GAP.search(text) and best_hits < 2:
        if best_key in {"marketing_phi", "baa", "exclusion_screening"}:
            return best_key
        return None

    return best_key


def _normalize_document_gap(
    item: dict,
    *,
    active_framework_slugs: Optional[set[str]] = None,
) -> Optional[dict]:
    """Keep only in-scope material gaps; stamp topic_key + framework slug."""
    if not isinstance(item, dict):
        return None
    title = (item.get("title") or "").strip()
    if not title:
        return None

    severity = (item.get("severity") or "medium").strip().lower()
    if severity == "low":
        return None
    if severity not in ("critical", "high", "medium"):
        severity = "medium"

    topic_key = _classify_document_gap_topic(
        item, active_framework_slugs=active_framework_slugs
    )
    if not topic_key:
        return None

    text = f"{title} {item.get('description') or ''}".lower()
    hard_signal = re.search(
        r"("
        r"\bmissing\b|\bnot required\b|\bnot mandated\b|\boptional\b|\bunencrypted\b|"
        r"\boral\b|\bverbal only\b|\bno mention\b|\bdoes not require\b|\bdoes not mandate\b|"
        r"every 2 years|every two years|every 3 years|every three years|triennial|"
        r"90[- ]?days?|60[- ]?minutes?|shared.{0,20}logins?|without (a )?baa|"
        r"no written|no encryption|encryption.{0,40}not required|"
        r"no auto[- ]?logoff|no oig|no (required )?dr test|no disaster|"
        r"regular trash|6[- ]?char|mfa optional|consumer apps?|"
        r"facetime|personal zoom|usb (drives? )?(permitted|allowed)|0\.5\s*%|half (a )?percent|"
        r"infrequent|too infrequent|less than annual|below (required|policy)|"
        r"\bno ai\b|no.{0,20}ai (governance|committee)|"
        r"stark|self[- ]referral|kickback|remuneration|\bgdpr\b|lawful basis|"
        r"data processing agreement|mandatory report|reportable event"
        r")",
        text,
        re.I,
    )
    # Always require an explicit non-compliant/missing-control signal for document gaps.
    if not hard_signal:
        return None

    framework_slug = DOCUMENT_GAP_TOPICS[topic_key]["framework_slug"]
    if active_framework_slugs is not None and framework_slug not in active_framework_slugs:
        return None
    category = FRAMEWORK_CATALOG.get(framework_slug, framework_slug.upper())

    normalized = dict(item)
    normalized["title"] = title[:500]
    normalized["severity"] = severity
    normalized["framework_slug"] = framework_slug
    normalized["category"] = category
    normalized["topic_key"] = topic_key
    return normalized


def _is_material_document_gap(item: dict) -> bool:
    """Drop soft/out-of-scope findings that inflate document upload gap counts."""
    return _normalize_document_gap(item) is not None


def _split_document_into_batches(text: str) -> list[str]:
    """Split document text into batches for exhaustive gap scanning."""
    text = (text or "").strip()
    if not text:
        return []

    sections: list[str] = []
    if LOGICAL_SECTION_SPLIT.search(text):
        parts = LOGICAL_SECTION_SPLIT.split(text)
        sections = [p.strip() for p in parts if p.strip()]
    elif CHUNK_SECTION_SPLIT.search(text):
        parts = CHUNK_SECTION_SPLIT.split(text)
        sections = [p.strip() for p in parts if p.strip()]
    else:
        sections = [text]

    batch_size = DOCUMENT_BATCH_SECTIONS
    batches: list[str] = []
    for i in range(0, len(sections), batch_size):
        batch = "\n\n".join(sections[i : i + batch_size])
        if batch.strip():
            batches.append(batch)

    if batches:
        return batches

    # Fallback: fixed-size character windows
    window = 35_000
    return [text[j : j + window] for j in range(0, len(text), window)]


def _merge_document_analyses(
    analyses: list[dict],
    *,
    active_framework_slugs: Optional[set[str]] = None,
) -> dict:
    """Merge batched extraction results; one in-scope gap per Requi topic."""
    by_topic: dict[str, dict] = {}
    framework_scores: dict[str, float] = {}
    recommendations: list[str] = []
    active = active_framework_slugs

    for analysis in analyses:
        if not isinstance(analysis, dict):
            continue
        for item in analysis.get("gaps_found") or []:
            normalized = _normalize_document_gap(
                item, active_framework_slugs=active
            )
            if not normalized:
                continue
            topic = normalized["topic_key"]
            existing = by_topic.get(topic)
            if existing is None:
                by_topic[topic] = normalized
            else:
                by_topic[topic] = _prefer_gap(existing, normalized)

        for slug, score in (analysis.get("framework_scores") or {}).items():
            if score is None:
                continue
            try:
                val = float(score)
            except (TypeError, ValueError):
                continue
            if active is not None and slug not in active:
                continue
            framework_scores[slug] = min(framework_scores.get(slug, 100.0), val)

        for rec in analysis.get("recommendations") or []:
            if rec and rec not in recommendations:
                recommendations.append(rec)

    merged_gaps = list(by_topic.values())

    if not framework_scores and merged_gaps:
        for g in merged_gaps:
            slug = g.get("framework_slug") or "hipaa"
            if active is None or slug in active:
                framework_scores[slug] = min(framework_scores.get(slug, 100.0), 55.0)

    overall = calculate_ai_score(framework_scores) if framework_scores else 50.0
    risk_level = calculate_risk_level(overall, 0.0)

    if not recommendations:
        recommendations = [
            "Review open gaps on the Compliance dashboard.",
            "Upload supporting policies to Documents and re-run analysis.",
        ]

    return {
        "framework_scores": framework_scores,
        "overall_ai_score": overall,
        "risk_level": risk_level,
        "gaps_found": merged_gaps,
        "recommendations": recommendations[:10],
    }


async def _call_extraction_model(
    user_message: str,
    assistant_message: str,
    *,
    system_prompt: str = EXTRACTION_PROMPT,
    max_tokens: int = 1200,
) -> Optional[dict]:
    if not settings.openai_api_key:
        return None
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    max_chars = settings.compliance_analysis_max_chars
    user_text = (user_message or "")[:max_chars]
    assistant_text = (assistant_message or "")[:max_chars]
    user_block = (
        f"USER QUESTION:\n{user_text}\n\n"
        f"DOCUMENT OR ASSISTANT CONTENT TO ANALYZE:\n{assistant_text}"
    )
    try:
        resp = await client.chat.completions.create(
            model=settings.compliance_extraction_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_block},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        raw = resp.choices[0].message.content or ""
        return _parse_json_from_text(raw)
    except Exception as exc:
        logger.warning("compliance_ai_extraction_failed: %s", exc)
        return None


async def _call_document_extraction_model(
    user_message: str,
    document_text: str,
    *,
    active_framework_slugs: Optional[set[str]] = None,
) -> Optional[dict]:
    """Scan uploaded document in section batches for org-active framework gaps."""
    batches = _split_document_into_batches(document_text)
    if not batches:
        return None

    active = active_framework_slugs or set(DEFAULT_STARTER_SLUGS)
    system_prompt = _build_document_extraction_prompt(active)

    logger.info(
        "compliance_document_extraction_start batches=%s chars=%s frameworks=%s",
        len(batches),
        len(document_text),
        sorted(active),
    )

    partial_results: list[dict] = []
    for idx, batch_text in enumerate(batches, start=1):
        batch_user = (
            f"{user_message}\n\n"
            f"Extract ONLY material gaps for active frameworks "
            f"{sorted(active)} from DOCUMENT SECTIONS "
            f"(batch {idx} of {len(batches)}). "
            f"Skip adequate controls. One gap per distinct weak control.\n\n"
            f"{batch_text}"
        )
        result = await _call_extraction_model(
            batch_user,
            batch_text,
            system_prompt=system_prompt,
            max_tokens=DOCUMENT_BATCH_MAX_TOKENS,
        )
        if result and isinstance(result.get("gaps_found"), list):
            gap_count = len(result["gaps_found"])
            logger.info(
                "compliance_document_batch_done batch=%s/%s gaps=%s",
                idx,
                len(batches),
                gap_count,
            )
            partial_results.append(result)
        else:
            logger.warning(
                "compliance_document_batch_empty batch=%s/%s",
                idx,
                len(batches),
            )

    if not partial_results:
        return None

    merged = _merge_document_analyses(
        partial_results, active_framework_slugs=active
    )
    logger.info(
        "compliance_document_extraction_done total_gaps=%s",
        len(merged.get("gaps_found") or []),
    )
    return merged


async def get_org_active_framework_slugs(
    db: AsyncSession,
    org: Organization,
) -> set[str]:
    """Return active compliance framework slugs for the organization."""
    await ensure_default_frameworks(db, org.id)
    result = await db.execute(
        select(ComplianceFramework).where(
            ComplianceFramework.organization_id == org.id,
            ComplianceFramework.is_active == True,
        )
    )
    slugs = {fw.slug for fw in result.scalars().all() if fw.slug}
    # Training/Documentation are score categories, not gap extract scopes —
    # map them to hipaa topics via hipaa if that starter is active.
    return slugs or set(DEFAULT_STARTER_SLUGS)


async def _ensure_framework_slot(
    db: AsyncSession,
    org: Organization,
    slug: str,
) -> bool:
    """Return True if framework exists (active or reactivated) or was created."""
    slug = slug.strip().lower()
    if slug not in FRAMEWORK_CATALOG:
        return False

    result = await db.execute(
        select(ComplianceFramework).where(
            ComplianceFramework.organization_id == org.id,
            ComplianceFramework.slug == slug,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        if not existing.is_active:
            plan = org.subscription.plan_type if org.subscription else PlanType.STANDARD
            limit = framework_limit_for_plan(plan)
            count_result = await db.execute(
                select(ComplianceFramework).where(
                    ComplianceFramework.organization_id == org.id,
                    ComplianceFramework.is_active == True,
                )
            )
            if limit is not None and len(count_result.scalars().all()) >= limit:
                return False
            existing.is_active = True
            await db.flush()
        return True

    plan = org.subscription.plan_type if org.subscription else PlanType.STANDARD
    limit = framework_limit_for_plan(plan)
    count_result = await db.execute(
        select(ComplianceFramework).where(
            ComplianceFramework.organization_id == org.id,
            ComplianceFramework.is_active == True,
        )
    )
    active = len(count_result.scalars().all())
    if limit is not None and active >= limit:
        return False

    db.add(
        ComplianceFramework(
            organization_id=org.id,
            slug=slug,
            name=FRAMEWORK_CATALOG[slug],
            score=75.0,
            is_active=True,
        )
    )
    await db.flush()
    return True


async def persist_ai_compliance_analysis(
    db: AsyncSession,
    org: Organization,
    analysis: dict,
    *,
    source_type: str = "chat",
    source_context: Optional[GapSourceContext] = None,
    active_framework_slugs: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Write gaps + snapshot; update framework scores. Returns summary for logging/SSE."""
    await ensure_default_frameworks(db, org.id)
    active = active_framework_slugs
    if active is None and source_type == "document_analysis":
        active = await get_org_active_framework_slugs(db, org)

    framework_scores = analysis.get("framework_scores") or {}
    if isinstance(framework_scores, dict):
        framework_scores = {k: float(v) for k, v in framework_scores.items() if v is not None}
    else:
        framework_scores = {}
    if active is not None:
        framework_scores = {k: v for k, v in framework_scores.items() if k in active}

    overall = float(analysis.get("overall_ai_score") or calculate_ai_score(framework_scores))
    risk_level = str(analysis.get("risk_level") or calculate_risk_level(overall, 0.0))
    gaps_raw = analysis.get("gaps_found") or []
    recommendations = analysis.get("recommendations") or []

    created_gaps = 0
    skipped_gaps = 0

    for item in gaps_raw:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        slug = (item.get("framework_slug") or "hipaa").strip().lower()
        if active is not None and slug not in active:
            skipped_gaps += 1
            continue

        # Document uploads must not auto-create inactive frameworks.
        if source_type == "document_analysis":
            fw_check = await db.execute(
                select(ComplianceFramework).where(
                    ComplianceFramework.organization_id == org.id,
                    ComplianceFramework.slug == slug,
                    ComplianceFramework.is_active == True,
                )
            )
            if not fw_check.scalar_one_or_none():
                skipped_gaps += 1
                continue
        else:
            await _ensure_framework_slot(db, org, slug)

        dup = await db.execute(
            select(ComplianceGap).where(
                ComplianceGap.organization_id == org.id,
                ComplianceGap.framework_slug == slug,
                ComplianceGap.title == title,
                ComplianceGap.status == "open",
            )
        )
        if dup.scalar_one_or_none():
            skipped_gaps += 1
            continue

        severity = item.get("severity") or "medium"
        if severity not in ("critical", "high", "medium", "low"):
            severity = "medium"

        gap = ComplianceGap(
            organization_id=org.id,
            framework_slug=slug,
            title=title[:500],
            description=(item.get("description") or "")[:2000] or None,
            severity=severity,
            category=(item.get("category") or FRAMEWORK_CATALOG.get(slug, slug))[:100],
            status="open",
        )
        apply_gap_source_context(gap, source_context)
        db.add(gap)
        created_gaps += 1

    for slug, score in framework_scores.items():
        fw_result = await db.execute(
            select(ComplianceFramework).where(
                ComplianceFramework.organization_id == org.id,
                ComplianceFramework.slug == slug,
                ComplianceFramework.is_active == True,
            )
        )
        fw = fw_result.scalar_one_or_none()
        if fw:
            fw.score = round(min(100.0, max(0.0, float(score))), 1)

    gaps_for_snapshot = [
        {
            "title": g.get("title"),
            "severity": g.get("severity"),
            "framework_slug": g.get("framework_slug"),
        }
        for g in gaps_raw
        if isinstance(g, dict)
        and g.get("title")
        and (active is None or (g.get("framework_slug") or "hipaa") in active)
    ]

    db.add(
        ComplianceScoreSnapshot(
            organization_id=org.id,
            framework_scores=framework_scores,
            overall_score=overall,
            risk_level=risk_level,
            gaps_found=gaps_for_snapshot,
            recommendations=recommendations if isinstance(recommendations, list) else [],
            source_type=source_type,
        )
    )
    await db.commit()

    return {
        "gaps_created": created_gaps,
        "gaps_skipped_duplicate": skipped_gaps,
        "overall_ai_score": overall,
        "risk_level": risk_level,
        "source_type": source_type,
    }


async def process_intelligence_compliance_update(
    db: AsyncSession,
    org: Organization,
    user_message: str,
    assistant_message: str,
    *,
    source_type: str = "chat",
    has_documents: bool = False,
    use_mock: bool = False,
    source_context: Optional[GapSourceContext] = None,
    conversation_id: Optional[uuid.UUID] = None,
    task_id: Optional[uuid.UUID] = None,
    workflow_id: Optional[uuid.UUID] = None,
    contract_id: Optional[uuid.UUID] = None,
    contract_name: Optional[str] = None,
    document_filename: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    Run compliance extraction after an Intelligence turn.
    Returns summary dict or None if skipped.
    """
    if not getattr(settings, "compliance_ai_extraction_enabled", True):
        return None

    plan = org.subscription.plan_type if org.subscription else PlanType.STANDARD
    if not FeatureGate.has_feature(plan, "compliance"):
        return None

    if not _should_analyze(user_message, assistant_message, has_documents=has_documents):
        return None

    if not (assistant_message or "").strip():
        return None

    if source_context is None:
        source_context = await build_gap_source_context(
            db,
            user_message=user_message,
            source_type=source_type,
            conversation_id=conversation_id,
            task_id=task_id,
            workflow_id=workflow_id,
            contract_id=contract_id,
            contract_name=contract_name,
            document_filename=document_filename,
        )

    if use_mock or settings.mock_chat_stream:
        analysis = _mock_analysis(user_message, assistant_message)
        active_slugs: Optional[set[str]] = None
    elif source_type == "document_analysis":
        active_slugs = await get_org_active_framework_slugs(db, org)
        analysis = await _call_document_extraction_model(
            user_message,
            assistant_message,
            active_framework_slugs=active_slugs,
        )
        if not analysis:
            analysis = await _call_extraction_model(user_message, assistant_message)
        if not analysis:
            analysis = _mock_analysis(user_message, assistant_message)
    else:
        active_slugs = None
        analysis = await _call_extraction_model(user_message, assistant_message)
        if not analysis:
            analysis = _mock_analysis(user_message, assistant_message)

    try:
        return await persist_ai_compliance_analysis(
            db,
            org,
            analysis,
            source_type=source_type,
            source_context=source_context,
            active_framework_slugs=active_slugs,
        )
    except Exception as exc:
        logger.exception("persist_ai_compliance_failed: %s", exc)
        await db.rollback()
        return None
