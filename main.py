from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from contextlib import asynccontextmanager
import uvicorn
import os
import logging

# Configure logging so 422 errors appear in uvicorn output
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from database import init_db
from scheduler import start_scheduler, stop_scheduler

# Import routes
from routes import (
    auth_routes,
    user_routes,
    pregnant_women_routes,
    usg_appointment_routes,
    dashboard_routes,
    admin_routes,
    anc_visit_routes,
    report_routes,
    report_program_routes,
    sms_routes,
    audit_routes,
    notification_routes,
    delivery_referral_routes,
    ivr_routes,
    pmsma_routes,
    mobilisation_routes,
    pnc_reminder_routes,
    district_analytics_routes,
    enhanced_tracking_routes,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan event handler for startup and shutdown
    """
    # Startup
    print("🚀 Starting Janani Jyoti API Server...")
    try:
        init_db()
        print("✅ Database initialized successfully")
    except Exception as e:
        print(f"❌ Database initialization error: {e}")

    try:
        start_scheduler()
        print("✅ APScheduler started successfully")
    except Exception as e:
        print(f"❌ Scheduler startup error: {e}")

    yield

    # Shutdown
    stop_scheduler()
    print("👋 Shutting down Janani Jyoti API Server...")

# Create FastAPI app
app = FastAPI(
    title="Janani Jyoti API",
    description="""
    ## Janani Jyoti - Maternal Care Management System
    
    A comprehensive API for managing maternal and infant care initiatives across districts.
    
    ### Features:
    * User Management (District, Block, Sub-Centre, USG Centre)
    * Pregnant Women Registration & Management
    * ANC Visit Tracking
    * USG Appointment Scheduling & Management
    * Grievance Registration & Resolution
    * Dashboard & Analytics
    * SMS Notifications
    * Bulk Data Upload
    
    ### User Roles:
    * **District User**: Overall monitoring, user management, reports
    * **Block User**: Block-level coordination, bulk uploads
    * **Sub-Centre User**: Pregnant women registration, ANC visits
    * **USG Centre**: Appointment management, report uploads
    * **Beneficiary**: Self-registration, grievance submission
    """,
    version="2.0.0",
    lifespan=lifespan,
    #docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json"
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,
)

# 422 Validation Error Handler — logs exact field errors to server log
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Serialize errors safely — UploadFile objects are not JSON serializable
    def safe_serialize(obj):
        if isinstance(obj, (str, int, float, bool, type(None))):
            return obj
        if isinstance(obj, dict):
            return {k: safe_serialize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [safe_serialize(i) for i in obj]
        return str(obj)  # fallback: convert anything else to string

    errors = safe_serialize(exc.errors())
    logger.error(
        f"[422 VALIDATION ERROR] {request.method} {request.url}\n"
        f"  Errors: {errors}"
    )
    return JSONResponse(status_code=422, content={"detail": errors})

# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Global exception handler for all unhandled exceptions
    """
    from fastapi import HTTPException as FastAPIHTTPException
    from starlette.exceptions import HTTPException as StarletteHTTPException
    if isinstance(exc, (FastAPIHTTPException, StarletteHTTPException)):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "Internal server error",
            "detail": str(exc)
        }
    )

# Root endpoint
@app.get("/", tags=["Root"])
async def root():
    """
    Root endpoint - API health check
    """
    return {
        "message": "Welcome to Janani Jyoti API",
        "version": "2.0.0",
        "status": "running",
        "documentation": "/docs"
    }

@app.get("/health", tags=["Root"])
async def health_check():
    """
    Health check endpoint
    """
    return {
        "status": "healthy",
        "service": "janani-jyoti-api"
    }

# API v1 routes
api_v1_prefix = "/api/v2"

# Mount static files for file serving
os.makedirs("uploads", exist_ok=True)
os.makedirs("uploads/prescriptions", exist_ok=True)
os.makedirs("uploads/usg_reports", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# Include routers
app.include_router(auth_routes.router, prefix=api_v1_prefix)
app.include_router(user_routes.router, prefix=api_v1_prefix)
app.include_router(pregnant_women_routes.router, prefix=api_v1_prefix)
app.include_router(anc_visit_routes.router, prefix=api_v1_prefix)
app.include_router(usg_appointment_routes.router, prefix=api_v1_prefix)
app.include_router(dashboard_routes.router, prefix=api_v1_prefix)
app.include_router(admin_routes.router, prefix=api_v1_prefix)
app.include_router(report_routes.router, prefix=api_v1_prefix)
app.include_router(report_program_routes.router, prefix=api_v1_prefix)
app.include_router(sms_routes.router, prefix=api_v1_prefix)
app.include_router(audit_routes.router, prefix=api_v1_prefix)
app.include_router(notification_routes.router, prefix=api_v1_prefix)
app.include_router(delivery_referral_routes.router, prefix=api_v1_prefix)
app.include_router(ivr_routes.router, prefix=api_v1_prefix)
app.include_router(pmsma_routes.router, prefix=api_v1_prefix)
app.include_router(mobilisation_routes.router, prefix=api_v1_prefix)
app.include_router(pnc_reminder_routes.router, prefix=api_v1_prefix)
app.include_router(district_analytics_routes.router, prefix=api_v1_prefix)
app.include_router(enhanced_tracking_routes.router, prefix=api_v1_prefix)

# Run the application
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
