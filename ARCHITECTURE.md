# Requi Health Backend Architecture

## System Overview

Production-grade Healthcare Compliance AI Platform with:
- ML-driven knowledge gap detection & resolution
- Controlled knowledge ingestion pipeline
- Daily self-updating intelligence (24h cycle)
- Multi-tenant SaaS with Stripe billing
- Seat-based pricing with feature gating

## Tech Stack

| Component | Technology |
|-----------|------------|
| API Framework | FastAPI |
| Database | PostgreSQL 15+ with pgvector |
| ORM | SQLAlchemy 2.0 |
| Migrations | Alembic |
| Cache/Queue | Redis |
| Background Jobs | Celery + APScheduler |
| LLM | Anthropic Claude |
| Embeddings | OpenAI / HuggingFace |
| Billing | Stripe |
| Auth | JWT (python-jose) |

## Pricing Tiers

| Plan | Price | Seats | Features |
|------|-------|-------|----------|
| CORE | $204/mo | 1 | AI Q&A only, no persistence |
| TEAM | $750/mo | 2+ | Gap detection, shared KB |
| ENTERPRISE | $2,004/mo | 5+ | Full ML, automation, admin |

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              API LAYER (FastAPI)                            │
├─────────────────────────────────────────────────────────────────────────────┤
│  Auth Router │ Billing Router │ AI Router │ Knowledge Router │ Admin Router │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SERVICE LAYER                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│  AuthService │ BillingService │ RetrievalService │ MLService │ AdminService │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
┌─────────────────────────────────────────────────────────────────────────────┐
│                           BACKGROUND WORKERS (Celery)                       │
├─────────────────────────────────────────────────────────────────────────────┤
│  ingestion_worker │ gap_resolution_worker │ daily_update_scheduler         │
│  knowledge_revalidation_worker │ embedding_worker                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
┌─────────────────────────────────────────────────────────────────────────────┐
│                           DATA LAYER                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  PostgreSQL (pgvector) │ Redis (cache/queue) │ S3 (documents)              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Knowledge Pipeline Flow

```
Source Registration → Document Ingestion → Chunking → Embeddings → Vector Store
                                                           │
User Query → Query Rewriting → Hybrid Retrieval → Reranking → Context Assembly
                                                           │
                                               Claude API → Structured Answer
                                                           │
                                               Gap Detection (if low confidence)
                                                           │
                                               Gap Resolution Pipeline
                                                           │
                                               Knowledge Record Creation
```

## Daily Self-Update Job

```
Every 24 hours:
1. Fetch all approved sources
2. Re-ingest documents (check for updates)
3. Revalidate existing knowledge records
4. Flag stale entries (>30 days)
5. Create review tasks for flagged items
6. Update embeddings if content changed
7. Generate audit log
```

## Feature Gating Matrix

| Feature | CORE | TEAM | ENTERPRISE |
|---------|------|------|------------|
| AI Q&A | ✅ | ✅ | ✅ |
| Knowledge Storage | ❌ | ✅ | ✅ |
| Gap Detection | ❌ | ✅ | ✅ |
| Gap Resolution | ❌ | ✅ | ✅ |
| Team Collaboration | ❌ | ✅ | ✅ |
| Daily Auto-Update | ❌ | ❌ | ✅ |
| Admin Dashboard | ❌ | ❌ | ✅ |
| Custom Sources | ❌ | ❌ | ✅ |
| Audit Logs | ❌ | ❌ | ✅ |
| Priority Processing | ❌ | ❌ | ✅ |

## Database Schema

See `app/db/models.py` for full schema.

Key tables:
- `organizations` - Multi-tenant root
- `users` - User accounts with role
- `subscriptions` - Stripe subscription data
- `seats` - Seat assignments
- `sources` - Approved knowledge sources
- `documents` - Ingested documents
- `document_chunks` - Chunked content with embeddings
- `knowledge_records` - Canonical knowledge
- `gap_tasks` - Knowledge gap detection tasks
- `audit_logs` - Compliance audit trail
