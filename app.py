from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_file
from config import get_db_connection
import hashlib
import re
import os
from datetime import datetime
from werkzeug.utils import secure_filename
import io

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # Required for flash messages and sessions

# Configure upload folder
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Test database connection
def test_db_connection():
    conn = get_db_connection()
    if conn:
        print("Successfully connected to MySQL database!")
        conn.close()
    else:
        print("Failed to connect to MySQL database!")

@app.route('/') 
def home():
    return render_template('home.html')

@app.route('/new-applicants/dashboard')
def new_applicants_dashboard():
    if 'user_id' not in session or session['role'] != 'new':
        flash('Please login to access this page', 'error')
        return redirect(url_for('home'))
        
    # Check if user has already submitted an application
    conn = get_db_connection()
    has_submitted = False
    application_status = None
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            # Check if there's an active application
            cursor.execute("""
                SELECT a.status, a.id 
                FROM applicants a 
                WHERE a.user_id = %s 
                ORDER BY a.submission_date DESC 
                LIMIT 1
            """, (session['user_id'],))
            result = cursor.fetchone()
            
            if result:
                has_submitted = True
                application_status = result['status']
                
        except Exception as e:
            print(f"Error: {e}")
        finally:
            cursor.close()
            conn.close()
            
    return render_template('new/dashboard.html', 
                         username=session['username'], 
                         has_submitted=has_submitted,
                         application_status=application_status)

@app.route('/new-applicants/messages')
def new_applicants_messages():
    return render_template('new/messages.html')

@app.route('/new-applicants/notifications')
def new_applicants_notifications():
    return render_template('new/notifications.html')

# --------------------------------------------------------ADMIN---------------------------------------------------------------------#
@app.route('/admin/dashboard')
def admin_dashboard():
    return render_template('admin/dashboard.html')

# announcements
@app.route('/admin/announcements')
def admin_announcements():
    return render_template('admin/announcements.html')

# admin applicantions.html
@app.route('/admin/applications')
def admin_applications():
    return render_template('admin/applications.html')

# -----------------------------------------------------------------------------------------------------

def is_valid_email(email):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(pattern, email) is not None

@app.route('/register', methods=['POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        confirm_password = request.form['confirmPassword']
        applicant_type = request.form['applicantType']
        
        # Validation
        if not is_valid_email(email):
            flash('Please enter a valid email address', 'error')
            return redirect(url_for('home'))
            
        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return redirect(url_for('home'))
            
        if len(password) < 6:
            flash('Password must be at least 6 characters long', 'error')
            return redirect(url_for('home'))
        
        # Hash the password
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                # Check if email already exists
                cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
                if cursor.fetchone():
                    flash('Email already registered', 'error')
                    return redirect(url_for('home'))
                
                # Insert into users table
                cursor.execute(
                    "INSERT INTO users (name, email, password_hash, role) VALUES (%s, %s, %s, %s)",
                    (username, email, password_hash, applicant_type)
                )
                conn.commit()
                flash('Registration successful! Please login.', 'success')
                return redirect(url_for('home'))
            except Exception as e:
                flash('Registration failed. Please try again.', 'error')
                print(f"Error: {e}")
            finally:
                cursor.close()
                conn.close()
    return redirect(url_for('home'))

@app.route('/login', methods=['POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    "SELECT * FROM users WHERE name = %s AND password_hash = %s",
                    (username, password_hash)
                )
                user = cursor.fetchone()
                
                if user:
                    # Store user data in session
                    session['user_id'] = user['id']
                    session['username'] = user['name']
                    session['role'] = user['role']
                    
                    # Redirect based on role
                    if user['role'] == 'admin':
                        return redirect(url_for('admin_dashboard'))
                    elif user['role'] == 'new':
                        return redirect(url_for('new_applicants_dashboard'))
                    else:
                        return redirect(url_for('home'))
                else:
                    flash('Invalid username or password', 'error')
            except Exception as e:
                flash('Login failed. Please try again.', 'error')
                print(f"Error: {e}")
            finally:
                cursor.close()
                conn.close()
    return redirect(url_for('home'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/submit-application', methods=['POST'])
def submit_application():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Please log in first'})

    try:
        # Get form data
        first_name = request.form.get('firstName')
        middle_name = request.form.get('middleName')
        last_name = request.form.get('lastName')
        address = request.form.get('address')
        contact_number = request.form.get('contactNumber')
        birth_date = request.form.get('birthDate')
        gwa = request.form.get('gwa')

        # Get file data
        cor_file = request.files.get('corUpload')
        cog_file = request.files.get('cogUpload')

        # Validate files
        if not cor_file or not cog_file:
            return jsonify({'success': False, 'message': 'Please upload both required files'})

        if not cor_file.filename.endswith('.pdf') or not cog_file.filename.endswith('.pdf'):
            return jsonify({'success': False, 'message': 'Only PDF files are allowed'})

        # Read file data
        cor_data = cor_file.read()
        cog_data = cog_file.read()

        # Get database connection
        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            # Check for existing application
            cursor.execute("""
                SELECT id FROM applicants 
                WHERE user_id = %s 
                ORDER BY submission_date DESC 
                LIMIT 1
            """, (session['user_id'],))
            existing_app = cursor.fetchone()

            if existing_app:
                # Update existing application status to 'pending'
                cursor.execute("""
                    UPDATE applicants 
                    SET status = 'pending', 
                        submission_date = CURDATE() 
                    WHERE id = %s
                """, (existing_app[0],))
            else:
                # Insert new application
                cursor.execute("""
                    INSERT INTO applicants (user_id, status, submission_date)
                    VALUES (%s, 'pending', CURDATE())
                """, (session['user_id'],))

            # Insert or update user_info
            cursor.execute("""
                INSERT INTO user_info (user_id, first_name, middle_name, last_name, address, contact_number, birth_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                first_name = VALUES(first_name),
                middle_name = VALUES(middle_name),
                last_name = VALUES(last_name),
                address = VALUES(address),
                contact_number = VALUES(contact_number),
                birth_date = VALUES(birth_date)
            """, (session['user_id'], first_name, middle_name, last_name, address, contact_number, birth_date))

            # Insert or update user_resources
            cursor.execute("""
                INSERT INTO user_resources (user_id, cor_file, cor_filename, cog_file, cog_filename, gwa_decimal, gwa_percent)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                cor_file = VALUES(cor_file),
                cor_filename = VALUES(cor_filename),
                cog_file = VALUES(cog_file),
                cog_filename = VALUES(cog_filename),
                gwa_decimal = VALUES(gwa_decimal),
                gwa_percent = VALUES(gwa_percent),
                upload_date = NOW()
            """, (
                session['user_id'],
                cor_data,
                cor_file.filename,
                cog_data,
                cog_file.filename,
                float(gwa) if float(gwa) <= 5.0 else None,
                int(float(gwa)) if float(gwa) > 5.0 else None
            ))

            conn.commit()
            return jsonify({'success': True, 'message': 'Application submitted successfully'})

        except Exception as db_error:
            conn.rollback()
            print(f"Database error: {str(db_error)}")
            return jsonify({'success': False, 'message': f'Database error: {str(db_error)}'})

        finally:
            cursor.close()
            conn.close()

    except Exception as e:
        print(f"Application error: {str(e)}")
        return jsonify({'success': False, 'message': f'Error processing application: {str(e)}'})

# Add a route to serve files from database
@app.route('/get-file/<int:user_id>/<file_type>')
def get_file(user_id, file_type):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Please log in first'})

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get file data from database
        if file_type == 'cor':
            cursor.execute("SELECT cor_file, cor_filename FROM user_resources WHERE user_id = %s", (user_id,))
        elif file_type == 'cog':
            cursor.execute("SELECT cog_file, cog_filename FROM user_resources WHERE user_id = %s", (user_id,))
        else:
            return jsonify({'success': False, 'message': 'Invalid file type'})

        result = cursor.fetchone()
        if not result:
            return jsonify({'success': False, 'message': 'File not found'})

        # Get file data and filename
        file_data = result['cor_file'] if file_type == 'cor' else result['cog_file']
        filename = result['cor_filename'] if file_type == 'cor' else result['cog_filename']

        cursor.close()
        conn.close()

        # Return file as response
        return send_file(
            io.BytesIO(file_data),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        print(f"Error retrieving file: {str(e)}")
        return jsonify({'success': False, 'message': f'Error retrieving file: {str(e)}'})

@app.route('/revoke-application', methods=['POST'])
def revoke_application():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Please log in first'})

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            # Delete from applicants table
            cursor.execute("""
                DELETE FROM applicants 
                WHERE user_id = %s
            """, (session['user_id'],))

            # Delete from user_resources table
            cursor.execute("""
                DELETE FROM user_resources 
                WHERE user_id = %s
            """, (session['user_id'],))

            # Delete from user_info table
            cursor.execute("""
                DELETE FROM user_info 
                WHERE user_id = %s
            """, (session['user_id'],))

            conn.commit()
            return jsonify({
                'success': True, 
                'message': 'Application has been revoked successfully. You can now submit a new application.'
            })

        except Exception as db_error:
            conn.rollback()
            print(f"Database error: {str(db_error)}")
            return jsonify({'success': False, 'message': f'Database error: {str(db_error)}'})

        finally:
            cursor.close()
            conn.close()

    except Exception as e:
        print(f"Application error: {str(e)}")
        return jsonify({'success': False, 'message': f'Error revoking application: {str(e)}'})

if __name__ == '__main__':
    test_db_connection()  # Test database connection on startup
    app.run(debug=True)