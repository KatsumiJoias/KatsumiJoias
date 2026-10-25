"""Read environment variables with sane defaults (Cloud Run sometimes stores literal 'None')."""

import os

_EMPTY_MARKERS = frozenset({'', 'none', 'null', 'undefined'})


def env_str(key: str, default: str = '') -> str:
    raw = os.getenv(key)
    if raw is None:
        return default
    value = str(raw).strip()
    if value.lower() in _EMPTY_MARKERS:
        return default
    return value


def gcp_project_id(default: str = 'katsumijoias') -> str:
    return (
        env_str('FIREBASE_PROJECT_ID')
        or env_str('GOOGLE_CLOUD_PROJECT')
        or env_str('GCP_PROJECT')
        or default
    )


def firebase_auth_domain() -> str:
    explicit = env_str('FIREBASE_AUTH_DOMAIN')
    if explicit:
        return explicit
    project = gcp_project_id()
    return f'{project}.firebaseapp.com'


def firebase_storage_bucket() -> str:
    explicit = env_str('FIREBASE_STORAGE_BUCKET')
    if explicit:
        return explicit
    return f'{gcp_project_id()}.firebasestorage.app'


def firebase_messaging_sender_id() -> str:
    """Sender ID must match FIREBASE_APP_ID (not the GCP project number)."""
    app_id = env_str('FIREBASE_APP_ID')
    if app_id.startswith('1:'):
        parts = app_id.split(':')
        if len(parts) >= 2 and parts[1].isdigit():
            return parts[1]
    return env_str('FIREBASE_MESSAGING_SENDER_ID')


def public_site_url(request=None) -> str:
    """Public base URL (no trailing slash). Prefer SITE_URL; else current request origin."""
    url = env_str('SITE_URL')
    if url:
        return url.rstrip('/')
    if request is not None:
        try:
            root = (request.url_root or '').rstrip('/')
            if root.startswith('http'):
                return root
        except Exception:
            pass
    return ''
