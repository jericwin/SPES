from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_file, Response
from config import get_db_connection
import hashlib
import re
import os
from datetime import datetime
from werkzeug.utils import secure_filename
import io
from functools import wraps
from docx2pdf import convert
import tempfile
import pythoncom

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # Required for flash messages and sessions

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Please log in first'})
        return f(*args, **kwargs)
    return decorated_function

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
    latest_announcement = None
    
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Get latest main announcement
            cursor.execute("""
                SELECT title, description, announcement_date
                FROM announcements 
                WHERE announcement_type = 'main'
                ORDER BY announcement_date DESC 
                LIMIT 1
            """)
            latest_announcement = cursor.fetchone()
            if latest_announcement and latest_announcement['announcement_date']:
                latest_announcement['announcement_date'] = latest_announcement['announcement_date'].strftime('%B %d, %Y')
            
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
                         application_status=application_status,
                         latest_announcement=latest_announcement)

@app.route('/new-applicants/messages')
def new_applicants_messages():
    return render_template('new/messages.html')

@app.route('/new-applicants/notifications')
def new_applicants_notifications():
    if 'user_id' not in session or session['role'] != 'new':
        flash('Please login to access this page', 'error')
        return redirect(url_for('home'))
        
    conn = get_db_connection()
    notifications = []
    
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            # Fetch notifications for the current user
            cursor.execute("""
                SELECT * FROM notifications 
                WHERE user_id = %s 
                ORDER BY created_at DESC
            """, (session['user_id'],))
            notifications = cursor.fetchall()
            
            # Format dates for display
            for notification in notifications:
                if notification['created_at']:
                    notification['created_at'] = notification['created_at'].strftime('%B %d, %Y %I:%M %p')
                    
        except Exception as e:
            print(f"Error fetching notifications: {e}")
            flash('Error loading notifications', 'error')
        finally:
            cursor.close()
            conn.close()
            
    return render_template('new/notifications.html', 
                         username=session['username'],
                         notifications=notifications)

# --------------------------------------------------------ADMIN---------------------------------------------------------------------#
@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get total users
        cursor.execute("SELECT COUNT(*) as count FROM users")
        user_count = cursor.fetchone()['count']
        
        # Get total applications
        cursor.execute("SELECT COUNT(*) as count FROM applicants")
        application_count = cursor.fetchone()['count']
        
        # Get total exams
        cursor.execute("SELECT COUNT(*) as count FROM exams")
        exam_count = cursor.fetchone()['count']
        
        # Get stats for applications
        stats = {
            'total_applicants': 0,
            'pending_applicants': 0,
            'approved_applicants': 0,
            'rejected_applicants': 0,
            'for_exam_applicants': 0,
            'for_interview_applicants': 0,
            'total_exams': exam_count
        }
        
        # Get counts by status
        cursor.execute("""
            SELECT status, COUNT(*) as count 
            FROM applicants 
            GROUP BY status
        """)
        status_counts = cursor.fetchall()
        for status_count in status_counts:
            status = status_count['status']
            count = status_count['count']
            if status == 'pending':
                stats['pending_applicants'] = count
            elif status == 'approved':
                stats['approved_applicants'] = count
            elif status == 'rejected':
                stats['rejected_applicants'] = count
            elif status == 'for exam':
                stats['for_exam_applicants'] = count
            elif status == 'for interview':
                stats['for_interview_applicants'] = count
        
        # Get monthly application data
        cursor.execute("""
            SELECT 
                DATE_FORMAT(submission_date, '%Y-%m') as month,
                COUNT(*) as count
            FROM applicants
            WHERE submission_date >= DATE_SUB(CURDATE(), INTERVAL 6 MONTH)
            GROUP BY DATE_FORMAT(submission_date, '%Y-%m')
            ORDER BY month ASC
        """)
        monthly_data = cursor.fetchall()
        
        # Get exam overview data
        exam_overview = {
            'completed': 0,
            'scheduled': 0,
            'in_progress': 0
        }
        cursor.execute("""
            SELECT 
                CASE 
                    WHEN status = 'approved' THEN 'completed'
                    WHEN status = 'for exam' THEN 'in_progress'
                    WHEN status = 'for interview' THEN 'scheduled'
                    ELSE NULL
                END as exam_status,
                COUNT(*) as count
            FROM applicants
            WHERE status IN ('approved', 'for exam', 'for interview')
            GROUP BY exam_status
        """)
        exam_data = cursor.fetchall()
        for data in exam_data:
            if data['exam_status']:
                exam_overview[data['exam_status']] = data['count']
        
        # Get recent applications
        cursor.execute("""
            SELECT 
                a.id as application_id,
                a.status,
                a.submission_date,
                u.name as applicant_name
            FROM applicants a
            JOIN users u ON a.user_id = u.id
            ORDER BY a.submission_date DESC
            LIMIT 5
        """)
        recent_applications = cursor.fetchall()
        
        # Format dates
        for app in recent_applications:
            if app['submission_date']:
                app['submission_date'] = app['submission_date'].strftime('%B %d, %Y')
        
        cursor.close()
        conn.close()
        
        return render_template('admin/dashboard.html', 
                             stats=stats,
                             recent_applications=recent_applications,
                             monthly_data=monthly_data,
                             exam_overview=exam_overview,
                             exam_count=exam_count)
    except Exception as e:
        print(f"Error in admin dashboard: {str(e)}")
        return "An error occurred", 500

# announcements
@app.route('/admin/announcements')
def admin_announcements():
    if 'user_id' not in session or session['role'] != 'admin':
        flash('Please login as admin to access this page', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    announcements = []
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            # Fetch announcements with admin name
            cursor.execute("""
                SELECT 
                    a.*,
                    u.name as admin_name
                FROM announcements a
                LEFT JOIN users u ON a.posted_by = u.id
                ORDER BY a.announcement_date DESC
            """)
            announcements = cursor.fetchall()

            # Format dates for display
            for announcement in announcements:
                if announcement['announcement_date']:
                    announcement['announcement_date'] = announcement['announcement_date'].strftime('%B %d, %Y')

        except Exception as e:
            print(f"Error fetching announcements: {e}")
            flash('Error loading announcements', 'error')
        finally:
            cursor.close()
            conn.close()

    return render_template('admin/announcements.html', announcements=announcements)

@app.route('/post-announcement', methods=['POST'])
def post_announcement():
    if 'user_id' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'})

    try:
        title = request.form.get('title')
        description = request.form.get('description')
        target_audience = request.form.get('targetAudience')
        announcement_type = request.form.get('announcementType')

        if not title or not description or not target_audience or not announcement_type:
            return jsonify({'success': False, 'message': 'Please fill in all required fields'})

        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                # Insert the announcement
                cursor.execute("""
                    INSERT INTO announcements 
                    (title, description, target_audience, announcement_type, announcement_date, posted_by)
                    VALUES (%s, %s, %s, %s, CURDATE(), %s)
                """, (title, description, target_audience, announcement_type, session['user_id']))
                
                # Get target users based on audience
                if target_audience == 'all':
                    cursor.execute("SELECT id FROM users WHERE role IN ('new', 'old')")
                elif target_audience == 'new':
                    cursor.execute("SELECT id FROM users WHERE role = 'new'")
                else:  # old
                    cursor.execute("SELECT id FROM users WHERE role = 'old'")
                
                target_users = cursor.fetchall()
                
                # Create notifications for each target user
                for user in target_users:
                    cursor.execute("""
                        INSERT INTO notifications (user_id, title, message, type)
                        VALUES (%s, %s, %s, 'general')
                    """, (user[0], title, description))
                
                conn.commit()
                return jsonify({'success': True, 'message': 'Announcement posted successfully'})
            except Exception as e:
                conn.rollback()
                print(f"Database error: {str(e)}")
                return jsonify({'success': False, 'message': f'Database error: {str(e)}'})
            finally:
                cursor.close()
                conn.close()

    except Exception as e:
        print(f"Error posting announcement: {str(e)}")
        return jsonify({'success': False, 'message': f'Error posting announcement: {str(e)}'})

@app.route('/get-announcement/<int:announcement_id>')
def get_announcement(announcement_id):
    if 'user_id' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'})

    conn = None
    cursor = None
    try:
        print(f"Attempting to fetch announcement with ID: {announcement_id}")
        conn = get_db_connection()
        if not conn:
            print("Database connection failed")
            return jsonify({'success': False, 'message': 'Database connection failed'})

        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT 
                a.*,
                u.name as admin_name
            FROM announcements a
            LEFT JOIN users u ON a.posted_by = u.id
            WHERE a.id = %s
        """
        print(f"Executing query: {query} with ID: {announcement_id}")
        cursor.execute(query, (announcement_id,))
        
        announcement = cursor.fetchone()
        print(f"Query result: {announcement}")
        
        if not announcement:
            print(f"No announcement found with ID: {announcement_id}")
            return jsonify({'success': False, 'message': 'Announcement not found'})

        # Format date for display
        if announcement['announcement_date']:
            announcement['announcement_date'] = announcement['announcement_date'].strftime('%B %d, %Y')

        return jsonify({
            'success': True,
            'announcement': announcement
        })

    except Exception as e:
        print(f"Error fetching announcement: {str(e)}")
        error_message = str(e)
        if "MySQL" in error_message:
            print(f"MySQL error: {error_message}")
            return jsonify({'success': False, 'message': 'Database error occurred'})
        elif "cursor" in error_message:
            print(f"Cursor error: {error_message}")
            return jsonify({'success': False, 'message': 'Error accessing database cursor'})
        else:
            print(f"General error: {error_message}")
            return jsonify({'success': False, 'message': 'An error occurred while fetching announcement details'})
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception as e:
                print(f"Error closing cursor: {str(e)}")
        if conn:
            try:
                conn.close()
            except Exception as e:
                print(f"Error closing connection: {str(e)}")

@app.route('/delete-announcement/<int:announcement_id>', methods=['POST'])
def delete_announcement(announcement_id):
    if 'user_id' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'})

    try:
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor(dictionary=True)
                
                # First get the announcement details to delete corresponding notifications
                cursor.execute("""
                    SELECT title FROM announcements 
                    WHERE id = %s
                """, (announcement_id,))
                announcement = cursor.fetchone()
                
                if announcement:
                    # Delete notifications with matching title
                    cursor.execute("""
                        DELETE FROM notifications 
                        WHERE title = %s AND type = 'general'
                    """, (announcement['title'],))
                
                # Delete the announcement
                cursor.execute("DELETE FROM announcements WHERE id = %s", (announcement_id,))
                
                conn.commit()
                return jsonify({'success': True, 'message': 'Announcement deleted successfully'})
            except Exception as e:
                conn.rollback()
                print(f"Database error: {str(e)}")
                return jsonify({'success': False, 'message': f'Database error: {str(e)}'})
            finally:
                cursor.close()
                conn.close()

    except Exception as e:
        print(f"Error deleting announcement: {str(e)}")
        return jsonify({'success': False, 'message': f'Error deleting announcement: {str(e)}'})

@app.route('/update-announcement', methods=['POST'])
def update_announcement():
    if 'user_id' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'})

    try:
        announcement_id = request.form.get('id')
        title = request.form.get('title')
        description = request.form.get('description')
        target_audience = request.form.get('targetAudience')
        announcement_type = request.form.get('announcementType')

        if not all([announcement_id, title, description, target_audience, announcement_type]):
            return jsonify({'success': False, 'message': 'Please fill in all required fields'})

        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                # Update the announcement
                cursor.execute("""
                    UPDATE announcements 
                    SET title = %s, 
                        description = %s, 
                        target_audience = %s, 
                        announcement_type = %s
                    WHERE id = %s
                """, (title, description, target_audience, announcement_type, announcement_id))
                
                # Delete existing notifications for this announcement
                cursor.execute("""
                    DELETE FROM notifications 
                    WHERE title = %s AND type = 'general'
                """, (title,))
                
                # Get target users based on audience
                if target_audience == 'all':
                    cursor.execute("SELECT id FROM users WHERE role IN ('new', 'old')")
                elif target_audience == 'new':
                    cursor.execute("SELECT id FROM users WHERE role = 'new'")
                else:  # old
                    cursor.execute("SELECT id FROM users WHERE role = 'old'")
                
                target_users = cursor.fetchall()
                
                # Create notifications for each target user
                for user in target_users:
                    cursor.execute("""
                        INSERT INTO notifications (user_id, title, message, type)
                        VALUES (%s, %s, %s, 'general')
                    """, (user[0], title, description))
                
                conn.commit()
                return jsonify({'success': True, 'message': 'Announcement updated successfully'})
            except Exception as e:
                conn.rollback()
                print(f"Database error: {str(e)}")
                return jsonify({'success': False, 'message': f'Database error: {str(e)}'})
            finally:
                cursor.close()
                conn.close()

    except Exception as e:
        print(f"Error updating announcement: {str(e)}")
        return jsonify({'success': False, 'message': f'Error updating announcement: {str(e)}'})

# admin applicantions.html
@app.route('/admin/applications')
def admin_applications():
    if 'user_id' not in session or session['role'] != 'admin':
        flash('Please login as admin to access this page', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    applications = []
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            # Fetch applications with user details
            cursor.execute("""
                SELECT 
                    a.id as application_id,
                    a.status,
                    a.submission_date,
                    u.name as applicant_name,
                    ui.first_name,
                    ui.middle_name,
                    ui.last_name,
                    ur.gwa_decimal,
                    ur.gwa_percent
                FROM applicants a
                JOIN users u ON a.user_id = u.id
                LEFT JOIN user_info ui ON u.id = ui.user_id
                LEFT JOIN user_resources ur ON u.id = ur.user_id
                ORDER BY a.submission_date DESC
            """)
            applications = cursor.fetchall()
        except Exception as e:
            print(f"Error fetching applications: {e}")
            flash('Error loading applications', 'error')
        finally:
            cursor.close()
            conn.close()

    return render_template('admin/applications.html', applications=applications)

@app.route('/update-application-status', methods=['POST'])
def update_application_status():
    if 'user_id' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'})

    try:
        data = request.get_json()
        application_id = data.get('application_id')
        new_status = data.get('status')

        if not application_id or not new_status:
            return jsonify({'success': False, 'message': 'Missing required data'})

        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor(dictionary=True)

                # Get user_id from applicants
                cursor.execute("""
                    SELECT user_id FROM applicants WHERE id = %s
                """, (application_id,))
                result = cursor.fetchone()
                if not result:
                    return jsonify({'success': False, 'message': 'Application not found'})

                user_id = result['user_id']

                # Update the application status
                cursor.execute("""
                    UPDATE applicants 
                    SET status = %s 
                    WHERE id = %s
                """, (new_status, application_id))

                # Create notification and assign exam only if status is 'for exam'
                if new_status == 'for exam':
                    # Insert notification
                    cursor.execute("""
                        INSERT INTO notifications (user_id, title, message, type, action_url)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        user_id,
                        'Examination Link Available',
                        'Your examination link is now available. Click here to take the exam.',
                        'exam_link',
                        '/examinations'
                    ))

                    # Only assign exam if user doesn't already have one
                    cursor.execute("""
                        SELECT id FROM exam_assignments WHERE user_id = %s
                    """, (user_id,))
                    existing_assignment = cursor.fetchone()

                    if not existing_assignment:
                        cursor.execute("SELECT id FROM exams ORDER BY id DESC LIMIT 1")
                        exam = cursor.fetchone()
                        if exam:
                            exam_id = exam['id']
                            cursor.execute("""
                                INSERT INTO exam_assignments (user_id, exam_id, status)
                                VALUES (%s, %s, %s)
                            """, (user_id, exam_id, 'assigned'))

                elif new_status == 'approved':
                    cursor.execute("""
                        INSERT INTO notifications (user_id, title, message, type)
                        VALUES (%s, %s, %s, %s)
                    """, (
                        user_id,
                        'Application Approved',
                        'Your application has been approved! Please wait for further instructions regarding the examination.',
                        'application_approved'
                    ))

                conn.commit()
                return jsonify({'success': True, 'message': 'Status updated successfully'})
            except Exception as e:
                conn.rollback()
                print(f"Database error: {str(e)}")
                return jsonify({'success': False, 'message': f'Database error: {str(e)}'})
            finally:
                cursor.close()
                conn.close()
    except Exception as e:
        print(f"Error updating status: {str(e)}")
        return jsonify({'success': False, 'message': f'Error updating status: {str(e)}'})


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

@app.route('/get-application-details/<int:application_id>')
def get_application_details(application_id):
    if 'user_id' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'})

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Fetch only essential application information
        cursor.execute("""
            SELECT 
                a.id as application_id,
                a.status,
                a.submission_date,
                u.name as applicant_name,
                ui.first_name,
                ui.middle_name,
                ui.last_name,
                ur.gwa_decimal,
                ur.gwa_percent,
                ur.cor_filename,
                ur.cog_filename,
                ur.upload_date
            FROM applicants a
            JOIN users u ON a.user_id = u.id
            LEFT JOIN user_info ui ON u.id = ui.user_id
            LEFT JOIN user_resources ur ON u.id = ur.user_id
            WHERE a.id = %s
        """, (application_id,))
        
        application = cursor.fetchone()
        
        if not application:
            return jsonify({'success': False, 'message': 'Application not found'})

        # Format dates for display
        if application['submission_date']:
            application['submission_date'] = application['submission_date'].strftime('%B %d, %Y')
        if application['upload_date']:
            application['upload_date'] = application['upload_date'].strftime('%B %d, %Y %H:%M')

        cursor.close()
        conn.close()

        return jsonify({
            'success': True,
            'application': application
        })

    except Exception as e:
        print(f"Error fetching application details: {str(e)}")
        return jsonify({'success': False, 'message': f'Error fetching application details: {str(e)}'})

@app.route('/download-document/<int:application_id>/<document_type>')
def download_document(application_id, document_type):
    if 'user_id' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'})

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Fetch document data
        cursor.execute("""
            SELECT 
                ur.cor_file,
                ur.cor_filename,
                ur.cog_file,
                ur.cog_filename
            FROM applicants a
            JOIN user_resources ur ON a.user_id = ur.user_id
            WHERE a.id = %s
        """, (application_id,))
        
        result = cursor.fetchone()
        
        if not result:
            return jsonify({'success': False, 'message': 'Document not found'})

        # Get the appropriate file data and filename based on document type
        if document_type == 'cor':
            file_data = result['cor_file']
            filename = result['cor_filename']
        elif document_type == 'cog':
            file_data = result['cog_file']
            filename = result['cog_filename']
        else:
            return jsonify({'success': False, 'message': 'Invalid document type'})

        if not file_data:
            return jsonify({'success': False, 'message': 'File not found'})

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
        print(f"Error downloading document: {str(e)}")
        return jsonify({'success': False, 'message': f'Error downloading document: {str(e)}'})

@app.route('/examinations')
def examinations():
    if 'user_id' not in session or session['role'] != 'new':
        flash('Please login to access this page', 'error')
        return redirect(url_for('home'))
        
    conn = get_db_connection()
    can_take_exam = False
    
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            # Check if user has an application with 'for exam' status
            cursor.execute("""
                SELECT status FROM applicants 
                WHERE user_id = %s 
                ORDER BY submission_date DESC 
                LIMIT 1
            """, (session['user_id'],))
            result = cursor.fetchone()
            
            if result and result['status'] == 'for exam':
                can_take_exam = True
                
        except Exception as e:
            print(f"Error checking exam status: {e}")
            flash('Error checking exam status', 'error')
        finally:
            cursor.close()
            conn.close()
            
    if not can_take_exam:
        flash('You are not authorized to take the exam', 'error')
        return redirect(url_for('new_applicants_dashboard'))
        
    return render_template('new/examinations.html', username=session['username'])

@app.route('/delete-notification/<int:notification_id>', methods=['POST'])
def delete_notification(notification_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Please log in first'})

    try:
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor(dictionary=True)
                # First verify the notification belongs to the current user
                cursor.execute("""
                    SELECT id FROM notifications 
                    WHERE id = %s AND user_id = %s
                """, (notification_id, session['user_id']))
                
                notification = cursor.fetchone()
                if not notification:
                    return jsonify({'success': False, 'message': 'Notification not found or unauthorized'})
                
                # Delete the notification
                cursor.execute("""
                    DELETE FROM notifications 
                    WHERE id = %s AND user_id = %s
                """, (notification_id, session['user_id']))
                
                conn.commit()
                return jsonify({'success': True, 'message': 'Notification deleted successfully'})
            except Exception as e:
                conn.rollback()
                print(f"Database error: {str(e)}")
                return jsonify({'success': False, 'message': f'Database error: {str(e)}'})
            finally:
                cursor.close()
                conn.close()

    except Exception as e:
        print(f"Error deleting notification: {str(e)}")
        return jsonify({'success': False, 'message': f'Error deleting notification: {str(e)}'})

@app.route('/delete-all-notifications', methods=['POST'])
def delete_all_notifications():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Please log in first'})

    try:
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                # Delete all notifications for the current user only
                cursor.execute("""
                    DELETE FROM notifications 
                    WHERE user_id = %s
                """, (session['user_id'],))
                
                conn.commit()
                return jsonify({'success': True, 'message': 'All notifications deleted successfully'})
            except Exception as e:
                conn.rollback()
                print(f"Database error: {str(e)}")
                return jsonify({'success': False, 'message': f'Database error: {str(e)}'})
            finally:
                cursor.close()
                conn.close()

    except Exception as e:
        print(f"Error deleting notifications: {str(e)}")
        return jsonify({'success': False, 'message': f'Error deleting notifications: {str(e)}'})

@app.route('/admin/exams')
def admin_exams():
    if 'user_id' not in session or session['role'] != 'admin':
        flash('Please login as admin to access this page', 'error')
        return redirect(url_for('home'))
    return render_template('admin/exams.html')

@app.route('/admin/get-exams')
@login_required
def get_exams():
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get all exams with creator name
        cursor.execute('''
            SELECT e.id, e.title, e.description, e.time_limit, e.passing_score, e.created_at,
                   u.name as created_by
            FROM exams e
            LEFT JOIN users u ON e.created_by = u.id
            ORDER BY e.created_at DESC
        ''')
        exams = cursor.fetchall()
        
        # Format dates
        for exam in exams:
            if exam['created_at']:
                exam['created_at'] = exam['created_at'].strftime('%B %d, %Y')
        
        cursor.close()
        conn.close()
        
        return jsonify({'success': True, 'exams': exams})
    except Exception as e:
        print(f"Error fetching exams: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/admin/add-exam', methods=['POST'])
@login_required
def add_exam():
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    
    try:
        title = request.form.get('title')
        description = request.form.get('description')
        time_limit = request.form.get('timeLimit')
        passing_score = request.form.get('passingScore')
        
        if not title:
            return jsonify({'success': False, 'message': 'Title is required'})
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Insert new exam
        cursor.execute('''
            INSERT INTO exams (title, description, time_limit, passing_score, created_by, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
        ''', (title, description, time_limit, passing_score, session['user_id']))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Exam created successfully'})
    except Exception as e:
        print(f"Error creating exam: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/admin/get-exam/<int:exam_id>')
@login_required
def get_exam(exam_id):
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get exam details
        cursor.execute('''
            SELECT e.id, e.title, e.description, e.time_limit, e.passing_score, e.created_at,
                   u.name as created_by
            FROM exams e
            LEFT JOIN users u ON e.created_by = u.id
            WHERE e.id = %s
        ''', (exam_id,))
        exam = cursor.fetchone()
        
        if not exam:
            return jsonify({'success': False, 'message': 'Exam not found'})
        
        # Get exam pages
        cursor.execute('''
            SELECT id, title, description, time_limit, order_number 
            FROM exam_pages 
            WHERE exam_id = %s 
            ORDER BY order_number
        ''', (exam_id,))
        pages = cursor.fetchall()
        
        # Get questions for each page
        for page in pages:
            cursor.execute('''
                SELECT id, question_text, question_type, points, correct_answer, is_case_sensitive
                FROM exam_questions 
                WHERE page_id = %s 
                ORDER BY id
            ''', (page['id'],))
            questions = cursor.fetchall()
            
            # Get options for multiple choice questions
            for question in questions:
                if question['question_type'] == 'multiple_choice':
                    cursor.execute('''
                        SELECT id, option_text, is_correct 
                        FROM question_options 
                        WHERE question_id = %s 
                        ORDER BY id
                    ''', (question['id'],))
                    question['options'] = cursor.fetchall()
            
            page['questions'] = questions
        
        exam['pages'] = pages
        
        # Format dates
        if exam['created_at']:
            exam['created_at'] = exam['created_at'].strftime('%B %d, %Y')
        
        cursor.close()
        conn.close()
        
        return jsonify({'success': True, 'exam': exam})
    except Exception as e:
        print(f"Error fetching exam: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/admin/add-page', methods=['POST'])
@login_required
def add_page():
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    
    try:
        exam_id = request.form.get('examId')
        title = request.form.get('title')
        description = request.form.get('description')
        time_limit = request.form.get('timeLimit')
        order_number = request.form.get('orderNumber')
        
        if not all([exam_id, title, order_number]):
            return jsonify({'success': False, 'message': 'Missing required fields'})
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Insert new page
        cursor.execute('''
            INSERT INTO exam_pages (exam_id, title, description, time_limit, order_number)
            VALUES (%s, %s, %s, %s, %s)
        ''', (exam_id, title, description, time_limit, order_number))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Page added successfully'})
    except Exception as e:
        print(f"Error adding page: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/admin/add-question', methods=['POST'])
@login_required
def add_question():
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    
    try:
        page_id = request.form.get('pageId')
        question_type = request.form.get('questionType')
        question_text = request.form.get('questionText')
        points = request.form.get('points')
        
        if not all([page_id, question_type, question_text, points]):
            return jsonify({'success': False, 'message': 'Missing required fields'})
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Insert question
        cursor.execute('''
            INSERT INTO exam_questions (page_id, question_text, question_type, points, correct_answer, is_case_sensitive)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (page_id, question_text, question_type, points, None, False))
        
        question_id = cursor.lastrowid
        
        # Handle question type specific data
        if question_type == 'multiple_choice':
            options = request.form.getlist('options[]')
            correct_option = int(request.form.get('correctOption'))
            
            for i, option_text in enumerate(options):
                cursor.execute('''
                    INSERT INTO question_options (question_id, option_text, is_correct)
                    VALUES (%s, %s, %s)
                ''', (question_id, option_text, i == correct_option))
                
                if i == correct_option:
                    cursor.execute('''
                        UPDATE exam_questions 
                        SET correct_answer = %s 
                        WHERE id = %s
                    ''', (option_text, question_id))
        
        elif question_type == 'identification':
            answer_text = request.form.get('answerText')
            is_case_sensitive = request.form.get('isCaseSensitive') == 'on'
            
            cursor.execute('''
                UPDATE exam_questions 
                SET correct_answer = %s, is_case_sensitive = %s 
                WHERE id = %s
            ''', (answer_text, is_case_sensitive, question_id))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Question added successfully'})
    except Exception as e:
        print(f"Error adding question: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/admin/delete-exam/<int:exam_id>', methods=['POST'])
@login_required
def delete_exam(exam_id):
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Delete exam and all related data
        cursor.execute('DELETE FROM exams WHERE id = %s', (exam_id,))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Exam deleted successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/admin/delete-page/<int:page_id>', methods=['POST'])
@login_required
def delete_page(page_id):
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Delete page (cascade will handle questions and options)
        cursor.execute('DELETE FROM exam_pages WHERE id = %s', (page_id,))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Page deleted successfully'})
    except Exception as e:
        print(f"Error deleting page: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/admin/delete-question/<int:question_id>', methods=['POST'])
@login_required
def delete_question(question_id):
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Delete question (cascade will handle options)
        cursor.execute('DELETE FROM exam_questions WHERE id = %s', (question_id,))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Question deleted successfully'})
    except Exception as e:
        print(f"Error deleting question: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

def is_admin():
    return 'user_id' in session and session.get('role') == 'admin'

@app.route('/get-current-exam')
def get_current_exam():
    if 'user_id' not in session or session['role'] != 'new':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get the exam assigned to the user
        cursor.execute("""
            SELECT e.*, u.name as created_by
            FROM exams e
            LEFT JOIN users u ON e.created_by = u.id
            WHERE e.id = (
                SELECT exam_id 
                FROM exam_assignments 
                WHERE user_id = %s 
                AND status = 'assigned'
                LIMIT 1
            )
        """, (session['user_id'],))
        
        exam = cursor.fetchone()
        
        if not exam:
            return jsonify({'success': False, 'message': 'No exam assigned'})
        
        # Get exam pages
        cursor.execute("""
            SELECT id, title, description, time_limit, order_number 
            FROM exam_pages 
            WHERE exam_id = %s 
            ORDER BY order_number
        """, (exam['id'],))
        pages = cursor.fetchall()
        
        # Get questions for each page
        for page in pages:
            cursor.execute("""
                SELECT id, question_text, question_type, points
                FROM exam_questions 
                WHERE page_id = %s 
                ORDER BY id
            """, (page['id'],))
            questions = cursor.fetchall()
            
            # Get options for multiple choice questions
            for question in questions:
                if question['question_type'] == 'multiple_choice':
                    cursor.execute("""
                        SELECT option_text 
                        FROM question_options 
                        WHERE question_id = %s 
                        ORDER BY id
                    """, (question['id'],))
                    question['options'] = cursor.fetchall()
            
            page['questions'] = questions
        
        exam['pages'] = pages
        
        # Format dates
        if exam['created_at']:
            exam['created_at'] = exam['created_at'].strftime('%B %d, %Y')
        
        cursor.close()
        conn.close()
        
        return jsonify({'success': True, 'exam': exam})
    except Exception as e:
        print(f"Error fetching exam: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/submit-exam', methods=['POST'])
def submit_exam():
    if 'user_id' not in session or session['role'] != 'new':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        data = request.get_json()
        exam_id = data.get('exam_id')
        answers = data.get('answers')
        
        if not exam_id or not answers:
            return jsonify({'success': False, 'message': 'Missing required data'})
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Calculate score
        total_points = 0
        earned_points = 0
        
        # Get all questions for the exam
        cursor.execute("""
            SELECT q.id, q.points, q.correct_answer, q.is_case_sensitive, q.question_type
            FROM exam_questions q
            JOIN exam_pages p ON q.page_id = p.id
            WHERE p.exam_id = %s
        """, (exam_id,))
        questions = cursor.fetchall()
        
        for question in questions:
            total_points += question[1]  # points
            user_answer = answers.get(str(question[0]))
            
            if user_answer:
                if question[4] == 'multiple_choice':  # question_type
                    if user_answer == question[2]:  # correct_answer
                        earned_points += question[1]
                else:  # identification
                    if question[3]:  # is_case_sensitive
                        if user_answer == question[2]:
                            earned_points += question[1]
                    else:
                        if user_answer.lower() == question[2].lower():
                            earned_points += question[1]
        
        # Calculate percentage
        score_percentage = (earned_points / total_points * 100) if total_points > 0 else 0
        
        # Get passing score
        cursor.execute("SELECT passing_score FROM exams WHERE id = %s", (exam_id,))
        passing_score = cursor.fetchone()[0]
        
        # Record exam attempt
        cursor.execute("""
            INSERT INTO exam_attempts 
            (user_id, exam_id, score, total_points, passed, submitted_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
        """, (session['user_id'], exam_id, earned_points, total_points, score_percentage >= passing_score))
        
        # Update exam assignment status
        cursor.execute("""
            UPDATE exam_assignments 
            SET status = 'completed', completed_at = NOW()
            WHERE user_id = %s AND exam_id = %s
        """, (session['user_id'], exam_id))
        
        # Update application status if passed
        if score_percentage >= passing_score:
            cursor.execute("""
                UPDATE applicants 
                SET status = 'for interview', exam_id = %s
                WHERE user_id = %s 
                ORDER BY submission_date DESC 
                LIMIT 1
            """, (exam_id, session['user_id']))
            
            # Create notification
            cursor.execute("""
                INSERT INTO notifications (user_id, title, message, type)
                VALUES (%s, %s, %s, 'exam_result')
            """, (
                session['user_id'],
                'Exam Completed',
                f'Congratulations! You have passed the exam with a score of {score_percentage:.1f}%. Please wait for interview scheduling.',
            ))
        else:
            cursor.execute("""
                UPDATE applicants 
                SET status = 'rejected', exam_id = %s
                WHERE user_id = %s 
                ORDER BY submission_date DESC 
                LIMIT 1
            """, (exam_id, session['user_id']))
            
            # Create notification
            cursor.execute("""
                INSERT INTO notifications (user_id, title, message, type)
                VALUES (%s, %s, %s, 'exam_result')
            """, (
                session['user_id'],
                'Exam Completed',
                f'You have completed the exam with a score of {score_percentage:.1f}%. Unfortunately, you did not meet the passing score.',
            ))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({
            'success': True, 
            'message': 'Exam submitted successfully',
            'score': score_percentage,
            'passed': score_percentage >= passing_score
        })
    except Exception as e:
        print(f"Error submitting exam: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
    
@app.route('/new-applicants/forms')
def new_applicants_forms():
    return render_template('new/spes-form.html')

@app.route('/preview-document/<document_type>')
def preview_document(document_type):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Please login first'})
        
    templates = {
        'APPLICATION-FORM': 'APPLICATION-FORM.docx',
        'SCHOOL-CERTIFICATION': 'SCHOOL-CERTIFICATION.docx',
        'SPES-EMPLOYMENT-CONTRACT': 'SPES-EMPLOYMENT-CONTRACT.docx',
        'OATH-OF-UNDERTAKING': 'OATH-OF-UNDERTAKING.docx'
    }
    
    if document_type not in templates:
        return jsonify({'success': False, 'message': 'Invalid document type'})
        
    try:
        pythoncom.CoInitialize()  # Initialize COM for this thread
        
        # Create temporary directory for conversion
        with tempfile.TemporaryDirectory() as temp_dir:
            docx_path = os.path.join('static/spes-forms', templates[document_type])
            pdf_path = os.path.join(temp_dir, f'{document_type}.pdf')
            
            # Convert DOCX to PDF
            convert(docx_path, pdf_path)
            
            # Read the PDF file
            with open(pdf_path, 'rb') as f:
                pdf_data = f.read()
                
            # Return PDF with appropriate headers
            return Response(
                pdf_data,
                mimetype='application/pdf',
                headers={
                    'Content-Disposition': f'inline; filename={document_type}.pdf'
                }
            )
    except Exception as e:
        print(f"Error converting document: {e}")
        return jsonify({'success': False, 'message': 'Error previewing document'})
    finally:
        try:
            pythoncom.CoUninitialize()  # Clean up COM
        except:
            pass

if __name__ == '__main__':
    test_db_connection()  # Test database connection on startup
    app.run(debug=True)