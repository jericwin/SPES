import base64
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse, unquote

import mysql.connector
from mysql.connector import Error
from mysql.connector.constants import ClientFlag


_SSL_CACHE: dict[str, str] = {}


def _write_ssl_file_if_needed(value: str) -> str | None:
    """Persist SSL material provided via env vars and return its file path."""
    if not value:
        return None

    if value in _SSL_CACHE:
        return _SSL_CACHE[value]

    potential_path = Path(value)
    if potential_path.exists():
        return str(potential_path)

    content: str
    stripped = value.strip()
    if stripped.startswith('-----BEGIN'):
        content = stripped
    else:
        try:
            content = base64.b64decode(stripped).decode('utf-8')
        except Exception:
            # Fall back to treating the raw value as the certificate data
            content = stripped

    fd, temp_path = tempfile.mkstemp(prefix='digispes_ssl_', suffix='.pem')
    with os.fdopen(fd, 'w') as temp_file:
        temp_file.write(content)
    _SSL_CACHE[value] = temp_path
    return temp_path


def _build_db_config() -> dict:
    database_url = os.environ.get('DATABASE_URL') or os.environ.get('JAWSDB_URL')
    config: dict[str, object] = {
        'host': os.environ.get('DB_HOST', 'localhost'),
        'port': int(os.environ.get('DB_PORT', '3306')),
        'user': os.environ.get('DB_USER', 'root'),
        'password': os.environ.get('DB_PASSWORD', ''),
        'database': os.environ.get('DB_NAME', 'digispes'),
    }

    if database_url:
        parsed = urlparse(database_url)
        if parsed.hostname:
            config['host'] = parsed.hostname
        if parsed.port:
            config['port'] = parsed.port
        if parsed.username:
            config['user'] = unquote(parsed.username)
        if parsed.password:
            config['password'] = unquote(parsed.password)
        if parsed.path and parsed.path != '/':
            config['database'] = parsed.path.lstrip('/')

    ssl_ca = _write_ssl_file_if_needed(os.environ.get('DB_SSL_CA', ''))
    ssl_cert = _write_ssl_file_if_needed(os.environ.get('DB_SSL_CERT', ''))
    ssl_key = _write_ssl_file_if_needed(os.environ.get('DB_SSL_KEY', ''))

    ssl_options = {}
    if ssl_ca:
        ssl_options['ssl_ca'] = ssl_ca
    if ssl_cert:
        ssl_options['ssl_cert'] = ssl_cert
    if ssl_key:
        ssl_options['ssl_key'] = ssl_key

    if ssl_options:
        config.update(ssl_options)
        config.setdefault('client_flags', []).append(ClientFlag.SSL)

    return config


def get_db_connection():
    db_config = _build_db_config()
    try:
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL database: {e}")
    return None