"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.errors import ApiError, api_error_handler
from app.routers import bootstrap, deposits, doctrine, health, library, machines, projects, proposals, search
from app.startup import bootstrap_owner_token


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await bootstrap_owner_token()
    yield


app = FastAPI(title="The Brain", version="0.1.0", lifespan=lifespan)

app.add_exception_handler(ApiError, api_error_handler)

app.include_router(health.router)
app.include_router(machines.router)
app.include_router(deposits.router)
app.include_router(library.router)
app.include_router(search.router)
app.include_router(doctrine.router)
app.include_router(proposals.router)
app.include_router(projects.router)
app.include_router(bootstrap.router)
