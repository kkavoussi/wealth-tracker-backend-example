"""
Finance App — FastAPI Backend
Entry point for the API server.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import auth, households, plaid, reports, assets, transactions, snaptrade, payroll
from app.core.config import settings

app = FastAPI(
    title="Finance App API",
    description="Personal finance tracking backend",
    version="0.1.0",
)

# CORS — allows the Next.js frontend to talk to this API
_origins = [
    "http://localhost:3000",                               # local dev
    "https://wealth-tracker-frontend.vercel.app",         # production
]
if settings.frontend_url not in _origins:
    _origins.append(settings.frontend_url)                # any custom domain

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    """Quick check that the server is running."""
    return {"status": "ok", "service": "finance-app-backend"}


app.include_router(auth.router,        prefix="/auth",        tags=["auth"])
app.include_router(households.router,  prefix="/households",  tags=["households"])
app.include_router(plaid.router,       prefix="/plaid",       tags=["plaid"])
app.include_router(reports.router,     prefix="/reports",     tags=["reports"])
app.include_router(assets.router,      prefix="/assets",      tags=["assets"])
app.include_router(transactions.router, prefix="/transactions", tags=["transactions"])
app.include_router(snaptrade.router,    prefix="/snaptrade",    tags=["snaptrade"])
app.include_router(payroll.router,      prefix="/payroll",      tags=["payroll"])
