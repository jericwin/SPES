from config import get_db_connection
import os

def apply_migration():
    conn = get_db_connection()
    if not conn:
        print("Failed to connect to database")
        return

    cursor = conn.cursor()
    
    try:
        # Check if column already exists
        cursor.execute("SHOW COLUMNS FROM applicants LIKE 'year_applied'")
        result = cursor.fetchone()
        
        if not result:
            print("Applying migration: 002_add_year_applied.sql")
            with open('migrations/002_add_year_applied.sql', 'r') as f:
                sql_script = f.read()
                
            # Split by semicolon and execute each statement
            statements = sql_script.split(';')
            for statement in statements:
                if statement.strip():
                    print(f"Executing: {statement.strip()[:50]}...")
                    cursor.execute(statement)
            
            conn.commit()
            print("Migration applied successfully.")
        else:
            print("Column 'year_applied' already exists. Skipping migration.")
            
    except Exception as e:
        print(f"Error applying migration: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    apply_migration()
