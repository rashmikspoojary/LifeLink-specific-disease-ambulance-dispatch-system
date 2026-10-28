-- LifeLink driver dashboard sample data
-- Safe demo inserts for MySQL

-- Sample user
INSERT INTO users (name, email, password, role, phone, location_text, medical_info, latitude, longitude)
SELECT
    'Sample Patient',
    'patient@lifelink.com',
    'patient123',
    'user',
    '9876543210',
    'Alvas Campus Main Gate',
    'Asthma history',
    13.0215300,
    74.9686600
WHERE NOT EXISTS (
    SELECT 1 FROM users WHERE email = 'patient@lifelink.com'
);

-- Sample driver
INSERT INTO drivers (name, email, password, phone, license_no, status, current_latitude, current_longitude, current_location_text, tracking_token)
SELECT
    'Ram Driver',
    'driver1@lifelink.com',
    'driver123',
    '7204212318',
    'KA05-2023-0098765',
    'available',
    13.0202000,
    74.9668000,
    'LifeLink Driver Stand',
    'sample-driver-token-001'
WHERE NOT EXISTS (
    SELECT 1 FROM drivers WHERE email = 'driver1@lifelink.com'
);

-- Sample ambulance
INSERT INTO ambulances (vehicle_number, type, status, current_latitude, current_longitude, current_location_text)
SELECT
    'KA19M1234',
    'ICU',
    'available',
    13.0202000,
    74.9668000,
    'LifeLink Driver Stand'
WHERE NOT EXISTS (
    SELECT 1 FROM ambulances WHERE vehicle_number = 'KA19M1234'
);

-- Sample booking linked to the sample user/driver/ambulance
SET @sample_user_id = (SELECT id FROM users WHERE email = 'patient@lifelink.com' LIMIT 1);
SET @sample_driver_id = (SELECT id FROM drivers WHERE email = 'driver1@lifelink.com' LIMIT 1);
SET @sample_ambulance_id = (SELECT id FROM ambulances WHERE vehicle_number = 'KA19M1234' LIMIT 1);

INSERT INTO bookings (
    user_id,
    ambulance_id,
    driver_id,
    status,
    created_at,
    pickup_location,
    pickup_latitude,
    pickup_longitude,
    disease_type,
    emergency_level,
    medical_notes,
    estimated_arrival_minutes
)
SELECT
    @sample_user_id,
    @sample_ambulance_id,
    @sample_driver_id,
    'pending',
    NOW(),
    'Alvas Campus Main Gate',
    13.0215300,
    74.9686600,
    'Asthma Attack',
    'high',
    'Patient has breathing difficulty. Oxygen support may be required.',
    8
WHERE @sample_user_id IS NOT NULL
  AND @sample_driver_id IS NOT NULL
  AND @sample_ambulance_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM bookings WHERE user_id = @sample_user_id AND driver_id = @sample_driver_id AND status IN ('pending', 'accepted', 'on-the-way', 'reached')
  );

-- Sample notifications
INSERT INTO notifications (user_id, driver_id, message, role, status, is_read, created_at)
SELECT @sample_user_id, NULL, 'Booking created successfully. Driver has been assigned and is waiting to accept.', 'user', 'unread', FALSE, NOW()
WHERE @sample_user_id IS NOT NULL;

INSERT INTO notifications (user_id, driver_id, message, role, status, is_read, created_at)
SELECT NULL, @sample_driver_id, 'You have a new assigned booking. Please login to driver dashboard and accept it.', 'driver', 'unread', FALSE, NOW()
WHERE @sample_driver_id IS NOT NULL;

INSERT INTO notifications (user_id, driver_id, message, role, status, is_read, created_at)
VALUES (NULL, NULL, 'Sample booking created for driver dashboard demo.', 'admin', 'unread', FALSE, NOW());

-- Sample login details:
-- Patient email: patient@lifelink.com
-- Patient password: patient123
-- Driver email: driver1@lifelink.com
-- Driver password: driver123
