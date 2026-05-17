"""
Authentication endpoints
"""

import re
import uuid as uuid_lib
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
import bcrypt
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.db.models import (
    Organization,
    PlanType,
    Seat,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)

router = APIRouter()

# OAuth2 scheme
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_v1_prefix}/auth/login")


# Pydantic models
class UserCreate(BaseModel):
    email: EmailStr
    password: str
    first_name: str
    last_name: str
    plan_type: str = "pro"
    organization_name: Optional[str] = None

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("plan_type")
    @classmethod
    def plan_type_valid(cls, v: str) -> str:
        normalized = v.lower().strip()
        if normalized not in ("standard", "pro", "enterprise"):
            raise ValueError("plan_type must be standard, pro, or enterprise")
        return normalized


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenPayload(BaseModel):
    sub: Optional[str] = None
    exp: Optional[datetime] = None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against bcrypt hash"""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


def get_password_hash(password: str) -> str:
    """Hash password with bcrypt (passlib incompatible with bcrypt 4.1+)"""
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")


def create_access_token(user_id: str, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token"""
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    
    to_encode = {"sub": user_id, "exp": expire, "type": "access"}
    encoded_jwt = jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return encoded_jwt


def create_refresh_token(user_id: str) -> str:
    """Create JWT refresh token"""
    expire = datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days)
    to_encode = {"sub": user_id, "exp": expire, "type": "refresh"}
    encoded_jwt = jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return encoded_jwt


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Get current user from JWT token"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        user_id: str = payload.get("sub")
        token_type: str = payload.get("type")
        
        if user_id is None or token_type != "access":
            raise credentials_exception
    
    except JWTError:
        raise credentials_exception
    
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if user is None or not user.is_active:
        raise credentials_exception
    
    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Get current active user"""
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip())
    slug = slug.strip("-") or "org"
    return slug[:90]


async def _ensure_unique_slug(db: AsyncSession, base_slug: str) -> str:
    slug = base_slug
    counter = 0
    while True:
        result = await db.execute(select(Organization).where(Organization.slug == slug))
        if not result.scalar_one_or_none():
            return slug
        counter += 1
        slug = f"{base_slug}-{counter}"[:100]


def _stripe_price_for_plan(plan_type: PlanType) -> str:
    price_map = {
        PlanType.STANDARD: settings.stripe_price_standard,
        PlanType.PRO: settings.stripe_price_pro,
        PlanType.ENTERPRISE: settings.stripe_price_enterprise,
    }
    return price_map[plan_type]


def _trial_subscription_active(subscription: Optional[Subscription]) -> bool:
    if not subscription:
        return False
    if subscription.status != SubscriptionStatus.TRIALING:
        return False
    if subscription.trial_end and subscription.trial_end < datetime.utcnow():
        return False
    return True


def _subscription_payload(subscription: Optional[Subscription]) -> Optional[dict]:
    if not subscription:
        return None
    return {
        "plan": subscription.plan_type.value,
        "status": subscription.status.value,
        "seats": subscription.seat_quantity,
        "trial_start": subscription.trial_start.isoformat() if subscription.trial_start else None,
        "trial_end": subscription.trial_end.isoformat() if subscription.trial_end else None,
        "trial_days": settings.trial_days,
        "is_trial_active": _trial_subscription_active(subscription),
    }


def _org_payload(
    org: Organization,
    seat: Seat,
    subscription: Optional[Subscription] = None,
) -> dict:
    subscription = subscription or org.subscription
    plan_type = subscription.plan_type.value if subscription else "standard"
    return {
        "id": str(org.id),
        "name": org.name,
        "role": seat.role.value,
        "plan": plan_type,
        "subscription_status": subscription.status.value if subscription else None,
        "subscription": _subscription_payload(subscription),
    }


@router.post("/register", response_model=dict)
async def register(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db),
):
    """Register user, organization, admin seat, and trialing subscription."""
    result = await db.execute(select(User).where(User.email == user_data.email))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    plan_type = PlanType(user_data.plan_type)
    limits = settings.get_plan_limits(plan_type.value)
    org_display_name = (
        user_data.organization_name.strip()
        if user_data.organization_name and user_data.organization_name.strip()
        else f"{user_data.first_name} {user_data.last_name}".strip() or "My Organization"
    )
    org_slug = await _ensure_unique_slug(db, _slugify(org_display_name))

    user = User(
        email=user_data.email,
        hashed_password=get_password_hash(user_data.password),
        first_name=user_data.first_name,
        last_name=user_data.last_name,
    )
    db.add(user)
    await db.flush()

    org = Organization(
        name=org_display_name,
        slug=org_slug,
        owner_id=user.id,
    )
    db.add(org)
    await db.flush()

    seat = Seat(
        organization_id=org.id,
        user_id=user.id,
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(seat)

    now = datetime.utcnow()
    trial_end = now + timedelta(days=settings.trial_days)
    local_id = uuid_lib.uuid4().hex

    subscription = Subscription(
        organization_id=org.id,
        plan_type=plan_type,
        stripe_subscription_id=f"local_sub_{local_id}",
        stripe_price_id=_stripe_price_for_plan(plan_type),
        stripe_customer_id=f"local_cust_{local_id}",
        status=SubscriptionStatus.TRIALING,
        seat_quantity=limits["min"],
        current_period_start=now,
        current_period_end=trial_end,
        trial_start=now,
        trial_end=trial_end,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(user)
    await db.refresh(org)
    await db.refresh(subscription)

    return {
        "id": str(user.id),
        "email": user.email,
        "message": "User registered successfully",
        "organization": _org_payload(org, seat, subscription),
    }


@router.post("/login", response_model=dict)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """Login and get tokens with user plan/role info"""
    # Find user
    result = await db.execute(select(User).where(User.email == form_data.username))
    user = result.scalar_one_or_none()
    
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Update last login
    user.last_login = datetime.utcnow()
    await db.commit()
    
    # Create tokens
    access_token = create_access_token(str(user.id))
    refresh_token = create_refresh_token(str(user.id))
    
    # Get user's primary organization with plan/role
    org_data = None
    from sqlalchemy.orm import selectinload
    org_result = await db.execute(
        select(Seat)
        .where(Seat.user_id == user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization).selectinload(Organization.subscription))
    )
    seat = org_result.scalar_one_or_none()
    org_data = None
    if seat and seat.organization:
        org_data = _org_payload(seat.organization, seat)
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "id": str(user.id),
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "organization": org_data,
        }
    }


@router.post("/refresh", response_model=Token)
async def refresh_token(
    refresh_token: str,
    db: AsyncSession = Depends(get_db),
):
    """Refresh access token"""
    try:
        payload = jwt.decode(
            refresh_token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id: str = payload.get("sub")
        token_type: str = payload.get("type")
        
        if user_id is None or token_type != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )
    
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    
    # Verify user exists
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    
    # Create new tokens
    new_access_token = create_access_token(str(user.id))
    new_refresh_token = create_refresh_token(str(user.id))
    
    return {
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
    }


@router.get("/me", response_model=dict)
async def get_me(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user info with plan and role"""
    from sqlalchemy.orm import selectinload
    
    # Get user's organizations with plan/role
    result = await db.execute(
        select(Seat)
        .where(Seat.user_id == current_user.id, Seat.is_active == True)
        .options(selectinload(Seat.organization).selectinload(Organization.subscription))
    )
    seats = result.scalars().all()
    
    organizations = []
    primary_plan = "standard"
    primary_role = "viewer"
    primary_subscription = None

    for seat in seats:
        org = seat.organization
        plan_type = "standard"
        subscription_info = _subscription_payload(org.subscription)

        if org.subscription:
            plan_type = org.subscription.plan_type.value
            primary_plan = plan_type
            primary_role = seat.role.value
            primary_subscription = subscription_info

        organizations.append({
            "id": str(org.id),
            "name": org.name,
            "role": seat.role.value,
            "plan": plan_type,
            "subscription": subscription_info,
        })

    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "first_name": current_user.first_name,
        "last_name": current_user.last_name,
        "plan": primary_plan,
        "role": primary_role,
        "is_admin": primary_role == "admin",
        "subscription": primary_subscription,
        "organizations": organizations,
    }
