"""Your servo/motor controller adapted to the Django Firebase paths.
Run this file alone on the Pi; do not run the separate relay listener alongside it.
Manual water runs to target moisture; spraying completes four endpoint passes.
The toggle-button pump must be physically OFF before starting this program.
"""
from datetime import timezone
import uuid
from pathlib import Path
import os
import time
import threading
from datetime import datetime
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
from firebase_admin import db as realtime_db
from daily_watering import DailyWatering, WeatherRain
SERVICE_ACCOUNT_FILE = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', str(Path(__file__).with_name('firebase-service-account.json')))
if not os.path.exists(SERVICE_ACCOUNT_FILE):
    SERVICE_ACCOUNT_FILE = str(Path(__file__).with_name('serviceAccountKey.json'))
USER_ID = os.environ.get('FIREBASE_USER_ID', '1')
DATABASE_URL = 'https://egggg-e3540-default-rtdb.asia-southeast1.firebasedatabase.app/'
SERVO_PIN = 13
IR_SENSOR = 22
RS485_EN = 18
M1_RPWM = 23
M1_LPWM = 24
M2_RPWM = 25
M2_LPWM = 12
PUMP_SWITCH_TYPE = 'toggle_button'
SERVO_REST_ANGLE = 20
SERVO_PRESS_ANGLE = 70
SERVO_PRESS_TIME = 0.4
PUMP_ON_ANGLE = 90
PUMP_OFF_ANGLE = 20
MOTOR_SPEED = 70
REVERSE_PAUSE = 0.35
WHITE_CLEAR_DELAY = 0.45
WHITE_IS_LOW = True
TARGET_SOIL_MOISTURE = 60.0
CHEMICAL_REQUIRED_PASSES = 4
daily_watering = None
FIRESTORE_LOG_INTERVAL = 300

def setup_hardware():
    global BASE_PATH, CharLCD, GPIO, adafruit_dht, board, control_ref, cred, dht, firestore_db, lcd, manual_motor_ref, manual_pump_ref, motor1_l, motor1_r, motor2_l, motor2_r, operation_ref, sensor_ref, ser, serial, servo, settings_ref, status_ref
    import board
    import adafruit_dht
    import RPi.GPIO as GPIO
    from RPLCD.i2c import CharLCD
    import serial
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f'Missing {SERVICE_ACCOUNT_FILE}')
    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred, {'databaseURL': DATABASE_URL, 'httpTimeout': 10})
    firestore_db = firestore.client()
    BASE_PATH = f'users/{USER_ID}'
    control_ref = realtime_db.reference(f'{BASE_PATH}/control')
    operation_ref = realtime_db.reference(f'{BASE_PATH}/control/operation')
    manual_pump_ref = realtime_db.reference(f'{BASE_PATH}/control/manual/pump')
    manual_motor_ref = realtime_db.reference(f'{BASE_PATH}/control/manual/motor')
    status_ref = realtime_db.reference(f'{BASE_PATH}/status')
    sensor_ref = realtime_db.reference(f'{BASE_PATH}/sensors')
    settings_ref = realtime_db.reference(f'{BASE_PATH}/settings')
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(SERVO_PIN, GPIO.OUT)
    GPIO.setup(IR_SENSOR, GPIO.IN)
    GPIO.setup(RS485_EN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(M1_RPWM, GPIO.OUT)
    GPIO.setup(M1_LPWM, GPIO.OUT)
    GPIO.setup(M2_RPWM, GPIO.OUT)
    GPIO.setup(M2_LPWM, GPIO.OUT)
    servo = GPIO.PWM(SERVO_PIN, 50)
    servo.start(0)
    motor1_r = GPIO.PWM(M1_RPWM, 1000)
    motor1_l = GPIO.PWM(M1_LPWM, 1000)
    motor2_r = GPIO.PWM(M2_RPWM, 1000)
    motor2_l = GPIO.PWM(M2_LPWM, 1000)
    motor1_r.start(0)
    motor1_l.start(0)
    motor2_r.start(0)
    motor2_l.start(0)
    dht = adafruit_dht.DHT22(board.D4)
    lcd = CharLCD('PCF8574', 39, cols=16, rows=2)
    ser = serial.Serial('/dev/serial0', baudrate=9600, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=1)
hardware_lock = threading.Lock()
servo_lock = threading.Lock()
rs485_lock = threading.Lock()
operation_lock = threading.Lock()
pump_state_lock = threading.Lock()
command_lock = threading.Lock()
web_commands = {}
worker_thread = None
worker_pump = None
worker_schedule_id = None
worker_manual_request = False
SCHEDULE_GRACE_SECONDS = 120
stop_event = threading.Event()
current_operation = 'idle'
current_direction = 'stop'
current_motor_speed = MOTOR_SPEED
pump_is_on = False
system_running = True
last_firestore_log = 0

def lcd_message(line1='', line2=''):
    try:
        lcd.clear()
        lcd.cursor_pos = (0, 0)
        lcd.write_string(str(line1)[:16])
        lcd.cursor_pos = (1, 0)
        lcd.write_string(str(line2)[:16])
    except Exception as e:
        print('LCD ERROR:', e)

def set_servo_angle(angle):
    angle = max(0, min(180, int(angle)))
    duty = 2.5 + angle / 18.0
    with servo_lock:
        servo.ChangeDutyCycle(duty)
        time.sleep(0.6)
        servo.ChangeDutyCycle(0)

def press_pump_switch():
    print('SERVO: PRESSING PUMP SWITCH')
    set_servo_angle(SERVO_PRESS_ANGLE)
    time.sleep(SERVO_PRESS_TIME)
    set_servo_angle(SERVO_REST_ANGLE)

def update_status(**kwargs):
    try:
        kwargs['updated_at'] = {'.sv': 'timestamp'}
        status_ref.update(kwargs)
        realtime_db.reference(f'{BASE_PATH}/pump_status').update({
            'water_pump': {'active': bool(pump_is_on and current_operation == 'water'),
                           'updated_at': {'.sv': 'timestamp'}},
            'spray_pump': {'active': bool(pump_is_on and current_operation == 'chemical'),
                           'updated_at': {'.sv': 'timestamp'}},
        })
    except Exception as e:
        print('STATUS UPDATE ERROR:', e)

def water_pump_on():
    global pump_is_on
    with pump_state_lock:
        if stop_event.is_set():
            return
        if pump_is_on:
            print('PUMP ALREADY ON')
            return
        print('TURNING PUMP ON')
        if PUMP_SWITCH_TYPE == 'toggle_button':
            press_pump_switch()
        elif PUMP_SWITCH_TYPE == 'rocker':
            set_servo_angle(PUMP_ON_ANGLE)
        pump_is_on = True
    update_status(water_pump='on')


def water_pump_off():
    global pump_is_on
    with pump_state_lock:
        if not pump_is_on:
            print('PUMP ALREADY OFF')
            return
        print('TURNING PUMP OFF')
        if PUMP_SWITCH_TYPE == 'toggle_button':
            press_pump_switch()
        elif PUMP_SWITCH_TYPE == 'rocker':
            set_servo_angle(PUMP_OFF_ANGLE)
        pump_is_on = False
    update_status(water_pump='off')


def motors_stop():
    global current_direction
    with hardware_lock:
        motor1_r.ChangeDutyCycle(0)
        motor1_l.ChangeDutyCycle(0)
        motor2_r.ChangeDutyCycle(0)
        motor2_l.ChangeDutyCycle(0)
    current_direction = 'stop'
    print('MOTORS: STOP')

def motors_forward(speed=MOTOR_SPEED):
    if stop_event.is_set():
        motors_stop()
        return
    global current_direction
    global current_motor_speed
    speed = max(0, min(int(speed), 100))
    with hardware_lock:
        motor1_r.ChangeDutyCycle(speed)
        motor1_l.ChangeDutyCycle(0)
        motor2_r.ChangeDutyCycle(speed)
        motor2_l.ChangeDutyCycle(0)
    current_direction = 'forward'
    current_motor_speed = speed
    print(f'MOTORS: FORWARD {speed}%')

def motors_backward(speed=MOTOR_SPEED):
    if stop_event.is_set():
        motors_stop()
        return
    global current_direction
    global current_motor_speed
    speed = max(0, min(int(speed), 100))
    with hardware_lock:
        motor1_r.ChangeDutyCycle(0)
        motor1_l.ChangeDutyCycle(speed)
        motor2_r.ChangeDutyCycle(0)
        motor2_l.ChangeDutyCycle(speed)
    current_direction = 'backward'
    current_motor_speed = speed
    print(f'MOTORS: BACKWARD {speed}%')

def is_white():
    try:
        value = GPIO.input(IR_SENSOR)
        if WHITE_IS_LOW:
            return value == GPIO.LOW
        else:
            return value == GPIO.HIGH
    except Exception as e:
        print('IR ERROR:', e)
        return False

def wait_until_black(timeout=3):
    start_time = time.time()
    while is_white():
        if stop_event.is_set():
            return False
        if time.time() - start_time >= timeout:
            print('WARNING: STILL DETECTING WHITE')
            return False
        time.sleep(0.02)
    return True

def modbus_crc(data):
    crc = 65535
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc >>= 1
                crc ^= 40961
            else:
                crc >>= 1
    return crc

def read_7in1():
    with rs485_lock:
        try:
            request_data = bytes([1, 3, 0, 0, 0, 7])
            crc = modbus_crc(request_data)
            request = request_data + bytes([crc & 255, crc >> 8 & 255])
            print('7IN1 TX:', request.hex(' '))
            GPIO.output(RS485_EN, GPIO.LOW)
            ser.reset_input_buffer()
            GPIO.output(RS485_EN, GPIO.HIGH)
            time.sleep(0.02)
            ser.write(request)
            ser.flush()
            time.sleep(0.02)
            GPIO.output(RS485_EN, GPIO.LOW)
            data = ser.read(19)
            print('7IN1 RX:', data.hex(' '))
            if len(data) != 19:
                print('INVALID RESPONSE LENGTH:', len(data))
                return None
            if data[0] != 1:
                print('INVALID SLAVE ADDRESS')
                return None
            if data[1] != 3:
                print('INVALID MODBUS FUNCTION')
                return None
            if data[2] != 14:
                print('INVALID BYTE COUNT')
                return None
            received_crc = data[-2] | data[-1] << 8
            calculated_crc = modbus_crc(data[:-2])
            if received_crc != calculated_crc:
                print('CRC ERROR')
                return None
            moisture_raw = data[3] << 8 | data[4]
            temperature_raw = data[5] << 8 | data[6]
            ec_raw = data[7] << 8 | data[8]
            ph_raw = data[9] << 8 | data[10]
            n_raw = data[11] << 8 | data[12]
            p_raw = data[13] << 8 | data[14]
            k_raw = data[15] << 8 | data[16]
            if temperature_raw >= 32768:
                temperature_raw -= 65536
            moisture = moisture_raw / 10.0
            soil_temperature = temperature_raw / 10.0
            ec = ec_raw
            ph = ph_raw / 10.0
            nitrogen = n_raw
            phosphorus = p_raw
            potassium = k_raw
            return {'moisture': moisture, 'temperature': soil_temperature, 'ec': ec, 'ph': ph, 'n': nitrogen, 'p': phosphorus, 'k': potassium}
        except Exception as e:
            print('7-IN-1 ERROR:', e)
            return None
        finally:
            GPIO.output(RS485_EN, GPIO.LOW)

def read_dht():
    try:
        humidity = dht.humidity
        temperature = dht.temperature
        if humidity is None or temperature is None:
            return (None, None)
        return (humidity, temperature)
    except RuntimeError:
        return (None, None)
    except Exception as e:
        print('DHT ERROR:', e)
        return (None, None)

def sensor_payload(soil_data, air_temperature, air_humidity, white=False):
    payload = {
        'user_id': USER_ID, 'soil_moisture': soil_data['moisture'],
        'soil_temperature': soil_data['temperature'], 'soil_ec': soil_data['ec'],
        'soil_ph': soil_data['ph'], 'n': soil_data['n'], 'p': soil_data['p'],
        'k': soil_data['k'], 'white_detected': white,
        'timestamp': firestore.SERVER_TIMESTAMP,
    }
    if air_temperature is not None:
        payload['temperature'] = air_temperature
    if air_humidity is not None:
        payload['humidity'] = air_humidity
    return payload


def update_sensor_database(soil_data, air_temperature, air_humidity, white):
    if soil_data is None:
        return
    try:
        firestore_db.collection('users').document(USER_ID).collection('sensor_data').document('latest').set(
            sensor_payload(soil_data, air_temperature, air_humidity, white))
    except Exception as exc:
        print('FIRESTORE LIVE SENSOR ERROR:', exc)


def save_sensor_history(soil_data, air_temperature, air_humidity):
    try:
        firestore_db.collection('users').document(USER_ID).collection('sensor_data').add(
            sensor_payload(soil_data, air_temperature, air_humidity))
    except Exception as exc:
        print('FIRESTORE HISTORY ERROR:', exc)


def get_current_moisture():
    soil_data = read_7in1()
    if soil_data is None:
        return None
    return soil_data['moisture']

def emergency_stop():
    global current_operation
    print('================================')
    print('STOP EVERYTHING')
    print('================================')
    stop_event.set()
    motors_stop()
    water_pump_off()
    update_status(operation='idle', motor='stop', water_pump='off')
    lcd_message('SYSTEM STOPPED', 'ALL OFF')

def watering_cycle():
    global current_operation
    with operation_lock:
        if current_operation != 'idle':
            print('SYSTEM BUSY')
            return
        current_operation = 'water'
    start_time = datetime.now(timezone.utc)
    initial_moisture = None
    try:
        print('================================')
        print('AUTO WATER START')
        print('TARGET:', TARGET_SOIL_MOISTURE, '%')
        print('================================')
        lcd_message('AUTO WATER', 'CHECKING')
        update_status(operation='water', motor='starting', water_pump='off')
        initial_moisture = get_current_moisture()
        if initial_moisture is None:
            print('SOIL SENSOR ERROR')
            return
        print('INITIAL MOISTURE:', initial_moisture)
        if initial_moisture >= TARGET_SOIL_MOISTURE:
            print('SOIL ALREADY WET')
            lcd_message('SOIL OK', f'{initial_moisture:.1f}%')
            return
        water_pump_on()
        motors_forward(MOTOR_SPEED)
        update_status(motor='forward', water_pump='on')
        time.sleep(0.3)
        if is_white():
            wait_until_black()
        endpoint_count = 0
        last_moisture_check = 0
        final_moisture = initial_moisture
        while not stop_event.is_set():
            if time.time() - last_moisture_check >= 2:
                moisture = get_current_moisture()
                last_moisture_check = time.time()
                if moisture is not None:
                    final_moisture = moisture
                    print('MOISTURE:', moisture, '%')
                    update_status(soil_moisture=moisture)
                    if moisture >= TARGET_SOIL_MOISTURE:
                        print('TARGET MOISTURE REACHED')
                        motors_stop()
                        water_pump_off()
                        lcd_message('WATER DONE', f'{moisture:.1f}%')
                        break
            if is_white():
                endpoint_count += 1
                previous_direction = current_direction
                print('WHITE ENDPOINT:', endpoint_count)
                motors_stop()
                update_status(motor='reversing', endpoint_count=endpoint_count)
                time.sleep(REVERSE_PAUSE)
                if stop_event.is_set():
                    break
                if previous_direction == 'forward':
                    motors_backward(MOTOR_SPEED)
                else:
                    motors_forward(MOTOR_SPEED)
                update_status(motor=current_direction)
                time.sleep(WHITE_CLEAR_DELAY)
                wait_until_black()
            time.sleep(0.02)
        if not stop_event.is_set():
            firestore_db.collection('irrigation_logs').add({'user_id': USER_ID, 'initial_moisture': initial_moisture, 'final_moisture': final_moisture, 'target_moisture': TARGET_SOIL_MOISTURE, 'endpoint_count': endpoint_count, 'result': 'completed', 'started_at': start_time, 'ended_at': firestore.SERVER_TIMESTAMP})
    except Exception as e:
        print('WATERING ERROR:', e)
    finally:
        motors_stop()
        water_pump_off()
        current_operation = 'idle'
        update_status(operation='idle', motor='stop', water_pump='off')
        try:
            operation_ref.set('idle')
        except:
            pass

def chemical_cycle():
    global current_operation
    with operation_lock:
        if current_operation != 'idle':
            print('SYSTEM BUSY')
            return
        current_operation = 'chemical'
    start_time = datetime.now(timezone.utc)
    try:
        print('================================')
        print('CHEMICAL SPRAY START')
        print('2 ROUND TRIPS')
        print('================================')
        lcd_message('CHEMICAL', 'STARTING')
        update_status(operation='chemical', motor='starting', water_pump='off', chemical_pass=0)
        water_pump_on()
        motors_forward(MOTOR_SPEED)
        update_status(motor='forward', water_pump='on')
        time.sleep(0.3)
        if is_white():
            wait_until_black()
        completed_passes = 0
        while completed_passes < CHEMICAL_REQUIRED_PASSES:
            if stop_event.is_set():
                break
            if is_white():
                completed_passes += 1
                round_trips = completed_passes // 2
                previous_direction = current_direction
                motors_stop()
                print('CHEMICAL PASS:', completed_passes, '/4')
                lcd_message('CHEMICAL', f'PASS {completed_passes}/4')
                update_status(chemical_pass=completed_passes, round_trips=round_trips, motor='stop')
                if completed_passes >= CHEMICAL_REQUIRED_PASSES:
                    break
                time.sleep(REVERSE_PAUSE)
                if stop_event.is_set():
                    break
                if previous_direction == 'forward':
                    motors_backward(MOTOR_SPEED)
                else:
                    motors_forward(MOTOR_SPEED)
                update_status(motor=current_direction)
                time.sleep(WHITE_CLEAR_DELAY)
                wait_until_black()
            time.sleep(0.02)
        motors_stop()
        water_pump_off()
        if not stop_event.is_set():
            lcd_message('CHEMICAL DONE', '2 ROUND TRIPS')
            firestore_db.collection('chemical_logs').add({'user_id': USER_ID, 'passes': completed_passes, 'round_trips': 2, 'result': 'completed', 'started_at': start_time, 'ended_at': firestore.SERVER_TIMESTAMP})
    except Exception as e:
        print('CHEMICAL ERROR:', e)
    finally:
        motors_stop()
        water_pump_off()
        current_operation = 'idle'
        update_status(operation='idle', motor='stop', water_pump='off')
        try:
            operation_ref.set('idle')
        except:
            pass

def check_schedule():
    if daily_watering is not None:
        daily_watering.tick(
            lambda: start_web_operation('water_pump'),
            busy=current_operation != 'idle' or pump_is_on
                 or (worker_thread is not None and worker_thread.is_alive()))

def sensor_loop():
    global system_running
    global last_firestore_log
    while system_running:
        try:
            (air_humidity, air_temperature) = read_dht()
            soil_data = read_7in1()
            white = is_white()
            print('--------------------------------')
            print('OPERATION:', current_operation)
            print('MOTOR:', current_direction)
            print('PUMP:', pump_is_on)
            print('IR WHITE:', white)
            if soil_data:
                print('SOIL MOISTURE:', soil_data['moisture'], '%')
                print('SOIL TEMP:', soil_data['temperature'])
                print('EC:', soil_data['ec'])
                print('pH:', soil_data['ph'])
                print('N:', soil_data['n'])
                print('P:', soil_data['p'])
                print('K:', soil_data['k'])
                update_sensor_database(soil_data, air_temperature, air_humidity, white)
                if time.time() - last_firestore_log >= FIRESTORE_LOG_INTERVAL:
                    save_sensor_history(soil_data, air_temperature, air_humidity)
                    last_firestore_log = time.time()
                if current_operation == 'idle':
                    lcd_message(f"M:{soil_data['moisture']:.1f}%", f"pH:{soil_data['ph']:.1f} T:{soil_data['temperature']:.1f}")
            check_schedule()
            time.sleep(2)
        except Exception as e:
            print('SENSOR LOOP ERROR:', e)
            time.sleep(1)

def initialize_system():
    global current_operation
    global pump_is_on
    print('================================')
    print('SMART IRRIGATION STARTING')
    print('================================')
    stop_event.clear()
    motors_stop()
    pump_is_on = False
    current_operation = 'idle'
    GPIO.output(RS485_EN, GPIO.LOW)
    if PUMP_SWITCH_TYPE == 'toggle_button':
        set_servo_angle(SERVO_REST_ANGLE)
    update_status(operation='idle', motor='stop', water_pump='off', online=True)
    lcd_message('SMART FARM', 'READY')
    print('SYSTEM READY')


def web_control_listener(event):
    """Read a full snapshot, including when Firebase delivers a nested patch."""
    global web_commands
    try:
        snapshot = web_control_ref.get() or {}
        with command_lock:
            web_commands = snapshot
    except Exception as exc:
        print('WEB CONTROL ERROR:', exc)
        with command_lock:
            web_commands = {}
        emergency_stop()


def acknowledge_manual(pump_name, request):
    """Clear a finished button request without overwriting a newer click."""
    def clear(current):
        if (isinstance(current, dict) and current.get('enabled') is True
                and current.get('requested_at') == request.get('requested_at')):
            return {**current, 'enabled': False, 'completed_at': {'.sv': 'timestamp'}}
        return current
    web_control_ref.child(pump_name).transaction(clear)


def start_web_operation(pump_name, request=None, schedule=None):
    global worker_thread, worker_pump, worker_schedule_id, worker_manual_request
    with operation_lock:
        if (not system_running or current_operation != 'idle' or pump_is_on
                or (worker_thread is not None and worker_thread.is_alive())):
            return False
        worker_pump = pump_name
        worker_schedule_id = schedule.get('id') if schedule else None
        worker_manual_request = request is not None
        stop_event.clear()

        def run():
            global worker_pump, worker_schedule_id
            try:
                if pump_name == 'water_pump':
                    watering_cycle()
                else:
                    chemical_cycle()
            finally:
                # Hardware cleanup in the cycle precedes all acknowledgements.
                if request is not None:
                    try:
                        acknowledge_manual(pump_name, request)
                    except Exception as exc:
                        print('MANUAL ACKNOWLEDGEMENT ERROR:', exc)
                if schedule is not None:
                    try:
                        def finish(current):
                            if current and current.get('id') == schedule.get('id'):
                                return {**current,
                                        'last_result': 'stopped' if stop_event.is_set() else 'finished',
                                        'finished_at': {'.sv': 'timestamp'}}
                            return current
                        web_control_ref.child('spray_pump/schedule').transaction(finish)
                    except Exception as exc:
                        print('SCHEDULE ACKNOWLEDGEMENT ERROR:', exc)
                with operation_lock:
                    worker_pump = None
                    worker_schedule_id = None

        worker_thread = threading.Thread(target=run, daemon=True)
        worker_thread.start()
        return True


handled_requests = {}


def process_web_controls(now=None):
    """One shared servo pump: do not run watering and spraying together."""
    with command_lock:
        snapshot = {name: dict(web_commands.get(name) or {})
                    for name in ('water_pump', 'spray_pump')}
    if worker_thread is not None and worker_thread.is_alive():
        active = snapshot.get(worker_pump) or {}
        if worker_schedule_id:
            schedule = active.get('schedule') or {}
            if schedule.get('enabled') is not True or schedule.get('id') != worker_schedule_id:
                emergency_stop()
        elif worker_manual_request and worker_pump and active.get('enabled') is not True:
            emergency_stop()
        return
    enabled = [name for name, data in snapshot.items() if data.get('enabled') is True]
    if len(enabled) > 1:
        update_status(error='Shared pump: turn one manual control OFF before starting the other.')
        return
    if enabled:
        name = enabled[0]
        request = snapshot[name]
        signature = request.get('requested_at')
        if name not in handled_requests or handled_requests[name] != signature:
            if start_web_operation(name, request=request):
                handled_requests[name] = signature
        return
    if current_operation != 'idle':
        return
    schedule = snapshot['spray_pump'].get('schedule') or {}
    schedule_id = schedule.get('id')
    if (not schedule_id or schedule.get('enabled') is not True
            or schedule.get('executed_id') == schedule_id):
        return
    try:
        start = datetime.fromisoformat(schedule['start_datetime'].replace('Z', '+00:00'))
        if start.utcoffset() is None:
            raise ValueError('Schedule start must include a timezone')
        late = ((now or datetime.now(timezone.utc)) - start).total_seconds()
        if not 0 <= late <= SCHEDULE_GRACE_SECONDS:
            return
        token = uuid.uuid4().hex

        def claim(current):
            if (current and current.get('id') == schedule_id
                    and current.get('enabled') is True
                    and current.get('executed_id') != schedule_id):
                return {**current, 'executed_id': schedule_id, 'execution_token': token}
            return current

        claimed = web_control_ref.child('spray_pump/schedule').transaction(claim)
        if claimed and claimed.get('execution_token') == token:
            if not start_web_operation('spray_pump', schedule=claimed):
                update_status(error='Scheduled spray skipped: hardware busy.')
    except Exception as exc:
        print('WEB SCHEDULE ERROR:', exc)


def main():
    global system_running, web_control_ref, daily_watering
    listener = None
    sensor_thread = None
    try:
        setup_hardware()
        web_control_ref = realtime_db.reference(f'{BASE_PATH}/pump_control')
        daily_watering = DailyWatering(web_control_ref.child('water_pump/daily_schedule'),
                                      WeatherRain())
        initialize_system()
        listener = web_control_ref.listen(web_control_listener)
        sensor_thread = threading.Thread(target=sensor_loop, daemon=True)
        sensor_thread.start()
        print(f'Connected: Firestore sensors and RTDB {BASE_PATH}/pump_control')
        while system_running:
            process_web_controls()
            time.sleep(0.2)
    except KeyboardInterrupt:
        print('Stopping controller')
    finally:
        system_running = False
        stop_event.set()
        if all(name in globals() for name in ('servo', 'motor1_r', 'motor1_l', 'motor2_r', 'motor2_l')):
            motors_stop()
            water_pump_off()
        if listener is not None:
            listener.close()
        if worker_thread is not None:
            worker_thread.join(timeout=15)
        if sensor_thread is not None:
            sensor_thread.join(timeout=15)
        if 'status_ref' in globals():
            update_status(online=False, operation='offline', motor='stop', water_pump='off')
        for name in ('motor1_r', 'motor1_l', 'motor2_r', 'motor2_l', 'servo'):
            device = globals().get(name)
            if device is not None:
                device.stop()
        if 'ser' in globals():
            ser.close()
        if 'dht' in globals():
            dht.exit()
        if 'GPIO' in globals():
            GPIO.cleanup()


if __name__ == '__main__':
    main()
operation_listener_registration = None
pump_listener_registration = None
motor_listener_registration = None
