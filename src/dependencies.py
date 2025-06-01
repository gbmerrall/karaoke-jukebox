"""
FastAPI dependencies for the karaoke app.
"""
from fastapi import Request, status
from fastapi.responses import RedirectResponse
import structlog
from typing import Tuple
from src.utils import get_session_user

logger = structlog.get_logger()

async def require_admin(request: Request) -> Tuple[str, bool]:
    """
    Dependency that ensures the user is an admin.
    Returns the username and admin status if authenticated.
    Redirects to home page if not authenticated or not admin.
    
    Args:
        request: The FastAPI request object
        
    Returns:
        Tuple[str, bool]: The username and admin status
        
    Raises:
        RedirectResponse: If user is not authenticated or not admin
    """
    user_name, is_admin = get_session_user(request)
    logger.info("admin_auth_check", user_name=user_name, is_admin=is_admin)
    
    if not user_name or not is_admin:
        logger.warning("unauthorized_admin_access", user_name=user_name)
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    
    return user_name, is_admin 