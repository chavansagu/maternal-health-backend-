from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
import os
from dotenv import load_dotenv

from database import get_db
from models import User
from schemas import TokenData

load_dotenv()

# Security Configuration
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-this-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))  # 24 hours

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# OAuth2 scheme
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v2/auth/login")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain password against a hashed password
    """
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """
    Hash a password
    """
    # Ensure password is within bcrypt's 72-byte limit
    if len(password.encode('utf-8')) > 72:
        password = password[:72]
    return pwd_context.hash(password)

# Aadhaar helpers disabled for current release
# def hash_aadhaar(aadhaar: str) -> str:
#     """Hash Aadhaar number using SHA-256 for secure, fast, searchable storage"""
#     import hashlib
#     return hashlib.sha256(aadhaar.strip().encode()).hexdigest()
#
# def verify_aadhaar(plain_aadhaar: str, hashed_aadhaar: str) -> bool:
#     """Verify plain Aadhaar against stored SHA-256 hash"""
#     if not hashed_aadhaar:
#         return False
#     # Legacy: if stored value is bcrypt hash, fall back to bcrypt verify
#     if hashed_aadhaar.startswith("$2b$") or hashed_aadhaar.startswith("$2a$"):
#         try:
#             return pwd_context.verify(plain_aadhaar, hashed_aadhaar)
#         except Exception:
#             return False
#     # Legacy: plain-text direct compare
#     if len(hashed_aadhaar) != 64:
#         return plain_aadhaar.strip() == hashed_aadhaar.strip()
#     # Normal path: SHA-256 compare
#     return hash_aadhaar(plain_aadhaar) == hashed_aadhaar
#
# def mask_aadhaar(aadhaar: str) -> str:
#     """Mask first 8 digits, keep last 4 visible — e.g. XXXXXXXX9012"""
#     aadhaar = str(aadhaar).strip()
#     if len(aadhaar) != 12:
#         return "X" * 8 + aadhaar[-4:] if len(aadhaar) >= 4 else "XXXXXXXXXXXX"
#     return "X" * 8 + aadhaar[8:]

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a JWT access token
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now() + expires_delta
    else:
        expire = datetime.now() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
    """
    Authenticate a user by username and password
    """
    user = db.query(User).filter(User.username == username).first()
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> User:
    """
    Get current authenticated user from JWT token
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except JWTError:
        raise credentials_exception
    
    user = db.query(User).filter(User.username == token_data.username).first()
    if user is None:
        raise credentials_exception
    return user

async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    Get current active user
    """
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user

# Role-based authorization decorators
def require_role(*allowed_roles):
    """
    Decorator to require specific user roles
    """
    async def role_checker(current_user: User = Depends(get_current_active_user)):
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Not authorized. Required roles: {allowed_roles}"
            )
        return current_user
    return role_checker

# Specific role dependencies
def get_district_user(current_user: User = Depends(get_current_active_user)) -> User:
    """Get current district user"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="District user access required")
    return current_user

def get_block_user(current_user: User = Depends(get_current_active_user)) -> User:
    """Get current block user"""
    if current_user.role != "block":
        raise HTTPException(status_code=403, detail="Block user access required")
    return current_user

def get_sub_centre_user(current_user: User = Depends(get_current_active_user)) -> User:
    """Get current sub-centre user"""
    if current_user.role != "sub_centre":
        raise HTTPException(status_code=403, detail="Sub-centre user access required")
    return current_user

def get_usg_centre_user(current_user: User = Depends(get_current_active_user)) -> User:
    """Get current USG centre user"""
    if current_user.role != "usg_centre":
        raise HTTPException(status_code=403, detail="USG centre user access required")
    return current_user

def get_dp_user(current_user: User = Depends(get_current_active_user)) -> User:
    """Get current Delivery Point user"""
    if current_user.role != "dp":
        raise HTTPException(status_code=403, detail="Delivery Point user access required")
    return current_user

def get_pmsma_user(current_user: User = Depends(get_current_active_user)) -> User:
    """Get current PMSMA user"""
    if current_user.role != "pmsma":
        raise HTTPException(status_code=403, detail="PMSMA user access required")
    return current_user