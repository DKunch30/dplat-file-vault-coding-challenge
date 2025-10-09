#!/bin/sh

# Ensure data directory exists and has proper permissions (world read/write for simplicity in container)
mkdir -p /app/data
chmod -R 777 /app/data

# Run migrations
echo "Running migrations..."
python manage.py makemigrations # scans models and creates migrations
python manage.py migrate # applies migrations to DB

# Start server
echo "Starting server..."
# starts Gunicorn server binding to all interfaces on port 8000
# serving WSGI entrypoint core.wsgi:application (process that handles HTTP requests in container)
gunicorn --bind 0.0.0.0:8000 core.wsgi:application 