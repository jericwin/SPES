from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_file, Response
from config import get_db_connection
import hashlib
import re
import os
import json
import uuid
import shutil
import subprocess
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
import io
from functools import wraps
import tempfile
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
import requests
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from dotenv import load_dotenv
# FAQS
from flask import Flask, request, jsonify
import google.generativeai as genai
# import openai

# import difflib
# FAQS

try:
    import pythoncom
except ImportError:
    pythoncom = None

try:
    from docx2pdf import convert as docx2pdf_convert
except ImportError:
    docx2pdf_convert = None

# Load environment variables from .env file
load_dotenv()

from collections import defaultdict

# Thread-safe in-memory rate limiter for API security
class RateLimiter:
    def __init__(self, max_requests=10, period=60):
        self.max_requests = max_requests
        self.period = period
        self.requests = defaultdict(list)
        self.lock = threading.Lock()

    def is_allowed(self, client_ip):
        now = time.time()
        with self.lock:
            # Filter out timestamps older than the period window
            self.requests[client_ip] = [t for t in self.requests[client_ip] if now - t < self.period]
            if len(self.requests[client_ip]) < self.max_requests:
                self.requests[client_ip].append(now)
                return True
            return False

# Initialize rate limiters for Gemini chatbot to prevent abuse
chat_burst_limiter = RateLimiter(max_requests=5, period=10)      # Max 5 requests per 10 seconds
chat_sustained_limiter = RateLimiter(max_requests=20, period=60)  # Max 20 requests per minute

# Configure Tesseract path
if os.name == 'nt':  # Windows
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
elif os.name == 'posix':  # Linux/Mac
    pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # Required for flash messages and sessions

def init_system_settings():
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_settings (
                    setting_key VARCHAR(100) PRIMARY KEY,
                    setting_value VARCHAR(255) NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
            cursor.execute("SELECT 1 FROM system_settings WHERE setting_key = 'maintenance_notice_enabled'")
            if not cursor.fetchone():
                cursor.execute("INSERT INTO system_settings (setting_key, setting_value) VALUES ('maintenance_notice_enabled', 'false')")
            conn.commit()
            print("System settings table initialized successfully.")
        except Exception as e:
            print(f"Error initializing system settings table: {e}")
        finally:
            try:
                cursor.close()
            except:
                pass
            conn.close()

# Try to initialize settings table on startup
try:
    init_system_settings()
except Exception as e:
    print(f"Startup initialization warning: {e}")

@app.route('/api/maintenance-notice', methods=['POST'])
def update_maintenance_notice():
    data = request.get_json() or {}
    passcode = data.get('passcode')
    action = data.get('action') # 'true' or 'false'
    
    expected_passcode = os.environ.get('MAINTENANCE_PASSCODE', 'digispes123')
    
    if passcode != expected_passcode:
        return jsonify({'success': False, 'message': 'Unauthorized: Invalid passcode.'}), 403
        
    if action not in ['true', 'false']:
        return jsonify({'success': False, 'message': 'Invalid action. Must be true or false.'}), 400
        
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE system_settings SET setting_value = %s WHERE setting_key = 'maintenance_notice_enabled'",
                (action,)
            )
            conn.commit()
            return jsonify({'success': True, 'message': f'Maintenance notice turned {action.upper()} globally.'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500
        finally:
            cursor.close()
            conn.close()
            
    return jsonify({'success': False, 'message': 'Database connection failed.'}), 500

# Google OAuth Configuration
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', 'your-google-client-id-here')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', 'your-google-client-secret-here')

# Gemini Configuration
GENAI_API_KEY = os.environ.get('GENAI_API_KEY')
gemini_model = None
if GENAI_API_KEY:
    try:
        genai.configure(api_key=GENAI_API_KEY)
        
        # Explicitly prefer gemini-1.5-flash for free tier
        model_name = 'gemini-1.5-flash' 
        
        # Verify if it exists, otherwise fallback to gemini-pro
        try:
            print("Checking available Gemini models...")
            available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
            # print(f"Available: {available_models}")
            
            # Priority list
            priorities = ['models/gemini-1.5-flash', 'models/gemini-1.5-flash-latest', 'models/gemini-pro']
            
            found = False
            for p in priorities:
                if p in available_models:
                    model_name = p
                    found = True
                    break
            
            if not found:
                # Fallback: find anything with 'flash'
                for m in available_models:
                    if 'flash' in m:
                        model_name = m
                        found = True
                        break
            
            if not found and 'models/gemini-pro' in available_models:
                model_name = 'models/gemini-pro'

        except Exception as list_err:
            print(f"Warning: Could not list models ({list_err}). Using default: {model_name}")

        print(f"Selected Gemini Model: {model_name}")
        gemini_model = genai.GenerativeModel(model_name)
        print("Gemini AI configured successfully.")
    except Exception as e:
        print(f"Error configuring Gemini AI: {e}")

socketio = SocketIO(app, cors_allowed_origins="*")

# Track active chat timers and sockets
active_chats = {}  # key: room, value: Timer
user_sid_map = {}  # key: user_id, value: sid


def convert_docx_to_pdf(docx_path: str, pdf_path: str) -> None:
    """Convert a DOCX file to PDF using the available platform tools."""
    if os.name == 'nt':
        if pythoncom is None or docx2pdf_convert is None:
            raise RuntimeError('DOCX to PDF conversion is unavailable on this platform.')
        pythoncom.CoInitialize()
        try:
            docx2pdf_convert(docx_path, pdf_path)
        finally:
            pythoncom.CoUninitialize()
    else:
        soffice_path = shutil.which('soffice')
        if not soffice_path:
            raise RuntimeError('LibreOffice (soffice) is not installed in the container.')
        output_dir = os.path.dirname(pdf_path)
        cmd = [
            soffice_path,
            '--headless',
            '--nologo',
            '--nolockcheck',
            '--convert-to',
            'pdf',
            docx_path,
            '--outdir',
            output_dir,
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            stderr = result.stderr.decode().strip() if result.stderr else ''
            stdout = result.stdout.decode().strip() if result.stdout else ''
            raise RuntimeError(f'LibreOffice conversion failed: {stderr or stdout}')
        generated_pdf = os.path.join(output_dir, os.path.splitext(os.path.basename(docx_path))[0] + '.pdf')
        if generated_pdf != pdf_path and os.path.exists(generated_pdf):
            os.replace(generated_pdf, pdf_path)
        elif not os.path.exists(pdf_path):
            raise RuntimeError('Converted PDF file was not produced.')

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

def role_required(roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('index'))
            if session.get('role') not in roles:
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def get_user_municipality():
    if 'user_id' not in session:
        return None
    
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT m.* FROM municipalities m
            JOIN users u ON u.municipality_id = m.id
            WHERE u.id = %s
        """, (session['user_id'],))
        municipality = cursor.fetchone()
        return municipality
    except Exception as e:
        print(f"Error getting municipality: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

def get_municipality_access_filter():
    if session.get('role') == 'provincial_peso':
        return ""  # No filter for provincial PESO - they can see all
    
    municipality = get_user_municipality()
    if municipality:
        return f"AND ui.municipality_id = {municipality['id']}"
    return "AND 1=0"  # If no municipality found, show nothing

# Configure upload folder
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_user_municipality_id():
    """Get the municipality_id of the current user"""
    if 'user_id' not in session:
        return None
    
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT municipality_id FROM users WHERE id = %s", (session['user_id'],))
        user = cursor.fetchone()
        return user['municipality_id'] if user else None
    except Exception as e:
        print(f"Error getting municipality_id: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

# Test database connection
def test_db_connection():
    conn = get_db_connection()
    if conn:
        print("Successfully connected to MySQL database!")
        conn.close()
    else:
        print("Failed to connect to MySQL database!")

@app.route('/peso/municipalities', methods=['GET', 'POST'])
@login_required
@role_required(['provincial_peso'])
def manage_municipalities():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        province = request.form.get('province', '').strip()

        if not name or not province:
            flash('Municipality name and province are required', 'error')
            return redirect(url_for('peso_dashboard'))

        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO municipalities (name, province, created_by) VALUES (%s, %s, %s)",
                    (name, province, session['user_id']),
                )
                conn.commit()
                flash('Municipality added successfully', 'success')
            except Exception as e:
                print(f"Error adding municipality: {e}")
                flash('Error adding municipality. Please try again.', 'error')
            finally:
                try:
                    if 'cursor' in locals() and cursor:
                        cursor.close()
                finally:
                    conn.close()

        return redirect(url_for('peso_dashboard'))

    return redirect(url_for('peso_dashboard'))


@app.route('/peso/update-municipality/<int:municipality_id>', methods=['POST'])
@login_required
@role_required(['provincial_peso'])
def update_municipality(municipality_id):
    name = request.form.get('name', '').strip()
    province = request.form.get('province', '').strip()

    if not name or not province:
        flash('Municipality name and province are required', 'error')
        return redirect(url_for('peso_dashboard'))

    conn = get_db_connection()
    if not conn:
        flash('Database connection failed', 'error')
        return redirect(url_for('peso_dashboard'))

    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE municipalities SET name = %s, province = %s WHERE id = %s",
            (name, province, municipality_id),
        )
        conn.commit()
        flash('Municipality updated successfully', 'success')
    except Exception as e:
        print(f"Error updating municipality: {e}")
        flash('Error updating municipality. Please try again.', 'error')
    finally:
        try:
            if 'cursor' in locals() and cursor:
                cursor.close()
        finally:
            conn.close()

    return redirect(url_for('peso_dashboard'))


@app.route('/peso/delete-municipality/<int:municipality_id>', methods=['POST'])
@login_required
@role_required(['provincial_peso'])
def delete_municipality(municipality_id):
    conn = get_db_connection()
    if not conn:
        flash('Database connection failed', 'error')
        return redirect(url_for('peso_dashboard'))

    try:
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE municipality_id = %s",
            (municipality_id,),
        )
        usage = cursor.fetchone()
        if usage and usage['cnt']:
            flash('Cannot delete municipality with linked user accounts.', 'error')
            return redirect(url_for('peso_dashboard'))

        cursor.execute("DELETE FROM municipalities WHERE id = %s", (municipality_id,))
        conn.commit()
        flash('Municipality deleted successfully', 'success')
    except Exception as e:
        print(f"Error deleting municipality: {e}")
        flash('Error deleting municipality. Please try again.', 'error')
    finally:
        try:
            if 'cursor' in locals() and cursor:
                cursor.close()
        finally:
            conn.close()

    return redirect(url_for('peso_dashboard'))

@app.route('/') 
def home():
    municipalities = []
    maintenance_notice_enabled = False
    conn = get_db_connection()
    cursor = None
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT id, name, province FROM municipalities ORDER BY province, name"
            )
            municipalities = cursor.fetchall()
            
            # Fetch maintenance notice status
            try:
                cursor.execute(
                    "SELECT setting_value FROM system_settings WHERE setting_key = 'maintenance_notice_enabled'"
                )
                res = cursor.fetchone()
                if res and res['setting_value'] == 'true':
                    maintenance_notice_enabled = True
            except Exception as se:
                print(f"Error fetching maintenance settings: {se}")
        except Exception as e:
            print(f"Error loading municipalities for registration: {e}")
        finally:
            try:
                if cursor:
                    cursor.close()
            finally:
                conn.close()

    return render_template(
        'home.html',
        google_client_id=GOOGLE_CLIENT_ID,
        municipalities=municipalities,
        maintenance_notice_enabled=maintenance_notice_enabled,
    )

@app.route('/peso/dashboard')
@login_required
@role_required(['provincial_peso'])
def peso_dashboard():
    municipalities = []
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            # Get municipalities data along with basic stats for display
            cursor.execute("""
                SELECT m.*, 
                       COUNT(DISTINCT u.id) AS admin_count,
                       COUNT(DISTINCT a.id) AS applicant_count
                FROM municipalities m
                LEFT JOIN users u ON u.municipality_id = m.id AND u.role = 'municipal_peso'
                LEFT JOIN user_info ui ON ui.municipality_id = m.id
                LEFT JOIN applicants a ON a.user_id = ui.user_id
                GROUP BY m.id
                ORDER BY m.province, m.name
            """)
            municipalities = cursor.fetchall()

            cursor.execute('SELECT name FROM users WHERE id = %s', (session['user_id'],))
            user = cursor.fetchone()
            if user:
                return render_template(
                    'peso/dashboard.html',
                    username=user['name'],
                    municipalities=municipalities,
                )
        except Exception as e:
            print(f"Error loading PESO dashboard: {e}")
            flash('An error occurred', 'error')
        finally:
            cursor.close()
            conn.close()
    return redirect(url_for('home'))


@app.route('/peso/reports')
@login_required
@role_required(['provincial_peso'])
def peso_reports():
    conn = get_db_connection()
    if not conn:
        return redirect(url_for('home'))

    try:
        cursor = conn.cursor(dictionary=True)
        
        # Get municipalities for filter
        cursor.execute("SELECT * FROM municipalities ORDER BY name")
        municipalities = cursor.fetchall()

        # Get available years
        cursor.execute("""
            SELECT DISTINCT YEAR(report_generated_at) as year 
            FROM final_reports 
            WHERE sent_to_provincial = 1
            ORDER BY year DESC
        """)
        available_years = [row['year'] for row in cursor.fetchall() if row['year']]
        
        # Filters
        selected_municipality = request.args.get('municipality_id')
        selected_year = request.args.get('year')
        
        if not selected_year and available_years:
            selected_year = str(available_years[0])

        where_clause = "WHERE fr.sent_to_provincial = 1"
        params = []

        if selected_municipality:
            where_clause += " AND u.municipality_id = %s"
            params.append(selected_municipality)
            
        if selected_year:
            where_clause += " AND YEAR(fr.report_generated_at) = %s"
            params.append(selected_year)

        # Get reports
        query = f"""
            SELECT 
                fr.*,
                u.name, u.email, u.id_number,
                SUBSTRING_INDEX(u.name, ' ', 1) as first_name,
                SUBSTRING_INDEX(SUBSTRING_INDEX(u.name, ' ', 2), ' ', -1) as middle_name,
                SUBSTRING_INDEX(u.name, ' ', -1) as last_name,
                m.name as municipality_name
            FROM final_reports fr
            JOIN users u ON fr.user_id = u.id
            LEFT JOIN municipalities m ON u.municipality_id = m.id
            {where_clause}
            ORDER BY fr.report_generated_at DESC, m.name ASC, fr.rank_position ASC
        """
        
        cursor.execute(query, tuple(params))
        final_reports = cursor.fetchall()
        
        # Get stats
        stats_query = f"""
            SELECT 
                COUNT(*) as total_reports,
                COUNT(DISTINCT u.municipality_id) as total_municipalities,
                SUM(CASE WHEN fr.total_score >= 90 THEN 1 ELSE 0 END) as score_90_100,
                SUM(CASE WHEN fr.total_score >= 80 AND fr.total_score < 90 THEN 1 ELSE 0 END) as score_80_89,
                SUM(CASE WHEN fr.total_score >= 70 AND fr.total_score < 80 THEN 1 ELSE 0 END) as score_70_79,
                SUM(CASE WHEN fr.total_score < 70 THEN 1 ELSE 0 END) as score_below_70
            FROM final_reports fr
            JOIN users u ON fr.user_id = u.id
            {where_clause}
        """
        cursor.execute(stats_query, tuple(params))
        stats = cursor.fetchone() or {}

        return render_template('peso/reports.html', 
                             municipalities=municipalities,
                             final_reports=final_reports,
                             stats=stats,
                             available_years=available_years,
                             selected_year=int(selected_year) if selected_year else None,
                             selected_municipality=int(selected_municipality) if selected_municipality else None)

    except Exception as e:
        print(f"Error loading PESO reports: {e}")
        flash('An error occurred loading reports', 'error')
        return redirect(url_for('peso_dashboard'))
    finally:
        cursor.close()
        conn.close()


@app.route('/peso/create-municipal-account', methods=['POST'])
@login_required
@role_required(['provincial_peso'])
def create_municipal_account():
    # Expected form fields: username, password, municipality_id
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    municipality_id = request.form.get('municipality_id')

    if not username or not password or not municipality_id:
        flash('Username, password and municipality are required', 'error')
        return redirect(url_for('peso_dashboard'))

    if len(password) < 6:
        flash('Password must be at least 6 characters long', 'error')
        return redirect(url_for('peso_dashboard'))

    conn = get_db_connection()
    if not conn:
        flash('Database connection failed', 'error')
        return redirect(url_for('peso_dashboard'))

    try:
        cursor = conn.cursor(dictionary=True)

        # Ensure municipality exists
        cursor.execute('SELECT id, province FROM municipalities WHERE id = %s', (municipality_id,))
        muni = cursor.fetchone()
        if not muni:
            flash('Selected municipality not found', 'error')
            return redirect(url_for('peso_dashboard'))

        # Ensure username is unique
        cursor.execute('SELECT id FROM users WHERE name = %s', (username,))
        if cursor.fetchone():
            flash('Username already taken', 'error')
            return redirect(url_for('peso_dashboard'))

        # Ensure municipality does not already have a municipal_peso account
        cursor.execute("SELECT id FROM users WHERE municipality_id = %s AND role = 'municipal_peso'", (municipality_id,))
        if cursor.fetchone():
            flash('This municipality already has a Municipal PESO account', 'error')
            return redirect(url_for('peso_dashboard'))

        # Hash password and insert user
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        slug = re.sub(r'[^a-z0-9]+', '.', username.lower()).strip('.')
        if not slug:
            slug = f"municipality{municipality_id}"
        base_email = f"{slug}.{municipality_id}@autogen.peso"

        # Ensure generated email is unique to satisfy NOT NULL/UNIQUE constraint
        candidate_email = base_email
        suffix = 1
        while True:
            cursor.execute('SELECT id FROM users WHERE email = %s', (candidate_email,))
            if not cursor.fetchone():
                break
            candidate_email = f"{slug}.{municipality_id}.{suffix}@autogen.peso"
            suffix += 1

        cursor.execute(
            "INSERT INTO users (name, email, password_hash, role, municipality_id) VALUES (%s, %s, %s, %s, %s)",
            (username, candidate_email, password_hash, 'municipal_peso', municipality_id)
        )
        conn.commit()
        flash('Municipal PESO account created successfully', 'success')
    except Exception as e:
        print(f"Error creating municipal account: {e}")
        flash('An error occurred while creating the account', 'error')
    finally:
        try:
            cursor.close()
        except:
            pass
        conn.close()

    return redirect(url_for('peso_dashboard'))

@app.route('/test-env')
def test_env():
    """Test route to verify environment variables are loaded correctly"""
    return jsonify({
        'google_client_id': GOOGLE_CLIENT_ID,
        'google_client_secret': '***' if GOOGLE_CLIENT_SECRET else 'Not set',
        'env_loaded': True
    })

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
    user_info = None
    
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Get user personal info
            cursor.execute("""
                SELECT first_name, middle_name, last_name, address, contact_number, birthdate
                FROM users
                WHERE id = %s
            """, (session['user_id'],))
            user_info = cursor.fetchone()

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
                         latest_announcement=latest_announcement,
                         user_info=user_info)

@app.route('/update-personal-info', methods=['POST'])
@login_required
def update_personal_info():
    if request.method == 'POST':
        first_name = request.form.get('firstName')
        middle_name = request.form.get('middleName')
        last_name = request.form.get('lastName')
        address = request.form.get('address')
        contact_number = request.form.get('contactNumber')
        birthdate = request.form.get('birthDate')
        
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE users 
                    SET first_name=%s, middle_name=%s, last_name=%s, address=%s, contact_number=%s, birthdate=%s
                    WHERE id=%s
                """, (first_name, middle_name, last_name, address, contact_number, birthdate, session['user_id']))
                conn.commit()
                flash('Personal information updated successfully', 'success')
            except Exception as e:
                print(f"Error updating personal info: {e}")
                flash('Error updating personal information', 'error')
            finally:
                cursor.close()
                conn.close()
        
        return redirect(url_for('new_applicants_dashboard'))

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
    if session.get('role') != 'municipal_peso':
        return redirect(url_for('login'))
    
    conn = None
    cursor = None
    try:
        municipality_id = get_user_municipality_id()
        if municipality_id is None:
            flash('Municipality not found', 'error')
            return redirect(url_for('home'))

        conn = get_db_connection()
        if not conn:
            flash('Unable to connect to the database', 'error')
            return redirect(url_for('home'))

        cursor = conn.cursor(dictionary=True)

        # Filter every metric to the admin's municipality scope
        cursor.execute("""
            SELECT 
                COUNT(*) AS total_applicants,
                SUM(CASE WHEN a.status = 'pending' THEN 1 ELSE 0 END) AS pending_applicants,
                SUM(CASE WHEN a.status = 'approved' THEN 1 ELSE 0 END) AS approved_applicants,
                SUM(CASE WHEN a.status = 'rejected' THEN 1 ELSE 0 END) AS rejected_applicants,
                SUM(CASE WHEN a.status = 'for exam' THEN 1 ELSE 0 END) AS for_exam_applicants,
                SUM(CASE WHEN a.status = 'for interview' THEN 1 ELSE 0 END) AS for_interview_applicants
            FROM applicants a
            JOIN users u ON a.user_id = u.id
            LEFT JOIN user_info ui ON u.id = ui.user_id
            WHERE (u.municipality_id = %s OR ui.municipality_id = %s)
        """, (municipality_id, municipality_id))
        stats_row = cursor.fetchone() or {}

        cursor.execute("""
            SELECT COUNT(*) AS count
            FROM exams e
            WHERE e.created_by = %s
        """, (session['user_id'],))
        exam_row = cursor.fetchone()
        exam_count = exam_row['count'] if exam_row else 0

        stats = {
            'total_applicants': stats_row.get('total_applicants', 0) or 0,
            'pending_applicants': stats_row.get('pending_applicants', 0) or 0,
            'approved_applicants': stats_row.get('approved_applicants', 0) or 0,
            'rejected_applicants': stats_row.get('rejected_applicants', 0) or 0,
            'for_exam_applicants': stats_row.get('for_exam_applicants', 0) or 0,
            'for_interview_applicants': stats_row.get('for_interview_applicants', 0) or 0,
            'total_exams': exam_count
        }

        cursor.execute("""
            SELECT 
                DATE_FORMAT(a.submission_date, '%Y-%m') AS month,
                COUNT(*) AS count
            FROM applicants a
            JOIN users u ON a.user_id = u.id
            LEFT JOIN user_info ui ON u.id = ui.user_id
            WHERE a.submission_date IS NOT NULL
              AND a.submission_date >= DATE_SUB(CURDATE(), INTERVAL 6 MONTH)
              AND (u.municipality_id = %s OR ui.municipality_id = %s)
            GROUP BY DATE_FORMAT(a.submission_date, '%Y-%m')
            ORDER BY month ASC
        """, (municipality_id, municipality_id))
        monthly_data = cursor.fetchall()

        exam_overview = {
            'completed': 0,
            'scheduled': 0,
            'in_progress': 0
        }
        cursor.execute("""
            SELECT 
                CASE 
                    WHEN a.status = 'approved' THEN 'completed'
                    WHEN a.status = 'for exam' THEN 'in_progress'
                    WHEN a.status = 'for interview' THEN 'scheduled'
                    ELSE NULL
                END AS exam_status,
                COUNT(*) AS count
            FROM applicants a
            JOIN users u ON a.user_id = u.id
            LEFT JOIN user_info ui ON u.id = ui.user_id
            WHERE a.status IN ('approved', 'for exam', 'for interview')
              AND (u.municipality_id = %s OR ui.municipality_id = %s)
            GROUP BY exam_status
        """, (municipality_id, municipality_id))
        exam_data = cursor.fetchall()
        for data in exam_data:
            if data['exam_status']:
                exam_overview[data['exam_status']] = data['count']

        cursor.execute("""
            SELECT 
                a.id AS application_id,
                a.status,
                a.submission_date,
                u.name AS applicant_name
            FROM applicants a
            JOIN users u ON a.user_id = u.id
            LEFT JOIN user_info ui ON u.id = ui.user_id
            WHERE (u.municipality_id = %s OR ui.municipality_id = %s)
            ORDER BY a.submission_date DESC
            LIMIT 5
        """, (municipality_id, municipality_id))
        recent_applications = cursor.fetchall()

        for app in recent_applications:
            if app['submission_date']:
                app['submission_date'] = app['submission_date'].strftime('%B %d, %Y')
        
        return render_template('admin/dashboard.html', 
                             stats=stats,
                             recent_applications=recent_applications,
                             monthly_data=monthly_data,
                             exam_overview=exam_overview,
                             exam_count=exam_count)
    except Exception as e:
        print(f"Error in admin dashboard: {str(e)}")
        return "An error occurred", 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# announcements
@app.route('/admin/announcements')
def admin_announcements():
    if 'user_id' not in session or session['role'] != 'municipal_peso':
        flash('Please login as admin to access this page', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    announcements = []
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            # Fetch announcements with admin name
            # If provincial_peso, show all announcements. If municipal_peso, show only their municipality's announcements
            if session['role'] == 'provincial_peso':
                cursor.execute("""
                    SELECT 
                        a.*,
                        u.name as admin_name
                    FROM announcements a
                    LEFT JOIN users u ON a.posted_by = u.id
                    ORDER BY a.announcement_date DESC
                """)
            else:
                municipality_id = get_user_municipality_id()
                cursor.execute("""
                    SELECT 
                        a.*,
                        u.name as admin_name
                    FROM announcements a
                    LEFT JOIN users u ON a.posted_by = u.id
                    WHERE u.municipality_id = %s
                    ORDER BY a.announcement_date DESC
                """, (municipality_id,))
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
    if 'user_id' not in session or session['role'] != 'municipal_peso':
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
    if 'user_id' not in session or session['role'] != 'municipal_peso':
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
        # Build query based on role
        if session['role'] == 'provincial_peso':
            query = """
                SELECT 
                    a.*,
                    u.name as admin_name
                FROM announcements a
                LEFT JOIN users u ON a.posted_by = u.id
                WHERE a.id = %s
            """
            params = (announcement_id,)
        else:
            municipality_id = get_user_municipality_id()
            query = """
                SELECT 
                    a.*,
                    u.name as admin_name
                FROM announcements a
                LEFT JOIN users u ON a.posted_by = u.id
                WHERE a.id = %s AND u.municipality_id = %s
            """
            params = (announcement_id, municipality_id)
        
        print(f"Executing query: {query} with ID: {announcement_id}")
        cursor.execute(query, params)
        
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
    if 'user_id' not in session or session['role'] != 'municipal_peso':
        return jsonify({'success': False, 'message': 'Unauthorized'})

    try:
        conn = get_db_connection()
        if conn:
            cursor = None
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
                if cursor:
                    cursor.close()
                conn.close()

    except Exception as e:
        print(f"Error deleting announcement: {str(e)}")
        return jsonify({'success': False, 'message': f'Error deleting announcement: {str(e)}'})

@app.route('/update-announcement', methods=['POST'])
def update_announcement():
    if 'user_id' not in session or session['role'] != 'municipal_peso':
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
    if 'user_id' not in session or session['role'] != 'municipal_peso':
        flash('Please login as admin to access this page', 'error')
        return redirect(url_for('home'))

    municipality_id = get_user_municipality_id()
    if municipality_id is None:
        flash('Municipality not found', 'error')
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
                WHERE u.municipality_id = %s OR ui.municipality_id = %s
                ORDER BY a.submission_date DESC
            """, (municipality_id, municipality_id))
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
    if 'user_id' not in session or session['role'] != 'municipal_peso':
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

def generate_id_from_gmail(gmail):
    """Generate a unique ID number from Gmail address"""
    # Extract username part before @
    username = gmail.split('@')[0]
    
    # Create a hash from the username
    hash_object = hashlib.md5(username.encode())
    hash_hex = hash_object.hexdigest()
    
    # Take first 8 characters and convert to uppercase
    id_part = hash_hex[:8].upper()
    
    # Add current year prefix
    current_year = datetime.now().year
    id_number = f"SPES{current_year}{id_part}"
    
    return id_number

def verify_google_token(token):
    """Verify Google OAuth token and return user info"""
    try:
        # Verify the token
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        
        # Check if the token is from the correct issuer
        if idinfo['iss'] not in ['accounts.google.com', 'https://accounts.google.com']:
            raise ValueError('Wrong issuer.')
        
        return {
            'email': idinfo['email'],
            'name': idinfo['name'],
            'picture': idinfo.get('picture', ''),
            'verified_email': idinfo.get('email_verified', False)
        }
    except ValueError as e:
        print(f"Token verification failed: {e}")
        return None

@app.route('/register', methods=['POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip()
        password = request.form['password']
        confirm_password = request.form['confirmPassword']
        applicant_type = request.form['applicantType']
        education_level = request.form.get('educationLevel', '').strip().lower()
        agreed_terms = request.form.get('agree_terms')
        municipality_id_raw = request.form.get('municipality_id', '').strip()
        municipality_id = None
        
        # Validation
        if not username:
            flash('Please enter a username', 'error')
            return redirect(url_for('home'))

        if not is_valid_email(email):
            flash('Please enter a valid email address', 'error')
            return redirect(url_for('home'))
            
        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return redirect(url_for('home'))
            
        if len(password) < 6:
            flash('Password must be at least 6 characters long', 'error')
            return redirect(url_for('home'))
        
        if applicant_type not in {'new', 'old'}:
            flash('Please select a valid applicant type', 'error')
            return redirect(url_for('home'))

        if education_level not in {'highschool', 'college'}:
            flash('Please select your education level', 'error')
            return redirect(url_for('home'))

        if agreed_terms != 'on':
            flash('You must agree to the Terms and Conditions to continue.', 'error')
            return redirect(url_for('home'))

        if not municipality_id_raw:
            flash('Please select your municipality.', 'error')
            return redirect(url_for('home'))

        try:
            municipality_id = int(municipality_id_raw)
        except (TypeError, ValueError):
            flash('Invalid municipality selection.', 'error')
            return redirect(url_for('home'))
        
        # Hash the password
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        conn = get_db_connection()
        if conn:
            cursor = None
            try:
                cursor = conn.cursor(dictionary=True, buffered=True)
                # Check if email already exists
                cursor.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(%s)", (email,))
                if cursor.fetchone():
                    flash('Email already registered', 'error')
                    return redirect(url_for('home'))
                
                # Check if username already exists (case insensitive)
                cursor.execute("SELECT id FROM users WHERE LOWER(name) = LOWER(%s)", (username,))
                if cursor.fetchone():
                    flash('Username already taken. Please choose another.', 'error')
                    return redirect(url_for('home'))
                
                cursor.execute(
                    "SELECT id FROM municipalities WHERE id = %s",
                    (municipality_id,)
                )
                if not cursor.fetchone():
                    flash('Selected municipality not found.', 'error')
                    return redirect(url_for('home'))

                # Insert into users table
                cursor.execute(
                    "INSERT INTO users (name, email, password_hash, role, education_level, municipality_id) VALUES (%s, %s, %s, %s, %s, %s)",
                    (username, email, password_hash, applicant_type, education_level, municipality_id)
                )
                conn.commit()
                flash('Registration successful! Please login.', 'success')
                return redirect(url_for('home'))
            except Exception as e:
                flash('Registration failed. Please try again.', 'error')
                print(f"Error: {e}")
            finally:
                try:
                    if cursor:
                        cursor.close()
                except Exception:
                    pass
                conn.close()
        else:
            flash('Unable to connect to the database. Please try again later.', 'error')
            return redirect(url_for('home'))
    return redirect(url_for('home'))

@app.route('/login', methods=['POST'])
def login():
    if request.method == 'POST':
        # Update role mapping for the new role system
        role_redirect_map = {
            'municipal_peso': '/admin/dashboard',
            'provincial_peso': '/peso/dashboard',
            'new': '/new-applicants/dashboard',
            'old': '/old-applicants/dashboard'
        }
        username = request.form['username']
        password = request.form['password']
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        conn = get_db_connection()
        if conn:
            cursor = None
            try:
                cursor = conn.cursor(dictionary=True, buffered=True)
                cursor.execute(
                    "SELECT * FROM users WHERE LOWER(name) = LOWER(%s) AND password_hash = %s LIMIT 1",
                    (username, password_hash)
                )
                user = cursor.fetchone()
                
                if user:
                    # Store user data in session
                    session['user_id'] = user['id']
                    session['username'] = user['name']
                    session['role'] = user['role']
                    
                    # Redirect based on role
                    if user['role'] == 'municipal_peso':
                        return redirect(url_for('admin_dashboard'))
                    elif user['role'] == 'new':
                        return redirect(url_for('new_applicants_dashboard'))
                    elif user['role'] == 'old':
                        return redirect(url_for('old_applicants_dashboard'))
                    elif user['role'] == 'provincial_peso':
                        return redirect(url_for('peso_dashboard'))
                    else:
                        return redirect(url_for('home'))
                else:
                    flash('Invalid username or password', 'error')
            except Exception as e:
                flash('Login failed. Please try again.', 'error')
                print(f"Error: {e}")
            finally:
                try:
                    if cursor:
                        cursor.close()
                except Exception:
                    pass
                conn.close()
    return redirect(url_for('home'))

@app.route('/gmail-login', methods=['OPTIONS','POST'])
def gmail_login():
    """Handle Gmail OAuth login"""
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        resp = Response(status=200)
        origin = request.headers.get('Origin', '*')
        resp.headers['Access-Control-Allow-Origin'] = origin
        resp.headers['Vary'] = 'Origin'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Credentials'] = 'true'
        return resp
    try:
        data = request.get_json()
        token = data.get('token')
        flow_type = (data.get('flowType') or 'login').lower()
        applicant_type = (data.get('applicantType') or 'new').lower()
        education_level = (data.get('educationLevel') or '').lower()
        municipality_id_raw = data.get('municipalityId')
        municipality_id = None
        if municipality_id_raw not in (None, '', 'null'):
            try:
                municipality_id = int(municipality_id_raw)
                if municipality_id <= 0:
                    municipality_id = None
            except (TypeError, ValueError):
                municipality_id = None
        
        if not token:
            resp = jsonify({'success': False, 'message': 'No token provided'})
            resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
            resp.headers['Vary'] = 'Origin'
            return resp
        
        # Verify the Google token
        user_info = verify_google_token(token)
        if not user_info:
            resp = jsonify({'success': False, 'message': 'Invalid token'})
            resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
            resp.headers['Vary'] = 'Origin'
            return resp
        
        email = user_info['email']
        name = user_info['name']
        
        # Check if it's a Gmail address
        if not email.endswith('@gmail.com'):
            resp = jsonify({'success': False, 'message': 'Please use a Gmail account'})
            resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
            resp.headers['Vary'] = 'Origin'
            return resp
        
        conn = get_db_connection()
        if conn:
            cursor = None
            try:
                cursor = conn.cursor(dictionary=True)
                
                # Check if user already exists
                cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
                user = cursor.fetchone()
                
                if user:
                    # User exists, log them in
                    if flow_type == 'register' and education_level in {'highschool', 'college'} and not user.get('education_level'):
                        try:
                            cursor.execute(
                                "UPDATE users SET education_level = %s WHERE id = %s",
                                (education_level, user['id'])
                            )
                            conn.commit()
                            user['education_level'] = education_level
                        except Exception as update_error:
                            print(f"Failed to update education level: {update_error}")

                    if flow_type == 'register' and municipality_id and not user.get('municipality_id'):
                        try:
                            cursor.execute(
                                "SELECT id FROM municipalities WHERE id = %s",
                                (municipality_id,)
                            )
                            if cursor.fetchone():
                                cursor.execute(
                                    "UPDATE users SET municipality_id = %s WHERE id = %s",
                                    (municipality_id, user['id'])
                                )
                                conn.commit()
                                user['municipality_id'] = municipality_id
                        except Exception as update_error:
                            print(f"Failed to update municipality: {update_error}")
                    session['user_id'] = user['id']
                    session['username'] = user['name']
                    session['role'] = user['role']
                    session['email'] = user['email']
                    session['education_level'] = user.get('education_level')
                    
                    # Generate and store ID number if not exists
                    if not user.get('id_number'):
                        id_number = generate_id_from_gmail(email)
                        cursor.execute("UPDATE users SET id_number = %s WHERE id = %s", (id_number, user['id']))
                        conn.commit()
                        session['id_number'] = id_number
                    else:
                        session['id_number'] = user['id_number']
                    
                    resp = jsonify({
                        'success': True,
                        'message': 'Login successful',
                        'redirect': get_redirect_url(user['role'])
                    })
                    resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
                    resp.headers['Vary'] = 'Origin'
                    return resp
                else:
                    # User does not exist yet
                    if flow_type != 'register':
                        resp = jsonify({
                            'success': False,
                            'message': 'No account found for this Google email. Please register first.',
                            'needsRegistration': True
                        })
                        resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
                        resp.headers['Vary'] = 'Origin'
                        return resp

                    if applicant_type not in {'new', 'old'}:
                        resp = jsonify({'success': False, 'message': 'Invalid applicant type provided.'})
                        resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
                        resp.headers['Vary'] = 'Origin'
                        return resp

                    if education_level not in {'highschool', 'college'}:
                        resp = jsonify({'success': False, 'message': 'Please select your education level.'})
                        resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
                        resp.headers['Vary'] = 'Origin'
                        return resp

                    if not municipality_id:
                        resp = jsonify({'success': False, 'message': 'Please select your municipality.'})
                        resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
                        resp.headers['Vary'] = 'Origin'
                        return resp

                    cursor.execute(
                        "SELECT id FROM municipalities WHERE id = %s",
                        (municipality_id,)
                    )
                    if not cursor.fetchone():
                        resp = jsonify({'success': False, 'message': 'Selected municipality not found.'})
                        resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
                        resp.headers['Vary'] = 'Origin'
                        return resp

                    # New user, create account
                    id_number = generate_id_from_gmail(email)

                    cursor.execute(
                        "INSERT INTO users (name, email, password_hash, role, id_number, education_level, municipality_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (name, email, 'gmail_oauth', applicant_type, id_number, education_level, municipality_id)
                    )
                    user_id = cursor.lastrowid
                    conn.commit()

                    # Set session
                    session['user_id'] = user_id
                    session['username'] = name
                    session['role'] = applicant_type
                    session['email'] = email
                    session['id_number'] = id_number
                    session['education_level'] = education_level

                    resp = jsonify({
                        'success': True,
                        'message': 'Account created and login successful',
                        'redirect': get_redirect_url(applicant_type)
                    })
                    resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
                    resp.headers['Vary'] = 'Origin'
                    return resp
                    
            except Exception as e:
                print(f"Database error: {e}")
                resp = jsonify({'success': False, 'message': 'Database error occurred'})
                resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
                resp.headers['Vary'] = 'Origin'
                return resp
            finally:
                if cursor:
                    cursor.close()
                conn.close()
        else:
            resp = jsonify({'success': False, 'message': 'Database connection failed'})
            resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
            resp.headers['Vary'] = 'Origin'
            return resp
            
    except Exception as e:
        print(f"Gmail login error: {e}")
        resp = jsonify({'success': False, 'message': 'Login failed'})
        resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
        resp.headers['Vary'] = 'Origin'
        return resp

def get_redirect_url(role):
    """Get redirect URL based on user role"""
    role_urls = {
        'municipal_peso': '/admin/dashboard',
        'provincial_peso': '/peso/dashboard',
        'new': '/new-applicants/dashboard',
        'old': '/old-applicants/dashboard'
    }
    return role_urls.get(role, '/')

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
    if 'user_id' not in session or session['role'] != 'municipal_peso':
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
                (user_id, status, submission_date, year_applied)
                VALUES (%s, 'for exam', CURDATE(), YEAR(CURDATE()))
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
    if 'user_id' not in session or session['role'] != 'municipal_peso':
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
    if 'user_id' not in session or session['role'] != 'municipal_peso':
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
    if 'user_id' not in session or session['role'] != 'municipal_peso':
        flash('Please login as admin to access this page', 'error')
        return redirect(url_for('home'))

    # Check municipality access
    municipality_id = get_user_municipality_id()
    if municipality_id is None and session['role'] != 'provincial_peso':
        flash('Municipality not found', 'error')
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
        
        # Get exams based on role and municipality
        if session['role'] == 'provincial_peso':
            # Provincial PESO can see all exams
            cursor.execute('''
                SELECT e.id, e.title, e.description, e.time_limit, e.passing_score, e.created_at, e.status, e.start_date, e.end_date,
                       u.name as created_by
                FROM exams e
                LEFT JOIN users u ON e.created_by = u.id
                ORDER BY e.created_at DESC
            ''')
        else:
            # Municipal PESO can only see exams created by their municipality
            municipality_id = get_user_municipality_id()
            cursor.execute('''
                SELECT e.id, e.title, e.description, e.time_limit, e.passing_score, e.created_at, e.status, e.start_date, e.end_date,
                       u.name as created_by
                FROM exams e
                LEFT JOIN users u ON e.created_by = u.id
                WHERE u.municipality_id = %s
                ORDER BY e.created_at DESC
            ''', (municipality_id,))
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
    return 'user_id' in session and session.get('role') == 'municipal_peso'

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
        # Modified for new workflow: exam passed -> pending for requirements instead of directly to interview
        if percentage_score >= passing_score:
            new_status = 'pending for requirements'
            # Unlock SPES forms for requirements submission
            unlock_spes_forms(session['user_id'], exam_id)
        else:
            new_status = 'failed'
        
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
        
        # Create notification based on result
        if percentage_score >= passing_score:
            notification_title = 'Exam Passed - Submit Requirements'
            notification_message = 'Congratulations! You passed the exam. Please submit your SPES requirements within 7 days.'
        else:
            notification_title = 'Exam Result'
            notification_message = 'Unfortunately, you did not pass the examination. Thank you for your participation.'
            
        cursor.execute("""
            INSERT INTO notifications (user_id, title, message, type)
            VALUES (%s, %s, %s, %s)
        """, (
            session['user_id'],
            notification_title,
            notification_message,
            'exam_result'
        ))
        
        # Log workflow change
        cursor.execute("""
            INSERT INTO workflow_audit (user_id, exam_id, old_status, new_status, change_reason, changed_by)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            session['user_id'], 
            exam_id, 
            'for exam', 
            new_status, 
            f'Exam completed with score {percentage_score:.1f}%',
            session['user_id']
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
    existing_files = {}
    
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Check if user can access SPES forms
            cursor.execute("SELECT status FROM examinees WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (session['user_id'],))
            row = cursor.fetchone()
            if row and row['status'] in ['for interview', 'pending for requirements']:
                can_access_spes_form = True
            
            # Get existing uploaded files
            cursor.execute("""
                SELECT application_form, employment_contract, oath_of_undertaking, 
                       school_certification, cor_or_coe, cog, barangay_indigency, 
                       psa_birth_certificate
                FROM user_documents 
                WHERE user_id = %s
            """, (session['user_id'],))
            files_row = cursor.fetchone()
            
            if files_row:
                existing_files = {
                    'application_form': files_row['application_form'],
                    'employment_contract': files_row['employment_contract'],
                    'oath_of_undertaking': files_row['oath_of_undertaking'],
                    'school_certification': files_row['school_certification'],
                    'cor_or_coe': files_row['cor_or_coe'],
                    'cog': files_row['cog'],
                    'barangay_indigency': files_row['barangay_indigency'],
                    'psa_birth_certificate': files_row['psa_birth_certificate']
                }
                
        except Exception as e:
            print(f"Error checking examinee status or files: {e}")
        finally:
            cursor.close()
            conn.close()
    
    return render_template('new/spes-form.html', 
                         username=session['username'], 
                         can_access_spes_form=can_access_spes_form,
                         existing_files=existing_files)

@app.route('/upload-forms', methods=['POST'])
@login_required
def upload_forms():
    """Handle SPES forms file uploads for new applicants"""
    if session.get('role') != 'new':
        return jsonify({'success': False, 'message': 'Access denied'})
    
    user_id = session['user_id']
    
    # Create user documents directory if it doesn't exist
    user_docs_dir = os.path.join('static', 'user_documents', str(user_id))
    os.makedirs(user_docs_dir, exist_ok=True)
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Database connection failed'})
    
    try:
        cursor = conn.cursor(dictionary=True)
        
        # Check if user can upload forms (must be in interview or pending requirements status)
        cursor.execute("SELECT status, exam_id FROM examinees WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (user_id,))
        examinee_row = cursor.fetchone()
        
        if not examinee_row or examinee_row['status'] not in ['for interview', 'pending for requirements']:
            return jsonify({'success': False, 'message': 'You are not authorized to upload forms at this time'})
        
        # Get the exam_id from the examinee record
        exam_id = examinee_row['exam_id']
        if not exam_id:
            return jsonify({'success': False, 'message': 'No exam assigned. Please contact administrator.'})
        
        # Define allowed file types and field mappings (form_field_name: db_column_name)
        allowed_extensions = {'pdf', 'jpg', 'jpeg', 'png', 'docx'}
        file_fields = {
            'applicationForm': 'application_form',
            'employmentContract': 'employment_contract', 
            'oathOfUndertaking': 'oath_of_undertaking',
            'schoolCertification': 'school_certification',
            'corOrCoe': 'cor_or_coe',
            'cog': 'cog',
            'barangayIndigency': 'barangay_indigency',
            'psaBirthCertificate': 'psa_birth_certificate'
        }
        
        uploaded_files = {}
        
        # Process each uploaded file
        for field_name, db_column in file_fields.items():
            if field_name in request.files:
                file = request.files[field_name]
                if file and file.filename:
                    # Check file extension
                    filename = secure_filename(file.filename)
                    file_extension = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
                    
                    if file_extension not in allowed_extensions:
                        return jsonify({'success': False, 'message': f'Invalid file type for {field_name}. Only PDF, JPG, JPEG, PNG allowed.'})
                    
                    # Generate unique filename
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    new_filename = f"{field_name}_{timestamp}.{file_extension}"
                    file_path = os.path.join(user_docs_dir, new_filename)
                    
                    # Save file
                    file.save(file_path)
                    uploaded_files[db_column] = new_filename
        
        if not uploaded_files:
            return jsonify({'success': False, 'message': 'No files were uploaded'})
        
        # Check if user already has documents record
        cursor.execute("SELECT id FROM user_documents WHERE user_id = %s", (user_id,))
        existing_record = cursor.fetchone()
        
        if existing_record:
            # Update existing record
            update_fields = []
            update_values = []
            
            for db_column, filename in uploaded_files.items():
                update_fields.append(f"{db_column} = %s")
                update_values.append(filename)
            
            update_values.append(user_id)
            update_query = f"UPDATE user_documents SET {', '.join(update_fields)} WHERE user_id = %s"
            cursor.execute(update_query, update_values)
        else:
            # Insert new record
            columns = ['user_id'] + list(uploaded_files.keys())
            values = [user_id] + list(uploaded_files.values())
            placeholders = ', '.join(['%s'] * len(values))
            
            insert_query = f"INSERT INTO user_documents ({', '.join(columns)}) VALUES ({placeholders})"
            cursor.execute(insert_query, values)
        
        conn.commit()
        
        # Create requirements submission entry if it doesn't exist
        cursor.execute("""
            SELECT id FROM requirements_submissions 
            WHERE user_id = %s 
            ORDER BY submitted_at DESC 
            LIMIT 1
        """, (user_id,))
        req_submission = cursor.fetchone()
        
        if not req_submission:
            # Create new requirements submission record using the exam_id we already have
            cursor.execute("""
                INSERT INTO requirements_submissions 
                (user_id, exam_id, submission_status, submitted_at)
                VALUES (%s, %s, 'pending_review', NOW())
            """, (user_id, exam_id))
        else:
            # Update existing submission
            cursor.execute("""
                UPDATE requirements_submissions 
                SET submission_status = 'pending_review', submitted_at = NOW()
                WHERE id = %s
            """, (req_submission['id'],))
        
        conn.commit()
        
        return jsonify({
            'success': True, 
            'message': f'Successfully uploaded {len(uploaded_files)} file(s). Your requirements are now under review.'
        })
        
    except Exception as e:
        conn.rollback()
        print(f"Error uploading forms: {str(e)}")
        return jsonify({'success': False, 'message': f'Error uploading forms: {str(e)}'})
    finally:
        cursor.close()
        conn.close()

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

    # Map logical document names to static docx resources
    docx_templates = {
        'APPLICATION-FORM': 'APPLICATION-FORM.docx',
        'SCHOOL-CERTIFICATION': 'SCHOOL-CERTIFICATION.docx',
        'SPES-EMPLOYMENT-CONTRACT': 'SPES-EMPLOYMENT-CONTRACT.docx',
        'OATH-OF-UNDERTAKING': 'OATH-OF-UNDERTAKING.docx'
    }

    if document_type not in docx_templates:
        return jsonify({'success': False, 'message': 'Invalid document type'})

    # Prefer a light-weight static PDF preview if one exists
    pdf_preview_dir = os.path.join('static', 'spes-forms', 'previews')
    pdf_path = os.path.join(pdf_preview_dir, f'{document_type}.pdf')

    try:
        if os.path.exists(pdf_path):
            return send_file(
                pdf_path,
                mimetype='application/pdf',
                as_attachment=False,
                download_name=f'{document_type}.pdf'
            )

        # Fall back to serving the DOCX directly for download when no cached preview exists.
        docx_path = os.path.join('static', 'spes-forms', docx_templates[document_type])
        if os.path.exists(docx_path):
            return send_file(
                docx_path,
                mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                as_attachment=True,
                download_name=docx_templates[document_type]
            )

        return jsonify({'success': False, 'message': 'Template file not found on the server.'})
    except Exception as e:
        print(f"Error serving document preview: {e}")
        return jsonify({'success': False, 'message': 'Error previewing document'})

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
    if 'user_id' not in session or session['role'] != 'municipal_peso':
        flash('Please login as admin to access this page', 'error')
        return redirect(url_for('home'))

    municipality_id = get_user_municipality_id()
    if municipality_id is None:
        flash('Municipality not found', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    examinees = []
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            if session['role'] == 'provincial_peso':
                # Provincial PESO can see all examinees
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
            else:
                # Municipal PESO can only see examinees from their municipality
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
                    WHERE u.municipality_id = %s OR ui.municipality_id = %s
                    ORDER BY e.created_at DESC
                """, (municipality_id, municipality_id))
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
    if session.get('role') != 'municipal_peso':
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
    if session.get('role') != 'municipal_peso':
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
    if 'user_id' not in session or session['role'] != 'municipal_peso':
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
                WHERE u.role != 'municipal_peso' AND (m.sender_id = %s OR m.receiver_id = %s)
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

# Editable Application Form Routes
@app.route('/edit-application-form', methods=['GET', 'POST'])
@login_required
def edit_application_form():
    if request.method == 'POST':
        try:
            # Get form data (now using FormData for file uploads)
            form_data = request.form.to_dict()
            
            # Generate DOCX with filled data
            from docxtpl import DocxTemplate
            import os
            import uuid
            from datetime import datetime
            
            # Handle image upload
            passport_image = None
            if 'passport_image' in request.files:
                file = request.files['passport_image']
                if file and file.filename:
                    # Save the uploaded image
                    user_id = session['user_id']
                    user_docs_path = os.path.join('static', 'user_documents', str(user_id))
                    os.makedirs(user_docs_path, exist_ok=True)
                    
                    # Generate unique filename for image
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    unique_id = str(uuid.uuid4())[:8]
                    image_filename = f'passport_photo_{timestamp}_{unique_id}.jpg'
                    image_path = os.path.join(user_docs_path, image_filename)
                    
                    # Save image
                    file.save(image_path)
                    passport_image = image_path
            
            # Load template
            template_path = os.path.join('static', 'spes-forms', 'APPLICATION-FORM.docx')
            doc = DocxTemplate(template_path)
            
            # Prepare context data - using the exact placeholder names from your template
            context = {
                'l_name': form_data.get('last_name', ''), # l_name is the placeholder for last name
                'f_name': form_data.get('first_name', ''), # f_name is the placeholder for first name
                'm_name': form_data.get('middle_name', ''), # m_name is the placeholder for middle name
                'gsis_relationship': form_data.get('gsis_relationship', ''),
                'passport_size_picture': '[PASSPORT PHOTO]',  # Will be replaced with actual image
                
                # Checkbox placeholders - handle civil status
                'b1': '☐',  # Single checkbox
                'b2': '☐',  # Married checkbox  
                'b3': '☐',  # Widow checkbox
                
                # Additional checkbox placeholders for other fields
                'b4': '☐',  # Male checkbox
                'b5': '☐',  # Female checkbox
                
                # Student category checkboxes
                'b6': '☐', # Students checkbox
                'b7': '☐', # ALS checkbox
                'b8': '☐', # OSY checkbox
                
                # Beneficiary/Parents status checkboxes
                'b9': '☐', # Living together
                'b10': '☐', # Solo Parent
                'b11': '☐', # Separated
                'b12': '☐', # Senior Citizen
                'b13': '☐', # Sugar Plantation Worker
                'b14': '☐', # Indigenous People
                'b15': '☐', # Displaced Worker
                'b16': '☐', # Local
                'b17': '☐', # OFW
                'b18': '☐', # Rebel Returnee
                'b19': '☐', # Victims of Armed Conflicts
                'b20': '☐', # Person with Disability
                
                # Add any other placeholders you've added to your template
                'applicant_name': f"{form_data.get('first_name', '')} {form_data.get('middle_name', '')} {form_data.get('last_name', '')}".strip(),
                'present_address': form_data.get('present_address', ''),
                'permanent_address': form_data.get('permanent_address', ''),
                'birth_date': form_data.get('birth_date', ''),
                'birth_place': form_data.get('birth_place', ''),
                'age': form_data.get('age', ''),
                'sex': form_data.get('sex', ''),
                'civil_status': form_data.get('civil_status', ''),
                'contact_number': form_data.get('contact_number', ''),
                'email': form_data.get('email', ''),
                'facebook': form_data.get('facebook', ''),
                'instagram': form_data.get('instagram', ''),
                'twitter': form_data.get('twitter', ''),
                'other_social': form_data.get('other_social', ''),
                
                # Education table placeholders
                'elem_school': form_data.get('elem_school', ''),
                'elem_degree': form_data.get('elem_degree', ''),
                'elem_year': form_data.get('elem_year', ''),
                'elem_attendance': form_data.get('elem_attendance', ''),
                'sec_school': form_data.get('sec_school', ''),
                'sec_degree': form_data.get('sec_degree', ''),
                'sec_year': form_data.get('sec_year', ''),
                'sec_attendance': form_data.get('sec_attendance', ''),
                'ter_school': form_data.get('ter_school', ''),
                'ter_degree': form_data.get('ter_degree', ''),
                'ter_year': form_data.get('ter_year', ''),
                'ter_attendance': form_data.get('ter_attendance', ''),
                'tech_school': form_data.get('tech_school', ''),
                'tech_degree': form_data.get('tech_degree', ''),
                'tech_year': form_data.get('tech_year', ''),
                'tech_attendance': form_data.get('tech_attendance', ''),
                'father_name': form_data.get('father_name', ''),
                'father_occupation': form_data.get('father_occupation', ''),
                'mother_name': form_data.get('mother_name', ''),
                'mother_occupation': form_data.get('mother_occupation', ''),
                'signature': '[SIGNATURE]',  # Will be replaced with actual signature image
                'signature_date': datetime.now().strftime('%B %d, %Y'),
                'applicant_signature': '[SIGNATURE]'  # Placeholder for signature
            }
            
            # Handle checkbox selection based on civil status
            civil_status = form_data.get('civil_status', '').lower()
            if civil_status == 'single':
                context['b1'] = '☑'  # Checked
                context['b2'] = '☐'  # Unchecked
                context['b3'] = '☐'  # Unchecked
            elif civil_status == 'married':
                context['b1'] = '☐'  # Unchecked
                context['b2'] = '☑'  # Checked
                context['b3'] = '☐'  # Unchecked
            elif civil_status == 'widowed':
                context['b1'] = '☐'  # Unchecked
                context['b2'] = '☐'  # Unchecked
                context['b3'] = '☑'  # Checked
            
            # Handle checkbox selection based on sex
            sex = form_data.get('sex', '').lower()
            if sex == 'male':
                context['b4'] = '☑'  # Checked
                context['b5'] = '☐'  # Unchecked
            elif sex == 'female':
                context['b4'] = '☐'  # Unchecked
                context['b5'] = '☑'  # Checked
            
            # Handle student category checkboxes
            student_category = form_data.get('student_category', '').lower()
            if student_category == 'students':
                context['b6'] = '☑'  # Students checked
            elif student_category == 'als':
                context['b7'] = '☑'  # ALS checked
            elif student_category == 'osy':
                context['b8'] = '☑'  # OSY checked
                
            # Handle beneficiary/parents status checkboxes (can be multiple selections)
            beneficiary_status = form_data.get('beneficiary_status', '').lower()
            if 'living together' in beneficiary_status:
                context['b9'] = '☑'  # Living together checked
            if 'solo parent' in beneficiary_status:
                context['b10'] = '☑'  # Solo Parent checked
            if 'separated' in beneficiary_status:
                context['b11'] = '☑'  # Separated checked
            if 'senior citizen' in beneficiary_status:
                context['b12'] = '☑'  # Senior Citizen checked
            if 'sugar plantation worker' in beneficiary_status:
                context['b13'] = '☑'  # Sugar Plantation Worker checked
            if 'indigenous people' in beneficiary_status:
                context['b14'] = '☑'  # Indigenous People checked
            if 'displaced worker' in beneficiary_status:
                context['b15'] = '☑'  # Displaced Worker checked
            if 'local' in beneficiary_status:
                context['b16'] = '☑'  # Local checked
            if 'ofw' in beneficiary_status:
                context['b17'] = '☑'  # OFW checked
            if 'rebel returnee' in beneficiary_status:
                context['b18'] = '☑'  # Rebel Returnee checked
            if 'victims of armed conflicts' in beneficiary_status:
                context['b19'] = '☑'  # Victims of Armed Conflicts checked
            if 'person with disability' in beneficiary_status:
                context['b20'] = '☑'  # Person with Disability checked
            
            # Render document
            doc.render(context)
            
            # Insert actual image if passport photo was uploaded
            if passport_image and os.path.exists(passport_image):
                from docx.shared import Inches
                from docx.enum.text import WD_ALIGN_PARAGRAPH

                # Find and replace the [PASSPORT PHOTO] placeholder with actual image
                for paragraph in doc.paragraphs:
                    if '[PASSPORT PHOTO]' in paragraph.text:
                        # Clear the paragraph
                        paragraph.clear()

                        # Add the image
                        run = paragraph.add_run()
                        run.add_picture(passport_image, width=Inches(1.2), height=Inches(1.5))
                        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        break

                # Also check in tables
                for table in doc.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            for paragraph in cell.paragraphs:
                                if '[PASSPORT PHOTO]' in paragraph.text:
                                    # Clear the paragraph
                                    paragraph.clear()

                                    # Add the image
                                    run = paragraph.add_run()
                                    run.add_picture(passport_image, width=Inches(1.2), height=Inches(1.5))
                                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                                    break

            # Insert signature if provided
            signature_data = form_data.get('signature', '')
            if signature_data:
                from docx.shared import Inches
                from docx.enum.text import WD_ALIGN_PARAGRAPH
                import base64
                from io import BytesIO

                try:
                    # Remove data URL prefix if present
                    if signature_data.startswith('data:image'):
                        signature_data = signature_data.split(',')[1]
                    
                    # Decode base64 image
                    signature_bytes = base64.b64decode(signature_data)
                    signature_stream = BytesIO(signature_bytes)

                    # Find and replace the [SIGNATURE] placeholder with actual signature
                    # Check regular paragraphs
                    for paragraph in doc.paragraphs:
                        for run in paragraph.runs:
                            if '[SIGNATURE]' in run.text:
                                # Replace the text with image in the same run
                                run.clear()
                                
                                # Add signature image with custom positioning
                                picture = run.add_picture(signature_stream, width=Inches(2.0), height=Inches(0.5))
                                
                                # Apply custom margins and positioning (like CSS)
                                # You can customize these values:
                                right_margin = Inches(0.5)  # Adjust this value for right margin
                                left_margin = Inches(0.2)   # Adjust this value for left margin
                                top_margin = Inches(0.1)    # Adjust this value for top margin
                                bottom_margin = Inches(0.1) # Adjust this value for bottom margin
                                
                                # Set paragraph indentation for margins
                                paragraph.paragraph_format.left_indent = left_margin
                                paragraph.paragraph_format.right_indent = right_margin
                                paragraph.paragraph_format.space_before = top_margin
                                paragraph.paragraph_format.space_after = bottom_margin
                                
                                break

                    # Check in tables
                    for table in doc.tables:
                        for row in table.rows:
                            for cell in row.cells:
                                for paragraph in cell.paragraphs:
                                    for run in paragraph.runs:
                                        if '[SIGNATURE]' in run.text:
                                            # Replace the text with image in the same run
                                            run.clear()
                                            
                                            # Add signature image with custom positioning
                                            picture = run.add_picture(signature_stream, width=Inches(2.0), height=Inches(0.5))
                                            
                                            # Apply custom margins and positioning for table cells
                                            right_margin = Inches(.5)  # Adjust this value for right margin
                                            left_margin = Inches(0.1)   # Adjust this value for left margin
                                            top_margin = Inches(0)   # Adjust this value for top margin
                                            bottom_margin = Inches(0) # Adjust this value for bottom margin
                                            
                                            # Set paragraph indentation for margins
                                            paragraph.paragraph_format.left_indent = left_margin
                                            paragraph.paragraph_format.right_indent = right_margin
                                            paragraph.paragraph_format.space_before = top_margin
                                            paragraph.paragraph_format.space_after = bottom_margin
                                            
                                            break
                except Exception as e:
                    print(f"Error inserting signature: {e}")
            else:
                # If no signature provided, replace [SIGNATURE] with empty text
                for paragraph in doc.paragraphs:
                    for run in paragraph.runs:
                        if '[SIGNATURE]' in run.text:
                            run.text = run.text.replace('[SIGNATURE]', '')
                            break

                # Also check in tables
                for table in doc.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            for paragraph in cell.paragraphs:
                                for run in paragraph.runs:
                                    if '[SIGNATURE]' in run.text:
                                        run.text = run.text.replace('[SIGNATURE]', '')
                                        break
            
            # Generate unique filename
            user_id = session['user_id']
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            unique_id = str(uuid.uuid4())[:8]
            filename = f'APPLICATION-FORM_{timestamp}_{unique_id}.docx'
            
            # Save to user documents folder
            user_docs_path = os.path.join('static', 'user_documents', str(user_id))
            os.makedirs(user_docs_path, exist_ok=True)
            output_path = os.path.join(user_docs_path, filename)
            
            doc.save(output_path)
            
            return jsonify({
                'success': True, 
                'message': 'Application form generated successfully',
                'filename': filename,
                'download_url': f'/download-generated-form/{user_id}/{filename}'
            })
            
        except Exception as e:
            return jsonify({'success': False, 'message': f'Error generating form: {str(e)}'})
    
    else:
        # Return form template data
        return jsonify({
            'success': True,
            'form_fields': {
                'personal_info': {
                    'last_name': {'type': 'text', 'label': 'Last Name', 'required': True},
                    'first_name': {'type': 'text', 'label': 'First Name', 'required': True},
                    'middle_name': {'type': 'text', 'label': 'Middle Name', 'required': False},
                    'address': {'type': 'textarea', 'label': 'Complete Address', 'required': True},
                    'barangay': {'type': 'text', 'label': 'Barangay', 'required': True},
                    'city': {'type': 'text', 'label': 'City/Municipality', 'required': True},
                    'province': {'type': 'text', 'label': 'Province', 'required': True},
                    'zip_code': {'type': 'text', 'label': 'ZIP Code', 'required': True},
                    'birth_date': {'type': 'date', 'label': 'Date of Birth', 'required': True},
                    'birth_place': {'type': 'text', 'label': 'Place of Birth', 'required': True},
                    'age': {'type': 'number', 'label': 'Age', 'required': True},
                    'sex': {'type': 'select', 'label': 'Sex', 'required': True, 'options': ['Male', 'Female']},
                    'civil_status': {'type': 'select', 'label': 'Civil Status', 'required': True, 'options': ['Single', 'Married', 'Widowed', 'Divorced']},
                    'contact_number': {'type': 'tel', 'label': 'Contact Number', 'required': True},
                    'email': {'type': 'email', 'label': 'Email Address', 'required': False}
                },
                'educational_info': {
                    'school_name': {'type': 'text', 'label': 'School Name', 'required': True},
                    'course': {'type': 'text', 'label': 'Course/Program', 'required': True},
                    'year_level': {'type': 'select', 'label': 'Year Level', 'required': True, 'options': ['1st Year', '2nd Year', '3rd Year', '4th Year', '5th Year']},
                    'gpa': {'type': 'text', 'label': 'GPA/GWA', 'required': True}
                },
                'family_info': {
                    'father_name': {'type': 'text', 'label': "Father's Name", 'required': False},
                    'father_occupation': {'type': 'text', 'label': "Father's Occupation", 'required': False},
                    'mother_name': {'type': 'text', 'label': "Mother's Name", 'required': False},
                    'mother_occupation': {'type': 'text', 'label': "Mother's Occupation", 'required': False},
                    'guardian_name': {'type': 'text', 'label': "Guardian's Name", 'required': False},
                    'guardian_relationship': {'type': 'text', 'label': 'Relationship to Guardian', 'required': False},
                    'guardian_contact': {'type': 'tel', 'label': "Guardian's Contact Number", 'required': False}
                }
            }
        })

@app.route('/download-generated-form/<int:user_id>/<filename>')
@login_required
def download_generated_form(user_id, filename):
    # Verify user can access this file
    if session['user_id'] != user_id and session['role'] != 'municipal_peso':
        return jsonify({'success': False, 'message': 'Unauthorized access'})
    
    file_path = os.path.join('static', 'user_documents', str(user_id), filename)
    
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True, download_name=filename)
    else:
        return jsonify({'success': False, 'message': 'File not found'})

@app.route('/edit-school-certification', methods=['GET', 'POST'])
@login_required
def edit_school_certification():
    if request.method == 'POST':
        try:
            # Get form data
            form_data = request.form.to_dict()

            from docxtpl import DocxTemplate
            from docx import Document
            import os
            import uuid
            from datetime import datetime
            import re

            # Load template
            template_path = os.path.join('static', 'spes-forms', 'SCHOOL-CERTIFICATION.docx')
            
            # First try with docxtpl
            try:
                doc = DocxTemplate(template_path)
                
                # Prepare context data - using the exact placeholder names from your template
                context = {
                    'f_name': form_data.get('f_name', ''),
                    'm_name': form_data.get('m_name', ''),
                    'l_name': form_data.get('l_name', ''),
                    'average': form_data.get('average', ''),
                    'day': form_data.get('day', ''),
                    'month': form_data.get('month', ''),
                    'r_name': form_data.get('r_name', ''),
                    'school': form_data.get('school', ''),
                    'municipality': form_data.get('municipality', ''),
                    'province': form_data.get('province', '')
                }

                # Render document
                doc.render(context)
                
            except Exception as e:
                # If docxtpl fails (likely due to text boxes), use manual replacement
                print(f"DocxTemplate failed: {e}. Using manual replacement...")
                
                # Load document with python-docx
                doc = Document(template_path)
                
                # Prepare replacement data
                replacements = {
                    '{{ f_name }}': form_data.get('f_name', ''),
                    '{{ m_name }}': form_data.get('m_name', ''),
                    '{{ l_name }}': form_data.get('l_name', ''),
                    '{{ average }}': form_data.get('average', ''),
                    '{{ day }}': form_data.get('day', ''),
                    '{{ month }}': form_data.get('month', ''),
                    '{{ r_name }}': form_data.get('r_name', ''),
                    '{{ school }}': form_data.get('school', ''),
                    '{{ municipality }}': form_data.get('municipality', ''),
                    '{{ province }}': form_data.get('province', '')
                }
                
                # Function to replace text in paragraphs
                def replace_text_in_paragraph(paragraph, replacements):
                    for placeholder, replacement in replacements.items():
                        if placeholder in paragraph.text:
                            # Get the full text
                            full_text = paragraph.text
                            # Replace the placeholder
                            new_text = full_text.replace(placeholder, replacement)
                            
                            # Clear the paragraph and add new text
                            paragraph.clear()
                            paragraph.add_run(new_text)
                
                # Function to replace text in text boxes and shapes
                def replace_text_in_shapes(doc, replacements):
                    # Handle inline shapes (text boxes)
                    for shape in doc.inline_shapes:
                        try:
                            # Access the text box content through the XML
                            if hasattr(shape, '_inline') and shape._inline.graphic:
                                textbox = shape._inline.graphic.graphicData
                                if textbox is not None:
                                    # Find text elements and replace
                                    for text_elem in textbox.iter():
                                        if hasattr(text_elem, 'text') and text_elem.text:
                                            for placeholder, replacement in replacements.items():
                                                if placeholder in text_elem.text:
                                                    text_elem.text = text_elem.text.replace(placeholder, replacement)
                        except:
                            continue
                    
                    # Handle floating shapes (another type of text box)
                    for shape in doc.part.related_parts:
                        try:
                            if hasattr(shape, 'element') and shape.element is not None:
                                for text_elem in shape.element.iter():
                                    if hasattr(text_elem, 'text') and text_elem.text:
                                        for placeholder, replacement in replacements.items():
                                            if placeholder in text_elem.text:
                                                text_elem.text = text_elem.text.replace(placeholder, replacement)
                        except:
                            continue
                
                # Replace text in all paragraphs
                for paragraph in doc.paragraphs:
                    replace_text_in_paragraph(paragraph, replacements)
                
                # Replace text in tables
                for table in doc.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            for paragraph in cell.paragraphs:
                                replace_text_in_paragraph(paragraph, replacements)
                
                # Try to replace text in text boxes and shapes
                replace_text_in_shapes(doc, replacements)
                
                # Also try to replace in headers and footers
                for section in doc.sections:
                    for paragraph in section.header.paragraphs:
                        replace_text_in_paragraph(paragraph, replacements)
                    for paragraph in section.footer.paragraphs:
                        replace_text_in_paragraph(paragraph, replacements)
            
            # Generate unique filename
            user_id = session['user_id']
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            unique_id = str(uuid.uuid4())[:8]
            filename = f'SCHOOL-CERTIFICATION_{timestamp}_{unique_id}.docx'

            # Save to user documents folder
            user_docs_path = os.path.join('static', 'user_documents', str(user_id))
            os.makedirs(user_docs_path, exist_ok=True)
            output_path = os.path.join(user_docs_path, filename)

            doc.save(output_path)

            return jsonify({
                'success': True,
                'message': 'School certification generated successfully',
                'filename': filename,
                'download_url': f'/download-generated-form/{user_id}/{filename}'
            })

        except Exception as e:
            return jsonify({'success': False, 'message': f'Error generating school certification: {str(e)}'})

    else:
        # Return form template data
        return jsonify({
            'success': True,
            'form_fields': {
                'student_info': {
                    'f_name': {'type': 'text', 'label': 'First Name', 'required': True},
                    'm_name': {'type': 'text', 'label': 'Middle Name', 'required': False},
                    'l_name': {'type': 'text', 'label': 'Last Name', 'required': True},
                    'average': {'type': 'text', 'label': 'Average (during first sem)', 'required': True}
                },
                'date_info': {
                    'day': {'type': 'text', 'label': 'Day', 'required': True, 'placeholder': 'e.g., 15'},
                    'month': {'type': 'text', 'label': 'Month', 'required': True, 'placeholder': 'e.g., January'}
                },
                'school_info': {
                    'r_name': {'type': 'text', 'label': 'Registrar Name', 'required': True},
                    'school': {'type': 'text', 'label': 'School Name', 'required': True},
                    'municipality': {'type': 'text', 'label': 'Municipality', 'required': True},
                    'province': {'type': 'text', 'label': 'Province', 'required': True}
                }
            }
        })

@app.route('/edit-employment-contract', methods=['GET', 'POST'])
@login_required
def edit_employment_contract():
    if request.method == 'POST':
        try:
            # Get form data
            form_data = request.form.to_dict()

            from docxtpl import DocxTemplate
            from docx import Document
            import os
            import uuid
            from datetime import datetime
            import re

            # Load template
            template_path = os.path.join('static', 'spes-forms', 'SPES-EMPLOYMENT-CONTRACT.docx')
            
            # First try with docxtpl
            try:
                doc = DocxTemplate(template_path)
                
                # Prepare context data - using the exact placeholder names from your template
                context = {
                    'f_name': form_data.get('f_name', ''),
                    'm_name': form_data.get('m_name', ''),
                    'l_name': form_data.get('l_name', ''),
                    'birth': form_data.get('birth', ''),
                    'age': form_data.get('age', ''),
                    'status': form_data.get('status', ''),
                    'address': form_data.get('address', ''),
                    'days': form_data.get('days', ''),
                    's_date': form_data.get('s_date', ''),
                    'e_date': form_data.get('e_date', ''),
                    'p_name': form_data.get('p_name', '')
                }

                # Render document
                doc.render(context)
                
            except Exception as e:
                # If docxtpl fails (likely due to text boxes), use manual replacement
                print(f"DocxTemplate failed: {e}. Using manual replacement...")
                
                # Load document with python-docx
                doc = Document(template_path)
                
                # Prepare replacement data
                replacements = {
                    '{{ f_name }}': form_data.get('f_name', ''),
                    '{{ m_name }}': form_data.get('m_name', ''),
                    '{{ l_name }}': form_data.get('l_name', ''),
                    '{{ birth }}': form_data.get('birth', ''),
                    '{{ age }}': form_data.get('age', ''),
                    '{{ status }}': form_data.get('status', ''),
                    '{{ address }}': form_data.get('address', ''),
                    '{{ days }}': form_data.get('days', ''),
                    '{{ s_date }}': form_data.get('s_date', ''),
                    '{{ e_date }}': form_data.get('e_date', ''),
                    '{{ p_name }}': form_data.get('p_name', '')
                }
                
                # Function to replace text in paragraphs
                def replace_text_in_paragraph(paragraph, replacements):
                    for placeholder, replacement in replacements.items():
                        if placeholder in paragraph.text:
                            # Get the full text
                            full_text = paragraph.text
                            # Replace the placeholder
                            new_text = full_text.replace(placeholder, replacement)
                            
                            # Clear the paragraph and add new text
                            paragraph.clear()
                            paragraph.add_run(new_text)
                
                # Function to replace text in text boxes and shapes
                def replace_text_in_shapes(doc, replacements):
                    # Handle inline shapes (text boxes)
                    for shape in doc.inline_shapes:
                        try:
                            # Access the text box content through the XML
                            if hasattr(shape, '_inline') and shape._inline.graphic:
                                textbox = shape._inline.graphic.graphicData
                                if textbox is not None:
                                    # Find text elements and replace
                                    for text_elem in textbox.iter():
                                        if hasattr(text_elem, 'text') and text_elem.text:
                                            for placeholder, replacement in replacements.items():
                                                if placeholder in text_elem.text:
                                                    text_elem.text = text_elem.text.replace(placeholder, replacement)
                        except:
                            continue
                    
                    # Handle floating shapes (another type of text box)
                    for shape in doc.part.related_parts:
                        try:
                            if hasattr(shape, 'element') and shape.element is not None:
                                for text_elem in shape.element.iter():
                                    if hasattr(text_elem, 'text') and text_elem.text:
                                        for placeholder, replacement in replacements.items():
                                            if placeholder in text_elem.text:
                                                text_elem.text = text_elem.text.replace(placeholder, replacement)
                        except:
                            continue
                
                # Replace text in all paragraphs
                for paragraph in doc.paragraphs:
                    replace_text_in_paragraph(paragraph, replacements)
                
                # Replace text in tables
                for table in doc.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            for paragraph in cell.paragraphs:
                                replace_text_in_paragraph(paragraph, replacements)
                
                # Try to replace text in text boxes and shapes
                replace_text_in_shapes(doc, replacements)
                
                # Also try to replace in headers and footers
                for section in doc.sections:
                    for paragraph in section.header.paragraphs:
                        replace_text_in_paragraph(paragraph, replacements)
                    for paragraph in section.footer.paragraphs:
                        replace_text_in_paragraph(paragraph, replacements)
            
            # Generate unique filename
            user_id = session['user_id']
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            unique_id = str(uuid.uuid4())[:8]
            filename = f'SPES-EMPLOYMENT-CONTRACT_{timestamp}_{unique_id}.docx'

            # Save to user documents folder
            user_docs_path = os.path.join('static', 'user_documents', str(user_id))
            os.makedirs(user_docs_path, exist_ok=True)
            output_path = os.path.join(user_docs_path, filename)

            doc.save(output_path)

            return jsonify({
                'success': True,
                'message': 'SPES Employment Contract generated successfully',
                'filename': filename,
                'download_url': f'/download-generated-form/{user_id}/{filename}'
            })

        except Exception as e:
            return jsonify({'success': False, 'message': f'Error generating SPES Employment Contract: {str(e)}'})
    
    else:
        # GET request - return form field definitions
        return jsonify({
            'success': True,
            'form_fields': {
                'personal_info': {
                    'f_name': {'type': 'text', 'label': 'First Name', 'required': True},
                    'm_name': {'type': 'text', 'label': 'Middle Name', 'required': False},
                    'l_name': {'type': 'text', 'label': 'Last Name', 'required': True},
                    'birth': {'type': 'date', 'label': 'Date of Birth', 'required': True},
                    'age': {'type': 'number', 'label': 'Age', 'required': True},
                    'status': {'type': 'select', 'label': 'Civil Status', 'required': True, 'options': ['Single', 'Married', 'Widowed', 'Divorced']},
                    'address': {'type': 'textarea', 'label': 'Address', 'required': True}
                },
                'employment_info': {
                    'days': {'type': 'number', 'label': 'Number of Days', 'required': True, 'placeholder': 'e.g., 20'},
                    's_date': {'type': 'date', 'label': 'Start Date', 'required': True},
                    'e_date': {'type': 'date', 'label': 'End Date', 'required': True}
                },
                'parent_info': {
                    'p_name': {'type': 'text', 'label': 'Parent/Guardian Name', 'required': True}
                }
            }
        })

@app.route('/edit-oath-undertaking', methods=['GET', 'POST'])
@login_required
def edit_oath_undertaking():
    if request.method == 'POST':
        try:
            # Get form data
            form_data = request.form.to_dict()

            from docxtpl import DocxTemplate
            from docx import Document
            import os
            import uuid
            from datetime import datetime
            import re

            # Load template
            template_path = os.path.join('static', 'spes-forms', 'OATH-OF-UNDERTAKING.docx')
            
            # First try with docxtpl
            try:
                doc = DocxTemplate(template_path)
                
                # Prepare context data - using the exact placeholder names from your template
                context = {
                    'f_name': form_data.get('f_name', ''),
                    'm_name': form_data.get('m_name', ''),
                    'l_name': form_data.get('l_name', ''),
                    'age': form_data.get('age', ''),
                    'address': form_data.get('address', ''),
                    'day': form_data.get('day', ''),
                    'month': form_data.get('month', '')
                }

                # Render document
                doc.render(context)
                
            except Exception as e:
                # If docxtpl fails (likely due to text boxes), use manual replacement
                print(f"DocxTemplate failed: {e}. Using manual replacement...")
                
                # Load document with python-docx
                doc = Document(template_path)
                
                # Prepare replacement data
                replacements = {
                    '{{ f_name }}': form_data.get('f_name', ''),
                    '{{ m_name }}': form_data.get('m_name', ''),
                    '{{ l_name }}': form_data.get('l_name', ''),
                    '{{ age }}': form_data.get('age', ''),
                    '{{ address }}': form_data.get('address', ''),
                    '{{ day }}': form_data.get('day', ''),
                    '{{ month }}': form_data.get('month', '')
                }
                
                # Function to replace text in paragraphs
                def replace_text_in_paragraph(paragraph, replacements):
                    for placeholder, replacement in replacements.items():
                        if placeholder in paragraph.text:
                            # Get the full text
                            full_text = paragraph.text
                            # Replace the placeholder
                            new_text = full_text.replace(placeholder, replacement)
                            
                            # Clear the paragraph and add new text
                            paragraph.clear()
                            paragraph.add_run(new_text)
                
                # Function to replace text in text boxes and shapes
                def replace_text_in_shapes(doc, replacements):
                    # Handle inline shapes (text boxes)
                    for shape in doc.inline_shapes:
                        try:
                            # Access the text box content through the XML
                            if hasattr(shape, '_inline') and shape._inline.graphic:
                                textbox = shape._inline.graphic.graphicData
                                if textbox is not None:
                                    # Find text elements and replace
                                    for text_elem in textbox.iter():
                                        if hasattr(text_elem, 'text') and text_elem.text:
                                            for placeholder, replacement in replacements.items():
                                                if placeholder in text_elem.text:
                                                    text_elem.text = text_elem.text.replace(placeholder, replacement)
                        except:
                            continue
                    
                    # Handle floating shapes (another type of text box)
                    for shape in doc.part.related_parts:
                        try:
                            if hasattr(shape, 'element') and shape.element is not None:
                                for text_elem in shape.element.iter():
                                    if hasattr(text_elem, 'text') and text_elem.text:
                                        for placeholder, replacement in replacements.items():
                                            if placeholder in text_elem.text:
                                                text_elem.text = text_elem.text.replace(placeholder, replacement)
                        except:
                            continue
                
                # Replace text in all paragraphs
                for paragraph in doc.paragraphs:
                    replace_text_in_paragraph(paragraph, replacements)
                
                # Replace text in tables
                for table in doc.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            for paragraph in cell.paragraphs:
                                replace_text_in_paragraph(paragraph, replacements)
                
                # Try to replace text in text boxes and shapes
                replace_text_in_shapes(doc, replacements)
                
                # Also try to replace in headers and footers
                for section in doc.sections:
                    for paragraph in section.header.paragraphs:
                        replace_text_in_paragraph(paragraph, replacements)
                    for paragraph in section.footer.paragraphs:
                        replace_text_in_paragraph(paragraph, replacements)
            
            # Generate unique filename
            user_id = session['user_id']
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            unique_id = str(uuid.uuid4())[:8]
            filename = f'OATH-OF-UNDERTAKING_{timestamp}_{unique_id}.docx'

            # Save to user documents folder
            user_docs_path = os.path.join('static', 'user_documents', str(user_id))
            os.makedirs(user_docs_path, exist_ok=True)
            output_path = os.path.join(user_docs_path, filename)

            doc.save(output_path)

            return jsonify({
                'success': True,
                'message': 'Oath of Undertaking generated successfully',
                'filename': filename,
                'download_url': f'/download-generated-form/{user_id}/{filename}'
            })

        except Exception as e:
            return jsonify({'success': False, 'message': f'Error generating Oath of Undertaking: {str(e)}'})
    
    else:
        # GET request - return form field definitions
        return jsonify({
            'success': True,
            'form_fields': {
                'personal_info': {
                    'f_name': {'type': 'text', 'label': 'First Name', 'required': True},
                    'm_name': {'type': 'text', 'label': 'Middle Name', 'required': False},
                    'l_name': {'type': 'text', 'label': 'Last Name', 'required': True},
                    'age': {'type': 'number', 'label': 'Age', 'required': True},
                    'address': {'type': 'textarea', 'label': 'Address', 'required': True}
                },
                'date_info': {
                    'day': {'type': 'text', 'label': 'Day', 'required': True, 'placeholder': 'e.g., 15'},
                    'month': {'type': 'text', 'label': 'Month', 'required': True, 'placeholder': 'e.g., January'}
                }
            }
        })
    
# ---------------------------------------
# 📘 Admin Chatbot Management Routes
# ---------------------------------------


# Load multilingual model (supports Tagalog + English)


# ---------------------------------------
# 🧩 Admin Chatbot Management (FAQs + Unanswered)
# ---------------------------------------
# model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')

@app.route('/admin/chatbot')
def admin_chatbot():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM faqs ORDER BY id DESC")
    faqs = cursor.fetchall()

    cursor.execute("SELECT * FROM unanswered_questions ORDER BY created_at DESC")
    unanswered = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('admin/chatbot.html', faqs=faqs, unanswered=unanswered)

@app.route('/add_faq', methods=['POST'])
def add_faq():
    question = request.form['question']
    answer = request.form['answer']

    conn = get_db_connection()
    cursor = conn.cursor()

    # Insert into FAQs
    cursor.execute("INSERT INTO faqs (question, answer) VALUES (%s, %s)", (question, answer))

    # Delete from unanswered_questions if it exists
    cursor.execute("DELETE FROM unanswered_questions WHERE question=%s", (question,))

    conn.commit()
    cursor.close()
    conn.close()

    flash('FAQ added successfully!', 'success')
    return redirect(url_for('admin_chatbot'))

@app.route('/edit_faq/<int:id>', methods=['GET', 'POST'])
def edit_faq(id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    if request.method == 'POST':
        question = request.form['question']
        answer = request.form['answer']

        # Update FAQ
        cursor.execute("UPDATE faqs SET question=%s, answer=%s WHERE id=%s", (question, answer, id))

        # Delete from unanswered_questions if it exists
        cursor.execute("DELETE FROM unanswered_questions WHERE question=%s", (question,))

        conn.commit()
        cursor.close()
        conn.close()
        flash('FAQ updated successfully!', 'success')
        return redirect(url_for('admin_chatbot'))
    else:
        cursor.execute("SELECT * FROM faqs WHERE id=%s", (id,))
        faq = cursor.fetchone()
        cursor.close()
        conn.close()
        return render_template('admin/edit_faq.html', faq=faq)


@app.route('/delete_faq/<int:id>')
def delete_faq(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM faqs WHERE id=%s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash('FAQ deleted successfully!', 'danger')
    return redirect(url_for('admin_chatbot'))

@app.route('/add_from_unanswered/<int:id>')
def add_from_unanswered(id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT question FROM unanswered_questions WHERE id=%s", (id,))
    uq = cursor.fetchone()

    if uq:
        cursor.execute("INSERT INTO faqs (question, answer) VALUES (%s, %s)", (uq['question'], ''))
        cursor.execute("DELETE FROM unanswered_questions WHERE id=%s", (id,))
        conn.commit()

    cursor.close()
    conn.close()
    flash('Moved to FAQs. Please add an answer.', 'info')
    return redirect(url_for('admin_chatbot'))

@app.route('/delete_unanswered/<int:id>')
def delete_unanswered(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM unanswered_questions WHERE id=%s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash('Unanswered question deleted.', 'danger')
    return redirect(url_for('admin_chatbot'))

@app.route('/chat', methods=['POST'])
def chat():
    # 1. Rate Limiting Check to prevent abuse/attacks (DDOS, cost exploitation)
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    if not chat_burst_limiter.is_allowed(client_ip) or not chat_sustained_limiter.is_allowed(client_ip):
        return jsonify({
            'response': "You are sending questions too quickly. Please wait a moment before trying again."
        }), 429

    data = request.get_json() or {}
    user_message = data.get('message', '').strip()

    # 2. Input validation & sanitization
    if not user_message:
        return jsonify({'response': "Please type a question."})

    if len(user_message) > 250:
        return jsonify({
            'response': "Your question is too long. Please keep it under 250 characters."
        }), 400

    # Basic tag sanitization to prevent html/script injection
    user_message = user_message.replace('<', '&lt;').replace('>', '&gt;')

    bot_response = None

    # 3. Load FAQs from faqs.json
    try:
        with open('faqs.json', 'r', encoding='utf-8') as f:
            faq_data = json.load(f)
            faqs = faq_data.get('faqs', [])
            
            # Format FAQs for Gemini context
            faq_context = ""
            for faq in faqs:
                faq_context += f"Q: {faq['question']}\nA: {faq['answer']}\n\n"
    except Exception as e:
        print(f"Error loading faqs.json: {e}")
        faq_context = ""

    # 4. Use Gemini API to answer
    if GENAI_API_KEY and gemini_model and faq_context:
        try:
            system_instruction = (
                "You are the DigiSPES AI Assistant. Your goal is to help students and applicants with the SPES program. "
                "Answer the user's question in a friendly, conversational, and helpful tone (Taglish is okay if asked in tagalog, but more in ENGLISH, it is preffered.). "
                "Use ONLY the provided FAQ context below to answer. "
                "If the context doesn't contain the answer, or if the question is unrelated to SPES, politely say you don't have that specific information. "
                "Do not make up information. SUMMARIZED but concise answer always unless told to. KEEP IT SHORT!"
            )
            
            prompt = f"{system_instruction}\n\nFAQ Context:\n{faq_context}\n\nUser Question: {user_message}"

            response = gemini_model.generate_content(prompt)
            bot_response = response.text
        except Exception as e:
            print(f"Gemini API Error: {e}")
            if "429" in str(e):
                bot_response = "Sorry, I'm receiving too many questions right now. Please try again in a minute."
            else:
                bot_response = "Sorry, I'm having trouble connecting to the AI right now."

    if not bot_response:
        bot_response = "Sorry, hindi ko pa alam ang sagot niyan. Please contact the PESO office."

    return jsonify({'response': bot_response})


    
    # Add the feedback page route
@app.route('/feedback')
@login_required
def feedback():
    return render_template('new/feedback.html', username=session.get('username'), role=session.get('role'))

@app.route('/submit_feedback', methods=['POST'])
@login_required
def submit_feedback():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Please login first'})

    try:
        rating = request.form.get('rating')
        message = request.form.get('message')
        is_anonymous = bool(request.form.get('anonymous'))

        if not rating or not message:
            return jsonify({'success': False, 'message': 'Rating and message are required'})

        # Validate rating
        try:
            rating = int(rating)
            if rating < 1 or rating > 5:
                return jsonify({'success': False, 'message': 'Rating must be between 1 and 5'})
        except ValueError:
            return jsonify({'success': False, 'message': 'Invalid rating format'})

        # Get user's municipality_id
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # First check user_info table for municipality_id
        cursor.execute("""
            SELECT municipality_id 
            FROM user_info 
            WHERE user_id = %s
        """, (session['user_id'],))
        result = cursor.fetchone()

        if result and result['municipality_id']:
            municipality_id = result['municipality_id']
        else:
            # Check users table
            cursor.execute("""
                SELECT municipality_id 
                FROM users 
                WHERE id = %s
            """, (session['user_id'],))
            result = cursor.fetchone()
            municipality_id = result['municipality_id'] if result else None

        if not municipality_id:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Municipality not found. Please contact administrator.'})

        # Insert feedback
        cursor.execute("""
            INSERT INTO feedback (user_id, municipality_id, rating, message, is_anonymous)
            VALUES (%s, %s, %s, %s, %s)
        """, (session['user_id'], municipality_id, rating, message, is_anonymous))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'success': True, 'message': 'Thank you for your feedback!'})

    except Exception as e:
        print(f"Error submitting feedback: {e}")
        return jsonify({'success': False, 'message': 'Error submitting feedback. Please try again.'})

@app.route('/admin/feedback')
@login_required
def admin_feedback():
    if session.get('role') != 'municipal_peso':
        flash('Unauthorized access', 'error')
        return redirect(url_for('home'))

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get all feedback for the admin's municipality
        cursor.execute("""
            SELECT 
                f.*,
                CASE 
                    WHEN f.is_anonymous = 1 THEN 'Anonymous User'
                    ELSE u.name
                END as user_name,
                DATE_FORMAT(f.created_at, '%M %d, %Y at %h:%i %p') as formatted_date
            FROM feedback f
            LEFT JOIN users u ON f.user_id = u.id
            WHERE f.municipality_id = (
                SELECT municipality_id 
                FROM users 
                WHERE id = %s
            )
            ORDER BY f.created_at DESC
        """, (session['user_id'],))
        
        feedback_list = cursor.fetchall()
        cursor.close()
        conn.close()

        return render_template('admin/feedback.html', feedback_list=feedback_list)

    except Exception as e:
        print(f"Error getting feedback: {e}")
        flash('Error loading feedback', 'error')
        return redirect(url_for('admin_dashboard'))

@app.route('/admin/generate-final-report', methods=['POST'])
@login_required
@role_required(['municipal_peso'])
def generate_final_report():
    """Generate final report for all passed candidates"""
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Database connection failed'})
    
    try:
        cursor = conn.cursor(dictionary=True)
        municipality_filter = get_municipality_access_filter()
        
        # Fix municipality filter for our query (replace ui. with u.)
        if municipality_filter and "ui.municipality_id" in municipality_filter:
            municipality_filter = municipality_filter.replace("ui.municipality_id", "u.municipality_id")
        
        # Get all passed candidates for final report
        cursor.execute(f"""
            SELECT 
                u.id as user_id,
                u.name,
                u.email,
                ip.exam_id,
                ex.title as exam_title,
                CASE 
                    WHEN ea.total_points > 0 THEN ROUND((ea.score / ea.total_points * 100), 2)
                    ELSE 0
                END as exam_score,
                ip.interview_score,
                CASE 
                    WHEN ea.total_points > 0 AND ip.interview_score IS NOT NULL THEN 
                        ROUND(((ea.score / ea.total_points * 100) + ip.interview_score) / 2, 2)
                    WHEN ea.total_points > 0 THEN 
                        ROUND((ea.score / ea.total_points * 100), 2)
                    WHEN ip.interview_score IS NOT NULL THEN
                        ip.interview_score
                    ELSE 0
                END as total_score,
                ip.final_decision,
                m.name as municipality_name
            FROM interview_panel ip
            JOIN users u ON ip.user_id = u.id
            JOIN exams ex ON ip.exam_id = ex.id
            JOIN municipalities m ON u.municipality_id = m.id
            LEFT JOIN exam_attempts ea ON ip.user_id = ea.user_id AND ip.exam_id = ea.exam_id
            WHERE ip.final_decision = 'passed'
            {municipality_filter}
            ORDER BY total_score DESC, u.name ASC
        """)
        passed_candidates = cursor.fetchall()
        
        if not passed_candidates:
            return jsonify({'success': False, 'message': 'No passed candidates found to generate report'})
        
        # Generate ranking
        for i, candidate in enumerate(passed_candidates):
            cursor.execute("""
                INSERT INTO final_reports 
                (user_id, exam_id, interview_panel_id, final_status, exam_score, 
                 interview_score, total_score, rank_position, generated_by, report_generated_at)
                SELECT %s, %s, ip.id, 'passed', %s, %s, %s, %s, %s, NOW()
                FROM interview_panel ip
                WHERE ip.user_id = %s AND ip.exam_id = %s
                ON DUPLICATE KEY UPDATE
                final_status = VALUES(final_status),
                exam_score = VALUES(exam_score),
                interview_score = VALUES(interview_score),
                total_score = VALUES(total_score),
                rank_position = VALUES(rank_position),
                generated_by = VALUES(generated_by),
                report_generated_at = VALUES(report_generated_at)
            """, (
                candidate['user_id'], candidate['exam_id'], 
                candidate['exam_score'], candidate['interview_score'], 
                candidate['total_score'], i + 1, session['user_id'],
                candidate['user_id'], candidate['exam_id']
            ))
        
        conn.commit()
        
        return jsonify({
            'success': True, 
            'message': f'Final report generated successfully for {len(passed_candidates)} passed candidates',
            'candidates_count': len(passed_candidates)
        })
        
    except Exception as e:
        conn.rollback()
        print(f"Error generating final report: {str(e)}")
        return jsonify({'success': False, 'message': f'Error generating final report: {str(e)}'})
    finally:
        cursor.close()
        conn.close()

@app.route('/admin/reports')
@login_required
def admin_reports():
    if session.get('role') != 'municipal_peso':
        flash('Unauthorized access', 'error')
        return redirect(url_for('home'))
    
    conn = get_db_connection()
    if not conn:
        flash('Database connection failed', 'error')
        return render_template('admin/reports.html', final_reports=[], stats={}, available_years=[], selected_year=None)
    
    try:
        cursor = conn.cursor(dictionary=True)
        municipality_filter = get_municipality_access_filter()
        
        # Fix municipality filter for our query (replace ui. with u.)
        if municipality_filter and "ui.municipality_id" in municipality_filter:
            municipality_filter = municipality_filter.replace("ui.municipality_id", "u.municipality_id")

        # Get available years (filtered by municipality)
        cursor.execute(f"""
            SELECT DISTINCT YEAR(COALESCE(a.submitted_at, a.submission_date)) as year 
            FROM applicants a
            JOIN users u ON a.user_id = u.id
            WHERE (a.submitted_at IS NOT NULL OR a.submission_date IS NOT NULL)
            {municipality_filter}
            ORDER BY year DESC
        """)
        available_years = [row['year'] for row in cursor.fetchall() if row['year']]
        
        selected_year = request.args.get('year')
        if not selected_year and available_years:
            selected_year = str(available_years[0])
        
        year_filter = ""
        if selected_year:
            year_filter = f" AND (YEAR(a.submitted_at) = {selected_year} OR YEAR(a.submission_date) = {selected_year})"
        
        # Get final reports with candidate details
        cursor.execute(f"""
            SELECT 
                fr.*,
                u.name, u.email, u.id_number,
                SUBSTRING_INDEX(u.name, ' ', 1) as first_name,
                SUBSTRING_INDEX(SUBSTRING_INDEX(u.name, ' ', 2), ' ', -1) as middle_name,
                SUBSTRING_INDEX(u.name, ' ', -1) as last_name,
                m.name as municipality_name
            FROM final_reports fr
            JOIN users u ON fr.user_id = u.id
            LEFT JOIN applicants a ON u.id = a.user_id
            LEFT JOIN municipalities m ON u.municipality_id = m.id
            WHERE 1=1 {municipality_filter}
            {year_filter}
            ORDER BY fr.rank_position ASC, fr.total_score DESC
        """)
        final_reports = cursor.fetchall()
        
        # Get statistics
        cursor.execute(f"""
            SELECT 
                COUNT(*) as total_passed,
                SUM(CASE WHEN fr.sent_to_provincial = 0 THEN 1 ELSE 0 END) as pending_provincial,
                SUM(CASE WHEN fr.sent_to_provincial = 1 THEN 1 ELSE 0 END) as sent_provincial,
                SUM(CASE WHEN fr.total_score >= 90 THEN 1 ELSE 0 END) as score_90_100,
                SUM(CASE WHEN fr.total_score >= 80 AND fr.total_score < 90 THEN 1 ELSE 0 END) as score_80_89,
                SUM(CASE WHEN fr.total_score >= 70 AND fr.total_score < 80 THEN 1 ELSE 0 END) as score_70_79,
                SUM(CASE WHEN fr.total_score >= 60 AND fr.total_score < 70 THEN 1 ELSE 0 END) as score_60_69,
                SUM(CASE WHEN fr.total_score < 60 THEN 1 ELSE 0 END) as score_below_60
            FROM final_reports fr
            JOIN users u ON fr.user_id = u.id
            LEFT JOIN applicants a ON u.id = a.user_id
            WHERE fr.final_status = 'passed' {municipality_filter}
            {year_filter}
        """)
        stats = cursor.fetchone() or {}
        
        # Get available slots from latest exam
        cursor.execute("""
            SELECT available_slots 
            FROM exams 
            ORDER BY created_at DESC 
            LIMIT 1
        """)
        exam = cursor.fetchone()
        stats['total_slots'] = exam['available_slots'] if exam else 0
        
        # Get municipality distribution for chart
        cursor.execute(f"""
            SELECT m.name, COUNT(*) as count
            FROM final_reports fr
            JOIN users u ON fr.user_id = u.id
            LEFT JOIN applicants a ON u.id = a.user_id
            LEFT JOIN municipalities m ON u.municipality_id = m.id
            WHERE fr.final_status = 'passed' {municipality_filter}
            {year_filter}
            GROUP BY m.name
            ORDER BY count DESC
        """)
        municipality_data = cursor.fetchall()
        municipality_labels = [row['name'] or 'Unknown' for row in municipality_data]
        municipality_counts = [row['count'] for row in municipality_data]
        
        return render_template('admin/reports.html', 
                             final_reports=final_reports, 
                             stats=stats,
                             municipality_labels=municipality_labels,
                             municipality_data=municipality_counts,
                             username=session.get('username'), 
                             role=session.get('role'),
                             available_years=available_years,
                             selected_year=int(selected_year) if selected_year else None)
    
    except Exception as e:
        print(f"Error in admin_reports: {str(e)}")
        flash(f"Error loading reports: {str(e)}", 'error')
        return render_template('admin/reports.html', final_reports=[], stats={}, 
                             username=session.get('username'), role=session.get('role'),
                             available_years=[], selected_year=None)
    finally:
        cursor.close()
        conn.close()

@app.route('/admin/send-to-provincial', methods=['POST'])
@login_required
@role_required(['municipal_peso'])
def send_to_provincial():
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Database connection failed'})
    
    try:
        cursor = conn.cursor()
        municipality_filter = get_municipality_access_filter()
        
        # Fix municipality filter for our query (replace ui. with u.)
        if municipality_filter and "ui.municipality_id" in municipality_filter:
            municipality_filter = municipality_filter.replace("ui.municipality_id", "u.municipality_id")
            
        # Update reports
        query = f"""
            UPDATE final_reports fr
            JOIN users u ON fr.user_id = u.id
            SET fr.sent_to_provincial = 1, fr.sent_at = NOW()
            WHERE fr.final_status = 'passed' AND fr.sent_to_provincial = 0
            {municipality_filter}
        """
        
        cursor.execute(query)
        updated_count = cursor.rowcount
        conn.commit()
        
        if updated_count > 0:
            return jsonify({'success': True, 'message': f'Successfully sent {updated_count} reports to Provincial PESO'})
        else:
            return jsonify({'success': False, 'message': 'No pending reports found to send'})
            
    except Exception as e:
        conn.rollback()
        print(f"Error sending to provincial: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
    finally:
        cursor.close()
        conn.close()

# ========== NEW WORKFLOW ROUTES ==========

def unlock_spes_forms(user_id, exam_id):
    """Helper function to unlock SPES forms for requirements submission"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        # Create entry in requirements_submissions table
        cursor.execute("""
            INSERT INTO requirements_submissions (user_id, exam_id, submission_status)
            VALUES (%s, %s, 'incomplete')
            ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP
        """, (user_id, exam_id))
        conn.commit()
        return True
    except Exception as e:
        print(f"Error unlocking SPES forms: {str(e)}")
        return False
    finally:
        cursor.close()
        conn.close()

@app.route('/admin/interview-panel')
@login_required
@role_required(['municipal_peso'])
def admin_interview_panel():
    conn = get_db_connection()
    if not conn:
        return render_template('admin/interview-panel.html', 
                             requirements_for_review=[], 
                             interview_candidates=[], 
                             stats={},
                             available_years=[],
                             selected_year=None)
    
    try:
        cursor = conn.cursor(dictionary=True)
        municipality_filter = get_municipality_access_filter()
        
        # Fix municipality filter for our query (replace ui. with u.)
        if municipality_filter and "ui.municipality_id" in municipality_filter:
            municipality_filter = municipality_filter.replace("ui.municipality_id", "u.municipality_id")

        # Get available years
        cursor.execute("SELECT DISTINCT year_applied FROM applicants ORDER BY year_applied DESC")
        available_years = [row['year_applied'] for row in cursor.fetchall() if row['year_applied']]
        
        selected_year = request.args.get('year')
        if not selected_year and available_years:
            selected_year = str(available_years[0])
        
        year_filter = ""
        if selected_year:
            year_filter = f" AND a.year_applied = {selected_year}"
        
        # Get requirements submissions that need review (pending_review status)
        cursor.execute(f"""
            SELECT 
                rs.id as submission_id,
                u.id as user_id,
                u.name, u.email,
                e.exam_id,
                ex.title as exam_title,
                CASE 
                    WHEN ea.total_points > 0 THEN ROUND((ea.score / ea.total_points * 100), 2)
                    ELSE 0
                END as exam_score,
                rs.submission_status,
                rs.submitted_at,
                rs.admin_notes,
                rs.spes_form_path,
                rs.certificate_of_registration_path,
                rs.copy_of_grades_path,
                rs.barangay_certificate_path,
                rs.parent_id_path,
                rs.birth_certificate_path,
                rs.passport_photo_path,
                u.municipality_id
            FROM requirements_submissions rs
            JOIN users u ON rs.user_id = u.id
            JOIN applicants a ON u.id = a.user_id AND rs.exam_id = a.exam_id
            JOIN examinees e ON u.id = e.user_id AND rs.exam_id = e.exam_id
            JOIN exams ex ON rs.exam_id = ex.id
            JOIN exam_attempts ea ON u.id = ea.user_id AND rs.exam_id = ea.exam_id
            WHERE rs.submission_status = 'pending_review'
            AND ea.passed = 'passed'
            {municipality_filter}
            {year_filter}
            ORDER BY rs.submitted_at ASC
        """)
        requirements_for_review = cursor.fetchall()
        
        # Get candidates who have approved requirements and are ready for interview
        cursor.execute(f"""
            SELECT 
                u.id as user_id,
                u.name, u.email,
                TRIM(SUBSTRING_INDEX(u.name, ' ', 1)) as first_name,
                CASE 
                    WHEN CHAR_LENGTH(u.name) - CHAR_LENGTH(REPLACE(u.name, ' ', '')) >= 2 THEN
                        TRIM(SUBSTRING_INDEX(SUBSTRING_INDEX(u.name, ' ', -2), ' ', 1))
                    ELSE ''
                END as middle_name,
                CASE 
                    WHEN CHAR_LENGTH(u.name) - CHAR_LENGTH(REPLACE(u.name, ' ', '')) >= 1 THEN
                        TRIM(SUBSTRING_INDEX(u.name, ' ', -1))
                    ELSE ''
                END as last_name,
                e.exam_id,
                ex.title as exam_title,
                CASE 
                    WHEN ea.total_points > 0 THEN ROUND((ea.score / ea.total_points * 100), 2)
                    ELSE 0
                END as exam_score,
                rs.submission_status as requirements_status,
                rs.submitted_at as requirements_submitted_at,
                COALESCE(ip.interview_date, NULL) as interview_date,
                COALESCE(ip.interview_time, NULL) as interview_time,
                COALESCE(ip.interview_status, 'requirements_approved') as interview_status,
                ip.interview_score,
                COALESCE(ip.final_decision, 'pending') as final_decision,
                ip.interview_notes
            FROM users u
            JOIN examinees e ON u.id = e.user_id
            JOIN applicants a ON u.id = a.user_id AND e.exam_id = a.exam_id
            JOIN exams ex ON e.exam_id = ex.id
            JOIN exam_attempts ea ON u.id = ea.user_id AND e.exam_id = ea.exam_id
            JOIN requirements_submissions rs ON u.id = rs.user_id AND e.exam_id = rs.exam_id
            LEFT JOIN interview_panel ip ON u.id = ip.user_id AND e.exam_id = ip.exam_id
            WHERE rs.submission_status = 'approved'
            AND ea.passed = 'passed'
            {municipality_filter}
            {year_filter}
            ORDER BY ip.final_decision ASC, rs.submitted_at ASC
        """)
        interview_candidates = cursor.fetchall()
        
        # Get statistics
        cursor.execute(f"""
            SELECT 
                COUNT(DISTINCT CASE WHEN rs.submission_status = 'pending_review' THEN u.id END) as pending_requirements_review,
                COUNT(DISTINCT CASE WHEN rs.submission_status = 'approved' THEN u.id END) as approved_requirements,
                COUNT(DISTINCT CASE WHEN ip.interview_status IS NULL OR ip.interview_status = 'scheduled' THEN u.id END) as pending_interviews,
                COUNT(DISTINCT CASE WHEN ip.final_decision = 'passed' THEN u.id END) as passed_candidates,
                COUNT(DISTINCT CASE WHEN ip.final_decision = 'failed' THEN u.id END) as failed_candidates
            FROM users u
            JOIN examinees e ON u.id = e.user_id
            JOIN applicants a ON u.id = a.user_id AND e.exam_id = a.exam_id
            JOIN exam_attempts ea ON u.id = ea.user_id AND e.exam_id = ea.exam_id
            LEFT JOIN requirements_submissions rs ON u.id = rs.user_id AND e.exam_id = rs.exam_id
            LEFT JOIN interview_panel ip ON u.id = ip.user_id AND e.exam_id = ip.exam_id
            WHERE ea.passed = 'passed'
            {municipality_filter}
            {year_filter}
        """)
        stats = cursor.fetchone() or {}
        
        return render_template('admin/interview-panel.html', 
                             requirements_for_review=requirements_for_review,
                             interview_candidates=interview_candidates, 
                             stats=stats,
                             available_years=available_years,
                             selected_year=int(selected_year) if selected_year else None)
    
    except Exception as e:
        print(f"Error in admin_interview_panel: {str(e)}")
        return render_template('admin/interview-panel.html', 
                             requirements_for_review=[], 
                             interview_candidates=[], 
                             stats={},
                             available_years=[],
                             selected_year=None)
    finally:
        cursor.close()
        conn.close()

@app.route('/admin/schedule-interview', methods=['POST'])
@login_required
@role_required(['municipal_peso'])
def schedule_interview():
    user_id = request.form.get('user_id')
    exam_id = request.form.get('exam_id')
    interview_date = request.form.get('interview_date')
    interview_time = request.form.get('interview_time')
    notes = request.form.get('notes', '')
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Database connection failed'})
    
    try:
        cursor = conn.cursor()
        
        # Get requirements submission ID
        cursor.execute("""
            SELECT id FROM requirements_submissions 
            WHERE user_id = %s AND exam_id = %s AND submission_status = 'approved'
        """, (user_id, exam_id))
        req_submission = cursor.fetchone()
        
        if not req_submission:
            return jsonify({'success': False, 'message': 'Requirements not found or not approved'})
        
        # Insert or update interview panel entry
        cursor.execute("""
            INSERT INTO interview_panel 
            (user_id, exam_id, requirements_submission_id, interview_date, interview_time, 
             interview_status, interview_notes, interviewed_by)
            VALUES (%s, %s, %s, %s, %s, 'scheduled', %s, %s)
            ON DUPLICATE KEY UPDATE
            interview_date = VALUES(interview_date),
            interview_time = VALUES(interview_time),
            interview_notes = VALUES(interview_notes),
            interview_status = 'scheduled'
        """, (user_id, exam_id, req_submission[0], interview_date, interview_time, notes, session['user_id']))
        
        # Update applicant status
        cursor.execute("""
            UPDATE applicants SET status = 'for interview'
            WHERE user_id = %s AND exam_id = %s
        """, (user_id, exam_id))
        
        cursor.execute("""
            UPDATE examinees SET status = 'for interview'
            WHERE user_id = %s AND exam_id = %s
        """, (user_id, exam_id))
        
        # Log workflow change
        cursor.execute("""
            INSERT INTO workflow_audit (user_id, exam_id, old_status, new_status, change_reason, changed_by)
            VALUES (%s, %s, 'requirements submitted', 'for interview', 'Interview scheduled', %s)
        """, (user_id, exam_id, session['user_id']))
        
        conn.commit()
        return jsonify({'success': True, 'message': 'Interview scheduled successfully'})
        
    except Exception as e:
        conn.rollback()
        print(f"Error scheduling interview: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
    finally:
        cursor.close()
        conn.close()

@app.route('/admin/save-interview-results', methods=['POST'])
@login_required
@role_required(['municipal_peso'])
def save_interview_results():
    user_id = request.form.get('user_id')
    exam_id = request.form.get('exam_id')
    interview_score = request.form.get('interview_score')
    interview_status = request.form.get('interview_status')
    interview_notes = request.form.get('interview_notes')
    decision_reason = request.form.get('decision_reason')
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Database connection failed'})
    
    try:
        cursor = conn.cursor()
        
        # Update interview panel results
        cursor.execute("""
            UPDATE interview_panel 
            SET interview_score = %s, interview_status = %s, interview_notes = %s, 
                decision_reason = %s, interviewed_by = %s
            WHERE user_id = %s AND exam_id = %s
        """, (interview_score, interview_status, interview_notes, decision_reason, session['user_id'], user_id, exam_id))
        
        # Update status based on interview completion
        if interview_status == 'completed':
            new_status = 'final interview pending'
        elif interview_status == 'no_show':
            new_status = 'failed'
        elif interview_status == 'cancelled':
            new_status = 'for interview'  # Keep for rescheduling
        else:
            new_status = 'for interview'
            
        cursor.execute("""
            UPDATE applicants SET status = %s
            WHERE user_id = %s AND exam_id = %s
        """, (new_status, user_id, exam_id))
        
        cursor.execute("""
            UPDATE examinees SET status = %s
            WHERE user_id = %s AND exam_id = %s
        """, (new_status, user_id, exam_id))
        
        # Log workflow change
        cursor.execute("""
            INSERT INTO workflow_audit (user_id, exam_id, old_status, new_status, change_reason, changed_by)
            VALUES (%s, %s, 'for interview', %s, %s, %s)
        """, (user_id, exam_id, new_status, f'Interview {interview_status}', session['user_id']))
        
        conn.commit()
        return jsonify({'success': True, 'message': 'Interview results saved successfully'})
        
    except Exception as e:
        conn.rollback()
        print(f"Error saving interview results: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
    finally:
        cursor.close()
        conn.close()

@app.route('/admin/mark-final-decision', methods=['POST'])
@login_required
@role_required(['municipal_peso'])
def mark_final_decision():
    data = request.get_json()
    user_id = data.get('user_id')
    exam_id = data.get('exam_id')
    final_decision = data.get('final_decision')
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Database connection failed'})
    
    try:
        cursor = conn.cursor(dictionary=True)
        
        # Check if interview_panel entry exists, if not create it
        cursor.execute("SELECT id FROM interview_panel WHERE user_id = %s AND exam_id = %s", (user_id, exam_id))
        if not cursor.fetchone():
            # Get requirements submission ID
            cursor.execute("""
                SELECT id FROM requirements_submissions 
                WHERE user_id = %s AND exam_id = %s AND submission_status = 'approved'
            """, (user_id, exam_id))
            req_submission = cursor.fetchone()
            
            if req_submission:
                # Insert default interview panel entry
                cursor.execute("""
                    INSERT INTO interview_panel 
                    (user_id, exam_id, requirements_submission_id, interview_status, final_decision, interview_score)
                    VALUES (%s, %s, %s, 'completed', %s, 0)
                """, (user_id, exam_id, req_submission['id'], final_decision))

        # Update interview panel final decision
        cursor.execute("""
            UPDATE interview_panel 
            SET final_decision = %s
            WHERE user_id = %s AND exam_id = %s
        """, (final_decision, user_id, exam_id))
        
        # Update applicant and examinee status
        new_status = 'passed' if final_decision == 'passed' else 'failed'
        
        cursor.execute("""
            UPDATE applicants SET status = %s
            WHERE user_id = %s AND exam_id = %s
        """, (new_status, user_id, exam_id))
        
        cursor.execute("""
            UPDATE examinees SET status = %s
            WHERE user_id = %s AND exam_id = %s
        """, (new_status, user_id, exam_id))
        
        # If passed, create final report entry
        if final_decision == 'passed':
            # Get exam and interview scores
            cursor.execute("""
                SELECT ea.score as exam_score, ip.interview_score
                FROM exam_attempts ea
                JOIN interview_panel ip ON ea.user_id = ip.user_id AND ea.exam_id = ip.exam_id
                WHERE ea.user_id = %s AND ea.exam_id = %s
            """, (user_id, exam_id))
            scores = cursor.fetchone()
            
            if scores:
                exam_score = scores['exam_score'] or 0
                interview_score = scores['interview_score'] or 0
                total_score = (exam_score + interview_score) / 2
                
                # Calculate rank position (simple ranking by total score)
                cursor.execute("""
                    SELECT COUNT(*) + 1 as rank_position
                    FROM final_reports fr2
                    JOIN exam_attempts ea2 ON fr2.user_id = ea2.user_id AND fr2.exam_id = ea2.exam_id
                    JOIN interview_panel ip2 ON fr2.user_id = ip2.user_id AND fr2.exam_id = ip2.exam_id
                    WHERE fr2.final_status = 'passed' 
                    AND ((ea2.score + COALESCE(ip2.interview_score, 0)) / 2) > %s
                """, (total_score,))
                rank_result = cursor.fetchone()
                rank_position = rank_result['rank_position'] if rank_result else 1
                
                # Insert final report
                cursor.execute("""
                    INSERT INTO final_reports 
                    (user_id, exam_id, interview_panel_id, final_status, exam_score, 
                     interview_score, total_score, rank_position, generated_by)
                    SELECT %s, %s, ip.id, %s, %s, %s, %s, %s, %s
                    FROM interview_panel ip
                    WHERE ip.user_id = %s AND ip.exam_id = %s
                    ON DUPLICATE KEY UPDATE
                    final_status = VALUES(final_status),
                    exam_score = VALUES(exam_score),
                    interview_score = VALUES(interview_score),
                    total_score = VALUES(total_score),
                    rank_position = VALUES(rank_position)
                """, (user_id, exam_id, final_decision, exam_score, interview_score, 
                     total_score, rank_position, session['user_id'], user_id, exam_id))
        
        # Log workflow change
        cursor.execute("""
            INSERT INTO workflow_audit (user_id, exam_id, old_status, new_status, change_reason, changed_by)
            VALUES (%s, %s, 'final interview pending', %s, %s, %s)
        """, (user_id, exam_id, new_status, f'Final decision: {final_decision}', session['user_id']))
        
        conn.commit()
        return jsonify({'success': True, 'message': f'Candidate marked as {final_decision} successfully'})
        
    except Exception as e:
        conn.rollback()
        print(f"Error marking final decision: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
    finally:
        cursor.close()
        conn.close()

@app.route('/admin/approve-requirements', methods=['POST'])
@login_required
@role_required(['municipal_peso'])
def approve_requirements():
    """Approve submitted requirements and move applicant to interview stage"""
    data = request.get_json()
    submission_id = data.get('submission_id')
    admin_notes = data.get('admin_notes', '')
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Database connection failed'})
    
    try:
        cursor = conn.cursor(dictionary=True)
        
        # Get submission details and verify municipality access
        cursor.execute("""
            SELECT rs.user_id, rs.exam_id, u.municipality_id, u.name
            FROM requirements_submissions rs
            JOIN users u ON rs.user_id = u.id
            WHERE rs.id = %s AND rs.submission_status = 'pending_review'
        """, (submission_id,))
        submission = cursor.fetchone()
        
        if not submission:
            return jsonify({'success': False, 'message': 'Submission not found or already processed'})
        
        # Check municipality access
        user_municipality = get_user_municipality_id()
        if session.get('role') != 'provincial_peso' and submission['municipality_id'] != user_municipality:
            return jsonify({'success': False, 'message': 'Access denied - different municipality'})
        
        # Update requirements submission to approved
        cursor.execute("""
            UPDATE requirements_submissions 
            SET submission_status = 'approved',
                admin_notes = %s,
                reviewed_at = NOW(),
                reviewed_by = %s
            WHERE id = %s
        """, (admin_notes, session['user_id'], submission_id))
        
        # Update applicant and examinee status to 'for interview'
        cursor.execute("""
            UPDATE applicants 
            SET status = 'for interview'
            WHERE user_id = %s AND exam_id = %s
        """, (submission['user_id'], submission['exam_id']))
        
        cursor.execute("""
            UPDATE examinees 
            SET status = 'for interview'
            WHERE user_id = %s AND exam_id = %s
        """, (submission['user_id'], submission['exam_id']))
        
        # Create interview panel entry
        cursor.execute("""
            INSERT INTO interview_panel 
            (user_id, exam_id, requirements_submission_id, interview_status, interviewed_by)
            VALUES (%s, %s, %s, 'requirements_approved', %s)
            ON DUPLICATE KEY UPDATE
            interview_status = 'requirements_approved',
            interviewed_by = %s,
            updated_at = NOW()
        """, (submission['user_id'], submission['exam_id'], submission_id, session['user_id'], session['user_id']))
        
        conn.commit()
        return jsonify({
            'success': True, 
            'message': f'Requirements approved for {submission["name"]}. Candidate moved to interview stage.'
        })
        
    except Exception as e:
        conn.rollback()
        print(f"Error approving requirements: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
    finally:
        cursor.close()
        conn.close()

@app.route('/admin/reject-requirements', methods=['POST'])
@login_required
@role_required(['municipal_peso'])
def reject_requirements():
    """Reject submitted requirements and send back for revision"""
    data = request.get_json()
    submission_id = data.get('submission_id')
    admin_notes = data.get('admin_notes', '')
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Database connection failed'})
    
    try:
        cursor = conn.cursor(dictionary=True)
        
        # Get submission details and verify municipality access
        cursor.execute("""
            SELECT rs.user_id, rs.exam_id, u.municipality_id, u.name
            FROM requirements_submissions rs
            JOIN users u ON rs.user_id = u.id
            WHERE rs.id = %s AND rs.submission_status = 'pending_review'
        """, (submission_id,))
        submission = cursor.fetchone()
        
        if not submission:
            return jsonify({'success': False, 'message': 'Submission not found or already processed'})
        
        # Check municipality access
        user_municipality = get_user_municipality_id()
        if session.get('role') != 'provincial_peso' and submission['municipality_id'] != user_municipality:
            return jsonify({'success': False, 'message': 'Access denied - different municipality'})
        
        # Update requirements submission to rejected
        cursor.execute("""
            UPDATE requirements_submissions 
            SET submission_status = 'rejected',
                admin_notes = %s,
                reviewed_at = NOW(),
                reviewed_by = %s
            WHERE id = %s
        """, (admin_notes, session['user_id'], submission_id))
        
        # Keep applicant and examinee status as 'pending for requirements' for resubmission
        cursor.execute("""
            UPDATE applicants 
            SET status = 'pending for requirements'
            WHERE user_id = %s AND exam_id = %s
        """, (submission['user_id'], submission['exam_id']))
        
        cursor.execute("""
            UPDATE examinees 
            SET status = 'pending for requirements'
            WHERE user_id = %s AND exam_id = %s
        """, (submission['user_id'], submission['exam_id']))
        
        conn.commit()
        return jsonify({
            'success': True, 
            'message': f'Requirements rejected for {submission["name"]}. Candidate can resubmit requirements.'
        })
        
    except Exception as e:
        conn.rollback()
        print(f"Error rejecting requirements: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
    finally:
        cursor.close()
        conn.close()

@app.route('/admin/get-requirements/<int:user_id>')
@login_required
@role_required(['municipal_peso'])
def get_requirements(user_id):
    """Get requirements details for a specific user"""
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Database connection failed'})
    
    try:
        cursor = conn.cursor(dictionary=True)
        
        # Get user's requirements submission details
        cursor.execute("""
            SELECT 
                rs.id as submission_id,
                u.name, u.email, u.municipality_id,
                rs.submission_status,
                rs.submitted_at,
                rs.admin_notes,
                rs.spes_form_path,
                rs.certificate_of_registration_path,
                rs.copy_of_grades_path,
                rs.barangay_certificate_path,
                rs.parent_id_path,
                rs.birth_certificate_path,
                rs.passport_photo_path,
                e.exam_id,
                ex.title as exam_title,
                ea.score as exam_score
            FROM users u
            JOIN requirements_submissions rs ON u.id = rs.user_id
            JOIN examinees e ON u.id = e.user_id AND rs.exam_id = e.exam_id
            JOIN exams ex ON rs.exam_id = ex.id
            JOIN exam_attempts ea ON u.id = ea.user_id AND rs.exam_id = ea.exam_id
            WHERE u.id = %s
            ORDER BY rs.submitted_at DESC
            LIMIT 1
        """, (user_id,))
        requirements = cursor.fetchone()
        
        if not requirements:
            return jsonify({'success': False, 'message': 'Requirements not found'})
        
        # Check municipality access
        user_municipality = get_user_municipality_id()
        if session.get('role') != 'provincial_peso' and requirements['municipality_id'] != user_municipality:
            return jsonify({'success': False, 'message': 'Access denied - different municipality'})
        
        # Generate HTML content for the modal
        html_content = f"""
        <div class="container-fluid">
            <div class="row mb-3">
                <div class="col-md-6">
                    <h6><strong>Applicant:</strong> {requirements['name']}</h6>
                    <p><strong>Email:</strong> {requirements['email']}</p>
                    <p><strong>Exam:</strong> {requirements['exam_title']}</p>
                    <p><strong>Exam Score:</strong> {requirements['exam_score']}</p>
                </div>
                <div class="col-md-6">
                    <p><strong>Status:</strong> <span class="badge bg-{'success' if requirements['submission_status'] == 'approved' else 'warning' if requirements['submission_status'] == 'pending_review' else 'danger'}">{requirements['submission_status'].replace('_', ' ').title()}</span></p>
                    <p><strong>Submitted:</strong> {requirements['submitted_at'].strftime('%B %d, %Y at %I:%M %p') if requirements['submitted_at'] else 'Not submitted'}</p>
                    {f'<p><strong>Admin Notes:</strong> {requirements["admin_notes"]}</p>' if requirements['admin_notes'] else ''}
                </div>
            </div>
            
            <h6><strong>Submitted Documents:</strong></h6>
            <div class="row">
                <div class="col-md-6">
                    <ul class="list-group list-group-flush">
        """
        
        # Add document links
        documents = [
            ('SPES Form', requirements['spes_form_path']),
            ('Certificate of Registration', requirements['certificate_of_registration_path']),
            ('Copy of Grades', requirements['copy_of_grades_path']),
            ('Barangay Certificate', requirements['barangay_certificate_path'])
        ]
        
        for doc_name, doc_path in documents:
            if doc_path:
                html_content += f"""
                        <li class="list-group-item d-flex justify-content-between align-items-center">
                            {doc_name}
                            <a href="/download-uploaded-document/{user_id}/{doc_name.lower().replace(' ', '_')}" 
                               class="btn btn-sm btn-outline-primary" target="_blank">
                                <i class="bi bi-download"></i> Download
                            </a>
                        </li>
                """
            else:
                html_content += f"""
                        <li class="list-group-item d-flex justify-content-between align-items-center">
                            {doc_name}
                            <span class="text-muted">Not submitted</span>
                        </li>
                """
        
        html_content += """
                    </ul>
                </div>
                <div class="col-md-6">
                    <ul class="list-group list-group-flush">
        """
        
        # Add remaining documents
        documents2 = [
            ('Parent ID', requirements['parent_id_path']),
            ('Birth Certificate', requirements['birth_certificate_path']),
            ('Passport Photo', requirements['passport_photo_path'])
        ]
        
        for doc_name, doc_path in documents2:
            if doc_path:
                html_content += f"""
                        <li class="list-group-item d-flex justify-content-between align-items-center">
                            {doc_name}
                            <a href="/download-uploaded-document/{user_id}/{doc_name.lower().replace(' ', '_')}" 
                               class="btn btn-sm btn-outline-primary" target="_blank">
                                <i class="bi bi-download"></i> Download
                            </a>
                        </li>
                """
            else:
                html_content += f"""
                        <li class="list-group-item d-flex justify-content-between align-items-center">
                            {doc_name}
                            <span class="text-muted">Not submitted</span>
                        </li>
                """
        
        html_content += """
                    </ul>
                </div>
            </div>
        </div>
        """
        
        return jsonify({'success': True, 'html': html_content})
        
    except Exception as e:
        print(f"Error getting requirements: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
    finally:
        cursor.close()
        conn.close()

# Route for submit requirements (to be called after exam passing)
@app.route('/submit-requirements', methods=['POST'])
@login_required
def submit_requirements():
    # This route handles the SPES forms submission after exam passing
    # Implementation would handle file uploads and update requirements_submissions table
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Database connection failed'})
    
    try:
        cursor = conn.cursor()
        
        # Update requirements submission status
        cursor.execute("""
            UPDATE requirements_submissions 
            SET submission_status = 'pending_review', submitted_at = NOW()
            WHERE user_id = %s
        """, (session['user_id'],))
        
        # Update applicant status
        cursor.execute("""
            UPDATE applicants SET status = 'requirements submitted'
            WHERE user_id = %s 
            ORDER BY submission_date DESC 
            LIMIT 1
        """, (session['user_id'],))
        
        cursor.execute("""
            UPDATE examinees SET status = 'requirements submitted'
            WHERE user_id = %s
        """, (session['user_id'],))
        
        # Log workflow change
        cursor.execute("""
            INSERT INTO workflow_audit (user_id, old_status, new_status, change_reason, changed_by)
            VALUES (%s, 'pending for requirements', 'requirements submitted', 'Requirements submitted', %s)
        """, (session['user_id'], session['user_id']))
        
        conn.commit()
        return jsonify({'success': True, 'message': 'Requirements submitted successfully'})
        
    except Exception as e:
        conn.rollback()
        print(f"Error submitting requirements: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
    finally:
        cursor.close()
        conn.close()
        
# Start background exam timer thread
try:
    threading.Thread(target=exam_auto_trigger, daemon=True).start()
except Exception as e:
    print(f'Notice: Failed to start exam auto trigger thread: {e}')

if __name__ == '__main__':
    test_db_connection()  # Test database connection on startup
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)