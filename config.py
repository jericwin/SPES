import os
from urllib.parse import urlparse
import mysql.connector
from mysql.connector import Error

# Database configuration with environment variable support
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', ''), 
    'database': os.getenv('DB_NAME', 'digispes'),
    'port': int(os.getenv('DB_PORT', 3306))
}

# Support full connection string if provided (e.g. DATABASE_URL or MYSQL_URL)
db_url = os.getenv('DATABASE_URL') or os.getenv('MYSQL_URL') or os.getenv('JAWSDB_URL')
if db_url:
    try:
        parsed = urlparse(db_url)
        if parsed.hostname:
            DB_CONFIG['host'] = parsed.hostname
        if parsed.username:
            DB_CONFIG['user'] = parsed.username
        if parsed.password:
            DB_CONFIG['password'] = parsed.password
        if parsed.path and len(parsed.path) > 1:
            DB_CONFIG['database'] = parsed.path.lstrip('/')
        if parsed.port:
            DB_CONFIG['port'] = int(parsed.port)
    except Exception as e:
        print(f"Warning: Failed to parse database URL: {e}")

def get_db_connection():
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL database: {e}")
        return None
