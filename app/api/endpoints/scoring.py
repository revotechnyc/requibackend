"""
Scoring Engine API — v2.1
Compliance Score, Risk Score, Audit Readiness Score
Enterprise: Team-level aggregated scoring
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_user
from app.core.permissions import FeatureGate
from app.db.database import get_db
from app.db.models import Organization, PlanType, Seat

router = APIRouter()


# ============== Scoring Models ==============

class ScoreInput(BaseModel):
    framework_id: str
    # Domain weights
    documentation_weight: float = 0.25
    process_weight: float = 0.30
    training_weight: float = 0.20
    audit_weight: float = 0.25


# ============== In-memory data (replace with DB queries in production) ==============
# PLACEHOLDER: These should query actual domain data in production
FAKE_DOMAIN_DATA: Dict[str, dict] = {}


# ============== Scoring Formulas ==============

def calculate_compliance_score(domain_data: dict) -> dict:
    """
    Compliance Score = (Documentation Completeness x 0.25) +
                       (Process Adherence x 0.30) +
                       (Training Effectiveness x 0.20) +
                       (Audit Preparedness x 0.25)
    Range: 0-100
    """
    doc = domain_data.get("documentation_completeness", 80)
    proc = domain_data.get("process_adherence", 75)
    train = domain_data.get("training_effectiveness", 70)
    audit = domain_data.get("audit_preparedness", 65)

    score = (doc * 0.25) + (proc * 0.30) + (train * 0.20) + (audit * 0.25)
    score = round(min(100, max(0, score)), 1)

    if score >= 90:
        interpretation = "Exemplary compliance posture. Fully prepared for audit."
        status = "exemplary"
    elif score >= 75:
        interpretation = "Good compliance foundation. Address medium-risk gaps for improvement."
        status = "good"
    elif score >= 60:
        interpretation = "Moderate compliance. Immediate action required on high-risk gaps."
        status = "moderate"
    else:
        interpretation = "Significant compliance deficiencies. Critical action required."
        status = "critical"

    return {
        "score": score,
        "status": status,
        "interpretation": interpretation,
        "components": {
            "documentation_completeness": {"score": doc, "weight": 0.25, "weighted": round(doc * 0.25, 2)},
            "process_adherence": {"score": proc, "weight": 0.30, "weighted": round(proc * 0.30, 2)},
            "training_effectiveness": {"score": train, "weight": 0.20, "weighted": round(train * 0.20, 2)},
            "audit_preparedness": {"score": audit, "weight": 0.25, "weighted": round(audit * 0.25, 2)},
        },
    }


def calculate_risk_score(domain_data: dict) -> dict:
    """
    Risk Score = Sum((Control_Weakness_Severity x Likelihood_of_Exploit) /
                     (Mitigation_Strength x 100)) / Number_of_Controls x 100
    Range: 0-100 (lower is better)
    """
    controls = domain_data.get("controls", [])
    if not controls:
        # Default: 3 sample controls
        controls = [
            {"severity": 3, "likelihood": 0.7, "mitigation": 0.6},
            {"severity": 2, "likelihood": 0.5, "mitigation": 0.8},
            {"severity": 4, "likelihood": 0.8, "mitigation": 0.4},
        ]

    total = 0
    control_scores = []
    for c in controls:
        numerator = c["severity"] * c["likelihood"]
        denominator = c["mitigation"] * 100
        cs = (numerator / max(denominator, 0.01)) * 100  # avoid div/0
        total += cs
        control_scores.append({
            "severity": c["severity"],
            "likelihood": c["likelihood"],
            "mitigation": c["mitigation"],
            "partial_score": round(cs, 2),
        })

    score = round(total / len(controls), 1)
    score = min(100, max(0, score))

    if score < 30:
        interpretation = "Low risk. Strong controls and effective mitigation."
        status = "low"
    elif score < 60:
        interpretation = "Medium risk. Some control weaknesses require attention."
        status = "medium"
    else:
        interpretation = "High risk. Critical vulnerabilities require immediate remediation."
        status = "high"

    return {
        "score": score,
        "status": status,
        "interpretation": interpretation,
        "controls_analyzed": len(control_scores),
        "control_details": control_scores,
    }


def calculate_audit_readiness_score(domain_data: dict) -> dict:
    """
    Audit Readiness Score = Sum(Evidence_Strength x 0.30 +
                                 Process_Documentation_Quality x 0.25 +
                                 Historical_Audit_Performance x 0.20 +
                                 Staff_Preparedness x 0.25)
    Range: 0-100
    """
    evidence = domain_data.get("evidence_strength", 70)
    doc_quality = domain_data.get("process_documentation_quality", 75)
    historical = domain_data.get("historical_audit_performance", 80)
    staff = domain_data.get("staff_preparedness", 65)

    score = (evidence * 0.30) + (doc_quality * 0.25) + (historical * 0.20) + (staff * 0.25)
    score = round(min(100, max(0, score)), 1)

    if score >= 85:
        interpretation = "Audit-ready. Comprehensive documentation and strong evidence."
        status = "ready"
    elif score >= 70:
        interpretation = "Near audit-ready. Minor gaps in documentation or evidence."
        status = "near_ready"
    elif score >= 50:
        interpretation = "Partially ready. Significant documentation gaps exist."
        status = "partial"
    else:
        interpretation = "Not audit-ready. Major deficiencies must be addressed."
        status = "not_ready"

    return {
        "score": score,
        "status": status,
        "interpretation": interpretation,
        "components": {
            "evidence_strength": {"score": evidence, "weight": 0.30, "weighted": round(evidence * 0.30, 2)},
            "process_documentation_quality": {"score": doc_quality, "weight": 0.25, "weighted": round(doc_quality * 0.25, 2)},
            "historical_audit_performance": {"score": historical, "weight": 0.20, "weighted": round(historical * 0.20, 2)},
            "staff_preparedness": {"score": staff, "weight": 0.25, "weighted": round(staff * 0.25, 2)},
        },
    }


def generate_recommendations(scores: dict) -> List[str]:
    """Generate actionable recommendations based on score analysis."""
    recs = []
    cs = scores.get("compliance", {})
    rs = scores.get("risk", {})
    ars = scores.get("audit_readiness", {})

    if cs.get("score", 0) < 75:
        recs.append("Priority: Improve documentation completeness — multiple gaps detected.")
    if rs.get("score", 0) > 50:
        recs.append("Priority: Strengthen control mitigation — high-risk vulnerabilities found.")
    if ars.get("score", 0) < 70:
        recs.append("Priority: Enhance evidence collection — audit readiness below threshold.")
    if cs.get("score", 0) >= 75 and rs.get("score", 0) <= 50 and ars.get("score", 0) >= 70:
        recs.append("Strong overall posture. Maintain current compliance practices.")

    return recs if recs else ["No critical recommendations at this time."]


# ============== Helpers ==============

async def _get_workspace(user, db: AsyncSession) -> Organization:
    result = await db.execute(
        select(Seat).where(Seat.user_id == user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization))
    )
    seat = result.scalar_one_or_none()
    if not seat:
        raise HTTPException(status_code=403, detail="No active workspace")
    return seat.organization


# ============== ENDPOINTS ==============

@router.get("/", response_model=dict)
async def get_scores(
    framework_id: Optional[str] = None,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /v1/scoring
    Retrieve all scores (Compliance, Risk, Audit Readiness) for the workspace.
    """
    org = await _get_workspace(current_user, db)

    # PLACEHOLDER: In production, query actual domain data
    # For now, use seeded demo data
    domain_data = FAKE_DOMAIN_DATA.get(str(org.id), {
        "documentation_completeness": 82,
        "process_adherence": 78,
        "training_effectiveness": 71,
        "audit_preparedness": 68,
        "controls": [
            {"severity": 3, "likelihood": 0.6, "mitigation": 0.7},
            {"severity": 2, "likelihood": 0.4, "mitigation": 0.85},
            {"severity": 4, "likelihood": 0.7, "mitigation": 0.5},
            {"severity": 1, "likelihood": 0.3, "mitigation": 0.9},
            {"severity": 3, "likelihood": 0.5, "mitigation": 0.6},
        ],
        "evidence_strength": 74,
        "process_documentation_quality": 79,
        "historical_audit_performance": 83,
        "staff_preparedness": 67,
    })

    compliance = calculate_compliance_score(domain_data)
    risk = calculate_risk_score(domain_data)
    audit_readiness = calculate_audit_readiness_score(domain_data)

    scores = {
        "compliance": compliance,
        "risk": risk,
        "audit_readiness": audit_readiness,
    }

    return {
        "workspace_id": str(org.id),
        "calculated_at": datetime.utcnow().isoformat(),
        "scores": scores,
        "recommendations": generate_recommendations(scores),
        "note": "Scores calculated from domain data. DevOps: Connect live data sources for real-time scoring.",
    }


@router.get("/compliance", response_model=dict)
async def get_compliance_score(
    framework_id: Optional[str] = None,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """GET /v1/scoring/compliance — Compliance score only."""
    org = await _get_workspace(current_user, db)
    domain_data = FAKE_DOMAIN_DATA.get(str(org.id), {
        "documentation_completeness": 82,
        "process_adherence": 78,
        "training_effectiveness": 71,
        "audit_preparedness": 68,
    })
    return {
        "score": calculate_compliance_score(domain_data),
        "workspace_id": str(org.id),
        "calculated_at": datetime.utcnow().isoformat(),
    }


@router.get("/risk", response_model=dict)
async def get_risk_score(
    framework_id: Optional[str] = None,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """GET /v1/scoring/risk — Risk score only."""
    org = await _get_workspace(current_user, db)
    domain_data = FAKE_DOMAIN_DATA.get(str(org.id), {
        "controls": [
            {"severity": 3, "likelihood": 0.6, "mitigation": 0.7},
            {"severity": 2, "likelihood": 0.4, "mitigation": 0.85},
            {"severity": 4, "likelihood": 0.7, "mitigation": 0.5},
        ],
    })
    return {
        "score": calculate_risk_score(domain_data),
        "workspace_id": str(org.id),
        "calculated_at": datetime.utcnow().isoformat(),
    }


@router.get("/audit-readiness", response_model=dict)
async def get_audit_readiness_score(
    framework_id: Optional[str] = None,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """GET /v1/scoring/audit-readiness — Audit readiness score only."""
    org = await _get_workspace(current_user, db)
    domain_data = FAKE_DOMAIN_DATA.get(str(org.id), {
        "evidence_strength": 74,
        "process_documentation_quality": 79,
        "historical_audit_performance": 83,
        "staff_preparedness": 67,
    })
    return {
        "score": calculate_audit_readiness_score(domain_data),
        "workspace_id": str(org.id),
        "calculated_at": datetime.utcnow().isoformat(),
    }


@router.get("/team", response_model=dict)
async def get_team_scores(
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /v1/scoring/team
    Enterprise: Aggregated team-level compliance scores.
    """
    org = await _get_workspace(current_user, db)
    result = await db.execute(select(Seat).where(Seat.user_id == current_user.id, Seat.is_active == True))
    seat = result.scalar_one()

    # Check Enterprise plan
    if not org.subscription or org.subscription.plan_type != PlanType.ENTERPRISE:
        raise HTTPException(status_code=403, detail="Team scoring requires Enterprise plan")

    # PLACEHOLDER: Aggregate per-user scores across team
    # DevOps: Implement actual team aggregation with per-user score queries
    team_scores = {
        "team_average_compliance": 76.5,
        "team_average_risk": 42.3,
        "team_average_audit_readiness": 71.8,
        "highest_performer": {"user_id": "placeholder", "compliance_score": 91.2},
        "lowest_performer": {"user_id": "placeholder", "compliance_score": 62.1},
        "members_scored": 3,  # PLACEHOLDER
    }

    return {
        "workspace_id": str(org.id),
        "team_scores": team_scores,
        "calculated_at": datetime.utcnow().isoformat(),
        "note": "DevOps: Implement per-user score aggregation for live team scoring.",
    }
