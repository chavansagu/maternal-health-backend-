#!/usr/bin/env python3
"""
Janani Jyoti - Installation Verification Script
This script checks if all required files are present and properly structured.
"""

import os
import sys
from pathlib import Path

# Color codes for terminal output
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
RESET = '\033[0m'
BOLD = '\033[1m'

def print_success(message):
    print(f"{GREEN}✓{RESET} {message}")

def print_error(message):
    print(f"{RED}✗{RESET} {message}")

def print_warning(message):
    print(f"{YELLOW}⚠{RESET} {message}")

def print_header(message):
    print(f"\n{BOLD}{message}{RESET}")
    print("=" * 60)

# Required files structure
REQUIRED_FILES = {
    "root": [
        "main.py",
        "models.py",
        "schemas.py",
        "database.py",
        "auth.py",
        "init_db.py",
        "requirements.txt",
        ".env.example",
        "setup_mariadb.sql"
    ],
    "routes": [
        "__init__.py",
        "auth_routes.py",
        "user_routes.py",
        "pregnant_women_routes.py",
        "usg_appointment_routes.py",
        "grievance_routes.py",
        "dashboard_routes.py"
    ],
    "docs": [
        "README.md",
        "QUICKSTART.md",
        "API_DOCUMENTATION.md",
        "DEPLOYMENT.md",
        "PROJECT_STRUCTURE.md",
        "MANIFEST.md"
    ]
}

def check_file_exists(filepath):
    """Check if a file exists"""
    return os.path.isfile(filepath)

def verify_installation():
    """Verify all required files are present"""
    print_header("Janani Jyoti - Installation Verification")
    
    all_good = True
    stats = {
        "total": 0,
        "present": 0,
        "missing": 0
    }
    
    # Check root files
    print_header("Checking Core Files")
    for filename in REQUIRED_FILES["root"]:
        stats["total"] += 1
        if check_file_exists(filename):
            print_success(f"{filename}")
            stats["present"] += 1
        else:
            print_error(f"{filename} - MISSING")
            stats["missing"] += 1
            all_good = False
    
    # Check routes directory
    print_header("Checking Routes Directory")
    if not os.path.isdir("routes"):
        print_error("routes/ directory - MISSING")
        print_warning("Create 'routes' directory and add route files")
        all_good = False
        stats["missing"] += len(REQUIRED_FILES["routes"])
    else:
        print_success("routes/ directory exists")
        for filename in REQUIRED_FILES["routes"]:
            filepath = os.path.join("routes", filename)
            stats["total"] += 1
            if check_file_exists(filepath):
                print_success(f"routes/{filename}")
                stats["present"] += 1
            else:
                print_error(f"routes/{filename} - MISSING")
                stats["missing"] += 1
                all_good = False
    
    # Check documentation files
    print_header("Checking Documentation Files")
    for filename in REQUIRED_FILES["docs"]:
        stats["total"] += 1
        if check_file_exists(filename):
            print_success(f"{filename}")
            stats["present"] += 1
        else:
            print_warning(f"{filename} - Missing (optional but recommended)")
            stats["missing"] += 1
    
    # Print summary
    print_header("Verification Summary")
    print(f"Total files checked: {stats['total']}")
    print(f"{GREEN}Present: {stats['present']}{RESET}")
    if stats['missing'] > 0:
        print(f"{RED}Missing: {stats['missing']}{RESET}")
    
    # Print next steps
    print_header("Next Steps")
    if all_good:
        print_success("All required files are present!")
        print("\n📋 Follow these steps:")
        print("   1. Copy .env.example to .env")
        print("      cp .env.example .env")
        print("\n   2. Edit .env and update database credentials")
        print("      nano .env")
        print("\n   3. Install dependencies")
        print("      pip install -r requirements.txt")
        print("\n   4. Setup database")
        print("      mysql < setup_mariadb.sql")
        print("\n   5. Initialize database")
        print("      python init_db.py")
        print("\n   6. Run the application")
        print("      python main.py")
        print("\n   7. Access API documentation")
        print("      http://localhost:8000/docs")
    else:
        print_error("Some files are missing!")
        print("\n📋 To fix:")
        print("   1. Re-download the complete project")
        print("   2. Ensure routes/ directory is included")
        print("   3. Run this script again to verify")
        print("\n   See MANIFEST.md for complete file list")
    
    # Check for .env file
    print_header("Configuration Check")
    if check_file_exists(".env"):
        print_success(".env file exists")
        print_warning("Remember to update database credentials in .env")
    else:
        print_warning(".env file not found")
        print("   Create it from template: cp .env.example .env")
    
    return all_good

def check_python_version():
    """Check Python version"""
    print_header("Python Version Check")
    version = sys.version_info
    if version.major == 3 and version.minor >= 9:
        print_success(f"Python {version.major}.{version.minor}.{version.micro}")
        return True
    else:
        print_error(f"Python {version.major}.{version.minor}.{version.micro}")
        print_warning("Python 3.9 or higher is required")
        return False

def test_imports():
    """Test if all modules can be imported"""
    print_header("Testing Module Imports")
    
    modules = [
        ("fastapi", "FastAPI"),
        ("sqlalchemy", "SQLAlchemy"),
        ("pydantic", "Pydantic"),
        ("jose", "python-jose"),
        ("passlib", "passlib")
    ]
    
    all_imported = True
    for module_name, display_name in modules:
        try:
            __import__(module_name)
            print_success(f"{display_name}")
        except ImportError:
            print_error(f"{display_name} - Not installed")
            all_imported = False
    
    if not all_imported:
        print("\n📦 Install missing packages:")
        print("   pip install -r requirements.txt")
    
    return all_imported

def main():
    """Main function"""
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}{'Janani Jyoti - Installation Verification':^60}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}\n")
    
    # Check current directory
    if not check_file_exists("main.py"):
        print_error("main.py not found in current directory")
        print("Please run this script from the project root directory")
        print("(The directory containing main.py)")
        sys.exit(1)
    
    # Run checks
    python_ok = check_python_version()
    files_ok = verify_installation()
    
    # Check if requirements are installed
    print("\n")
    imports_ok = test_imports()
    
    # Final status
    print_header("Overall Status")
    if files_ok and python_ok:
        print_success("Installation verification PASSED")
        print("\n🚀 You're ready to start!")
        print("   See QUICKSTART.md for next steps")
        sys.exit(0)
    else:
        print_error("Installation verification FAILED")
        print("\n⚠️  Please fix the issues above and run this script again")
        sys.exit(1)

if __name__ == "__main__":
    main()
