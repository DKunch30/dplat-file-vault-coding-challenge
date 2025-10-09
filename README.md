# Abnormal File Vault

A Django-based file management application designed for efficient file handling and storage.

## ✅ Core Features Implemented

- **UserId-based access control:** All requests require a `UserId` header.
- **Deduplication by SHA-256:** Files with identical hashes are stored once; re-uploads create reference records.
- **Reference tracking:** Each original file reports how many reference copies exist (`reference_count`).
- **Automatic promotion:** When an original file is deleted, one reference is promoted to be the new original.
- **Quota enforcement:** Each user has a configurable 10 MB storage quota (dedup-aware).
- **Rate limiting:** Throttles requests per user (`2/second`) using custom `UserIdRateThrottle`.
- **Filtering & searching:** Support for filename search, file type, size range, and date range.
- **Storage statistics endpoint:** Shows original usage, deduplicated usage, and storage savings.
- **Automatic file cleanup:** Deletes the physical file from disk once all database records are gone.


## 🚀 Technology Stack

### Backend
- Django 4.x (Python web framework)
- Django REST Framework (API development)
- SQLite (Development database)
- Gunicorn (WSGI HTTP Server)
- WhiteNoise (Static file serving)

### Infrastructure
- Docker and Docker Compose
- Local file storage with volume mounting

## 📋 Prerequisites

Before you begin, ensure you have installed:
- Docker (20.10.x or higher) and Docker Compose (2.x or higher)
- Python (3.9 or higher) - for local development

## 🛠️ Installation & Setup

### Using Docker (Recommended)

```bash
docker-compose up --build
```

### Local Development Setup

#### Backend Setup
1. **Create and activate virtual environment**
   ```bash
   cd backend
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Create necessary directories**
   ```bash
   mkdir -p media staticfiles data
   ```

4. **Run migrations**
   ```bash
   python manage.py migrate
   ```

5. **Start the development server**
   ```bash
   python manage.py runserver
   ```

## 🌐 Accessing the Application

- Backend API: http://localhost:8000/api
- Example: http://localhost:8000/api/files/
- ⚠️ **Every request must include the `UserId` header**, e.g.:

```bash
curl -H "UserId: u1" http://localhost:8000/api/files/
```

## 📝 API Documentation

### ⚙️ Required Headers

Every API request **must include** a `UserId` header, for example:

```bash
-H "UserId: u1"
```

### File Management Endpoints

#### List Files
- **GET** `/api/files/`
- Returns a list of all uploaded files
- Response includes file metadata (name, size, type, upload date)

#### Upload File
- **POST** `/api/files/`
- Upload a new file
- Request: Multipart form data with 'file' field
- Returns: File metadata including ID and upload status

#### Get File Details
- **GET** `/api/files/<file_id>/`
- Retrieve details of a specific file
- Returns: Complete file metadata

#### Delete File
- **DELETE** `/api/files/<file_id>/`
- Remove a file from the system
- Returns: 204 No Content on success

#### Get Storage Stats
- **GET** `/api/files/storage_stats/`
- Returns per-user storage usage summary
- Example:
  ```json
  {
    "user_id": "u1",
    "total_storage_used": 105691,
    "original_storage_used": 211382,
    "storage_savings": 105691,
    "savings_percentage": 50.0
  }

#### Download File
- Access file directly through the file URL provided in metadata

## 🧪 Running Automated Tests

This project includes an automated test suite validating all core behaviors:
- Deduplication and reference creation
- Filtering and search
- Throttling (2 req/sec)
- Quota enforcement
- Delete → promotion → file cleanup

### Run tests with Docker:
```bash
docker compose exec backend python manage.py test files -v 2
```

## 🧭 Quick API Smoke Tests

List files:
```bash
curl -i -H "UserId: u1" http://localhost:8000/api/files/
```

Upload a file:
```bash
curl -i -F "file=@/path/to/sample.pdf" -H "UserId: u1" http://localhost:8000/api/files/
```

Get storage stats:
```bash
curl -i -H "UserId: u1" http://localhost:8000/api/files/storage_stats/
```

Throttle test:
```bash
for i in {1..5}; do curl -i -H "UserId: u1" http://localhost:8000/api/files/; done
```

Delete a file:
```bash
curl -i -X DELETE -H "UserId: u1" http://localhost:8000/api/files/{FILE_UUID}/
```

## 🗄️ Project Structure

```
file-hub/
├── backend/                # Django backend
│   ├── files/             # Main application
│   │   ├── models.py      # Data models
│   │   ├── views.py       # API views
│   │   ├── urls.py        # URL routing
│   │   └── serializers.py # Data serialization
│   ├── core/              # Project settings
│   └── requirements.txt   # Python dependencies
└── docker-compose.yml    # Docker composition
```

## 🔧 Development Features

- Hot reloading for backend development
- Django Debug Toolbar for debugging
- SQLite for easy development

## 🐛 Troubleshooting

1. **Port Conflicts**
   ```bash
   # If port 8000 is in use, modify docker-compose.yml or use:
   python manage.py runserver 8001
   ```

2. **File Upload Issues**
   - Maximum file size: 10MB
   - Ensure proper permissions on media directory
   - Check network tab for detailed error messages

3. **Database Issues**
   ```bash
   # Reset database
   rm backend/data/db.sqlite3
   python manage.py migrate
   ```

# Project Submission Instructions

## Preparing Your Submission

1. Before creating your submission zip file, ensure:
   - All features are implemented and working as expected
   - All tests are passing
   - The application runs successfully locally
   - Remove any unnecessary files or dependencies
   - Clean up any debug/console logs

2. Create the submission zip file:
   ```bash
   # Activate your backend virtual environment first
   cd backend
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   
   # Run the submission script from the project root
   cd ..
   python create_submission_zip.py
   ```

   The script will:
   - Create a zip file named `username_YYYYMMDD.zip` (e.g., `johndoe_20240224.zip`)
   - Respect .gitignore rules to exclude unnecessary files
   - Preserve file timestamps
   - Show you a list of included files and total size
   - Warn you if the zip is unusually large

3. Verify your submission zip file:
   - Extract the zip file to a new directory
   - Ensure all necessary files are included
   - Verify that no unnecessary files (like __pycache__, etc.) are included
   - Test the application from the extracted files to ensure everything works

## Video Documentation Requirement

**Video Guidance** - Record a screen share demonstrating:
- How you leveraged Gen AI to help build the features
- Your prompting techniques and strategies
- Any challenges you faced and how you overcame them
- Your thought process in using AI effectively

**IMPORTANT**: Please do not provide a demo of the application functionality. Focus only on your Gen AI usage and approach.

## Submission Process

1. Submit your project through this Google Form:
   [Project Submission Form](https://forms.gle/nr6DZAX3nv6r7bru9)

2. The form will require:
   - Your project zip file (named `username_YYYYMMDD.zip`)
   - Your video documentation
   - Any additional notes or comments about your implementation

Make sure to test the zip file and video before submitting to ensure they are complete and working as expected.

