"""
File Upload Validation Utility
Validates file type, size, and content for secure uploads
"""
import os
import magic
from fastapi import UploadFile, HTTPException, status
from typing import List
from dotenv import load_dotenv

load_dotenv()

# File size limit in bytes
MAX_FILE_SIZE = int(os.getenv("MAX_UPLOAD_SIZE_MB", "10")) * 1024 * 1024  # 10MB default

# Allowed MIME types for each category
MIME_TYPES = {
    "pdf": ["application/pdf"],
    "image": ["image/jpeg", "image/jpg", "image/png"],
}

def get_allowed_extensions(file_type: str) -> List[str]:
    """Get allowed extensions from environment"""
    env_key = f"ALLOWED_{file_type.upper()}_TYPES"
    extensions = os.getenv(env_key, "")
    return [ext.strip().lower() for ext in extensions.split(",") if ext.strip()]

def validate_file_extension(filename: str, allowed_extensions: List[str]) -> bool:
    """Validate file extension against whitelist"""
    if not filename:
        return False
    ext = os.path.splitext(filename)[1].lower()
    return ext in allowed_extensions

def validate_file_size(file: UploadFile, max_size: int = MAX_FILE_SIZE) -> bool:
    """Validate file size"""
    file.file.seek(0, 2)  # Seek to end
    file_size = file.file.tell()
    file.file.seek(0)  # Reset to beginning
    return file_size <= max_size

def get_file_mime_type(file: UploadFile) -> str:
    """Get actual MIME type by reading file content"""
    try:
        # Read first 2048 bytes for magic number detection
        file.file.seek(0)
        file_header = file.file.read(2048)
        file.file.seek(0)
        
        # Use python-magic to detect MIME type
        mime = magic.Magic(mime=True)
        mime_type = mime.from_buffer(file_header)
        return mime_type
    except Exception:
        # Fallback to content_type from upload
        return file.content_type or ""

def validate_file_content(file: UploadFile, allowed_extensions: List[str]) -> bool:
    """Validate file content matches extension"""
    mime_type = get_file_mime_type(file)
    
    # Check if MIME type matches allowed extensions
    for ext in allowed_extensions:
        if ext == ".pdf" and mime_type in MIME_TYPES["pdf"]:
            return True
        elif ext in [".jpg", ".jpeg", ".png"] and mime_type in MIME_TYPES["image"]:
            return True
    
    return False

async def validate_upload_file(
    file: UploadFile,
    file_type: str,
    max_size: int = MAX_FILE_SIZE
) -> None:
    """
    Comprehensive file validation
    
    Args:
        file: Uploaded file
        file_type: Type category (prescription, report, attachment)
        max_size: Maximum file size in bytes
    
    Raises:
        HTTPException: If validation fails
    """
    if not file or not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No file provided"
        )
    
    # Get allowed extensions for this file type
    allowed_extensions = get_allowed_extensions(file_type)
    if not allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"File type configuration missing for {file_type}"
        )
    
    # Validate file extension
    if not validate_file_extension(file.filename, allowed_extensions):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type. Allowed types: {', '.join(allowed_extensions)}"
        )
    
    # Validate file size
    if not validate_file_size(file, max_size):
        max_mb = max_size / (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size: {max_mb}MB"
        )
    
    # Validate file content matches extension
    if not validate_file_content(file, allowed_extensions):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File content does not match extension. Possible file type mismatch or corrupted file."
        )
    
    # Reset file pointer after validation
    file.file.seek(0)
