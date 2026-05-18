# Requi Health Backend - Deployment Guide

## Prerequisites

- Python 3.11+
- PostgreSQL 15+ with pgvector extension
- Redis 7+
- Stripe account
- Anthropic API key
- OpenAI API key (for embeddings)

## Local Development Setup

### 1. Clone and Setup Environment

```bash
cd requi-backend
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Setup PostgreSQL with pgvector

```bash
# Install pgvector (macOS with Homebrew)
brew install pgvector

# Or use Docker
docker run -d \
  --name requi-postgres \
  -e POSTGRES_USER=requi \
  -e POSTGRES_PASSWORD=requi_password \
  -e POSTGRES_DB=requi_health \
  -p 5432:5432 \
  ankane/pgvector:latest
```

### 3. Setup Redis

```bash
# Using Docker
docker run -d \
  --name requi-redis \
  -p 6379:6379 \
  redis:7-alpine
```

### 4. Configure Environment Variables

```bash
cp .env.example .env
# Edit .env with your API keys and settings
```

### 5. Run Database Migrations

```bash
alembic upgrade head
```

### 6. Start the Application

```bash
# Start API server
uvicorn app.main:app --reload

# Start Celery worker (in another terminal)
celery -A app.tasks.celery_app worker --loglevel=info

# Start Celery beat for scheduled tasks (in another terminal)
celery -A app.tasks.celery_app beat --loglevel=info
```

## Production Deployment

### HTTPS with certbot (optional — uvicorn TLS)

This API is **FastAPI + uvicorn** (not Flask). To use the same Let's Encrypt files as a Node server:

```bash
# certbot (on the host)
sudo certbot certonly --nginx -d requi.io -d www.requi.io
```

`.env` on the server:

```bash
SSL_ENABLED=true
SSL_CERTFILE=/etc/letsencrypt/live/requi.io/fullchain.pem
SSL_KEYFILE=/etc/letsencrypt/live/requi.io/privkey.pem
SSL_PORT=443
APP_ENV=production
PORT=8000
```

Start with HTTPS:

```bash
python scripts/run_uvicorn.py
# or: python -m app.main
```

Docker with certs mounted from the host:

```bash
docker compose -f docker-compose.yml -f docker-compose.ssl.yml up -d api
```

**Recommended for requi.io:** terminate SSL in **Apache or Nginx** on port 443 and proxy to the API on `http://127.0.0.1:8000` (`SSL_ENABLED=false`). That matches your frontend same-origin setup and avoids binding port 443 inside Docker.

### Docker Deployment

```bash
# Build and run with Docker Compose
docker-compose up -d
```

### Kubernetes Deployment

```bash
# Apply Kubernetes manifests
kubectl apply -f k8s/
```

### Environment Variables for Production

```bash
APP_ENV=production
DEBUG=false
SECRET_KEY=<generate-strong-secret>
JWT_SECRET_KEY=<generate-different-secret>

# Database
DATABASE_URL=postgresql://requi:<password>@<host>:5432/requi_health

# Stripe (use live keys for production)
STRIPE_SECRET_KEY=sk_live_...
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...

# API Keys
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

## Stripe Configuration

### 1. Create Products and Prices

```bash
# Create Core Plan Product
curl https://api.stripe.com/v1/products \
  -u sk_test_...: \
  -d name="Requi Core" \
  -d description="Single-user AI compliance intelligence"

# Create Price for Core Plan
curl https://api.stripe.com/v1/prices \
  -u sk_test_...: \
  -d product=<product_id> \
  -d unit_amount=20400 \
  -d currency=usd \
  -d "recurring[interval]"=month

# Repeat for Team ($750) and Enterprise ($2004) plans
```

### 2. Configure Webhook

In Stripe Dashboard:
- Endpoint URL: `https://your-api.com/api/v1/billing/webhook`
- Events to listen for:
  - `customer.subscription.created`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.paid`
  - `invoice.payment_failed`

### 3. Update .env with Price IDs

```bash
STRIPE_PRICE_CORE=price_...
STRIPE_PRICE_TEAM=price_...
STRIPE_PRICE_ENTERPRISE=price_...
```

## API Endpoints

### Authentication
- `POST /api/v1/auth/register` - Register new user
- `POST /api/v1/auth/login` - Login
- `POST /api/v1/auth/refresh` - Refresh token
- `GET /api/v1/auth/me` - Get current user

### Organizations
- `GET /api/v1/organizations` - List organizations
- `POST /api/v1/organizations` - Create organization
- `GET /api/v1/organizations/{id}` - Get organization
- `PATCH /api/v1/organizations/{id}` - Update organization
- `POST /api/v1/organizations/{id}/seats` - Add member

### Billing
- `GET /api/v1/billing/plans` - Get pricing plans
- `POST /api/v1/billing/subscriptions` - Create subscription
- `PATCH /api/v1/billing/subscriptions/{id}` - Update subscription
- `DELETE /api/v1/billing/subscriptions/{id}` - Cancel subscription
- `POST /api/v1/billing/checkout-session` - Create checkout session
- `POST /api/v1/billing/webhook` - Stripe webhook

### AI Q&A
- `POST /api/v1/ai/ask` - Ask compliance question
- `GET /api/v1/ai/conversations` - List conversations
- `POST /api/v1/ai/conversations` - Create conversation
- `GET /api/v1/ai/conversations/{id}` - Get conversation

### Knowledge Management
- `GET /api/v1/knowledge` - List knowledge records
- `GET /api/v1/knowledge/{id}` - Get knowledge record
- `PATCH /api/v1/knowledge/{id}` - Update knowledge
- `POST /api/v1/knowledge/{id}/approve` - Approve knowledge
- `GET /api/v1/knowledge/gaps` - List gap tasks

### Sources (Enterprise)
- `GET /api/v1/sources` - List sources
- `POST /api/v1/sources` - Create source
- `POST /api/v1/sources/{id}/ingest` - Trigger ingestion

### Admin (Enterprise)
- `GET /api/v1/admin/stats` - System statistics
- `GET /api/v1/admin/audit-logs` - Audit logs
- `POST /api/v1/admin/trigger-daily-update` - Trigger daily update

## Example Requests

### Register User
```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "securepassword",
    "first_name": "John",
    "last_name": "Doe"
  }'
```

### Login
```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=user@example.com&password=securepassword"
```

### Create Organization
```bash
curl -X POST http://localhost:8000/api/v1/organizations \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Healthcare Corp",
    "slug": "healthcare-corp"
  }'
```

### Create Subscription
```bash
curl -X POST http://localhost:8000/api/v1/billing/subscriptions?organization_id=<org_id> \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "plan_type": "team",
    "seat_quantity": 5,
    "payment_method_id": "pm_..."
  }'
```

### Ask Question
```bash
curl -X POST http://localhost:8000/api/v1/ai/ask \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What are the HIPAA requirements for data encryption?"
  }'
```

## Monitoring

### Health Check
```bash
curl http://localhost:8000/health
```

### Celery Flower (Task Monitoring)
```bash
celery -A app.tasks.celery_app flower --port=5555
```

## Troubleshooting

### Database Connection Issues
```bash
# Test PostgreSQL connection
psql $DATABASE_URL -c "SELECT 1"

# Check pgvector extension
psql $DATABASE_URL -c "CREATE EXTENSION IF NOT EXISTS vector"
```

### Redis Connection Issues
```bash
# Test Redis connection
redis-cli ping
```

### Celery Worker Issues
```bash
# Check worker status
celery -A app.tasks.celery_app inspect active
celery -A app.tasks.celery_app inspect scheduled
```

## License

Proprietary - Requi Health Inc.
