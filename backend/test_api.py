import requests
import os
import json
from datetime import datetime

BASE_URL = "http://localhost:8000/api"

def print_response(response, title):
    print(f"\n=== {title} ===")
    print(f"Status Code: {response.status_code}")
    try:
        print("Response:", json.dumps(response.json(), indent=2))
    except:
        print("Response:", response.text)
    print("=" * 50)

def test_list_files():
    """Test GET /api/files/ endpoint"""
    response = requests.get(f"{BASE_URL}/files/")
    print_response(response, "List Files")
    return response

def test_upload_file():
    """Test POST /api/files/ endpoint"""
    # Create a test file
    test_file_path = "test_file.txt"
    with open(test_file_path, "w") as f:
        f.write(f"Test file content created at {datetime.now()}")
    
    # Upload the file
    with open(test_file_path, "rb") as f:
        files = {
            "file": ("test_file.txt", f, "text/plain")
        }
        response = requests.post(f"{BASE_URL}/files/", files=files)
    
    # Clean up test file
    os.remove(test_file_path)
    
    print_response(response, "Upload File")
    return response

def test_get_file_details(file_id):
    """Test GET /api/files/<file_id>/ endpoint"""
    response = requests.get(f"{BASE_URL}/files/{file_id}/")
    print_response(response, f"Get File Details (ID: {file_id})")
    return response

def test_delete_file(file_id):
    """Test DELETE /api/files/<file_id>/ endpoint"""
    response = requests.delete(f"{BASE_URL}/files/{file_id}/")
    print_response(response, f"Delete File (ID: {file_id})")
    return response

def main():
    print("Starting API Tests...")
    
    # Test 1: List files (should be empty initially)
    list_response = test_list_files()
    
    # Test 2: Upload a file
    upload_response = test_upload_file()
    if upload_response.status_code == 201:
        file_id = upload_response.json().get("id")
        
        # Test 3: Get file details
        test_get_file_details(file_id)
        
        # Test 4: List files (should now include our uploaded file)
        test_list_files()
        
        # Test 5: Delete the file
        test_delete_file(file_id)
        
        # Test 6: List files (should be empty again)
        test_list_files()
    else:
        print("File upload failed, skipping remaining tests")

if __name__ == "__main__":
    main() 