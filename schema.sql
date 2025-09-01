-- Create users table
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('admin', 'new', 'old') NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create applicants table
CREATE TABLE IF NOT EXISTS applicants (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    status ENUM('pending', 'approved', 'rejected', 'for exam', 'for interview') NOT NULL DEFAULT 'pending',
    submission_date DATE,
    exam_id INT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE SET NULL
);

-- Create user_info table
CREATE TABLE IF NOT EXISTS user_info (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    first_name VARCHAR(255),
    middle_name VARCHAR(255),
    last_name VARCHAR(255),
    address TEXT,
    contact_number VARCHAR(20),
    birth_date DATE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Create user_resources table
CREATE TABLE IF NOT EXISTS user_resources (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    cor_file LONGBLOB,
    cor_filename VARCHAR(255),
    cog_file LONGBLOB,
    cog_filename VARCHAR(255),
    gwa_decimal DECIMAL(3,2),
    gwa_percent INT,
    upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Create exams table
CREATE TABLE IF NOT EXISTS exams (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    time_limit INT,
    passing_score INT DEFAULT 70,
    available_slots INT DEFAULT 200,
    created_by INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    start_date DATETIME,
    end_date DATETIME,
    status ENUM('ongoing', 'closed', 'ended') DEFAULT 'closed',
    permanent_close BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (created_by) REFERENCES users(id)
);

-- Create exam_pages table
CREATE TABLE IF NOT EXISTS exam_pages (
    id INT AUTO_INCREMENT PRIMARY KEY,
    exam_id INT NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    time_limit INT,
    order_number INT NOT NULL,
    FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE
);

-- Create exam_questions table
CREATE TABLE IF NOT EXISTS exam_questions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    page_id INT NOT NULL,
    question_text TEXT NOT NULL,
    question_type ENUM('multiple_choice', 'identification') NOT NULL,
    points INT NOT NULL DEFAULT 1,
    correct_answer TEXT,
    is_case_sensitive BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (page_id) REFERENCES exam_pages(id) ON DELETE CASCADE
);

-- Create question_options table
CREATE TABLE IF NOT EXISTS question_options (
    id INT AUTO_INCREMENT PRIMARY KEY,
    question_id INT NOT NULL,
    option_text TEXT NOT NULL,
    is_correct BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (question_id) REFERENCES exam_questions(id) ON DELETE CASCADE
);

-- Create exam_assignments table
CREATE TABLE IF NOT EXISTS exam_assignments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    exam_id INT NOT NULL,
    status ENUM('assigned', 'in_progress', 'completed') NOT NULL DEFAULT 'assigned',
    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP NULL,
    completed_at TIMESTAMP NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE
);

-- Create exam_attempts table
CREATE TABLE IF NOT EXISTS exam_attempts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    exam_id INT NOT NULL,
    score DECIMAL(5,2) NOT NULL,
    total_points INT NOT NULL,
    passed BOOLEAN NOT NULL,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE
);

-- Create notifications table
CREATE TABLE IF NOT EXISTS notifications (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    type ENUM('general', 'application_approved', 'exam_link', 'exam_result') NOT NULL DEFAULT 'general',
    action_url VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Create announcements table
CREATE TABLE IF NOT EXISTS announcements (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    target_audience ENUM('all', 'new', 'old') NOT NULL,
    announcement_type ENUM('main', 'general') NOT NULL DEFAULT 'general',
    announcement_date DATE NOT NULL,
    posted_by INT,
    FOREIGN KEY (posted_by) REFERENCES users(id) ON DELETE SET NULL
);

-- Create examinees table
CREATE TABLE IF NOT EXISTS examinees (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    status ENUM('for exam', 'pending', 'for interview', 'failed') NOT NULL DEFAULT 'for exam',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
); 