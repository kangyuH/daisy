from __future__ import annotations

from fastapi import APIRouter

from app.api import bots, dispatcher, health, im, queue, tasks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(queue.router)
api_router.include_router(bots.router)
api_router.include_router(im.router)
api_router.include_router(tasks.router)
api_router.include_router(dispatcher.router)
