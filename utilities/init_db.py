"""
Database initialization script for Janani Jyoti
Creates tables and optionally seeds with sample data
"""
from sqlalchemy.orm import Session
from database import engine, SessionLocal, init_db
from models import (
    District, Block, Ward, SubCentre, USGCentre, User
)
from auth import get_password_hash
from datetime import datetime

def seed_sample_data(db: Session):
    """
    Seed database with sample data for testing
    """
    print("📊 Seeding sample data...")
    
    # Create District
    district1 = District(
        name="Khordha",
        code="KH001",
        is_active=True
    )
    db.add(district1)
    db.commit()
    db.refresh(district1)
    print(f"✅ Created district: {district1.name}")
    
    # Create Blocks
    block1 = Block(
        name="Bhubaneswar",
        code="BH001",
        district_id=district1.id,
        is_active=True
    )
    block2 = Block(
        name="Jatni",
        code="JT001",
        district_id=district1.id,
        is_active=True
    )
    db.add_all([block1, block2])
    db.commit()
    db.refresh(block1)
    db.refresh(block2)
    print(f"✅ Created blocks: {block1.name}, {block2.name}")
    
    # Create Wards
    wards = []
    for i in range(1, 6):
        ward = Ward(
            name=f"Ward {i}",
            code=f"W{i:03d}",
            block_id=block1.id,
            is_active=True
        )
        wards.append(ward)
    db.add_all(wards)
    db.commit()
    print(f"✅ Created {len(wards)} wards")
    
    # Create Sub-Centres
    sub_centre1 = SubCentre(
        name="Khandagiri Sub-Centre",
        code="SC001",
        block_id=block1.id,
        address="Khandagiri, Bhubaneswar",
        contact_number="0674-1234567",
        is_active=True
    )
    sub_centre2 = SubCentre(
        name="Patia Sub-Centre",
        code="SC002",
        block_id=block1.id,
        address="Patia, Bhubaneswar",
        contact_number="0674-7654321",
        is_active=True
    )
    db.add_all([sub_centre1, sub_centre2])
    db.commit()
    db.refresh(sub_centre1)
    db.refresh(sub_centre2)
    print(f"✅ Created sub-centres: {sub_centre1.name}, {sub_centre2.name}")
    
    # Create USG Centres
    usg_centre1 = USGCentre(
        name="Capital Hospital USG Centre",
        code="USG001",
        address="Unit 6, Bhubaneswar",
        contact_number="0674-2345678",
        email="usg.capital@health.gov.in",
        is_empanelled=True,
        is_private=False,
        district_id=district1.id,
        is_active=True
    )
    usg_centre2 = USGCentre(
        name="Apollo Diagnostic Centre",
        code="USG002",
        address="Saheed Nagar, Bhubaneswar",
        contact_number="0674-8765432",
        email="apollo.bbs@apollo.com",
        is_empanelled=True,
        is_private=True,
        district_id=district1.id,
        is_active=True
    )
    db.add_all([usg_centre1, usg_centre2])
    db.commit()
    db.refresh(usg_centre1)
    db.refresh(usg_centre2)
    print(f"✅ Created USG centres: {usg_centre1.name}, {usg_centre2.name}")
    
    # Create Users
    # District Admin
    district_admin = User(
        username="district_admin",
        email="district@janani.gov.in",
        password_hash=get_password_hash("Admin@123"),
        role="district",
        full_name="District Admin",
        mobile_number="9876543210",
        district_id=district1.id,
        is_active=True
    )
    
    # Block User
    block_user = User(
        username="block_bhubaneswar",
        email="block.bbsr@janani.gov.in",
        password_hash=get_password_hash("Block@123"),
        role="block",
        full_name="Block Coordinator - Bhubaneswar",
        mobile_number="9876543211",
        district_id=district1.id,
        block_id=block1.id,
        is_active=True
    )
    
    # Sub-Centre User (ANM)
    anm_user = User(
        username="anm_khandagiri",
        email="anm.khandagiri@janani.gov.in",
        password_hash=get_password_hash("Anm@123"),
        role="sub_centre",
        full_name="ANM - Khandagiri",
        mobile_number="9876543212",
        district_id=district1.id,
        block_id=block1.id,
        sub_centre_id=sub_centre1.id,
        is_active=True
    )
    
    # USG Centre User
    usg_user = User(
        username="usg_capital",
        email="usg.capital@janani.gov.in",
        password_hash=get_password_hash("Usg@123"),
        role="usg_centre",
        full_name="USG Technician - Capital Hospital",
        mobile_number="9876543213",
        district_id=district1.id,
        usg_centre_id=usg_centre1.id,
        is_active=True
    )
    
    db.add_all([district_admin, block_user, anm_user, usg_user])
    db.commit()
    
    print("\n✅ Sample users created:")
    print(f"   District Admin - Username: district_admin, Password: Admin@123")
    print(f"   Block User     - Username: block_bhubaneswar, Password: Block@123")
    print(f"   ANM User       - Username: anm_khandagiri, Password: Anm@123")
    print(f"   USG User       - Username: usg_capital, Password: Usg@123")
    
    print("\n✨ Sample data seeding completed!")

def main():
    """
    Main function to initialize database
    """
    print("🚀 Initializing Janani Jyoti Database...")
    
    try:
        # Create all tables
        init_db()
        print("✅ Database tables created successfully!")
        
        # Ask if user wants to seed sample data
        seed = input("\n❓ Do you want to seed sample data? (yes/no): ").lower().strip()
        
        if seed in ['yes', 'y']:
            db = SessionLocal()
            try:
                seed_sample_data(db)
            except Exception as e:
                print(f"❌ Error seeding data: {e}")
                db.rollback()
            finally:
                db.close()
        else:
            print("⏭️  Skipping sample data seeding.")
        
        print("\n✅ Database initialization completed!")
        print("\n📝 Next steps:")
        print("   1. Update .env file with your database credentials")
        print("   2. Run: python main.py")
        print("   3. Access API documentation at: http://localhost:8000/docs")
        
    except Exception as e:
        print(f"❌ Database initialization failed: {e}")
        raise

if __name__ == "__main__":
    main()
