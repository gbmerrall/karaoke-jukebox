"""
Authentication routes for login and session management.
Uses cookie-based sessions with itsdangerous for signing.
"""

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer, BadSignature
from typing import Optional, Tuple
from app.config import settings
import logging

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Authentication"])

# Jinja2 templates
templates = Jinja2Templates(directory="app/templates")

# Session serializer
serializer = URLSafeSerializer(settings.secret_key)

# Session cookie name
SESSION_COOKIE_NAME = "karaoke_session"


# Session helpers

def create_session_data(username: str, is_admin: bool = False) -> dict:
    """Create session data dictionary."""
    return {
        "username": username,
        "is_admin": is_admin
    }


def encode_session(data: dict) -> str:
    """Encode session data to signed cookie string."""
    return serializer.dumps(data)


def decode_session(session_string: str) -> Optional[dict]:
    """Decode session data from signed cookie string."""
    try:
        return serializer.loads(session_string)
    except BadSignature:
        logger.warning("Invalid session signature")
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


def require_session(request: Request) -> Tuple[str, bool]:
    """
    Dependency that requires a valid session.

    Returns:
        Tuple of (username, is_admin)

    Raises:
        HTTPException: If no valid session (redirects to login)
    """
    username, is_admin = get_session_user(request)
    if not username:
        # Return redirect response
        raise RedirectResponse(url="/", status_code=302)
    return username, is_admin


def require_admin(request: Request) -> Tuple[str, bool]:
    """
    Dependency that requires admin session.

    Returns:
        Tuple of (username, is_admin=True)

    Raises:
        HTTPException: If not admin (redirects to login)
    """
    username, is_admin = get_session_user(request)
    if not username or not is_admin:
        raise RedirectResponse(url="/?error=Unauthorized", status_code=302)
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

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error_message": error
        }
    )


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: Optional[str] = Form(None)
):
    """
    Handle login submission.

    - Regular users: just username required
    - Admin user: username='admin' + password required
    """
    username = username.strip()

    if not username:
        return RedirectResponse(url="/?error=Username+required", status_code=303)

    is_admin = False

    # Check if admin login
    if username.lower() == "admin":
        # Validate admin password
        if not password:
            return RedirectResponse(url="/?error=Admin+password+required", status_code=303)

        if password != settings.admin_password:
            logger.warning("Failed admin login attempt")
            return RedirectResponse(url="/?error=Invalid+admin+password", status_code=303)

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
        max_age=86400,  # 24 hours
        samesite="lax"
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
