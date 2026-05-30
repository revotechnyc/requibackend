"""Email delivery service for REQUI notifications.
Configure SMTP via requi-backend/.env (SMTP_SERVER, SMTP_USER, SMTP_PASSWORD, …).
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from jinja2 import Template

from app.core.config import settings

logger = logging.getLogger(__name__)


# ============================================================
# EMAIL TEMPLATES (HTML)
# ============================================================

EMAIL_BASE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ subject }}</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f7; margin: 0; padding: 0; }
        .container { max-width: 600px; margin: 40px auto; background: #ffffff; border-radius: 20px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.06); }
        .header { background: linear-gradient(135deg, #7C6FCC 0%, #9B8FD4 100%); padding: 40px 32px; text-align: center; }
        .header h1 { color: #ffffff; font-size: 22px; font-weight: 600; margin: 0; letter-spacing: -0.5px; }
        .header p { color: rgba(255,255,255,0.8); font-size: 13px; margin: 8px 0 0; }
        .content { padding: 32px; }
        .title { font-size: 18px; font-weight: 600; color: #1d1d1f; margin-bottom: 12px; line-height: 1.4; }
        .message { font-size: 14px; color: #6e6e73; line-height: 1.7; margin-bottom: 24px; }
        .cta-button { display: inline-block; background: #1d1d1f; color: #ffffff; text-decoration: none; padding: 14px 28px; border-radius: 12px; font-size: 14px; font-weight: 500; transition: all 0.2s; }
        .cta-button:hover { background: #333; }
        .footer { padding: 24px 32px; text-align: center; border-top: 1px solid #f0f0f2; }
        .footer p { font-size: 12px; color: #86868b; margin: 4px 0; }
        .footer a { color: #7C6FCC; text-decoration: none; }
        .divider { height: 1px; background: #f0f0f2; margin: 24px 0; }
        .badge { display: inline-block; background: #f5f5f7; padding: 6px 12px; border-radius: 8px; font-size: 12px; color: #6e6e73; margin-bottom: 16px; }
        .progress-bar { width: 100%; height: 6px; background: #f0f0f2; border-radius: 3px; margin: 16px 0; overflow: hidden; }
        .progress-fill { height: 100%; background: linear-gradient(90deg, #7C6FCC, #9B8FD4); border-radius: 3px; transition: width 0.3s; }
        .meta-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 20px 0; }
        .meta-item { background: #fbfbfd; padding: 12px 16px; border-radius: 12px; }
        .meta-label { font-size: 11px; color: #86868b; text-transform: uppercase; letter-spacing: 0.5px; }
        .meta-value { font-size: 14px; color: #1d1d1f; font-weight: 500; margin-top: 4px; }
        @media (prefers-color-scheme: dark) {
            body { background: #1c1c1e; }
            .container { background: #2c2c2e; box-shadow: 0 4px 24px rgba(0,0,0,0.3); }
            .title { color: #f5f5f7; }
            .message { color: #98989d; }
            .meta-item { background: #3a3a3c; }
            .meta-value { color: #f5f5f7; }
            .footer { border-color: #38383a; }
            .footer p { color: #98989d; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>REQUI</h1>
            <p>AI-Powered Compliance Intelligence</p>
        </div>
        <div class="content">
            {% if badge %}<span class="badge">{{ badge }}</span>{% endif %}
            <h2 class="title">{{ title }}</h2>
            <p class="message">{{ message }}</p>
            {% if progress_used is defined %}
            <div class="progress-bar"><div class="progress-fill" style="width: {{ progress_pct }}%"></div></div>
            <p style="font-size: 12px; color: #86868b; text-align: center;">{{ progress_used }} / {{ progress_total }} used</p>
            {% endif %}
            {% if cta_link and cta_label %}
            <div style="text-align: center; margin-top: 24px;">
                <a href="{{ cta_link }}" class="cta-button">{{ cta_label }}</a>
            </div>
            {% endif %}
        </div>
        <div class="footer">
            <p>Sent by REQUI Health</p>
            <p>team@requi.io &middot; <a href="https://requi.io">requi.io</a></p>
            <p style="font-size: 11px; color: #c7c7cc; margin-top: 12px;">If you didn't expect this email, you can <a href="#">unsubscribe</a>.</p>
        </div>
    </div>
</body>
</html>
"""


class EmailService:
    """Email delivery service using Gmail API or SMTP."""

    def __init__(
        self,
        sender_email: str = "team@requi.io",
        sender_name: str = "REQUI Health",
        gmail_api_key: Optional[str] = None,
        smtp_host: Optional[str] = None,
        smtp_port: int = 587,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
    ):
        self.sender_email = sender_email
        self.sender_name = sender_name
        self.gmail_api_key = gmail_api_key
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self._base_template = Template(EMAIL_BASE_TEMPLATE)

    async def send(
        self,
        to_email: str,
        subject: str,
        title: str,
        message: str,
        cta_link: Optional[str] = None,
        cta_label: Optional[str] = None,
        badge: Optional[str] = None,
        progress_used: Optional[int] = None,
        progress_total: Optional[int] = None,
    ) -> bool:
        """Send a premium HTML email."""
        progress_pct = 0
        if progress_used is not None and progress_total:
            progress_pct = min(100, int((progress_used / progress_total) * 100))

        html_body = self._base_template.render(
            subject=subject,
            title=title,
            message=message,
            cta_link=cta_link,
            cta_label=cta_label,
            badge=badge,
            progress_used=progress_used,
            progress_total=progress_total,
            progress_pct=progress_pct,
        )

        if self.gmail_api_key:
            return await self._send_via_gmail_api(to_email, subject, html_body)
        elif self.smtp_host:
            return await self._send_via_smtp(to_email, subject, html_body)
        else:
            logger.info(
                "Email not sent (SMTP not configured): to=%s subject=%s",
                to_email,
                subject,
            )
            return True

    async def _send_via_gmail_api(self, to_email: str, subject: str, html_body: str) -> bool:
        """Send via Gmail API (Gmail API Key or OAuth)."""
        import aiohttp

        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
        headers = {"Authorization": f"Bearer {self.gmail_api_key}", "Content-Type": "application/json"}

        import base64
        msg = MIMEMultipart("alternative")
        msg["To"] = to_email
        msg["From"] = f"{self.sender_name} <{self.sender_email}>"
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload = {"raw": raw}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                return resp.status == 200

    async def _send_via_smtp(self, to_email: str, subject: str, html_body: str) -> bool:
        """Send via SMTP (Gmail, SendGrid, AWS SES, etc.)."""
        try:
            import aiosmtplib
        except ImportError:
            logger.error(
                "aiosmtplib is not installed; rebuild the API image with requirements-docker.txt"
            )
            return False

        msg = MIMEMultipart("alternative")
        msg["To"] = to_email
        msg["From"] = f"{self.sender_name} <{self.sender_email}>"
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_password,
                start_tls=True,
            )
            return True
        except Exception as e:
            logger.exception("SMTP send failed to %s: %s", to_email, e)
            return False

    async def send_bulk(
        self,
        recipients: list,
        subject: str,
        title: str,
        message: str,
        **kwargs
    ) -> Dict[str, bool]:
        """Send to multiple recipients concurrently."""
        tasks = [self.send(r, subject, title, message, **kwargs) for r in recipients]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {r: isinstance(res, bool) and res for r, res in zip(recipients, results)}


# Singleton instance
_email_service: Optional[EmailService] = None


def get_email_service() -> EmailService:
    global _email_service
    if _email_service is None:
        _email_service = EmailService(
            sender_email=(settings.smtp_from_email or settings.smtp_user or "team@requi.io"),
            sender_name=settings.smtp_sender_name,
            smtp_host=(settings.smtp_server or "").strip() or None,
            smtp_port=settings.smtp_port,
            smtp_user=(settings.smtp_user or "").strip() or None,
            smtp_password=(settings.smtp_password or "").strip() or None,
        )
    return _email_service


# Production app URL for email CTAs (welcome, trial reminders, etc.)
LIVE_APP_URL = "https://requi.io"


def _app_url(path: str = "") -> str:
    base = LIVE_APP_URL.rstrip("/")
    if not path:
        return base
    if path.startswith("#"):
        return f"{base}{path}"
    return f"{base}{path if path.startswith('/') else '/' + path}"


async def send_welcome_email(
    *,
    to_email: str,
    first_name: str = "",
    trial_days: Optional[int] = None,
) -> bool:
    """Send welcome email after signup. Never raises — returns False on failure."""
    days = trial_days if trial_days is not None else settings.trial_days
    greeting = first_name.strip() or "there"
    title = "Welcome to Requi Health"
    message = (
        f"Hi {greeting},\n\n"
        f"Thanks for signing up. Your workspace is ready and your {days}-day free trial "
        f"has started. Sign in to explore AI-powered compliance intelligence, upload documents, "
        f"and manage your compliance workflow.\n\n"
        f"If you have questions, reply to this email or visit our site."
    )
    subject = "Welcome to Requi Health — your trial has started"
    try:
        service = get_email_service()
        ok = await service.send(
            to_email=to_email,
            subject=subject,
            title=title,
            message=message.replace("\n\n", "<br><br>").replace("\n", "<br>"),
            cta_link=_app_url("/login"),
            cta_label="Sign in to your workspace",
            badge=f"{days}-day free trial",
        )
        if ok:
            logger.info("Welcome email sent to %s", to_email)
        else:
            logger.warning("Welcome email failed for %s", to_email)
        return ok
    except Exception:
        logger.exception("Welcome email error for %s", to_email)
        return False


def _customer_app_url(path: str = "") -> str:
    base = (settings.frontend_url or LIVE_APP_URL).rstrip("/")
    if not path:
        return base
    return f"{base}{path if path.startswith('/') else '/' + path}"


async def send_workspace_viewer_invite_email(
    *,
    to_email: str,
    invited_name: str,
    inviter_name: str,
    organization_name: str,
    accept_url: str,
    custom_message: Optional[str] = None,
) -> bool:
    """Send customer-app viewer invitation. Never raises."""
    greeting = invited_name.strip() or "there"
    inviter = inviter_name.strip() or "Your team admin"
    org = organization_name.strip() or "a Requi workspace"
    extra = ""
    if custom_message and custom_message.strip():
        extra = f"<br><br><em>{custom_message.strip()}</em>"

    title = f"You’re invited to view {org}"
    subject = f"Requi Health — view-only access to {org}"
    message = (
        f"Hi {greeting},<br><br>"
        f"{inviter} invited you as a <strong>view-only</strong> member of "
        f"<strong>{org}</strong> on Requi Health. You can read dashboards, tasks, "
        f"and compliance views — without editing or using Intelligence."
        f"{extra}<br><br>"
        "Click below to accept your invitation and set up access. This link expires in 7 days."
    )
    try:
        service = get_email_service()
        ok = await service.send(
            to_email=to_email,
            subject=subject,
            title=title,
            message=message,
            cta_link=accept_url,
            cta_label="Accept invitation",
            badge="View-only access",
        )
        if ok:
            logger.info("Workspace viewer invite email sent to %s", to_email)
        else:
            logger.warning("Workspace viewer invite email failed for %s", to_email)
        return ok
    except Exception:
        logger.exception("Workspace viewer invite email error for %s", to_email)
        return False


async def send_platform_admin_invite_email(
    *,
    to_email: str,
    invited_name: str,
    inviter_name: str,
    role_label: str,
    admin_portal_url: str,
    temporary_password: Optional[str] = None,
) -> bool:
    """Send platform-admin invitation email. Never raises — returns False on failure."""
    title = "You’ve been invited to the Requi Admin Portal"
    subject = "Requi Admin Portal — invitation"
    password_html = ""
    if temporary_password:
        password_html = (
            "<br><br><strong>Temporary password:</strong> "
            f"<code style=\"background:#f5f5f7;padding:2px 6px;border-radius:6px;\">{temporary_password}</code>"
            "<br><span style=\"font-size:12px;color:#86868b;\">Please change it after your first login.</span>"
        )

    greeting = invited_name.strip() or "there"
    inviter = inviter_name.strip() or "A Super Admin"
    message = (
        f"Hi {greeting},<br><br>"
        f"{inviter} invited you to the Requi Admin Portal as <strong>{role_label}</strong>."
        f"{password_html}<br><br>"
        "Use the button below to open the portal and sign in."
    )
    try:
        service = get_email_service()
        ok = await service.send(
            to_email=to_email,
            subject=subject,
            title=title,
            message=message,
            cta_link=admin_portal_url,
            cta_label="Open Admin Portal",
            badge="Admin portal access",
        )
        if ok:
            logger.info("Platform admin invite email sent to %s", to_email)
        else:
            logger.warning("Platform admin invite email failed for %s", to_email)
        return ok
    except Exception:
        logger.exception("Platform admin invite email error for %s", to_email)
        return False


async def send_trial_two_days_left_email(
    *,
    to_email: str,
    first_name: str = "",
    days_remaining: int = 2,
    trial_end: Optional[datetime] = None,
) -> bool:
    """Send trial expiry reminder when N days remain. Never raises."""
    greeting = first_name.strip() or "there"
    end_label = ""
    if trial_end:
        end_label = trial_end.strftime("%B %d, %Y")

    title = f"Your trial ends in {days_remaining} days"
    message = (
        f"Hi {greeting},\n\n"
        f"Your Requi Health free trial {'ends on ' + end_label if end_label else 'is ending soon'}. "
        f"You have {days_remaining} days left to use AI compliance intelligence, documents, and your workspace.\n\n"
        f"Upgrade now to keep uninterrupted access after your trial ends."
    )
    subject = f"Requi Health — {days_remaining} days left on your free trial"
    try:
        service = get_email_service()
        ok = await service.send(
            to_email=to_email,
            subject=subject,
            title=title,
            message=message.replace("\n\n", "<br><br>").replace("\n", "<br>"),
            cta_link=_app_url("/pricing"),
            cta_label="View plans & upgrade",
            badge=f"{days_remaining} days remaining",
        )
        if ok:
            logger.info("Trial reminder email sent to %s (%s days left)", to_email, days_remaining)
        else:
            logger.warning("Trial reminder email failed for %s", to_email)
        return ok
    except Exception:
        logger.exception("Trial reminder email error for %s", to_email)
        return False
