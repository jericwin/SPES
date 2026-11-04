#!/bin/bash
set -e

echo "=== DigiSPES Container Initializing ==="

# Check if an external database is configured
if [ -n "$DATABASE_URL" ] || [ -n "$JAWSDB_URL" ] || { [ -n "$DB_HOST" ] && [ "$DB_HOST" != "localhost" ] && [ "$DB_HOST" != "127.0.0.1" ]; }; then
    echo "Using external MySQL database configuration."
else
    echo "Starting bundled MariaDB server..."
    mkdir -p /var/run/mysqld
    chown -R mysql:mysql /var/run/mysqld

    if [ ! -d "/var/lib/mysql/mysql" ]; then
        echo "Installing MariaDB system tables..."
        mysql_install_db --user=mysql --datadir=/var/lib/mysql > /dev/null 2>&1
    fi

    # Enable networking on 127.0.0.1
    sed -i 's/^bind-address.*/bind-address = 0.0.0.0/' /etc/mysql/mariadb.conf.d/50-server.cnf 2>/dev/null || true

    service mariadb start || /etc/init.d/mariadb start

    echo "Waiting for MariaDB to be ready..."
    for i in $(seq 1 30); do
        if mysqladmin ping -u root --silent 2>/dev/null; then
            break
        fi
        sleep 1
    done

    # Setup database and application user
    mysql -u root -e "CREATE DATABASE IF NOT EXISTS digispes CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" || true
    mysql -u root -e "CREATE USER IF NOT EXISTS 'digispes_user'@'%' IDENTIFIED BY 'digispes_pass';" || true
    mysql -u root -e "GRANT ALL PRIVILEGES ON digispes.* TO 'digispes_user'@'%';" || true
    mysql -u root -e "FLUSH PRIVILEGES;" || true

    # Always check if exams table exists, if not import
    HAS_EXAMS=$(mysql -u root -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'digispes' AND table_name = 'exams';" 2>/dev/null || echo 0)
    if [ "$HAS_EXAMS" -eq 0 ]; then
        echo "Importing initial database dump (digispes JAN 5.sql)..."
        if [ -f "/app/digispes (JAN 5).sql" ]; then
            mysql -u root --force --default-character-set=utf8mb4 digispes < "/app/digispes (JAN 5).sql" || true
        elif [ -f "/app/schema.sql" ]; then
            mysql -u root --force --default-character-set=utf8mb4 digispes < "/app/schema.sql" || true
        fi
        echo "Database imported successfully!"
    else
        echo "Database already populated."
    fi

    # Set default credentials for local MariaDB
    export DB_HOST="127.0.0.1"
    export DB_PORT="3306"
    export DB_USER="digispes_user"
    export DB_PASSWORD="digispes_pass"
    export DB_NAME="digispes"
fi

echo "Starting DigiSPES web application on port ${PORT:-5000}..."
exec gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 --bind 0.0.0.0:${PORT:-5000} app:app
