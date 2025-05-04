from flask import Flask, render_template
app = Flask(__name__)

@app.route('/') 
def home():
    return render_template('home.html')

@app.route('/new-applicants/dashboard')
def new_applicants_dashboard():
    return render_template('new/dashboard.html')

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

# admin applicantions.html
@app.route('/admin/applications')
def admin_applications():
    return render_template('admin/applications.html')

if __name__ == '__main__':
    app.run(debug=True)