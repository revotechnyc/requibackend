"""
Roles and permissions for the SaaS admin portal (platform team / blog content team).
Separate from customer organization UserRole.
"""

from enum import Enum


class PlatformAdminRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    BLOG_WRITER = "blog_writer"
    BLOG_EDITOR = "blog_editor"
    BLOG_ADMIN = "blog_admin"


INVITEABLE_PLATFORM_ROLES: tuple[str, ...] = (
    PlatformAdminRole.BLOG_WRITER.value,
    PlatformAdminRole.BLOG_EDITOR.value,
    PlatformAdminRole.BLOG_ADMIN.value,
)

PLATFORM_ROLE_LABELS: dict[str, str] = {
    PlatformAdminRole.SUPER_ADMIN.value: "Super Admin",
    PlatformAdminRole.BLOG_WRITER.value: "Blog Writer",
    PlatformAdminRole.BLOG_EDITOR.value: "Blog Editor / Publisher",
    PlatformAdminRole.BLOG_ADMIN.value: "Blog Admin",
}

PLATFORM_ROLE_DESCRIPTIONS: dict[str, str] = {
    PlatformAdminRole.BLOG_WRITER.value: (
        "Create drafts, edit own drafts, upload media. Cannot publish."
    ),
    PlatformAdminRole.BLOG_EDITOR.value: (
        "Edit all posts, review, publish/unpublish, schedule, SEO metadata."
    ),
    PlatformAdminRole.BLOG_ADMIN.value: (
        "Full blog module management, categories, tags, and contributors."
    ),
}
