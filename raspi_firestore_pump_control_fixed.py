"""
Raspberry Pi Firestore Pump Control

This script controls water and spray pumps from Firestore commands and
also supports the legacy control/settings document.
"""

import os
import time
from datetime import datetime

import board
import adafruit_dht
import RPi.GPIO as GPIO
from RPLCD.i2c import CharLCD
import serial

import firebase_admin
from firebase_admin import credentials, firestore


# =========================================================
# FIREBASE
# =========================================================

SERVICE_ACCOUNT_FILE = "firebase-service-account.json"

if not os.path.exists(SERVICE_ACCOUNT_FILE):
    raise FileNotFoundError(f"Firebase service account file not found: {SERVICE_ACCOUNT_FILE}")

cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
firebase_admin.initialize_app(cred)

db = firestore.client()


# =========================================================
# GPIO SETUP
# =========================================================

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# RELAY
RELAY = 17

# SOIL SENSOR
SOIL_SENSOR = 27

# RS485
RS485_EN = 18

# BTS7960
RPWM_1 = 23
LPWM_1 = 24
RPWM_2 = 25
LPWM_2 = 12

GPIO.setup(RELAY, GPIO.OUT)
GPIO.setup(SOIL_SENSOR, GPIO.IN)
GPIO.setup(RS485_EN, GPIO.OUT)
GPIO.setup(RPWM_1, GPIO.OUT)
GPIO.setup(LPWM_1, GPIO.OUT)
GPIO.setup(RPWM_2, GPIO.OUT)
GPIO.setup(LPWM_2, GPIO.OUT)

# RELAY OFF
GPIO.output(RELAY, GPIO.HIGH)

# RS485 RECEIVE MODE
GPIO.output(RS485_EN, 0)


# =========================================================
# PWM
# =========================================================

pwm_r1 = GPIO.PWM(RPWM_1, 1000)
pwm_l1 = GPIO.PWM(LPWM_1, 1000)
pwm_r2 = GPIO.PWM(RPWM_2, 1000)
pwm_l2 = GPIO.PWM(LPWM_2, 1000)

pwm_r1.start(0)
pwm_l1.start(0)
pwm_r2.start(0)
pwm_l2.start(0)


# =========================================================
# SENSORS
# =========================================================

dht = adafruit_dht.DHT22(board.D4)

lcd = CharLCD('PCF8574', 0x27)


# =========================================================
# SERIAL NPK
# =========================================================

ser = serial.Serial(port='/dev/serial0', baudrate=9600, timeout=1)


# =========================================================
# GLOBAL VARIABLES
# =========================================================

MODE = "manual"
SCHEDULE_TIMES = ["06:00", "18:00"]
last_run = ""
lcd_busy = False

USER_ID = os.environ.get('HITECH_USER_ID') or input('Enter Firestore user ID: ').strip()
if not USER_ID:
    raise SystemExit('user ID is required')

CHECK_INTERVAL_SECONDS = 2


# =========================================================
# LCD FUNCTION
# =========================================================

def lcd_message(line1="", line2=""):
    global lcd_busy
    lcd_busy = True
    lcd.clear()
    lcd.write_string(str(line1)[:16])
    lcd.cursor_pos = (1, 0)
    lcd.write_string(str(line2)[:16])


# =========================================================
# SOIL SENSOR
# =========================================================

def read_soil():
    return "DRY" if GPIO.input(SOIL_SENSOR) == 1 else "WET"


# =========================================================
# DHT22
# =========================================================

def read_dht():
    try:
        humidity = dht.humidity
        temperature = dht.temperature
        return (humidity or 0), (temperature or 0)
    except Exception as e:
        print("DHT ERROR:", e)
        return 0, 0


# =========================================================
# MOTOR CONTROL
# =========================================================

def rail_forward(speed=70):
    pwm_r1.ChangeDutyCycle(speed)
    pwm_l1.ChangeDutyCycle(0)
    pwm_r2.ChangeDutyCycle(speed)
    pwm_l2.ChangeDutyCycle(0)


def rail_backward(speed=70):
    pwm_r1.ChangeDutyCycle(0)
    pwm_l1.ChangeDutyCycle(speed)
    pwm_r2.ChangeDutyCycle(0)
    pwm_l2.ChangeDutyCycle(speed)


def rail_stop():
    pwm_r1.ChangeDutyCycle(0)
    pwm_l1.ChangeDutyCycle(0)
    pwm_r2.ChangeDutyCycle(0)
    pwm_l2.ChangeDutyCycle(0)


# =========================================================
# NPK SENSOR
# =========================================================

def read_npk():
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        GPIO.output(RS485_EN, 1)
        time.sleep(0.1)

        request = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x03, 0x05, 0xCB])
        ser.write(request)
        ser.flush()
        time.sleep(0.1)
        GPIO.output(RS485_EN, 0)
        time.sleep(0.3)

        data = ser.read(11)
        print("RAW:", data.hex())

        if len(data) >= 9:
            n = (data[3] << 8) | data[4]
            p = (data[5] << 8) | data[6]
            k = (data[7] << 8) | data[8]
            return n, p, k
    except Exception as e:
        print("NPK ERROR:", e)

    return 0, 0, 0


# =========================================================
# WATERING / SPRAYING
# =========================================================

def watering_cycle(trigger_type="MANUAL"):
    global lcd_busy
    print("\n====================")
    print("WATERING START")
    print("====================")

    lcd_message(trigger_type, "WATERING ON")
    GPIO.output(RELAY, GPIO.LOW)
    print("PUMP ON")

    lcd_message(trigger_type, "RAIL FORWARD")
    rail_forward(70)
    time.sleep(6)
    rail_stop()
    time.sleep(1)

    lcd_message(trigger_type, "RAIL BACKWARD")
    rail_backward(70)
    time.sleep(6)
    rail_stop()

    GPIO.output(RELAY, GPIO.HIGH)
    print("PUMP OFF")

    lcd_message("WATER DONE", "COMPLETE")
    time.sleep(3)
    lcd_busy = False


# =========================================================
# FIRESTORE HELPERS
# =========================================================

def update_pump_status(pump_name, status):
    try:
        status_ref = db.collection('users').document(USER_ID).collection('pump_status').document(pump_name)
        status_ref.set({
            'pump': pump_name,
            'status': status,
            'updated_at': datetime.utcnow().isoformat() + 'Z',
            'updated_by': os.uname().nodename if hasattr(os, 'uname') else 'raspi',
        }, merge=True)
        print(f"✓ Updated pump_status/{pump_name} = {status}")
    except Exception as e:
        print("PUMP STATUS ERROR:", e)


def get_control_settings():
    try:
        doc = db.collection('control').document('settings').get()
        return doc.to_dict() if doc.exists else {}
    except Exception as e:
        print("FIRESTORE CONTROL ERROR:", e)
        return {}


def get_pending_commands():
    try:
        commands_ref = db.collection('users').document(USER_ID).collection('commands')
        return commands_ref.where('executed', '==', False).get()
    except Exception as e:
        print("FIRESTORE COMMANDS ERROR:", e)
        return []


def mark_command_executed(command_doc):
    try:
        command_doc.reference.update({
            'executed': True,
            'executed_timestamp': datetime.utcnow().isoformat() + 'Z',
            'executed_by': os.uname().nodename if hasattr(os, 'uname') else 'raspi',
        })
    except Exception as e:
        print("COMMAND UPDATE ERROR:", e)


# =========================================================
# MANUAL CONTROL
# =========================================================

def check_manual():
    global lcd_busy
    settings = get_control_settings()

    try:
        if settings.get('water_spray'):
            watering_cycle('MANUAL')
            db.collection('control').document('settings').update({'water_spray': False})

        if settings.get('pump'):
            lcd_message('MANUAL', 'PUMP ON')
            GPIO.output(RELAY, GPIO.LOW)
            time.sleep(5)
            GPIO.output(RELAY, GPIO.HIGH)
            lcd_message('PUMP OFF', 'COMPLETE')
            time.sleep(3)
            lcd_busy = False
            db.collection('control').document('settings').update({'pump': False})

        commands = get_pending_commands()
        for cmd_doc in commands:
            cmd = cmd_doc.to_dict() or {}
            cmd_type = cmd.get('command_type')
            duration = int(cmd.get('duration', 5) or 5)

            print(f"COMMAND: {cmd_type} duration={duration}")
            if cmd_type == 'water':
                update_pump_status('water_pump', 'active')
                watering_cycle('COMMAND')
                update_pump_status('water_pump', 'inactive')
            elif cmd_type == 'spray':
                lcd_message('COMMAND', 'SPRAY ON')
                update_pump_status('spray_pump', 'active')
                GPIO.output(RELAY, GPIO.LOW)
                time.sleep(duration)
                GPIO.output(RELAY, GPIO.HIGH)
                update_pump_status('spray_pump', 'inactive')
                lcd_message('SPRAY OFF', 'COMPLETE')
                time.sleep(2)
                lcd_busy = False
            else:
                print(f"UNKNOWN COMMAND: {cmd_type}")

            mark_command_executed(cmd_doc)
    except Exception as e:
        print("MANUAL ERROR:", e)


# =========================================================
# AUTOMATIC SCHEDULE
# =========================================================

def check_schedule():
    global last_run
    now = datetime.now().strftime("%H:%M")
    if now in SCHEDULE_TIMES and last_run != now:
        print("AUTO WATERING")
        watering_cycle('AUTO')
        last_run = now


# =========================================================
# START LCD
# =========================================================

lcd_message('SMART FARM', 'STARTING...')
time.sleep(3)
lcd_busy = False


# =========================================================
# MAIN LOOP
# =========================================================

while True:
    humidity, temperature = read_dht()
    soil = read_soil()
    n, p, k = read_npk()

    settings = get_control_settings()
    MODE = settings.get('mode', 'manual')
    SCHEDULE_TIMES = settings.get('schedule_times', SCHEDULE_TIMES)

    if MODE == 'manual':
        check_manual()
    elif MODE == 'automatic':
        check_schedule()

    if not lcd_busy:
        lcd.clear()
        lcd.write_string(f"{MODE[:6]} {soil}")
        lcd.cursor_pos = (1, 0)
        lcd.write_string(f"T:{temperature:.1f}C")
        time.sleep(3)

    if not lcd_busy:
        lcd.clear()
        lcd.write_string(f"H:{humidity:.0f}%")
        lcd.cursor_pos = (1, 0)
        lcd.write_string(f"N:{n}")
        time.sleep(3)

    if not lcd_busy:
        lcd.clear()
        lcd.write_string(f"P:{p}")
        lcd.cursor_pos = (1, 0)
        lcd.write_string(f"K:{k}")
        time.sleep(3)

    print("========================")
    print("MODE:", MODE)
    print("SOIL:", soil)
    print("TEMP:", temperature)
    print("HUM:", humidity)
    print("NPK:", n, p, k)
    print("========================")
    time.sleep(CHECK_INTERVAL_SECONDS)
