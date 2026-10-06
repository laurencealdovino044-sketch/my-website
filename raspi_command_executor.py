"""
Raspberry Pi Command Executor - Polls Firestore for pump commands
This script runs on the Raspberry Pi and listens for commands from the web app
"""

import time
import os
import threading
from datetime import datetime
from pathlib import Path

# Try to import Firestore and GPIO
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIRESTORE_AVAILABLE = True
except ImportError:
    print("Warning: firebase_admin not installed. Run: pip install firebase-admin")
    FIRESTORE_AVAILABLE = False

try:
    from gpiozero import LED
    GPIO_AVAILABLE = True
    water_pump = LED(17)  # GPIO 17 for watering
    spray_pump = LED(18)  # GPIO 18 for spraying
except ImportError:
    print("Warning: gpiozero not installed or not on Raspberry Pi. Using mock.")
    GPIO_AVAILABLE = False
    
    class MockLED:
        def __init__(self, pin):
            self.pin = pin
            self.state = False
        
        def on(self):
            self.state = True
            print(f"[GPIO {self.pin}] ON")
        
        def off(self):
            self.state = False
            print(f"[GPIO {self.pin}] OFF")
    
    water_pump = MockLED(17)
    spray_pump = MockLED(18)

# Firestore client
firebase_client = None
user_id = None
active_pump_thread = None


def initialize_firebase(service_account_path):
    """Initialize Firebase connection"""
    global firebase_client
    
    if not FIRESTORE_AVAILABLE:
        print("Firebase not available. Cannot initialize.")
        return False
    
    try:
        if not Path(service_account_path).exists():
            print(f"Service account file not found: {service_account_path}")
            return False
        
        creds = credentials.Certificate(service_account_path)
        firebase_admin.initialize_app(creds)
        firebase_client = firestore.client()
        print("✓ Firebase initialized successfully")
        return True
    except Exception as e:
        print(f"✗ Firebase initialization failed: {e}")
        return False


def activate_water_pump(duration=5):
    """Activate water pump for specified duration"""
    global active_pump_thread
    
    def run_pump():
        print(f"💧 Water pump ON for {duration}s")
        water_pump.on()
        update_pump_status('water_pump', True)
        
        time.sleep(duration)
        
        water_pump.off()
        print(f"💧 Water pump OFF")
        update_pump_status('water_pump', False)
    
    if active_pump_thread and active_pump_thread.is_alive():
        print("⚠ Another pump is running. Skipping.")
        return
    
    active_pump_thread = threading.Thread(target=run_pump, daemon=True)
    active_pump_thread.start()


def activate_spray_pump(duration=5):
    """Activate spray pump for specified duration"""
    global active_pump_thread
    
    def run_pump():
        print(f"🌪️  Spray pump ON for {duration}s")
        spray_pump.on()
        update_pump_status('spray_pump', True)
        
        time.sleep(duration)
        
        spray_pump.off()
        print(f"🌪️  Spray pump OFF")
        update_pump_status('spray_pump', False)
    
    if active_pump_thread and active_pump_thread.is_alive():
        print("⚠ Another pump is running. Skipping.")
        return
    
    active_pump_thread = threading.Thread(target=run_pump, daemon=True)
    active_pump_thread.start()


def update_pump_status(pump_name, is_active):
    """Update pump status in Firestore"""
    if not firebase_client or not user_id:
        return
    
    try:
        status_ref = firebase_client.collection('users').document(user_id).collection('pump_status').document(pump_name)
        status_ref.set({
            'pump': pump_name,
            'status': 'active' if is_active else 'inactive',
            'updated_at': datetime.now().isoformat(),
            'raspi_hostname': os.uname()[1],  # Hostname of the Raspberry Pi
        }, merge=True)
    except Exception as e:
        print(f"✗ Failed to update pump status: {e}")


def check_and_execute_commands():
    """Check Firestore for pending commands and execute them"""
    if not firebase_client or not user_id:
        print("✗ Firebase or user_id not set")
        return
    
    try:
        commands_ref = firebase_client.collection('users').document(user_id).collection('commands')
        
        # Query for unexecuted commands
        pending_commands = commands_ref.where('executed', '==', False).get()
        
        for cmd_doc in pending_commands:
            cmd_data = cmd_doc.to_dict()
            command_type = cmd_data.get('command_type')
            duration = cmd_data.get('duration', 5)
            
            print(f"\n📋 Command received: {command_type} ({duration}s)")
            
            # Execute command
            if command_type == 'water':
                activate_water_pump(duration)
            elif command_type == 'spray':
                activate_spray_pump(duration)
            elif command_type == 'fertilize':
                print("🧪 Fertilizer command received (not implemented)")
            
            # Mark command as executed
            cmd_doc.reference.update({
                'executed': True,
                'executed_timestamp': datetime.now().isoformat(),
                'executed_by': os.uname()[1],
            })
            print(f"✓ Command marked as executed")
        
        if pending_commands:
            print(f"Processed {len(pending_commands)} command(s)")
        
    except Exception as e:
        print(f"✗ Error checking commands: {e}")


def main_loop(check_interval=2):
    """Main loop - continuously check for commands"""
    print(f"\n🚀 Starting Raspberry Pi Command Executor")
    print(f"   Checking for commands every {check_interval}s")
    print(f"   User ID: {user_id}")
    print(f"   GPIO Available: {GPIO_AVAILABLE}")
    print(f"   Firebase Available: {FIRESTORE_AVAILABLE}")
    print("-" * 50)
    
    try:
        while True:
            check_and_execute_commands()
            time.sleep(check_interval)
    except KeyboardInterrupt:
        print("\n\n⏹️  Shutting down...")
        water_pump.off()
        spray_pump.off()
        print("✓ Pumps turned off")


if __name__ == '__main__':
    # Configuration
    SERVICE_ACCOUNT_PATH = Path(__file__).parent / 'firebase-service-account.json'
    
    # Replace with your actual user ID
    user_id = os.environ.get('HITECH_USER_ID') or input("Enter user ID: ")
    
    # Initialize Firebase
    if not initialize_firebase(str(SERVICE_ACCOUNT_PATH)):
        print("✗ Cannot proceed without Firebase")
        exit(1)
    
    # Start main loop
    main_loop(check_interval=2)
