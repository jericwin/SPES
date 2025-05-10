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
def admin_dashboard():
    if 'user_id' not in session or session['role'] != 'admin':
        flash('Please login as admin to access this page', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    stats = {
        'total_applicants': 0,
        'pending_applicants': 0,
        'approved_applicants': 0,
        'rejected_applicants': 0,
        'for_exam_applicants': 0,
        'for_interview_applicants': 0
    }
    recent_applications = []
    monthly_data = []
    exam_overview = {
        'completed': 0,
        'scheduled': 0,
        'in_progress': 0
    }
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
            
            # Get total applicants count
            cursor.execute("""
                SELECT COUNT(*) as total FROM applicants
            """)
            stats['total_applicants'] = cursor.fetchone()['total']

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

            # Get monthly application data for the last 6 months
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

            # Get recent applications (last 5)
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
                LIMIT 5
            """)
            recent_applications = cursor.fetchall()

            # Format dates for display
            for app in recent_applications:
                if app['submission_date']:
                    app['submission_date'] = app['submission_date'].strftime('%B %d, %Y')

        except Exception as e:
            print(f"Error fetching dashboard data: {e}")
            flash('Error loading dashboard data', 'error')
        finally:
            cursor.close()
            conn.close()

    return render_template('admin/dashboard.html', 
                         stats=stats, 
                         recent_applications=recent_applications,
                         monthly_data=monthly_data,
                         exam_overview=exam_overview,
                         latest_announcement=latest_announcement)

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
                cursor.execute("""
                    INSERT INTO announcements 
                    (title, description, target_audience, announcement_type, announcement_date, posted_by)
                    VALUES (%s, %s, %s, %s, CURDATE(), %s)
                """, (title, description, target_audience, announcement_type, session['user_id']))
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

    try:
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor(dictionary=True)
                cursor.execute("""
                    SELECT 
                        a.*,
                        u.name as admin_name
                    FROM announcements a
                    LEFT JOIN users u ON a.posted_by = u.id
                    WHERE a.id = %s
                """, (announcement_id,))
                
                announcement = cursor.fetchone()
                
                if not announcement:
                    return jsonify({'success': False, 'message': 'Announcement not found'})

                # Format date for display
                if announcement['announcement_date']:
                    announcement['announcement_date'] = announcement['announcement_date'].strftime('%B %d, %Y')

                return jsonify({
                    'success': True,
                    'announcement': announcement
                })

            except Exception as e:
                print(f"Database error: {str(e)}")
                return jsonify({'success': False, 'message': f'Database error: {str(e)}'})
            finally:
                cursor.close()
                conn.close()

    except Exception as e:
        print(f"Error fetching announcement: {str(e)}")
        return jsonify({'success': False, 'message': f'Error fetching announcement: {str(e)}'})

@app.route('/delete-announcement/<int:announcement_id>', methods=['POST'])
def delete_announcement(announcement_id):
    if 'user_id' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'})

    try:
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
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
                
                # First get the user_id for this application
                cursor.execute("""
                    SELECT user_id FROM applicants WHERE id = %s
                """, (application_id,))
                result = cursor.fetchone()
                if not result:
                    return jsonify({'success': False, 'message': 'Application not found'})
                
                user_id = result['user_id']
                
                # Update application status
                cursor.execute("""
                    UPDATE applicants 
                    SET status = %s 
                    WHERE id = %s
                """, (new_status, application_id))
                
                # Create notification based on status
                if new_status == 'approved':
                    cursor.execute("""
                        INSERT INTO notifications (user_id, title, message, type)
                        VALUES (%s, %s, %s, %s)
                    """, (
                        user_id,
                        'Application Approved',
                        'Your application has been approved! Please wait for further instructions regarding the examination.',
                        'application_approved'
                    ))
                elif new_status == 'for exam':
                    cursor.execute("""
                        INSERT INTO notifications (user_id, title, message, type, action_url)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        user_id,
                        'Examination Link Available',
                        'Your examination link is now available. Click here to take the exam.',
                        'exam_link',
                        '/examinations'  # This will be implemented later
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

if __name__ == '__main__':
    test_db_connection()  # Test database connection on startup
    app.run(debug=True)