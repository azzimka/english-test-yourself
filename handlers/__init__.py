from .common import router as common_router, fallback_router
from .admin import router as admin_router
from .student import router as student_router

__all__ = ["common_router", "admin_router", "student_router", "fallback_router"]
