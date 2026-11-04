#!/bin/bash
set -e

echo "=== DigiSPES Container Initializing ==="

# Check if an external database is configured
if [ -n "$DATABASE_URL" ] || [ -n "$JAWSDB_URL" ] || { [ -n "$DB_HOST" ] && [ "$DB_HOST" != "localhost" ] && [ "$DB_HOST" != "127.0.0.1" ]; }; then
    echo "Using external MySQL database configuration..."
else
    echo "Starting bundled MariaDB server..."
    mkdir -p /var/run/mysqld
    chown -R mysql:mysql /var/run/mysqld

    if [ ! -d "/var/lib/mysql/mysql" ]; then
        echo "Installing MariaDB system tables..."
        mysql_install_db --user=mysql --datadir=/var/lib/mysql > /dev/null 2>&1
    fi

    service mariadb start || /etc/init.d/mariadb start

    echo "Waiting for MariaDB to be ready..."
    for i in $(seq 1 30); do
        if mysqladmin ping -u root --silent 2>/dev/null; then
            break
        fi
        sleep 1
    done

    if ! mysql -u root -e "USE digispes;" 2>/dev/null; then
        echo "Creating digispes database..."
        mysql -u root -e "CREATE DATABASE IF NOT EXISTS digispes CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

        if [ -f "/app/digispes (JAN 5).sql" ]; then
            echo "Importing digispes (JAN 5).sql seed data..."
            mysql -u root digispes < "/app/digispes (JAN 5).sql" || true
        elif [ -f "/app/schema.sql" ]; then
            echo "Importing schema.sql..."
            mysql -u root digispes < "/app/schema.sql" || true
        fi
        mysql -u root -e "FLUSH PRIVILEGES;" || true
        echo "Database initialized successfully!"
    else
        echo "Database digispes already exists."
    fi
fi

if [ -f "/app/apply_migration.py" ]; then
    echo "Running migrations..."
    python /app/apply_migration.py || true
fi

echo "Starting DigiSPES web application on port ${PORT:-5000}..."
exec gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 --bind 0.0.0.0:${PORT:-5000} app:app
