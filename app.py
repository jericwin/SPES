from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_file, Response
from config import get_db_connection
import hashlib
import re
import os
import uuid
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
import io
from functools import wraps
from docx2pdf import convert
import tempfile
import pythoncom
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter
# import numpy as np
import base64
# import cv2
import mysql.connector
import threading
import time
import pytz
from flask_socketio import SocketIO, emit, join_room, leave_room
from threading import Timer

# FAQS
from flask import Flask, request, jsonify
import difflib
# FAQS

# Configure Tesseract path
if os.name == 'nt':  # Windows
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
elif os.name == 'posix':  # Linux/Mac
    pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # Required for flash messages and sessions
socketio = SocketIO(app, cors_allowed_origins="*")

# Track active chat timers and sockets
active_chats = {}  # key: room, value: Timer
user_sid_map = {}  # key: user_id, value: sid

# Helper to close chat room
def close_chat(room, reason):
    socketio.emit('chat_closed', {'room': room, 'reason': reason}, room=room)
    if room in active_chats:
        active_chats[room].cancel()
        del active_chats[room]

# Reset inactivity timer for a chat room
def reset_inactivity_timer(room):
    if room in active_chats:
        active_chats[room].cancel()
    timer = Timer(180, lambda: close_chat(room, 'No messages for 3 minutes. Chat closed.'))
    active_chats[room] = timer
    timer.start()

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
    if 'user_id' not in session or session['role'] != 'new':
        flash('Please login to access this page', 'error')
        return redirect(url_for('home'))
    return render_template('new/messages.html', username=session['username'])

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
                # If exam_link, check assignment status and exam status
                if notification['type'] == 'exam_link' and notification['action_url'] == '/examinations':
                    # Get the most recent exam assignment for this user
                    cursor.execute("""
                        SELECT ea.status, e.status as exam_status 
                        FROM exam_assignments ea
                        JOIN exams e ON ea.exam_id = e.id
                        WHERE ea.user_id = %s 
                        ORDER BY ea.assigned_at DESC LIMIT 1
                    """, (session['user_id'],))
                    assignment = cursor.fetchone()
                    notification['assignment_status'] = assignment['status'] if assignment else None
                    notification['exam_status'] = assignment['exam_status'] if assignment else None
        except Exception as e:
            print(f"Error fetching notifications: {e}")
            flash('Error loading notifications', 'error')
        finally:
            cursor.close()
            conn.close()
    return render_template('new/notifications.html', 
                         username=session['username'],
                         notifications=notifications)

@app.route('/old-applicants/dashboard')
def old_applicants_dashboard():
    if 'user_id' not in session or session['role'] != 'old':
        flash('Please login to access this page', 'error')
        return redirect(url_for('home'))
    
    # Get latest announcement for old applicants
    conn = get_db_connection()
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
        except Exception as e:
            print(f"Error: {e}")
        finally:
            cursor.close()
            conn.close()
    return render_template('old/dashboard.html', 
                         username=session['username'], 
                         latest_announcement=latest_announcement)

@app.route('/old-applicants/messages')
def old_applicants_messages():
    if 'user_id' not in session or session['role'] != 'old':
        flash('Please login to access this page', 'error')
        return redirect(url_for('home'))
    return render_template('old/messages.html', username=session['username'])

@app.route('/old-applicants/notifications')
def old_applicants_notifications():
    if 'user_id' not in session or session['role'] != 'old':
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
    return render_template('old/notifications.html', 
                         username=session['username'],
                         notifications=notifications)

@app.route('/old-applicants/forms')
def old_applicants_forms():
    if 'user_id' not in session or session['role'] != 'old':
        flash('Please login to access this page', 'error')
        return redirect(url_for('home'))
    
    # Check if user has already uploaded documents
    conn = get_db_connection()
    uploaded_documents = None
    has_uploaded = False
    
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT * FROM user_documents 
                WHERE user_id = %s 
                ORDER BY updated_at DESC 
                LIMIT 1
            """, (session['user_id'],))
            uploaded_documents = cursor.fetchone()
            
            if uploaded_documents:
                has_uploaded = True
                # Format the date
                if uploaded_documents['updated_at']:
                    uploaded_documents['updated_at'] = uploaded_documents['updated_at'].strftime('%B %d, %Y at %I:%M %p')
                    
        except Exception as e:
            print(f"Error fetching uploaded documents: {e}")
        finally:
            cursor.close()
            conn.close()
    
    return render_template('old/spes-form.html', 
                         username=session['username'],
                         uploaded_documents=uploaded_documents,
                         has_uploaded=has_uploaded)

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
                    ui.last_name
                FROM applicants a
                JOIN users u ON a.user_id = u.id
                LEFT JOIN user_info ui ON u.id = ui.user_id
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
                    # Get the most recent exam
                    cursor.execute("""
                        SELECT id FROM exams 
                        ORDER BY created_at DESC 
                        LIMIT 1
                    """)
                    exam = cursor.fetchone()

                    if not exam:
                        return jsonify({'success': False, 'message': 'No exam available. Please create an exam first.'})

                    exam_id = exam['id']

                    # Update exam_id in applicants table
                    cursor.execute("""
                        UPDATE applicants 
                        SET exam_id = %s 
                        WHERE id = %s
                    """, (exam_id, application_id))

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

                    # Check if user already has an active exam assignment
                    cursor.execute("""
                        SELECT id FROM exam_assignments 
                        WHERE user_id = %s 
                        AND status IN ('assigned', 'in_progress')
                    """, (user_id,))
                    existing_assignment = cursor.fetchone()

                    if not existing_assignment:
                        # Create new exam assignment
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
                    # Insert into examinees if not already present
                    cursor.execute("""
                        SELECT id FROM examinees WHERE user_id = %s
                    """, (user_id,))
                    if not cursor.fetchone():
                        cursor.execute("""
                            INSERT INTO examinees (user_id, status)
                            VALUES (%s, 'for exam')
                        """, (user_id,))

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
                    elif user['role'] == 'old':
                        return redirect(url_for('old_applicants_dashboard'))
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

def process_file(file, filename):
    if not file:
        return None, None
    
    try:
        # Read file content
        file_content = file.read()
        file.seek(0)  # Reset file pointer
        
        # If it's an image, compress and convert to PDF
        if file.content_type.startswith('image/'):
            try:
                # Open and compress image
                image = Image.open(io.BytesIO(file_content))
                
                # Calculate new dimensions while maintaining aspect ratio
                max_size = (1024, 1024)  # Maximum dimensions
                image.thumbnail(max_size, Image.Resampling.LANCZOS)
                
                # Convert to RGB if necessary (for PNG with transparency)
                if image.mode in ('RGBA', 'LA'):
                    background = Image.new('RGB', image.size, (255, 255, 255))
                    background.paste(image, mask=image.split()[-1])
                    image = background
                elif image.mode != 'RGB':
                    image = image.convert('RGB')
                
                # Save as compressed PDF
                pdf_buffer = io.BytesIO()
                image.save(pdf_buffer, format='PDF', quality=85, optimize=True)
                file_content = pdf_buffer.getvalue()
                filename = os.path.splitext(filename)[0] + '.pdf'
                
                # If PDF is still too large, compress it further
                if len(file_content) > 4 * 1024 * 1024:  # If larger than 4MB
                    # Reduce quality and size further
                    image.thumbnail((800, 800), Image.Resampling.LANCZOS)
                    pdf_buffer = io.BytesIO()
                    image.save(pdf_buffer, format='PDF', quality=70, optimize=True)
                    file_content = pdf_buffer.getvalue()
                
            except Exception as e:
                print(f"Error converting image to PDF: {str(e)}")
                return None, None
        
        return file_content, filename
    except Exception as e:
        print(f"Error processing file: {str(e)}")
        return None, None

@app.route('/update-old-applicant-forms', methods=['POST'])
def update_old_applicant_forms():
    if 'user_id' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    
    try:
        user_id = request.form.get('user_id')
        if not user_id:
            return jsonify({'success': False, 'message': 'User ID is required'})
        
        # Get form data
        application_form = request.files.get('applicationForm')
        employment_contract = request.files.get('employmentContract')
        oath_of_undertaking = request.files.get('oathOfUndertaking')
        school_certification = request.files.get('schoolCertification')
        cor_or_coe = request.files.get('corOrCoe')
        cog = request.files.get('cog')
        barangay_indigency = request.files.get('barangayIndigency')
        psa_birth_certificate = request.files.get('psaBirthCertificate')
        
        # Create user documents directory if it doesn't exist
        user_docs_dir = os.path.join('static', 'user_documents', str(user_id))
        os.makedirs(user_docs_dir, exist_ok=True)
        
        # Save files with unique names
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        uuid_str = str(uuid.uuid4())
        
        files_saved = {}
        file_mapping = {
            'applicationForm': 'APPLICATION-FORM',
            'employmentContract': 'SPES-EMPLOYMENT-CONTRACT',
            'oathOfUndertaking': 'OATH-OF-UNDERTAKING',
            'schoolCertification': 'SCHOOL-CERTIFICATION',
            'corOrCoe': 'COR-COE',
            'cog': 'COPY-OF-GRADES',
            'barangayIndigency': 'BARANGAY-INDIGENCY',
            'psaBirthCertificate': 'PSA-BIRTH-CERTIFICATE'
        }
        
        # Process each file if provided
        files_to_process = [
            ('applicationForm', application_form),
            ('employmentContract', employment_contract),
            ('oathOfUndertaking', oath_of_undertaking),
            ('schoolCertification', school_certification),
            ('corOrCoe', cor_or_coe),
            ('cog', cog),
            ('barangayIndigency', barangay_indigency),
            ('psaBirthCertificate', psa_birth_certificate)
        ]
        
        for field_name, file in files_to_process:
            if file and file.filename:
                # Validate file type
                if not file.filename.lower().endswith(('.docx', '.pdf')):
                    return jsonify({
                        'success': False,
                        'message': f'{field_name} must be a .docx or .pdf file'
                    })
                
                # Check file size (5MB limit)
                if len(file.read()) > 5 * 1024 * 1024:
                    return jsonify({
                        'success': False,
                        'message': f'{field_name} file size exceeds 5MB limit'
                    })
                file.seek(0)  # Reset file pointer
                
                file_extension = os.path.splitext(file.filename)[1].lower()
                new_filename = f"{file_mapping[field_name]}_{timestamp}_{uuid_str}{file_extension}"
                file_path = os.path.join(user_docs_dir, new_filename)
                file.save(file_path)
                files_saved[field_name] = new_filename
        
        # Update database
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                
                # Check if user already has documents
                cursor.execute("SELECT id FROM user_documents WHERE user_id = %s", (user_id,))
                existing_docs = cursor.fetchone()
                
                if existing_docs:
                    # Update existing record with only the files that were provided
                    update_fields = []
                    update_values = []
                    
                    for field_name in file_mapping.keys():
                        if field_name in files_saved:
                            update_fields.append(f"{field_name.replace('Form', '_form').replace('Contract', '_contract').replace('Undertaking', '_undertaking').replace('Certification', '_certification').replace('OrCoe', '_or_coe').replace('Indigency', '_indigency').replace('Certificate', '_certificate')} = %s")
                            update_values.append(files_saved[field_name])
                    
                    if update_fields:
                        update_fields.append("updated_at = NOW()")
                        update_values.append(user_id)
                        
                        query = f"UPDATE user_documents SET {', '.join(update_fields)} WHERE user_id = %s"
                        cursor.execute(query, update_values)
                else:
                    # Insert new record
                    cursor.execute("""
                        INSERT INTO user_documents (
                            user_id, application_form, employment_contract, 
                            oath_of_undertaking, school_certification, cor_or_coe,
                            cog, barangay_indigency, psa_birth_certificate, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """, (
                        user_id,
                        files_saved.get('applicationForm'),
                        files_saved.get('employmentContract'),
                        files_saved.get('oathOfUndertaking'),
                        files_saved.get('schoolCertification'),
                        files_saved.get('corOrCoe'),
                        files_saved.get('cog'),
                        files_saved.get('barangayIndigency'),
                        files_saved.get('psaBirthCertificate')
                    ))
                
                conn.commit()
                cursor.close()
                conn.close()
                
                return jsonify({
                    'success': True,
                    'message': 'Documents updated successfully!'
                })
                
            except Exception as e:
                print(f"Error updating documents: {e}")
                return jsonify({
                    'success': False,
                    'message': 'Error updating documents to database'
                })
        else:
            return jsonify({
                'success': False,
                'message': 'Database connection error'
            })
            
    except Exception as e:
        print(f"Error in update_old_applicant_forms: {e}")
        return jsonify({
            'success': False,
            'message': 'An error occurred while updating documents'
        })

@app.route('/submit-old-applicant-forms', methods=['POST'])
def submit_old_applicant_forms():
    if 'user_id' not in session or session['role'] != 'old':
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    
    try:
        user_id = session['user_id']
        
        # Get form data
        application_form = request.files.get('applicationForm')
        employment_contract = request.files.get('employmentContract')
        oath_of_undertaking = request.files.get('oathOfUndertaking')
        school_certification = request.files.get('schoolCertification')
        cor_or_coe = request.files.get('corOrCoe')
        cog = request.files.get('cog')
        barangay_indigency = request.files.get('barangayIndigency')
        psa_birth_certificate = request.files.get('psaBirthCertificate')
        
        # Validate that all required files are present
        required_files = [
            ('applicationForm', application_form),
            ('employmentContract', employment_contract),
            ('oathOfUndertaking', oath_of_undertaking),
            ('schoolCertification', school_certification),
            ('corOrCoe', cor_or_coe),
            ('cog', cog),
            ('barangayIndigency', barangay_indigency),
            ('psaBirthCertificate', psa_birth_certificate)
        ]
        
        missing_files = []
        field_name_mapping = {
            'applicationForm': 'Application Form',
            'employmentContract': 'Employment Contract',
            'oathOfUndertaking': 'Oath of Undertaking',
            'schoolCertification': 'School Certification',
            'corOrCoe': 'COR/COE',
            'cog': 'Copy of Grades',
            'barangayIndigency': 'Barangay Indigency',
            'psaBirthCertificate': 'PSA Birth Certificate'
        }
        
        for field_name, file in required_files:
            if not file or file.filename == '':
                missing_files.append(field_name_mapping[field_name])
        
        if missing_files:
            return jsonify({
                'success': False, 
                'message': f'Missing required files: {", ".join(missing_files)}'
            })
        
        # Validate file types and sizes
        for field_name, file in required_files:
            if file and file.filename:
                # Check file extension
                if not file.filename.lower().endswith(('.docx', '.pdf')):
                    return jsonify({
                        'success': False,
                        'message': f'{field_name_mapping[field_name]} must be a .docx or .pdf file'
                    })
                
                # Check file size (5MB limit)
                if len(file.read()) > 5 * 1024 * 1024:
                    return jsonify({
                        'success': False,
                        'message': f'{field_name_mapping[field_name]} file size exceeds 5MB limit'
                    })
                file.seek(0)  # Reset file pointer
        
        # Create user documents directory if it doesn't exist
        user_docs_dir = os.path.join('static', 'user_documents', str(user_id))
        os.makedirs(user_docs_dir, exist_ok=True)
        
        # Save files with unique names
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        uuid_str = str(uuid.uuid4())
        
        files_saved = {}
        file_mapping = {
            'applicationForm': 'APPLICATION-FORM',
            'employmentContract': 'SPES-EMPLOYMENT-CONTRACT',
            'oathOfUndertaking': 'OATH-OF-UNDERTAKING',
            'schoolCertification': 'SCHOOL-CERTIFICATION',
            'corOrCoe': 'COR-COE',
            'cog': 'COPY-OF-GRADES',
            'barangayIndigency': 'BARANGAY-INDIGENCY',
            'psaBirthCertificate': 'PSA-BIRTH-CERTIFICATE'
        }
        
        for field_name, file in required_files:
            if file and file.filename:
                file_extension = os.path.splitext(file.filename)[1].lower()
                new_filename = f"{file_mapping[field_name]}_{timestamp}_{uuid_str}{file_extension}"
                file_path = os.path.join(user_docs_dir, new_filename)
                file.save(file_path)
                files_saved[field_name] = new_filename
        
        # Save to database
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                
                # Check if user already has documents
                cursor.execute("SELECT id FROM user_documents WHERE user_id = %s", (user_id,))
                existing_docs = cursor.fetchone()
                
                if existing_docs:
                    # Update existing record
                    cursor.execute("""
                        UPDATE user_documents SET
                            application_form = %s,
                            employment_contract = %s,
                            oath_of_undertaking = %s,
                            school_certification = %s,
                            cor_or_coe = %s,
                            cog = %s,
                            barangay_indigency = %s,
                            psa_birth_certificate = %s,
                            updated_at = NOW()
                        WHERE user_id = %s
                    """, (
                        files_saved.get('applicationForm'),
                        files_saved.get('employmentContract'),
                        files_saved.get('oathOfUndertaking'),
                        files_saved.get('schoolCertification'),
                        files_saved.get('corOrCoe'),
                        files_saved.get('cog'),
                        files_saved.get('barangayIndigency'),
                        files_saved.get('psaBirthCertificate'),
                        user_id
                    ))
                else:
                    # Insert new record
                    cursor.execute("""
                        INSERT INTO user_documents (
                            user_id, application_form, employment_contract, 
                            oath_of_undertaking, school_certification, cor_or_coe,
                            cog, barangay_indigency, psa_birth_certificate, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """, (
                        user_id,
                        files_saved.get('applicationForm'),
                        files_saved.get('employmentContract'),
                        files_saved.get('oathOfUndertaking'),
                        files_saved.get('schoolCertification'),
                        files_saved.get('corOrCoe'),
                        files_saved.get('cog'),
                        files_saved.get('barangayIndigency'),
                        files_saved.get('psaBirthCertificate')
                    ))
                
                conn.commit()
                cursor.close()
                conn.close()
                
                return jsonify({
                    'success': True,
                    'message': 'All documents uploaded successfully!'
                })
                
            except Exception as e:
                print(f"Error saving documents: {e}")
                return jsonify({
                    'success': False,
                    'message': 'Error saving documents to database'
                })
        else:
            return jsonify({
                'success': False,
                'message': 'Database connection error'
            })
            
    except Exception as e:
        print(f"Error in submit_old_applicant_forms: {e}")
        return jsonify({
            'success': False,
            'message': 'An error occurred while uploading documents'
        })

@app.route('/submit-application', methods=['POST'])
def submit_application():
    try:
        # Get form data
        first_name = request.form.get('firstName')
        middle_name = request.form.get('middleName')
        last_name = request.form.get('lastName')
        address = request.form.get('address')
        contact_number = request.form.get('contactNumber')
        birth_date = request.form.get('birthDate')

        # Get files
        cor_1st_sem = request.files.get('cor1stSemUpload')
        cor_2nd_sem = request.files.get('cor2ndSemUpload')
        cog = request.files.get('cogUpload')

        # Validate required fields
        if not all([first_name, last_name, address, contact_number, birth_date, cor_1st_sem, cor_2nd_sem, cog]):
            return jsonify({'success': False, 'message': 'Please fill in all required fields'})

        # Get user ID from session
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'success': False, 'message': 'Please log in to submit an application'})

        # Process COR files
        cor_1st_sem_content, cor_1st_sem_filename = process_file(cor_1st_sem, cor_1st_sem.filename)
        cor_2nd_sem_content, cor_2nd_sem_filename = process_file(cor_2nd_sem, cor_2nd_sem.filename)
        cog_content, cog_filename = process_file(cog, cog.filename)

        if not all([cor_1st_sem_content, cor_2nd_sem_content, cog_content]):
            return jsonify({'success': False, 'message': 'Error processing uploaded files'})

        # Save to database
        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Insert into user_info table
            cursor.execute("""
                INSERT INTO user_info 
                (user_id, first_name, middle_name, last_name, address, contact_number, birth_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                user_id,
                first_name,
                middle_name,
                last_name,
                address,
                contact_number,
                birth_date
            ))

            # Insert into user_resources table
            cursor.execute("""
                INSERT INTO user_resources 
                (user_id, cor_file, cor_filename, cog_file, cog_filename)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                user_id,
                cor_1st_sem_content,
                cor_1st_sem_filename,
                cog_content,
                cog_filename
            ))

            # Insert into applicants table
            cursor.execute("""
                INSERT INTO applicants
                (user_id, status, submission_date)
                VALUES (%s, 'for exam', CURDATE())
            """, (user_id,))

            # Insert into examinees if not already present
            cursor.execute("""
                SELECT id FROM examinees WHERE user_id = %s
            """, (user_id,))
            if not cursor.fetchone():
                cursor.execute("""
                    INSERT INTO examinees (user_id, status)
                    VALUES (%s, 'for exam')
            """, (user_id,))

            # Insert notification for auto-approval
            cursor.execute("""
                INSERT INTO notifications (user_id, title, message, type)
                VALUES (%s, %s, %s, %s)
            """, (
                user_id,
                'Application Approved',
                'Your application has been automatically approved! Please wait for further instructions regarding the examination.',
                'application_approved'
            ))

            conn.commit()
            return jsonify({'success': True, 'message': 'Application submitted successfully'})

        except mysql.connector.Error as err:
            if conn:
                conn.rollback()
            error_msg = f"Database error: {err}"
            print(error_msg)
            return jsonify({'success': False, 'message': 'Error saving application to database. Please try again.'})

        except Exception as e:
            if conn:
                conn.rollback()
            error_msg = f"Unexpected error: {str(e)}"
            print(error_msg)
            return jsonify({'success': False, 'message': 'An unexpected error occurred. Please try again.'})

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    except Exception as e:
        error_msg = f"Error in submit_application: {str(e)}"
        print(error_msg)
        return jsonify({'success': False, 'message': 'An error occurred while submitting the application'})

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
            SELECT e.id, e.title, e.description, e.time_limit, e.passing_score, e.created_at, e.status, e.start_date, e.end_date,
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
            if exam.get('start_date') and isinstance(exam['start_date'], (datetime,)):
                exam['start_date'] = exam['start_date'].strftime('%B %d, %Y at %I%p').replace('AM', 'AM').replace('PM', 'PM')
            if exam.get('end_date') and isinstance(exam['end_date'], (datetime,)):
                exam['end_date'] = exam['end_date'].strftime('%B %d, %Y at %I%p').replace('AM', 'AM').replace('PM', 'PM')
        
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
        available_slots = request.form.get('availableSlots')
        start_date = request.form.get('startDate')
        status = 'closed'  # Default status
        end_date = None
        if start_date and time_limit:
            try:
                start_dt = datetime.strptime(start_date, '%Y-%m-%dT%H:%M')
                end_dt = start_dt + timedelta(minutes=int(time_limit))
                end_date = end_dt.strftime('%Y-%m-%d %H:%M:%S')
                start_date = start_dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception as e:
                print(f"Error parsing dates: {e}")
                start_date = None
                end_date = None
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Insert new exam
        cursor.execute('''
            INSERT INTO exams (title, description, time_limit, passing_score, available_slots, created_by, created_at, start_date, status, end_date)
            VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
        ''', (title, description, time_limit, passing_score, available_slots, session['user_id'], start_date, status, end_date))
        
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
            SELECT e.id, e.title, e.description, e.time_limit, e.passing_score, e.created_at, e.status, e.start_date, e.end_date, e.available_slots,
                   u.name as created_by
            FROM exams e
            LEFT JOIN users u ON e.created_by = u.id
            WHERE e.id = %s
        ''', (exam_id,))
        exam = cursor.fetchone()
        if not exam:
            return jsonify({'success': False, 'message': 'Exam not found'})
        # Save the original DB value for start_date
        if exam.get('start_date'):
            if isinstance(exam['start_date'], str):
                exam['db_start_date'] = exam['start_date']
            elif isinstance(exam['start_date'], (datetime,)):
                exam['db_start_date'] = exam['start_date'].strftime('%Y-%m-%d %H:%M:%S')
        else:
            exam['db_start_date'] = ''
        # Get exam pages
        cursor.execute('''
            SELECT id, title, description, time_limit, order_number 
            FROM exam_pages 
            WHERE exam_id = %s 
            ORDER BY order_number
        ''', (exam_id,))
        pages = cursor.fetchall()
        for page in pages:
            cursor.execute('''
                SELECT id, question_text, question_type, points, correct_answer, is_required, demographic_input_type
                FROM exam_questions 
                WHERE page_id = %s 
                ORDER BY id
            ''', (page['id'],))
            questions = cursor.fetchall()
            for question in questions:
                if question['question_type'] == 'multiple_choice':
                    cursor.execute('''
                        SELECT option_text 
                        FROM question_options 
                        WHERE question_id = %s 
                        ORDER BY id
                    ''', (question['id'],))
                    question['options'] = cursor.fetchall()
            for q in questions:
                if q['question_type'] == 'demographic':
                    q['input_type'] = q['demographic_input_type']
            page['questions'] = questions
        exam['pages'] = pages
        # Fix available_slots to always be an integer
        exam['available_slots'] = int(exam['available_slots']) if exam['available_slots'] is not None else 0
        # Format start_date for input[type=datetime-local] with NO timezone conversion
        if exam.get('start_date'):
            if isinstance(exam['start_date'], str):
                exam['start_date'] = exam['start_date'].replace(' ', 'T')[:16]
            elif isinstance(exam['start_date'], (datetime,)):
                exam['start_date'] = exam['start_date'].strftime('%Y-%m-%dT%H:%M')
        # Format dates for display only (not used in edit modal)
        if exam['created_at']:
            exam['created_at'] = exam['created_at'].strftime('%B %d, %Y') if isinstance(exam['created_at'], (datetime,)) else exam['created_at']
        if exam.get('end_date') and isinstance(exam['end_date'], (datetime,)):
            exam['end_date'] = exam['end_date'].strftime('%B %d, %Y at %I%p').replace('AM', 'AM').replace('PM', 'PM')
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
        is_required = 1 if request.form.get('isRequired') == 'on' else 0
        demographic_input_type = None
        if question_type == 'demographic':
            points = 0
            demographic_input_type = request.form.get('demographicInputType')
        
        if not all([page_id, question_type, question_text]) or (question_type != 'demographic' and not points):
            return jsonify({'success': False, 'message': 'Missing required fields'})
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if question_type == 'demographic':
            cursor.execute('''
                INSERT INTO exam_questions (page_id, question_text, question_type, points, correct_answer, is_case_sensitive, is_required, demographic_input_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ''', (page_id, question_text, question_type, points, None, False, is_required, demographic_input_type))
        else:
            cursor.execute('''
                INSERT INTO exam_questions (page_id, question_text, question_type, points, correct_answer, is_case_sensitive, is_required)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (page_id, question_text, question_type, points, None, False, is_required))
        
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
        # Essay and demographic: no correct answer, no options, nothing to do
        
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
            
        # Check if exam is ended
        if exam['status'] == 'ended':
            return jsonify({'success': False, 'message': 'This exam has ended and is no longer available'})
        
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
                SELECT id, question_text, question_type, points, correct_answer, is_required, demographic_input_type
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
            
            # For demographic, add input_type field
            for q in questions:
                if q['question_type'] == 'demographic':
                    q['input_type'] = q['demographic_input_type']
            
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

def update_exam_attempt_score(user_id, exam_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get examinee_id for this user and exam
        cursor.execute("""
            SELECT id FROM examinees 
            WHERE user_id = %s AND exam_id = %s
        """, (user_id, exam_id))
        examinee = cursor.fetchone()
        
        if examinee:
            # Calculate total score from examinee_answers
            cursor.execute("""
                SELECT COALESCE(SUM(score), 0) as total_score 
                FROM examinee_answers 
                WHERE examinee_id = %s AND score IS NOT NULL
            """, (examinee['id'],))
            result = cursor.fetchone()
            total_score = result['total_score'] if result else 0
            
            # Get total possible points from exam_questions
            cursor.execute("""
                SELECT COALESCE(SUM(points), 0) as total_points
                FROM exam_questions q
                JOIN exam_pages p ON q.page_id = p.id
                WHERE p.exam_id = %s
            """, (exam_id,))
            result = cursor.fetchone()
            total_points = result['total_points'] if result else 0
            
            # Update exam_attempts with the new calculated score
            cursor.execute("""
                UPDATE exam_attempts 
                SET score = %s, 
                    total_points = %s,
                    passed = CASE 
                        WHEN (%s / NULLIF(%s, 0) * 100) >= (
                            SELECT passing_score 
                            FROM exams 
                            WHERE id = %s
                        ) THEN 'passed'
                        ELSE 'failed'
                    END
                WHERE user_id = %s AND exam_id = %s
            """, (total_score, total_points, total_score, total_points, exam_id, user_id, exam_id))
            
            conn.commit()
            return total_score, total_points
    except Exception as e:
        print(f"Error updating exam attempt score: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
    return 0, 0

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
        
        # Get all questions for the exam, including is_required
        cursor.execute("""
            SELECT q.id, q.points, q.correct_answer, q.is_case_sensitive, q.question_type, q.is_required
            FROM exam_questions q
            JOIN exam_pages p ON q.page_id = p.id
            WHERE p.exam_id = %s
        """, (exam_id,))
        questions = cursor.fetchall()
        
        # Validate required questions
        missing_required = []
        for question in questions:
            qid = str(question[0])
            is_required = question[5]
            if is_required:
                ans = answers.get(qid)
                if ans is None or (isinstance(ans, str) and ans.strip() == ""):
                    missing_required.append(qid)
        if missing_required:
            return jsonify({'success': False, 'message': 'Please answer all required questions before submitting.', 'missing_required': missing_required})
        
        # Get examinee_id for this user and exam
        cursor.execute("SELECT id FROM examinees WHERE user_id = %s AND exam_id = %s", (session['user_id'], exam_id))
        ex_row = cursor.fetchone()
        if ex_row:
            examinee_id = ex_row[0]
        else:
            # fallback: get by user only
            cursor.execute("SELECT id FROM examinees WHERE user_id = %s", (session['user_id'],))
            ex_row = cursor.fetchone()
            examinee_id = ex_row[0] if ex_row else None

        # Insert answers into examinee_answers with auto-scoring for objective questions
        question_map = {str(q[0]): q for q in questions}  # id: (id, points, correct_answer, is_case_sensitive, question_type)
        for qid, ans in answers.items():
            q = question_map.get(str(qid))
            score = None
            if q:
                qtype = q[4]
                if qtype in ['multiple_choice', 'identification']:
                    correct = False
                    if qtype == 'multiple_choice':
                        correct = (ans == q[2])
                    elif qtype == 'identification':
                        if q[3]:  # is_case_sensitive
                            correct = (ans == q[2])
                        else:
                            if ans is not None and q[2] is not None:
                                correct = (str(ans).lower() == str(q[2]).lower())
                    score = q[1] if correct else 0
            cursor.execute("""
                INSERT INTO examinee_answers (examinee_id, question_id, answer_text, submitted_at, status, score)
                VALUES (%s, %s, %s, NOW(), 'pending', %s)
            """, (examinee_id, qid, ans, score))
        
        # Update exam_attempts score
        total_score, total_points = update_exam_attempt_score(session['user_id'], exam_id)
        
        # Get exam passing score
        cursor.execute("SELECT passing_score FROM exams WHERE id = %s", (exam_id,))
        exam = cursor.fetchone()
        passing_score = exam[0] if exam else 0
        
        # Calculate percentage score
        percentage_score = (total_score / total_points * 100) if total_points > 0 else 0
        
        # Update application and examinee status based on passing score
        new_status = 'for interview' if percentage_score >= passing_score else 'pending'
        
        cursor.execute("""
            UPDATE applicants 
            SET status = %s, exam_id = %s
            WHERE user_id = %s 
            ORDER BY submission_date DESC 
            LIMIT 1
        """, (new_status, exam_id, session['user_id']))
        
        cursor.execute("""
            UPDATE examinees
            SET status = %s, exam_id = %s
            WHERE user_id = %s 
        """, (new_status, exam_id, session['user_id']))
        
        # Create notification
        cursor.execute("""
            INSERT INTO notifications (user_id, title, message, type)
            VALUES (%s, %s, %s, %s)
        """, (
            session['user_id'],
            'Exam Completed',
            'You have completed the examination. Please wait for the result announcement.',
            'exam_result'
        ))
        
        # Update exam assignment status
        cursor.execute("""
            UPDATE exam_assignments 
            SET status = 'completed', completed_at = NOW()
            WHERE user_id = %s AND exam_id = %s
        """, (session['user_id'], exam_id))
        
        # Check if an attempt already exists
        cursor.execute("""
            SELECT id FROM exam_attempts WHERE user_id = %s AND exam_id = %s
        """, (session['user_id'], exam_id))
        attempt = cursor.fetchone()

        passed = percentage_score >= passing_score

        if not attempt:
            # Insert new attempt
            cursor.execute("""
                INSERT INTO exam_attempts (user_id, exam_id, score, total_points, passed, submitted_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
            """, (session['user_id'], exam_id, total_score, total_points, passed))
        else:
            # Update existing attempt (optional, for idempotency)
            cursor.execute("""
                UPDATE exam_attempts
                SET score = %s, total_points = %s, passed = %s, submitted_at = NOW()
                WHERE user_id = %s AND exam_id = %s
            """, (total_score, total_points, passed, session['user_id'], exam_id))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Exam submitted successfully'})
    except Exception as e:
        print(f"Error submitting exam: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/view-exam-results/<int:exam_id>')
def view_exam_results(exam_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get exam details
        cursor.execute("""
            SELECT e.title, e.available_slots
            FROM exams e
            WHERE e.id = %s
        """, (exam_id,))
        exam = cursor.fetchone()
        
        if not exam:
            flash('Exam not found', 'error')
            return redirect(url_for('new_applicants_dashboard'))
        
        # Get all exam attempts with user details, ordered by score
        cursor.execute("""
            SELECT 
                ea.score,
                ea.total_points,
                ea.submitted_at,
                u.name,
                CASE 
                    WHEN ROW_NUMBER() OVER (ORDER BY ea.score DESC) <= %s THEN 'Selected'
                    ELSE 'Not Selected'
                END as selection_status
            FROM exam_attempts ea
            JOIN users u ON ea.user_id = u.id
            WHERE ea.exam_id = %s
            ORDER BY ea.score DESC
        """, (exam['available_slots'], exam_id))
        results = cursor.fetchall()
        
        # Calculate statistics
        total_attempts = len(results)
        selected_count = sum(1 for r in results if r['selection_status'] == 'Selected')
        
        cursor.close()
        conn.close()
        
        return render_template('new/exam_results.html',
                             exam=exam,
                             results=results,
                             total_attempts=total_attempts,
                             selected_count=selected_count)
    except Exception as e:
        print(f"Error fetching exam results: {str(e)}")
        flash('Error loading exam results', 'error')
        return redirect(url_for('new_applicants_dashboard'))

@app.route('/new-applicants/forms')
def new_applicants_forms():
    if 'user_id' not in session or session['role'] != 'new':
        flash('Please login to access this page', 'error')
        return redirect(url_for('home'))
    can_access_spes_form = False
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT status FROM examinees WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (session['user_id'],))
            row = cursor.fetchone()
            if row and row['status'] == 'for interview':
                can_access_spes_form = True
        except Exception as e:
            print(f"Error checking examinee status: {e}")
        finally:
            cursor.close()
            conn.close()
    return render_template('new/spes-form.html', username=session['username'], can_access_spes_form=can_access_spes_form)

@app.route('/preview-template/<template_name>')
def preview_template(template_name):
    try:
        template_path = os.path.join('static', 'spes-forms', f'{template_name}.docx')
        if os.path.exists(template_path):
            return send_file(template_path, as_attachment=False)
        else:
            return "Template not found", 404
    except Exception as e:
        print(f"Error previewing template: {e}")
        return "Error loading template", 500

@app.route('/download-template/<template_name>')
def download_template(template_name):
    try:
        template_path = os.path.join('static', 'spes-forms', f'{template_name}.docx')
        if os.path.exists(template_path):
            return send_file(template_path, as_attachment=True, download_name=f'{template_name}.docx')
        else:
            return "Template not found", 404
    except Exception as e:
        print(f"Error downloading template: {e}")
        return "Error downloading template", 500

@app.route('/download-uploaded-document/<int:user_id>/<document_type>')
def download_uploaded_document(user_id, document_type):
    if 'user_id' not in session or session['user_id'] != user_id:
        return "Unauthorized", 403
    
    try:
        conn = get_db_connection()
        if conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT {} FROM user_documents 
                WHERE user_id = %s 
                ORDER BY updated_at DESC 
                LIMIT 1
            """.format(document_type), (user_id,))
            result = cursor.fetchone()
            
            if result and result[document_type]:
                file_path = os.path.join('static', 'user_documents', str(user_id), result[document_type])
                if os.path.exists(file_path):
                    return send_file(file_path, as_attachment=True)
                else:
                    return "File not found", 404
            else:
                return "Document not found", 404
                
        else:
            return "Database error", 500
            
    except Exception as e:
        print(f"Error downloading uploaded document: {e}")
        return "Error downloading document", 500

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

# Define keywords for document validation
COR_KEYWORDS = [
    'certificate of registration',
    'registration',
    'certificate of enrollment',
    'student',
    'student number',
    'student id',
    'student\'s information',
    'student\'s name',
    'enrollment',
    'enrolled',
    'registration',
    'registered',
    'bona fide',
    'bonafide',
    'good moral',
    'moral',
    'schedule',
]

COG_KEYWORDS = [
    'copy of grades',
    'transcript of records',
    'academic record',
    '1.00',
    '1.25',
    '1.50',
    '1.75',
    '2.00',
    '2.25',
    '2.50',
    '2.75',
    '80','81','82','83','84','85','86','87','88','89',
    '90','91','92','93','94','95','96','97','98','99',
    '100',
    
]

def preprocess_image(image):
    # Convert to grayscale
    image = image.convert('L')
    
    # Enhance contrast
    enhancer = ImageEnhance.Contrast(image)
    image = enhancer.enhance(2.0)
    
    # Enhance sharpness
    enhancer = ImageEnhance.Sharpness(image)
    image = enhancer.enhance(2.0)
    
    # Apply threshold
    image = image.point(lambda x: 0 if x < 128 else 255, '1')
    
    return image

@app.route('/validate-document-content', methods=['POST'])
def validate_document_content():
    try:
        data = request.get_json()
        base64_image = data.get('imageData')
        document_type = data.get('documentType')

        if not all([base64_image, document_type]):
            return jsonify({
                'valid': False, 
                'message': 'Missing required data. Please try again.'
            }), 400

        # Convert base64 to PIL Image
        try:
            # Remove the data URL prefix if present
            if ',' in base64_image:
                base64_image = base64_image.split(',')[1]
            
            # Decode base64 image
            image_data = base64.b64decode(base64_image)
            image = Image.open(io.BytesIO(image_data))
            
            # Convert to RGB if necessary
            if image.mode != 'RGB':
                image = image.convert('RGB')
                
            # Preprocess image
            processed_image = preprocess_image(image)
                
        except Exception as e:
            return jsonify({
                'valid': False,
                'message': f'Error processing image: {str(e)}'
            }), 400

        # Perform OCR with custom configuration
        try:
            custom_config = r'--oem 3 --psm 6 -c preserve_interword_spaces=1'
            text = pytesseract.image_to_string(processed_image, config=custom_config).lower()
            
            if not text.strip():
                return jsonify({
                    'valid': False,
                    'message': 'No text could be extracted from the image. Please ensure the image is clear and contains readable text.'
                })
        except Exception as e:
            return jsonify({
                'valid': False,
                'message': f'Error performing OCR: {str(e)}'
            }), 500

        # Validate content based on document type
        keywords = COR_KEYWORDS if document_type == 'cor' else COG_KEYWORDS
        matches = [keyword for keyword in keywords if keyword in text]

        # Prepare detailed response
        if len(matches) >= 1:
            return jsonify({
                'valid': True,
                'message': f'Document content validated successfully.',
                'debug_info': {
                    'extracted_text': text,  # Show full text
                    'matched_keywords': matches,
                    'total_keywords_found': len(matches),
                    'text_length': len(text),
                    'all_keywords': keywords
                }
            })
        else:
            return jsonify({
                'valid': False,
                'message': f'Invalid document content. Expected {document_type.upper()} document.',
                'debug_info': {
                    'extracted_text': text,  # Show full text
                    'matched_keywords': matches,
                    'expected_keywords': keywords,
                    'text_length': len(text),
                    'all_keywords': keywords
                }
            })

    except Exception as e:
        return jsonify({
            'valid': False,
            'message': f'Error processing document: {str(e)}'
        }), 500

@app.route('/get-exam-results/<int:exam_id>')
def get_exam_results(exam_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get exam details
        cursor.execute("""
            SELECT e.title, e.available_slots
            FROM exams e
            WHERE e.id = %s
        """, (exam_id,))
        exam = cursor.fetchone()
        
        if not exam:
            return jsonify({'success': False, 'message': 'Exam not found'})
        
        # Get all exam attempts with user details, ordered by score
        cursor.execute("""
            SELECT 
                ea.score,
                ea.total_points,
                ea.submitted_at,
                ea.passed,
                u.name,
                CASE 
                    WHEN ROW_NUMBER() OVER (ORDER BY ea.score DESC) <= %s THEN 'Selected'
                    ELSE 'Not Selected'
                END as selection_status
            FROM exam_attempts ea
            JOIN users u ON ea.user_id = u.id
            WHERE ea.exam_id = %s
            ORDER BY ea.score DESC
        """, (exam['available_slots'], exam_id))
        results = cursor.fetchall()
        
        # Calculate statistics
        total_attempts = len(results)
        selected_count = sum(1 for r in results if r['selection_status'] == 'Selected')
        
        cursor.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'exam': exam,
            'results': results,
            'total_attempts': total_attempts,
            'selected_count': selected_count
        })
    except Exception as e:
        print(f"Error fetching exam results: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
    
@app.route('/admin/examinees')
def admin_examinees():
    if 'user_id' not in session or session['role'] != 'admin':
        flash('Please login as admin to access this page', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    examinees = []
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT 
                    e.id as examinee_id,
                    e.status,
                    u.name as applicant_name,
                    ui.first_name,
                    ui.middle_name,
                    ui.last_name
                FROM examinees e
                JOIN users u ON e.user_id = u.id
                LEFT JOIN user_info ui ON u.id = ui.user_id
                ORDER BY e.created_at DESC
            """)
            examinees = cursor.fetchall()
        except Exception as e:
            print(f"Error fetching examinees: {e}")
            flash('Error loading examinees', 'error')
        finally:
            cursor.close()
            conn.close()

    return render_template('admin/examinees.html', examinees=examinees)

@app.route('/admin/trigger-exam-start')
def trigger_exam_start():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # Find all exams that should start
        cursor.execute("SELECT id FROM exams WHERE status = 'closed' AND start_date IS NOT NULL AND start_date <= %s", (now,))
        exams_to_start = cursor.fetchall()
        started_exam_ids = []
        for exam in exams_to_start:
            exam_id = exam['id']
            # Set exam status to ongoing
            cursor.execute("UPDATE exams SET status = 'ongoing' WHERE id = %s", (exam_id,))
            # Assign all examinees (status 'for exam', exam_id IS NULL)
            cursor.execute("UPDATE examinees SET exam_id = %s WHERE status = 'for exam' AND (exam_id IS NULL OR exam_id = '')", (exam_id,))
            started_exam_ids.append(exam_id)
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'started_exam_ids': started_exam_ids})
    except Exception as e:
        print(f"Error in trigger_exam_start: {e}")
        return jsonify({'success': False, 'message': str(e)})

def close_exam_after_timer(exam_id, minutes):
    time.sleep(minutes * 60)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE exams SET status = 'closed' WHERE id = %s AND status = 'ongoing'", (exam_id,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error closing exam {exam_id} after timer: {e}")

def exam_auto_trigger():
    while True:
        with app.app_context():
            try:
                conn = get_db_connection()
                cursor = conn.cursor(dictionary=True)
                ph_tz = pytz.timezone('Asia/Manila')
                now = datetime.now(ph_tz)

                # First, let's get all exams that need to be closed
                cursor.execute("""
                    SELECT id, start_date, time_limit
                    FROM exams
                    WHERE status = 'ongoing'
                      AND start_date IS NOT NULL
                      AND time_limit IS NOT NULL
                """)
                exams_to_close = cursor.fetchall()
                
                # Close exams that have reached their time limit
                for exam in exams_to_close:
                    exam_id = exam['id']
                    start_date = exam['start_date']
                    time_limit = exam['time_limit']
                    if start_date is not None and time_limit is not None:
                        if start_date.tzinfo is None:
                            start_date = ph_tz.localize(start_date)
                        end_time = start_date + timedelta(minutes=int(time_limit))
                        if now >= end_time:
                            print(f"Closing exam {exam_id} at {now} (end time was {end_time})")
                            # Mark exam as ended
                            cursor.execute("""
                                UPDATE exams 
                                SET status = 'ended', 
                                    end_date = %s
                                WHERE id = %s AND status = 'ongoing'
                            """, (end_time, exam_id))
                            
                            # Get all examinees for this exam
                            cursor.execute("""
                                SELECT user_id 
                                FROM examinees 
                                WHERE exam_id = %s AND status = 'for exam'
                            """, (exam_id,))
                            examinees = cursor.fetchall()
                            for ex in examinees:
                                user_id = ex['user_id']
                                cursor.execute("""
                                    INSERT INTO notifications (user_id, title, message, type)
                                    VALUES (%s, 'Exam Closed', 'The examination period has ended.', 'exam_result')
                                """, (user_id,))

                # Now handle starting new exams
                cursor.execute("""
                    SELECT id, start_date, time_limit
                    FROM exams
                    WHERE status = 'closed'
                      AND start_date IS NOT NULL
                      AND time_limit IS NOT NULL
                """)
                exams_to_start = cursor.fetchall()
                
                for exam in exams_to_start:
                    exam_id = exam['id']
                    start_date = exam['start_date']
                    time_limit = exam['time_limit']
                    
                    if start_date is not None and time_limit is not None:
                        if start_date.tzinfo is None:
                            start_date = ph_tz.localize(start_date)
                        
                        # Start exam if current time is past or equal to start date
                        if now >= start_date:
                            print(f"Starting exam {exam_id} at {now} (start time: {start_date})")
                            
                            # Update exam status to ongoing
                            cursor.execute("""
                                UPDATE exams 
                                SET status = 'ongoing' 
                                WHERE id = %s AND status = 'closed'
                            """, (exam_id,))
                            
                            # Assign exam_id to examinees who are waiting for an exam
                            cursor.execute("""
                                UPDATE examinees SET exam_id = %s WHERE status = 'for exam' AND (exam_id IS NULL OR exam_id = '')
                            """, (exam_id,))
                            # Assign examinees who are still waiting for an exam
                            cursor.execute("""
                                SELECT id, user_id 
                                FROM examinees 
                                WHERE status = 'for exam' AND exam_id = %s
                            """, (exam_id,))
                            examinees = cursor.fetchall()
                            for ex in examinees:
                                user_id = ex['user_id']
                                cursor.execute("""
                                    INSERT INTO exam_assignments (user_id, exam_id, status) 
                                    VALUES (%s, %s, 'assigned')
                                """, (user_id, exam_id))
                                cursor.execute("""
                                    INSERT INTO notifications (user_id, title, message, type, action_url) 
                                    VALUES (%s, 'Examination Started', 'Your examination is now available. Click here to take the exam.', 'exam_link', '/examinations')
                                """, (user_id,))

                conn.commit()
                cursor.close()
                conn.close()
                
            except Exception as e:
                print(f"Error in exam_auto_trigger: {e}")
                try:
                    if 'conn' in locals():
                        conn.close()
                except:
                    pass
            
            time.sleep(30)  # Check every 30 seconds

@app.route('/admin/examinee-answers/<int:examinee_id>')
@login_required
def get_examinee_answers(examinee_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute('''
            SELECT ea.id, ea.question_id, q.page_id, q.question_text, q.question_type, ea.answer_text, ea.status, ea.score, q.correct_answer, q.points, q.is_case_sensitive
            FROM examinee_answers ea
            JOIN exam_questions q ON ea.question_id = q.id
            WHERE ea.examinee_id = %s
            ORDER BY q.page_id, ea.id
        ''', (examinee_id,))
        answers = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'answers': answers})
    except Exception as e:
        print(f"Error fetching examinee answers: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/admin/update-examinee-answer', methods=['POST'])
@login_required
def update_examinee_answer():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    try:
        data = request.get_json()
        answer_id = data.get('answer_id')
        status = data.get('status')
        score = data.get('score')
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get examinee_id and exam_id for this answer
        cursor.execute("""
            SELECT ea.examinee_id, e.exam_id, e.user_id 
            FROM examinee_answers ea
            JOIN examinees e ON ea.examinee_id = e.id
            WHERE ea.id = %s
        """, (answer_id,))
        result = cursor.fetchone()
        
        if not result:
            return jsonify({'success': False, 'message': 'Answer not found'})
            
        examinee_id = result['examinee_id']
        exam_id = result['exam_id']
        user_id = result['user_id']
        
        # Start transaction
        cursor.execute("START TRANSACTION")
        
        try:
            if status is not None:
                cursor.execute('UPDATE examinee_answers SET status = %s WHERE id = %s', (status, answer_id))
            if score is not None:
                cursor.execute('UPDATE examinee_answers SET score = %s WHERE id = %s', (score, answer_id))
                
            # Calculate new total score
            cursor.execute("""
                SELECT COALESCE(SUM(score), 0) as total_score 
                FROM examinee_answers 
                WHERE examinee_id = %s AND score IS NOT NULL
            """, (examinee_id,))
            total_score_result = cursor.fetchone()
            total_score = total_score_result['total_score'] if total_score_result else 0
            
            # Get total possible points
            cursor.execute("""
                SELECT COALESCE(SUM(points), 0) as total_points
                FROM exam_questions q
                JOIN exam_pages p ON q.page_id = p.id
                WHERE p.exam_id = %s
            """, (exam_id,))
            total_points_result = cursor.fetchone()
            total_points = total_points_result['total_points'] if total_points_result else 0
            
            # Get passing score
            cursor.execute("SELECT passing_score FROM exams WHERE id = %s", (exam_id,))
            exam = cursor.fetchone()
            passing_score = exam['passing_score'] if exam else 0
            
            # Calculate percentage score
            percentage_score = (total_score / total_points * 100) if total_points > 0 else 0
            
            # Update exam_attempts immediately
            cursor.execute("""
                UPDATE exam_attempts 
                SET score = %s, 
                    total_points = %s,
                    passed = CASE 
                        WHEN %s >= %s THEN 'passed'
                        ELSE 'failed'
                    END
                WHERE user_id = %s AND exam_id = %s
            """, (total_score, total_points, percentage_score, passing_score, user_id, exam_id))
            
            # Update examinee status based on passed field
            cursor.execute("""
                UPDATE examinees
                SET status = CASE 
                    WHEN (
                        SELECT passed 
                        FROM exam_attempts 
                        WHERE user_id = %s AND exam_id = %s
                    ) = 'passed' THEN 'for interview'
                    ELSE 'pending'
                END
                WHERE user_id = %s AND exam_id = %s
            """, (user_id, exam_id, user_id, exam_id))
            
            conn.commit()
            return jsonify({'success': True, 'message': 'Answer updated successfully'})
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"Error updating examinee answer: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
    
@app.route('/admin/messages')
def admin_messages():
    if 'user_id' not in session or session['role'] != 'admin':
        flash('Please login as admin to access this page', 'error')
        return redirect(url_for('home'))
    return render_template('admin/messages.html', username=session['username'])

# --- Admin Messaging API ---
@app.route('/admin/messages/users')
@login_required
def get_message_users():
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    conn = get_db_connection()
    users = []
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            # Get all users who have sent or received messages with admin
            cursor.execute('''
                SELECT DISTINCT u.id, u.name, u.role
                FROM users u
                JOIN messages m ON (u.id = m.sender_id OR u.id = m.receiver_id)
                WHERE u.role != 'admin' AND (m.sender_id = %s OR m.receiver_id = %s)
            ''', (session['user_id'], session['user_id']))
            users = cursor.fetchall()
        except Exception as e:
            print(f"Error fetching message users: {e}")
        finally:
            cursor.close()
            conn.close()
    return jsonify({'success': True, 'users': users})

@app.route('/admin/messages/history/<int:user_id>')
@login_required
def get_message_history(user_id):
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    conn = get_db_connection()
    messages = []
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute('''
                SELECT m.id, m.sender_id, m.receiver_id, m.content, m.timestamp, m.is_read,
                       u1.name AS sender_name, u2.name AS receiver_name
                FROM messages m
                JOIN users u1 ON m.sender_id = u1.id
                JOIN users u2 ON m.receiver_id = u2.id
                WHERE (m.sender_id = %s AND m.receiver_id = %s)
                   OR (m.sender_id = %s AND m.receiver_id = %s)
                ORDER BY m.timestamp ASC
            ''', (session['user_id'], user_id, user_id, session['user_id']))
            messages = cursor.fetchall()
        except Exception as e:
            print(f"Error fetching message history: {e}")
        finally:
            cursor.close()
            conn.close()
    return jsonify({'success': True, 'messages': messages})

@app.route('/admin/messages/send', methods=['POST'])
@login_required
def send_message():
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    data = request.get_json()
    receiver_id = data.get('receiver_id')
    content = data.get('content')
    if not receiver_id or not content:
        return jsonify({'success': False, 'message': 'Missing data'}), 400
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO messages (sender_id, receiver_id, content) VALUES (%s, %s, %s)
            ''', (session['user_id'], receiver_id, content))
            conn.commit()
        except Exception as e:
            print(f"Error sending message: {e}")
            return jsonify({'success': False, 'message': 'Database error'}), 500
        finally:
            cursor.close()
            conn.close()
    return jsonify({'success': True, 'message': 'Message sent'})

# --- SocketIO events for real-time messaging ---
@socketio.on('join')
def handle_join(data):
    # data: {room: str}
    join_room(data['room'])
    user_id = data.get('user_id')
    if user_id:
        user_sid_map[user_id] = request.sid
    emit('status', {'msg': f"{data.get('username', 'A user')} has entered the room."}, room=data['room'])

@socketio.on('leave')
def handle_leave(data):
    leave_room(data['room'])
    emit('status', {'msg': f"{data.get('username', 'A user')} has left the room."}, room=data['room'])
    close_chat(data['room'], 'A participant has left the chat.')

@socketio.on('disconnect')
def handle_disconnect():
    # Find user_id by sid
    user_id = None
    for uid, sid in user_sid_map.items():
        if sid == request.sid:
            user_id = uid
            break
    if user_id:
        # Find all rooms this user is in and close them
        for room in list(active_chats.keys()):
            if str(user_id) in room:
                close_chat(room, 'A participant has disconnected.')
        del user_sid_map[user_id]

@socketio.on('send_message')
def handle_send_message(data):
    # data: {sender_id, receiver_id, content, room (optional)}
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO messages (sender_id, receiver_id, content) VALUES (%s, %s, %s)
            ''', (data['sender_id'], data['receiver_id'], data['content']))
            conn.commit()
        except Exception as e:
            print(f"Error saving socket message: {e}")
        finally:
            cursor.close()
            conn.close()
    # Emit to chat room if provided, else to both user rooms
    if 'room' in data:
        emit('receive_message', data, room=data['room'])
    else:
        emit('receive_message', data, room=f'user_{data["sender_id"]}')
        emit('receive_message', data, room=f'user_{data["receiver_id"]}')

@socketio.on('chat_request')
def handle_chat_request(data):
    # data: {applicant_id, applicant_name}
    # Notify all admins (or a specific admin room)
    emit('chat_request', data, room='admin_room', broadcast=True)

@socketio.on('chat_accept')
def handle_chat_accept(data):
    # data: {applicant_id, admin_id, admin_name}
    room = f'chat_{data["applicant_id"]}_{data["admin_id"]}'
    emit('chat_accept', {'room': room, 'admin_name': data['admin_name']}, room=f'user_{data["applicant_id"]}')

@app.route('/admin/update-exam', methods=['POST'])
@login_required
def update_exam():
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    try:
        exam_id = request.form.get('examId')
        title = request.form.get('title')
        description = request.form.get('description')
        time_limit = request.form.get('timeLimit')
        passing_score = request.form.get('passingScore')
        available_slots = request.form.get('availableSlots')
        start_date = request.form.get('startDate')
        status = request.form.get('status')  # Optional, for future use
        end_date = request.form.get('endDate')  # Optional, for future use

        if not all([exam_id, title, description, time_limit, passing_score, available_slots, start_date]):
            return jsonify({'success': False, 'message': 'Missing required fields'})

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE exams SET title=%s, description=%s, time_limit=%s, passing_score=%s, available_slots=%s, start_date=%s WHERE id=%s
        ''', (title, description, time_limit, passing_score, available_slots, start_date, exam_id))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Exam updated successfully'})
    except Exception as e:
        print(f"Error updating exam: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/user/messages/history/<int:admin_id>')
def get_user_message_history(admin_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    user_id = session['user_id']
    conn = get_db_connection()
    messages = []
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute('''
                SELECT m.id, m.sender_id, m.receiver_id, m.content, m.timestamp
                FROM messages m
                WHERE (m.sender_id = %s AND m.receiver_id = %s)
                   OR (m.sender_id = %s AND m.receiver_id = %s)
                ORDER BY m.timestamp ASC
            ''', (user_id, admin_id, admin_id, user_id))
            messages = cursor.fetchall()
        except Exception as e:
            print(f"Error fetching user message history: {e}")
        finally:
            cursor.close()
            conn.close()
    return jsonify({'success': True, 'messages': messages})

# Base FAQs and default answers
faq_data = {
    "greeting": "Hello! 👋 How can I assist you about SPES today?",
    "thanks": "You're welcome! If you have more questions, just type them here 😊",
    "what is digispes": "DigiSPES is the digital portal for SPES (Special Program for Employment of Students) application, screening, and communication with PESO Paete.",
    
    "ilang sets ng documents": "Apat (4) na sets ng documents. Bawat set ay kailangang naka-staple nang magkakahiwalay.",
    "ano laman bawat set": "✔ Set 1 – SPES Forms + Original at Certified True Copy ng ibang requirements\n✔ Set 2, 3, 4 – SPES Forms + Photocopy ng ibang requirements.",
    "saan ilalagay sets": "Ilagay ang lahat ng sets sa isang Long Folder at Long Plastic Envelope.",
    "kailangan ba ng id photo": "Oo, kailangan ng isang (1) extra Passport Size ID photo na may blue background.\n Sa likod ng ID photo, isulat ang iyong pangalan gamit ang format: LAST NAME, FIRST NAME, MIDDLE INITIAL.",
    "minimum grade requirement": "Wala dapat subject na mas mababa sa 80 o 2.75.",
    "computerized ba forms": "Lahat ng ipi-fill out sa SPES forms ay dapat computerized, maliban sa signature na kailangang pirmahan gamit ang ballpen. Bawal ang e-signature.",
    "ano isusulat sa upper right corner ng application form": "Ilagay ang pangalan ng iyong magulang o guardian na GSIS beneficiary para sa GSIS Insurance.",
    "ilagay ba special skills sa form": "Oo, dapat mong isulat ang iyong mga espesyal na kasanayan sa application form.",
    "kailan ipapasa requirements": "Itabi muna ang mga requirements. Ipasa lamang ang mga ito kung nakapasa ka na sa exam.",
    "saan ipapasa requirements": "Ipapasa ang mga ito sa PESO Office – Paete Municipal na matatagpuan sa may stage, sa mismong araw ng interview.",
    "may retake ba sa exam": "Wala pong retake sa SPES exam.\nKaya siguraduhing maayos ang inyong device at internet connection, at basahin nang mabuti ang mga instructions at warnings bago magsimula.",
    "kailan schedule ng exam": "Malalaman ang schedule ng exam sa loob ng isang (1) linggo matapos makapagsumite ng initial documents.",
    "kailan lalabas result ng exam": "Lalabas ang resulta ng exam isang (1) linggo pagkatapos ito ay maisagawa.",
    "kailan ipapasa final requirements": "Ang petsa ng submission ng final requirements ay malalaman isang (1) linggo matapos ang exam.",
    "schedule ng interview at pagpasa ng physical requirements": "Malalaman ang iskedyul ng interview at pagpapasa ng physical copies ng requirements isang (1) linggo matapos ang exam.",
}

# Keyword variations
faq_variations = {
    "greeting": ["hi", "hello", "good morning", "good afternoon", "hey", "kumusta", "yo"],
    "thanks": ["thank you", "thanks", "salamat", "ty", "maraming salamat"],
    "what is digispes": ["ano ang digispes", "what is digispes", "ano po ang digispes", "digispes meaning", "digispes system", "digispes portal"],

    "ilang sets ng documents": ["ilang set", "ilang copies", "ilang documents", "how many sets", "ilang set po", "ilang copy"],
    "ano laman bawat set": ["laman ng set", "ano sa loob ng set", "content ng set", "ano ang nilalaman"],
    "saan ilalagay sets": ["saan ilalagay", "where to put documents", "where to place sets"],
    "kailangan ba ng id photo": ["kailangan ba ng picture", "id picture", "photo required", "passport size photo"],
    "minimum grade requirement": ["minimum grade", "passing grade", "lowest grade"],
    "computerized ba forms": ["pwede handwritten", "computerized form", "typewritten", "bawal e-signature"],
    "ano isusulat sa upper right corner ng application form": ["gsis", "guardian name", "upper right"],
    "ilagay ba special skills sa form": ["special skills", "kakayahan", "ilalagay skills"],
    "kailan ipapasa requirements": ["when to pass", "submit requirements", "ipapasa kelan"],
    "saan ipapasa requirements": ["saan ipapasa", "submit documents", "submission location"],
    "may retake ba sa exam": ["retake", "ulit exam", "what if fail"],
    "kailan schedule ng exam": ["exam schedule", "exam date", "when is the exam"],
    "kailan lalabas result ng exam": ["result ng exam", "exam result", "result release"],
    "kailan ipapasa final requirements": ["final requirements", "submission date", "final docs"],
    "schedule ng interview at pagpasa ng physical requirements": ["interview schedule", "physical submission", "document handoff"],
}

# Reverse keyword matcher
keyword_map = {kw: k for k, kws in faq_variations.items() for kw in kws}

def normalize(text):
    return text.lower().strip().replace("?", "")

def find_answer(question):
    norm_q = normalize(question)

    # Direct match via keyword mapping
    for keyword, category in keyword_map.items():
        if keyword in norm_q:
            return faq_data.get(category)

    # Fallback fuzzy matching
    all_keys = list(keyword_map.keys()) + list(faq_data.keys())
    close = difflib.get_close_matches(norm_q, all_keys, n=1, cutoff=0.5)
    if close:
        matched = keyword_map.get(close[0], close[0])
        return faq_data.get(matched, "Pasensya na, hindi ko po naintindihan. Pakiulit ang tanong.")

    return "Pasensya na, wala akong sagot diyan. Subukang i-rephrase ang tanong."

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    user_msg = data.get("message", "")
    response = find_answer(user_msg)
    return jsonify({"response": response})

if __name__ == '__main__':
    test_db_connection()  # Test database connection on startup
    threading.Thread(target=exam_auto_trigger, daemon=True).start()
    socketio.run(app, debug=True)