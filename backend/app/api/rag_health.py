from fastapi import APIRouter, Depends

from ..core.rag_health import get_rag_health
from ..core.security import get_current_user

router = APIRouter(tags=["RAG Health"])


@router.get("/rag-health")
async def rag_health(current_user: dict = Depends(get_current_user)):
    """RAG configuration, plateau diagnostics and per-symbol stats."""
    return await get_rag_health()
