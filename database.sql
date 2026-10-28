-- =========================================
-- LIFELINK DATABASE
-- =========================================

CREATE DATABASE IF NOT EXISTS lifelink;
USE lifelink;

-- =========================================
-- USERS TABLE
-- =========================================
CREATE TABLE  users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL,
    role ENUM('admin','user','driver') DEFAULT 'user'
);

INSERT INTO users (name, email, password, role) VALUES
('Admin', 'admin@lifelink.com', 'admin', 'admin');
INSERT INTO users (name, email, password, role) VALUES
('User', 'user@lifelink.com', 'user', 'user');

-- =========================================
-- DRIVERS TABLE
-- =========================================
CREATE TABLE IF NOT EXISTS drivers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100),
    phone VARCHAR(20),
    licence_no VARCHAR(50),
    ambulance_id INT NULL,
    status ENUM('available','busy') DEFAULT 'available'
);
-- =========================================
-- AMBULANCES TABLE
-- =========================================
CREATE TABLE ambulances (
    id INT AUTO_INCREMENT PRIMARY KEY,
    vehicle_number VARCHAR(50),
    type VARCHAR(50),
    hospital_id INT NULL,
    status ENUM('available','assigned','on-duty') DEFAULT 'available'
);

CREATE TABLE hospitals (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    location_text VARCHAR(255),
    latitude DECIMAL(10,7),
    longitude DECIMAL(10,7),
    emergency_contact VARCHAR(20),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- =========================================
-- BOOKINGS TABLE
-- =========================================
CREATE TABLE bookings (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT,
    hospital_id INT,
    ambulance_id INT,
    status ENUM('requested','accepted','on-the-way','completed') DEFAULT 'requested',
    created_at DATETIME,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (ambulance_id) REFERENCES ambulances(id) ON DELETE SET NULL
);

-- =========================================
-- PAYMENTS TABLE
-- =========================================
CREATE TABLE payments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT,
    amount DECIMAL(10,2),
    status ENUM('SUCCESS','FAILED') DEFAULT 'SUCCESS',
    created_at DATETIME,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- =========================================
-- FEEDBACK TABLE
-- =========================================
CREATE TABLE feedback (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT,
    rating INT CHECK (rating BETWEEN 1 AND 5),
    message TEXT,
    created_at DATETIME,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- =========================================
-- Workspace of drivers
-- =========================================
USE lifelink;
ALTER TABLE drivers
DROP COLUMN licence_no;
DESCRIBE drivers;
-- =========================================
-- Workspaces of ambulances
-- =========================================
USE lifelink;
DESCRIBE ambulances;
SHOW COLUMNS FROM ambulances;
-- =========================================
-- Workspaces of BOOKINGS
-- =========================================
USE lifelink;
SHOW TABLES;

SELECT id, name, email, role FROM users;

USE lifelink;
SELECT * FROM bookings;

USE lifelink;
SELECT id, name FROM users;

SELECT id, vehicle_number FROM ambulances;

INSERT INTO bookings (user_id, ambulance_id, status, created_at)
VALUES (2, 2, 'requested', NOW());
-- =========================================
-- Workspaces of payments
-- =========================================
USE lifelink;
SELECT * FROM payments;

INSERT INTO payments (user_id, amount, status, created_at)
VALUES (2, 750, 'SUCCESS', NOW());

USE lifelink;
DESCRIBE payments;
-- =========================================
-- Workspaces of Feedback
-- =========================================
USE lifelink;

CREATE TABLE notifications (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT,
    message TEXT,
    is_read BOOLEAN DEFAULT FALSE,
    created_at DATETIME,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
use lifelink;
SELECT * FROM notifications;
INSERT INTO notifications (user_id, message, created_at)
VALUES (2, 'Test notification from admin', NOW());
