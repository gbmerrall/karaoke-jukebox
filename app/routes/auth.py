"""
Authentication routes for login and session management.
Uses cookie-based sessions with itsdangerous for signing.
"""

import logging
import secrets
from typing import Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadData, URLSafeTimedSerializer

from app.config import settings
from app.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Authentication"])

# Jinja2 templates
templates = Jinja2Templates(directory="app/templates")

# Session serializer. URLSafeTimedSerializer embeds a timestamp in the token so
# the signature itself expires - a captured cookie cannot be replayed forever.
serializer = URLSafeTimedSerializer(settings.secret_key)

# Session cookie name
SESSION_COOKIE_NAME = "karaoke_session"

# Server-enforced session lifetime in seconds (24 hours). This is checked
# against the token's embedded timestamp, not just the browser cookie max-age.
SESSION_MAX_AGE = 86400

# Rate limit admin password attempts per client IP to defeat brute forcing.
_admin_login_limiter = RateLimiter(max_events=5, window_seconds=300)


# Session helpers


def create_session_data(username: str, is_admin: bool = False) -> dict:
    """Create session data dictionary."""
    return {"username": username, "is_admin": is_admin}


def encode_session(data: dict) -> str:
    """Encode session data to signed cookie string."""
    return serializer.dumps(data)


def decode_session(session_string: str) -> Optional[dict]:
    """Decode and verify a signed session cookie.

    Returns None for any invalid token: bad signature, tampered payload, or an
    expired timestamp (older than SESSION_MAX_AGE). Catching BadData covers all
    of these because it is the base class for every itsdangerous failure.
    """
    try:
        return serializer.loads(session_string, max_age=SESSION_MAX_AGE)
    except BadData:
        logger.warning("Rejected invalid or expired session cookie")
        return None


def get_session_from_cookie(request: Request) -> Optional[dict]:
    """Extract and validate session from request cookies."""
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_cookie:
        return None
    return decode_session(session_cookie)


def get_session_user(request: Request) -> Tuple[Optional[str], bool]:
    """
    Get username and admin status from session.

    Returns:
        Tuple of (username, is_admin) or (None, False) if no session
    """
    session = get_session_from_cookie(request)
    if not session:
        return None, False
    return session.get("username"), session.get("is_admin", False)


def _redirect_to_login(url: str) -> HTTPException:
    """Build an HTTPException that the browser follows as a redirect.

    FastAPI dependencies cannot return a Response to short-circuit a request;
    they must raise. Raising a RedirectResponse fails (it is not an exception).
    A 303 HTTPException with a Location header is rendered by Starlette's
    default handler as a real redirect that browsers follow.
    """
    return HTTPException(status_code=303, headers={"Location": url})


def require_session(request: Request) -> Tuple[str, bool]:
    """
    Dependency that requires a valid session.

    Returns:
        Tuple of (username, is_admin)

    Raises:
        HTTPException: 303 redirect to login if no valid session.
    """
    username, is_admin = get_session_user(request)
    if not username:
        raise _redirect_to_login("/")
    return username, is_admin


def require_admin(request: Request) -> Tuple[str, bool]:
    """
    Dependency that requires admin session.

    Returns:
        Tuple of (username, is_admin=True)

    Raises:
        HTTPException: 303 redirect to login if not admin.
    """
    username, is_admin = get_session_user(request)
    if not username or not is_admin:
        raise _redirect_to_login("/?error=Unauthorized")
    return username, is_admin


# Routes


@router.get("/", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    """Render the login page."""
    # If already logged in, redirect to appropriate page
    username, is_admin = get_session_user(request)
    if username:
        redirect_url = "/admin" if is_admin else "/app"
        return RedirectResponse(url=redirect_url, status_code=302)

    return templates.TemplateResponse(request, "login.html", {"error_message": error})


@router.post("/login")
async def login(
    request: Request, username: str = Form(...), password: Optional[str] = Form(None)
):
    """
    Handle login submission.

    - Regular users: just username required
    - Admin user: username='admin' + password required (rate limited per IP)
    """
    username = username.strip()

    if not username:
        return RedirectResponse(url="/?error=Username+required", status_code=303)

    is_admin = False

    # Check if admin login
    if username.lower() == "admin":
        client_ip = request.client.host if request.client else "unknown"

        # Throttle brute-force attempts before checking the password.
        if not _admin_login_limiter.allow(client_ip):
            logger.warning(f"Admin login rate limited for {client_ip}")
            return RedirectResponse(
                url="/?error=Too+many+attempts.+Try+again+later.", status_code=303
            )

        # Validate admin password
        if not password:
            return RedirectResponse(
                url="/?error=Admin+password+required", status_code=303
            )

        # Constant-time comparison avoids leaking the password via timing.
        if not secrets.compare_digest(password, settings.admin_password):
            logger.warning(f"Failed admin login attempt from {client_ip}")
            return RedirectResponse(
                url="/?error=Invalid+admin+password", status_code=303
            )

        # Successful login clears the throttle for this IP.
        _admin_login_limiter.reset(client_ip)
        is_admin = True
        logger.info(f"Admin logged in: {username}")
    else:
        logger.info(f"User logged in: {username}")

    # Create session
    session_data = create_session_data(username, is_admin)
    session_cookie = encode_session(session_data)

    # Redirect to appropriate page based on user type
    redirect_url = "/admin" if is_admin else "/app"
    response = RedirectResponse(url=redirect_url, status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_cookie,
        httponly=True,
        max_age=SESSION_MAX_AGE,
        samesite="lax",
    )

    return response


@router.post("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    username, _ = get_session_user(request)
    if username:
        logger.info(f"User logged out: {username}")

    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
