from fastapi import APIRouter, Depends, HTTPException

from ..core.security import require_trader
from ..models.schemas import OrderRequest, OrderResponse, CloseRequest, ModifyRequest
from ..services.trade_service import place_order as svc_place_order
from ..services.trade_service import close_position as svc_close_position
from ..services.trade_service import modify_position as svc_modify_position
from ..services.trade_service import TradeError

router = APIRouter(prefix="/trade", tags=["Trading"])

# Every trading endpoint needs a logged-in user with a trading role, so each trade
# is attributable to a person. The shared MT5_API_TOKEN, which identifies nobody,
# used to be accepted here too and no longer is.


@router.post("/order", response_model=OrderResponse)
async def place_order(order: OrderRequest, current_user: dict = Depends(require_trader)):
    """Place a market or pending order."""
    try:
        result = await svc_place_order(order, current_user["id"])
        return OrderResponse(**result)
    except TradeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.post("/close")
async def close_position(close_req: CloseRequest, current_user: dict = Depends(require_trader)):
    """Close an open position."""
    try:
        return await svc_close_position(close_req.ticket, close_req.volume, current_user["id"])
    except TradeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.post("/modify")
async def modify_position(mod_req: ModifyRequest, current_user: dict = Depends(require_trader)):
    """Modify SL and/or TP of a position."""
    try:
        return await svc_modify_position(mod_req.ticket, mod_req.sl, mod_req.tp, current_user["id"])
    except TradeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
