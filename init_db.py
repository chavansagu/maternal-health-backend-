"""
Database initialization script for Janani Jyoti
Creates tables and optionally seeds with sample data
"""
from sqlalchemy.orm import Session
from sqlalchemy import text
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
    
    try:
        # Debug: Test database connection
        print("🔍 Testing database connection...")
        result = db.execute(text("SELECT 1"))
        print(f"✅ Database connection successful: {result.fetchone()}")
        
        # Debug: Check existing data counts
        district_count = db.query(District).count()
        block_count = db.query(Block).count()
        ward_count = db.query(Ward).count()
        subcentre_count = db.query(SubCentre).count()
        usg_count = db.query(USGCentre).count()
        user_count = db.query(User).count()
        
        print(f"🔍 Current record counts:")
        print(f"   Districts: {district_count}")
        print(f"   Blocks: {block_count}")
        print(f"   Wards: {ward_count}")
        print(f"   Sub-centres: {subcentre_count}")
        print(f"   USG centres: {usg_count}")
        print(f"   Users: {user_count}")
        
    except Exception as e:
        print(f"❌ Database connection test failed: {e}")
        return
    
    # Create District if not exists
    try:
        print("🔍 Checking for district KH001...")
        district1 = db.query(District).filter(District.code == "KH001").first()
        if not district1:
            print("🔍 Creating district...")
            district1 = District(
                name="Khordha",
                code="KH001",
                is_active=True
            )
            db.add(district1)
            db.commit()
            db.refresh(district1)
            print(f"✅ Created district: {district1.name} (ID: {district1.id})")
        else:
            print(f"⏭️  District already exists: {district1.name} (ID: {district1.id})")
    except Exception as e:
        print(f"❌ Error creating district: {e}")
        db.rollback()
        return
    
    # Create Blocks if not exists
    try:
        print("🔍 Checking for block BH001...")
        block1 = db.query(Block).filter(Block.code == "BH001").first()
        if not block1:
            print("🔍 Creating block BH001...")
            block1 = Block(
                name="Bhubaneswar",
                code="BH001",
                district_id=district1.id,
                is_active=True
            )
            db.add(block1)
            db.commit()
            db.refresh(block1)
            print(f"✅ Created block: {block1.name} (ID: {block1.id})")
        else:
            print(f"⏭️  Block already exists: {block1.name} (ID: {block1.id})")
            
        print("🔍 Checking for block JT001...")
        block2 = db.query(Block).filter(Block.code == "JT001").first()
        if not block2:
            print("🔍 Creating block JT001...")
            block2 = Block(
                name="Jatni",
                code="JT001",
                district_id=district1.id,
                is_active=True
            )
            db.add(block2)
            db.commit()
            db.refresh(block2)
            print(f"✅ Created block: {block2.name} (ID: {block2.id})")
        else:
            print(f"⏭️  Block already exists: {block2.name} (ID: {block2.id})")
    except Exception as e:
        print(f"❌ Error creating blocks: {e}")
        db.rollback()
        return
    
    # Create Wards if not exists
    try:
        print(f"🔍 Checking for wards in block {block1.id}...")
        existing_wards = db.query(Ward).filter(Ward.block_id == block1.id).count()
        if existing_wards == 0:
            print("🔍 Creating wards...")
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
        else:
            print(f"⏭️  Wards already exist: {existing_wards} wards")
    except Exception as e:
        print(f"❌ Error creating wards: {e}")
        db.rollback()
        return
    
    # Create Sub-Centres if not exists
    try:
        print("🔍 Checking for sub-centre SC001...")
        sub_centre1 = db.query(SubCentre).filter(SubCentre.code == "SC001").first()
        if not sub_centre1:
            print("🔍 Creating sub-centre SC001...")
            sub_centre1 = SubCentre(
                name="Khandagiri Sub-Centre",
                code="SC001",
                block_id=block1.id,
                address="Khandagiri, Bhubaneswar",
                contact_number="0674-1234567",
                is_active=True
            )
            db.add(sub_centre1)
            db.commit()
            db.refresh(sub_centre1)
            print(f"✅ Created sub-centre: {sub_centre1.name} (ID: {sub_centre1.id})")
        else:
            print(f"⏭️  Sub-centre already exists: {sub_centre1.name} (ID: {sub_centre1.id})")
            
        print("🔍 Checking for sub-centre SC002...")
        sub_centre2 = db.query(SubCentre).filter(SubCentre.code == "SC002").first()
        if not sub_centre2:
            print("🔍 Creating sub-centre SC002...")
            sub_centre2 = SubCentre(
                name="Patia Sub-Centre",
                code="SC002",
                block_id=block1.id,
                address="Patia, Bhubaneswar",
                contact_number="0674-7654321",
                is_active=True
            )
            db.add(sub_centre2)
            db.commit()
            db.refresh(sub_centre2)
            print(f"✅ Created sub-centre: {sub_centre2.name} (ID: {sub_centre2.id})")
        else:
            print(f"⏭️  Sub-centre already exists: {sub_centre2.name} (ID: {sub_centre2.id})")
    except Exception as e:
        print(f"❌ Error creating sub-centres: {e}")
        db.rollback()
        return
    
    # Create USG Centres if not exists
    try:
        print("🔍 Checking for USG centre USG001...")
        usg_centre1 = db.query(USGCentre).filter(USGCentre.code == "USG001").first()
        if not usg_centre1:
            print("🔍 Creating USG centre USG001...")
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
            db.add(usg_centre1)
            db.commit()
            db.refresh(usg_centre1)
            print(f"✅ Created USG centre: {usg_centre1.name} (ID: {usg_centre1.id})")
        else:
            print(f"⏭️  USG centre already exists: {usg_centre1.name} (ID: {usg_centre1.id})")
            
        print("🔍 Checking for USG centre USG002...")
        usg_centre2 = db.query(USGCentre).filter(USGCentre.code == "USG002").first()
        if not usg_centre2:
            print("🔍 Creating USG centre USG002...")
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
            db.add(usg_centre2)
            db.commit()
            db.refresh(usg_centre2)
            print(f"✅ Created USG centre: {usg_centre2.name} (ID: {usg_centre2.id})")
        else:
            print(f"⏭️  USG centre already exists: {usg_centre2.name} (ID: {usg_centre2.id})")
    except Exception as e:
        print(f"❌ Error creating USG centres: {e}")
        db.rollback()
        return
    
    # Create Users if not exists
    try:
        print("🔍 Checking for user district_admin...")
        if not db.query(User).filter(User.username == "district_admin").first():
            print("🔍 Creating user district_admin...")
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
            db.add(district_admin)
            db.commit()
            print("✅ Created user: district_admin")
        else:
            print("⏭️  User already exists: district_admin")
        
        print("🔍 Checking for user block_bhubaneswar...")
        if not db.query(User).filter(User.username == "block_bhubaneswar").first():
            print("🔍 Creating user block_bhubaneswar...")
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
            db.add(block_user)
            db.commit()
            print("✅ Created user: block_bhubaneswar")
        else:
            print("⏭️  User already exists: block_bhubaneswar")
        
        print("🔍 Checking for user anm_khandagiri...")
        if not db.query(User).filter(User.username == "anm_khandagiri").first():
            print("🔍 Creating user anm_khandagiri...")
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
            db.add(anm_user)
            db.commit()
            print("✅ Created user: anm_khandagiri")
        else:
            print("⏭️  User already exists: anm_khandagiri")
        
        print("🔍 Checking for user usg_capital...")
        if not db.query(User).filter(User.username == "usg_capital").first():
            print("🔍 Creating user usg_capital...")
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
            db.add(usg_user)
            db.commit()
            print("✅ Created user: usg_capital")
        else:
            print("⏭️  User already exists: usg_capital")
    except Exception as e:
        print(f"❌ Error creating users: {e}")
        db.rollback()
        return
    
    # Final count check
    try:
        final_district_count = db.query(District).count()
        final_block_count = db.query(Block).count()
        final_ward_count = db.query(Ward).count()
        final_subcentre_count = db.query(SubCentre).count()
        final_usg_count = db.query(USGCentre).count()
        final_user_count = db.query(User).count()
        
        print(f"\n🔍 Final record counts:")
        print(f"   Districts: {final_district_count}")
        print(f"   Blocks: {final_block_count}")
        print(f"   Wards: {final_ward_count}")
        print(f"   Sub-centres: {final_subcentre_count}")
        print(f"   USG centres: {final_usg_count}")
        print(f"   Users: {final_user_count}")
    except Exception as e:
        print(f"❌ Error checking final counts: {e}")
    
    print("\n✅ Sample users (if created):")
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
