"""
ML service for gap detection, confidence scoring, and GPT-5.5 integration

This module replaces the previous Claude/Anthropic integration with
OpenAI's GPT-5.5 series for all agent operations.
"""

import json
from typing import List, Optional, Tuple
from uuid import UUID

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import GapTask, GapTaskStatus, KnowledgeRecord, KnowledgeStatus
from app.services.retrieval import RetrievalService

# Initialize OpenAI client for GPT-5.5
openai_client = AsyncOpenAI(api_key=settings.openai_api_key)


# ============================================================
# SYSTEM PROMPTS FOR GPT-5.5 AGENTS
# ============================================================

CONTRACT_AGENT_PROMPT = """You are the Contract Extraction Agent for an enterprise healthcare compliance system.

Your job is to read contract text and extract ONLY:
- Reporting obligations
- Compliance requirements  
- Deadlines
- Frequencies
- Regulatory references (if present)

OUTPUT FORMAT (JSON only):
{
  "requirements": [
    {
      "description": "string",
      "category": "FWA | HIPAA | Reporting | Quality | Claims | General",
      "obligation_type": "reporting | policy | audit | certification | other",
      "frequency": "monthly | quarterly | annual | one-time | null",
      "deadline": "YYYY-MM-DD | null",
      "regulatory_reference": "string | null",
      "source_type": "contract",
      "confidence": "high | medium | low"
    }
  ]
}

RULES:
1. Extract ONLY explicit obligations from text
2. Ignore vague language ("may", "should consider", "as appropriate")
3. If frequency not stated → null
4. If deadline not stated → null
5. Do not infer beyond text
6. Categorize into: FWA, HIPAA, Reporting, Quality, Claims, General
7. Confidence = high if explicit, medium if implied, low if ambiguous

Respond with valid JSON only. No markdown, no explanations outside JSON."""


COMPLIANCE_AGENT_PROMPT = """You are the Compliance Intelligence Agent.

Your job is to normalize extracted requirements into standardized healthcare compliance categories.

KNOWN FRAMEWORKS:
- Federal: CMS, HIPAA (45 CFR), OIG, 42 CFR 438 (Medicaid Managed Care)
- State: Medicaid state plans, state-specific regulations
- Contract: MCP agreements, FQHC contracts, value-based contracts

OUTPUT FORMAT (JSON only):
{
  "normalized_requirements": [
    {
      "original_description": "string",
      "standardized_category": "string",
      "compliance_domain": "Federal | State | Contract",
      "reporting_type": "encounter | quality | financial | administrative | other",
      "priority_level": "critical | high | medium | low",
      "source_type": "contract | federal | state",
      "confidence": "high | medium | low"
    }
  ]
}

PRIORITY RULES:
- critical: Regulatory violation risk if not met
- high: Contractually required, audit risk
- medium: Operational requirement
- low: Best practice

Respond with valid JSON only. No markdown, no explanations outside JSON."""


GAP_AGENT_PROMPT = """You are the Gap & Risk Detection Agent.

Your job is to compare required compliance obligations against the organization's current state.

GAP TYPES:
- missing: requirement exists, no task/document
- partial: requirement exists, incomplete evidence
- untracked: requirement exists, no system linkage
- overdue: deadline passed

OUTPUT FORMAT (JSON only):
{
  "gaps": [
    {
      "requirement_id": "string",
      "gap_type": "missing | partial | untracked | overdue",
      "severity": "critical | high | medium | low",
      "reason": "string explaining why gap exists",
      "recommended_action": "string",
      "confidence": "high | medium | low"
    }
  ],
  "risk_summary": {
    "risk_score": 0-100,
    "risk_level": "critical | high | medium | low",
    "drivers": ["string"],
    "confidence": "high | medium | low"
  }
}

RISK SCORE CALCULATION:
Base: 0
+25 per critical gap
+20 per high gap
+15 per overdue item
+10 per repeated gap (same requirement type)
+5 per partial gap
Cap at 100

SEVERITY RULES:
- critical: Regulatory violation risk
- high: Required but incomplete
- medium: Partially implemented
- low: Improvement opportunity

Respond with valid JSON only. No markdown, no explanations outside JSON."""


EXECUTION_AGENT_PROMPT = """You are the Execution Agent.

Your job is to convert compliance gaps into structured operational tasks.

TASK TYPES:
- REQUIRED_TASK: Must complete for compliance
- DOCUMENT_REQUEST: Need documentation
- REVIEW_TASK: Manual review needed
- INVESTIGATION_TASK: Research required

OUTPUT FORMAT (JSON only):
{
  "tasks": [
    {
      "title": "string (max 100 chars)",
      "description": "string",
      "action_type": "REQUIRED_TASK | DOCUMENT_REQUEST | REVIEW_TASK | INVESTIGATION_TASK",
      "priority": "critical | high | medium | low",
      "category": "FWA | HIPAA | Reporting | Quality | Claims | General",
      "due_date": "YYYY-MM-DD",
      "linked_requirement": "string",
      "suggested_assignee": "string | null",
      "confidence": "high | medium | low"
    }
  ]
}

DUE DATE LOGIC:
- If regulatory deadline exists → use it
- If gap.severity = critical → today + 7 days
- If gap.severity = high → today + 14 days
- If gap.severity = medium → today + 30 days
- Else → today + 60 days

PRIORITY MAPPING:
- critical gap → CRITICAL priority
- high gap → HIGH priority
- medium gap → MEDIUM priority
- low gap → LOW priority

Respond with valid JSON only. No markdown, no explanations outside JSON."""


ORCHESTRATOR_PROMPT = """You are the AI Orchestrator for a healthcare compliance system.

You coordinate intelligence from multiple agents and produce a unified response.

FINAL OUTPUT FORMAT (JSON only):
{
  "answer": "string (human-readable summary, max 200 words)",
  "requirements": [],
  "gaps": [],
  "tasks": [],
  "risk_score": 0-100,
  "scores": {
    "compliance_score": 0-100,
    "risk_score": 0-100,
    "audit_readiness_score": 0-100
  },
  "confidence": "high | medium | low"
}

ANSWER GENERATION RULES:
1. Start with direct answer to query
2. Summarize key findings (2-3 sentences)
3. Mention critical gaps if any
4. List immediate actions
5. Keep under 200 words
6. Be concise and actionable

CONFIDENCE RULES:
- high: All agents returned high confidence
- medium: Some agents returned medium confidence
- low: Any agent returned low confidence or error

Respond with valid JSON only. No markdown, no explanations outside JSON."""


# ============================================================
# GPT-5.5 SERVICE
# ============================================================

class GPT55Service:
    """OpenAI GPT-5.5 service for all agent operations"""

    @staticmethod
    async def call_agent(
        system_prompt: str,
        user_input: dict,
        model: str = None,
        max_tokens: int = None,
        temperature: float = None,
    ) -> dict:
        """Call GPT-5.5 with system prompt and structured input"""
        model = model or settings.openai_model
        max_tokens = max_tokens or settings.openai_max_tokens
        temperature = temperature or settings.openai_temperature

        try:
            response = await openai_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_input)},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content
            parsed = json.loads(content)

            return {
                "result": parsed,
                "model": model,
                "tokens_used": response.usage.total_tokens if response.usage else 0,
                "status": "success",
            }

        except json.JSONDecodeError:
            return {
                "result": {},
                "model": model,
                "tokens_used": 0,
                "status": "error",
                "error": "Failed to parse JSON response",
            }
        except Exception as e:
            return {
                "result": {},
                "model": model,
                "tokens_used": 0,
                "status": "error",
                "error": str(e),
            }

    @staticmethod
    async def generate_answer(
        query: str,
        context: str,
        conversation_history: Optional[List[dict]] = None,
    ) -> dict:
        """Generate compliance answer using GPT-5.5"""
        system_prompt = """You are a healthcare compliance expert AI assistant.

Rules:
1. Only use information from the provided context
2. If the context doesn't contain enough information, say so clearly
3. Always cite your sources using [Source X] format
4. Structure your answers clearly with headings and bullet points
5. Include confidence level (High/Medium/Low) based on source quality
6. Never make up information or hallucinate

Respond in JSON format with fields: answer, confidence, citations"""

        messages = [{"role": "system", "content": system_prompt}]

        # Add conversation history
        if conversation_history:
            for msg in conversation_history[-5:]:
                messages.append({"role": msg["role"], "content": msg["content"]})

        user_message = f"""Context:\n{context}\n\n---\n\nQuestion: {query}\n\nProvide a comprehensive answer based on the context above."""
        messages.append({"role": "user", "content": user_message})

        try:
            response = await openai_client.chat.completions.create(
                model=settings.openai_model,
                messages=messages,
                max_tokens=settings.openai_max_tokens,
                temperature=settings.openai_temperature,
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content
            parsed = json.loads(content)

            return {
                "answer": parsed.get("answer", content),
                "confidence": parsed.get("confidence", "medium").lower(),
                "model": settings.openai_model,
                "tokens_used": response.usage.total_tokens if response.usage else 0,
            }

        except Exception as e:
            return {
                "answer": f"Error generating answer: {str(e)}",
                "confidence": "low",
                "model": settings.openai_model,
                "tokens_used": 0,
            }

    @staticmethod
    async def rewrite_query(query: str) -> str:
        """Rewrite query for better retrieval"""
        try:
            response = await openai_client.chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": "Rewrite healthcare compliance queries to be more specific and searchable. Return only the rewritten query, no explanation.",
                    },
                    {"role": "user", "content": f"Rewrite: {query}"},
                ],
                max_tokens=200,
                temperature=0.1,
            )
            return response.choices[0].message.content.strip()
        except Exception:
            return query

    @staticmethod
    async def generate_proposed_knowledge(
        query: str,
        context: str,
        citations: List[dict],
    ) -> dict:
        """Generate proposed knowledge record from gap resolution"""
        prompt = f"""Based on the following context, create a structured knowledge record:

Query: {query}

Context:\n{context}

Generate JSON with fields: topic, question, answer, takeaways (array)"""

        try:
            response = await openai_client.chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a knowledge management AI. Create structured knowledge records from compliance research. Respond in JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2000,
                temperature=0.1,
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content
            return json.loads(content)

        except Exception as e:
            return {
                "topic": query[:100],
                "question": query,
                "answer": f"Error generating knowledge: {str(e)}",
                "takeaways": [],
            }


# ============================================================
# CONFIDENCE SCORER
# ============================================================

class ConfidenceScorer:
    """Score confidence of retrieved knowledge"""

    @staticmethod
    def calculate_relevance_score(retrieval_score: float) -> float:
        return min(max(retrieval_score, 0.0), 1.0)

    @staticmethod
    def calculate_recency_score(document_date: Optional[str]) -> float:
        if not document_date:
            return 0.5
        return 1.0

    @staticmethod
    def calculate_trust_score(authority_score: float, is_official: bool) -> float:
        base_score = authority_score
        if is_official:
            base_score = min(base_score + 0.2, 1.0)
        return base_score

    @staticmethod
    def calculate_agreement_score(sources: List[dict]) -> float:
        if len(sources) <= 1:
            return 1.0
        return 0.8

    @staticmethod
    def calculate_overall_confidence(
        relevance: float,
        recency: float,
        trust: float,
        agreement: float,
    ) -> float:
        weights = {"relevance": 0.35, "recency": 0.20, "trust": 0.30, "agreement": 0.15}
        confidence = (
            relevance * weights["relevance"]
            + recency * weights["recency"]
            + trust * weights["trust"]
            + agreement * weights["agreement"]
        )
        return round(confidence, 3)


# ============================================================
# GAP DETECTION SERVICE
# ============================================================

class GapDetectionService:
    """Knowledge gap detection service using GPT-5.5"""

    def __init__(self):
        self.confidence_scorer = ConfidenceScorer()
        self.retrieval_service = RetrievalService()
        self.gpt55 = GPT55Service()

    async def detect_gap(
        self,
        db: AsyncSession,
        organization_id: UUID,
        query: str,
        context: str,
        citations: List[dict],
    ) -> Tuple[bool, float, str]:
        """Detect if there's a knowledge gap"""
        if not citations:
            return True, 1.0, "No relevant sources found for this query"

        avg_retrieval_score = sum(c["relevance_score"] for c in citations) / len(citations)
        relevance = self.confidence_scorer.calculate_relevance_score(avg_retrieval_score)
        recency = 1.0
        trust = 0.8
        agreement = self.confidence_scorer.calculate_agreement_score(citations)

        overall_confidence = self.confidence_scorer.calculate_overall_confidence(
            relevance, recency, trust, agreement
        )

        if overall_confidence < settings.gap_detection_threshold:
            gap_description = self._generate_gap_description(
                query, overall_confidence, relevance, recency, trust, agreement
            )
            return True, overall_confidence, gap_description

        return False, overall_confidence, ""

    def _generate_gap_description(
        self, query: str, confidence: float, relevance: float,
        recency: float, trust: float, agreement: float,
    ) -> str:
        issues = []
        if relevance < 0.5:
            issues.append("low relevance of available sources")
        if recency < 0.5:
            issues.append("outdated information")
        if trust < 0.5:
            issues.append("low trust in source authority")
        if agreement < 0.5:
            issues.append("conflicting information across sources")

        if issues:
            return f"Knowledge gap detected due to {', '.join(issues)}"
        return f"Knowledge gap detected (confidence: {confidence:.2f})"

    async def create_gap_task(
        self,
        db: AsyncSession,
        organization_id: UUID,
        original_query: str,
        gap_description: str,
        confidence_score: float,
    ) -> GapTask:
        task = GapTask(
            organization_id=organization_id,
            original_query=original_query,
            gap_description=gap_description,
            confidence_score=confidence_score,
            status=GapTaskStatus.DETECTED,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task


# ============================================================
# AGENT PIPELINE SERVICES
# ============================================================

class ContractAgent:
    """Contract extraction using GPT-5.5"""

    def __init__(self):
        self.gpt55 = GPT55Service()

    async def extract(self, document_text: str) -> dict:
        """Extract requirements from contract text"""
        result = await self.gpt55.call_agent(
            system_prompt=CONTRACT_AGENT_PROMPT,
            user_input={"contract_text": document_text},
        )
        return result["result"]


class ComplianceAgent:
    """Compliance normalization using GPT-5.5"""

    def __init__(self):
        self.gpt55 = GPT55Service()

    async def normalize(self, requirements: list, org_context: dict) -> dict:
        """Normalize requirements to compliance framework"""
        result = await self.gpt55.call_agent(
            system_prompt=COMPLIANCE_AGENT_PROMPT,
            user_input={
                "requirements": requirements,
                "organization_context": org_context,
            },
        )
        return result["result"]


class GapRiskAgent:
    """Gap and risk detection using GPT-5.5"""

    def __init__(self):
        self.gpt55 = GPT55Service()

    async def analyze(self, requirements: list, org_state: dict) -> dict:
        """Detect gaps and calculate risk"""
        result = await self.gpt55.call_agent(
            system_prompt=GAP_AGENT_PROMPT,
            user_input={
                "requirements": requirements,
                "organization_state": org_state,
            },
        )
        return result["result"]


class ExecutionAgent:
    """Task generation using GPT-5.5"""

    def __init__(self):
        self.gpt55 = GPT55Service()

    async def generate_tasks(self, gaps: list, org_context: dict) -> dict:
        """Convert gaps into actionable tasks"""
        result = await self.gpt55.call_agent(
            system_prompt=EXECUTION_AGENT_PROMPT,
            user_input={
                "gaps": gaps,
                "organization_context": org_context,
            },
        )
        return result["result"]


# ============================================================
# ORCHESTRATOR SERVICE
# ============================================================

class OrchestratorService:
    """Main orchestrator controlling the GPT-5.5 agent pipeline"""

    def __init__(self):
        self.contract_agent = ContractAgent()
        self.compliance_agent = ComplianceAgent()
        self.gap_agent = GapRiskAgent()
        self.execution_agent = ExecutionAgent()
        self.gpt55 = GPT55Service()

    async def run_pipeline(
        self,
        db: AsyncSession,
        organization_id: UUID,
        query: Optional[str] = None,
        document_text: Optional[str] = None,
        org_context: dict = None,
    ) -> dict:
        """Execute full agent pipeline"""
        requirements = []

        # Step 1: Contract extraction (if document provided)
        if document_text:
            contract_output = await self.contract_agent.extract(document_text)
            requirements = contract_output.get("requirements", [])

        # Step 2: Compliance normalization
        if requirements:
            compliance_output = await self.compliance_agent.normalize(
                requirements, org_context or {}
            )
            requirements = compliance_output.get("normalized_requirements", requirements)

        # Step 3: Load organization state
        org_state = org_context or {"documents": [], "tasks": [], "deadlines": []}

        # Step 4: Gap detection
        gap_output = await self.gap_agent.analyze(requirements, org_state)
        gaps = gap_output.get("gaps", [])
        risk_summary = gap_output.get("risk_summary", {"risk_score": 0, "risk_level": "low"})

        # Step 5: Task generation
        task_output = await self.execution_agent.generate_tasks(gaps, org_state)
        tasks = task_output.get("tasks", [])

        # Step 6: Assemble final response via GPT-5.5
        final_response = await self.gpt55.call_agent(
            system_prompt=ORCHESTRATOR_PROMPT,
            user_input={
                "query": query or "Analyze compliance status",
                "requirements": requirements,
                "gaps": gaps,
                "tasks": tasks,
                "risk_score": risk_summary.get("risk_score", 0),
            },
        )

        result = final_response["result"]
        return {
            "answer": result.get("answer", ""),
            "requirements": requirements,
            "gaps": gaps,
            "tasks": tasks,
            "risk_score": risk_summary.get("risk_score", 0),
            "risk_level": risk_summary.get("risk_level", "low"),
            "scores": result.get("scores", {
                "compliance_score": 0,
                "risk_score": risk_summary.get("risk_score", 0),
                "audit_readiness_score": 0,
            }),
            "confidence": result.get("confidence", "medium"),
            "tokens_used": final_response.get("tokens_used", 0),
        }


# ============================================================
# LEGACY ML SERVICE (for backward compatibility)
# ============================================================

class MLService:
    """Unified ML service interface using GPT-5.5"""

    def __init__(self):
        self.gpt55 = GPT55Service()
        self.gap_detection = GapDetectionService()
        self.retrieval = RetrievalService()
        self.orchestrator = OrchestratorService()

    async def answer_query(
        self,
        db: AsyncSession,
        organization_id: UUID,
        query: str,
        conversation_history: Optional[List[dict]] = None,
    ) -> dict:
        """Full pipeline: retrieve context, generate answer, detect gaps"""
        rewritten_query = await self.gpt55.rewrite_query(query)

        context, citations = await self.retrieval.get_context_for_query(
            db, organization_id, rewritten_query
        )

        is_gap, confidence, gap_description = await self.gap_detection.detect_gap(
            db, organization_id, query, context, citations
        )

        answer_result = await self.gpt55.generate_answer(
            query, context, conversation_history
        )

        gap_task_id = None
        if is_gap:
            gap_task = await self.gap_detection.create_gap_task(
                db, organization_id, query, gap_description, confidence
            )
            gap_task_id = str(gap_task.id)

        return {
            "query": query,
            "rewritten_query": rewritten_query,
            "answer": answer_result["answer"],
            "confidence": answer_result["confidence"],
            "citations": citations,
            "knowledge_gap_detected": is_gap,
            "gap_confidence": confidence,
            "gap_description": gap_description if is_gap else None,
            "gap_task_id": gap_task_id,
            "tokens_used": answer_result["tokens_used"],
        }

    async def run_full_pipeline(
        self,
        db: AsyncSession,
        organization_id: UUID,
        query: str = None,
        document_text: str = None,
        org_context: dict = None,
    ) -> dict:
        """Run complete agent pipeline"""
        return await self.orchestrator.run_pipeline(
            db=db,
            organization_id=organization_id,
            query=query,
            document_text=document_text,
            org_context=org_context,
        )
