from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
from app.config import settings
from app.database import db_service
from app.services.docker_service import docker_service
from app.routes import health, ships, stat

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    # Startup
    logger.info("Starting Bay API service...")

    try:
        # Initialize database
        await db_service.initialize()
        await db_service.create_tables()
        logger.info("Database initialized")

        # Initialize Docker service
        await docker_service.initialize()
        logger.info("Docker service initialized")

        logger.info("Bay API service started successfully")

    except Exception as e:
        logger.error(f"Failed to start service: {e}")
        raise

    yield

    # Shutdown
    logger.info("Shutting down Bay API service...")

    # Close Docker client
    try:
        await docker_service.close()
        logger.info("Docker service closed")
    except Exception as e:
        logger.error(f"Error closing Docker service: {e}")


def create_app() -> FastAPI:
    """Create FastAPI application"""
    app = FastAPI(
        title="Bay API",
        description="Agent Sandbox Bay API Service",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure appropriately for production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    app.include_router(health.router, tags=["health"])
    app.include_router(ships.router, tags=["ships"])
    app.include_router(stat.router, tags=["stat"])

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="info",
    )
