#!/usr/bin/env python3
"""
Debug script to check user data and permissions
"""

import requests
import json

BASE_URL = "http://localhost:8000/api/v2"

def debug_user_info():
    """Debug user information and permissions"""
    
    print("Debug: User Information")
    print("=" * 50)
    
    # Login as district admin
    print("1. Logging in as district admin...")
    login_data = {
        "username": "district_admin",
        "password": "Admin@123"
    }
    
    try:
        response = requests.post(f"{BASE_URL}/auth/login", data=login_data)
        if response.status_code == 200:
            token = response.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            print("Login successful")
        else:
            print("Login failed:", response.text)
            return
    except Exception as e:
        print(f"Connection error: {e}")
        return
    
    # Get current user info
    print("\n2. Getting current user information...")
    try:
        response = requests.get(f"{BASE_URL}/auth/me", headers=headers)
        if response.status_code == 200:
            user_info = response.json()
            print("User information retrieved:")
            print(f"   - ID: {user_info.get('id')}")
            print(f"   - Username: {user_info.get('username')}")
            print(f"   - Role: {user_info.get('role')}")
            print(f"   - District ID: {user_info.get('district_id')}")
            print(f"   - Block ID: {user_info.get('block_id')}")
            print(f"   - Sub-Centre ID: {user_info.get('sub_centre_id')}")
            print(f"   - USG Centre ID: {user_info.get('usg_centre_id')}")
            print(f"   - Is Active: {user_info.get('is_active')}")
        else:
            print("Failed to get user info:", response.text)
            return
    except Exception as e:
        print(f"Error getting user info: {e}")
        return
    
    # Get existing districts
    print("\n3. Getting existing districts...")
    try:
        response = requests.get(f"{BASE_URL}/admin/districts", headers=headers)
        if response.status_code == 200:
            districts = response.json()
            print(f"Found {len(districts)} districts:")
            for district in districts:
                print(f"   - ID: {district['id']}, Name: {district['name']}, Code: {district['code']}")
        else:
            print("Failed to get districts:", response.text)
    except Exception as e:
        print(f"Error getting districts: {e}")
    
    # Get existing blocks
    print("\n4. Getting existing blocks...")
    try:
        response = requests.get(f"{BASE_URL}/admin/blocks", headers=headers)
        if response.status_code == 200:
            blocks = response.json()
            print(f"Found {len(blocks)} blocks:")
            for block in blocks:
                print(f"   - ID: {block['id']}, Name: {block['name']}, Code: {block['code']}, District ID: {block['district_id']}")
        else:
            print("Failed to get blocks:", response.text)
    except Exception as e:
        print(f"Error getting blocks: {e}")
    
    print("\nDebug completed!")

if __name__ == "__main__":
    debug_user_info()