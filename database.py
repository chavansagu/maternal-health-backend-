from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from typing import Generator
import os
from dotenv import load_dotenv

load_dotenv()

# Database Configuration
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://root:@localhost:3306/janani_jyoti"
)

# Create SQLAlchemy engine
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,          # Test connections before using
    pool_recycle=3600,           # Recycle connections after 1 hour
    pool_size=10,                # Connection pool size
    max_overflow=20,             # Extra connections for bursts
    pool_timeout=30,             # Wait time for connection
    connect_args={
        "connect_timeout": 10,   # MySQL connection timeout
    },
    echo=False  # Set to True for SQL logging during development
)

# Create SessionLocal class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Dependency to get DB session
def get_db() -> Generator:
    """
    Database session dependency for FastAPI
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass  # Ignore close errors

# Create all tables
def init_db():
    """
    Initialize database tables
    """
    from models import Base
    Base.metadata.create_all(bind=engine)
    print("Database tables created successfully!")

if __name__ == "__main__":
    init_db()
