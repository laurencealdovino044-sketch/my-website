from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from predictions.firestore import get_firestore_client
from firebase_admin import firestore
from datetime import datetime, timedelta


class Command(BaseCommand):
    help = 'Add test sensor data to Firestore for the active season of a user'

    def get_current_pump_status(self, user_id):
        """Get current pump status from Firestore"""
        client = get_firestore_client()
        if client is None:
            return {
                'water_pump_active': False,
                'spray_pump_active': False,
                'water_schedule_active': False,
                'spray_schedule_active': False,
            }
        
        try:
            pump_status_ref = client.collection('users').document(user_id).collection('pump_status')
            
            # Get water pump status
            water_doc = pump_status_ref.document('water_pump').get()
            water_status = water_doc.to_dict() if water_doc.exists else {}
            
            # Get spray pump status
            spray_doc = pump_status_ref.document('spray_pump').get()
            spray_status = spray_doc.to_dict() if spray_doc.exists else {}
            
            return {
                'water_pump_active': water_status.get('status') == 'active',
                'spray_pump_active': spray_status.get('status') == 'active',
                'water_schedule_active': water_status.get('status') == 'scheduled',
                'spray_schedule_active': spray_status.get('status') == 'scheduled',
            }
        except Exception:
            return {
                'water_pump_active': False,
                'spray_pump_active': False,
                'water_schedule_active': False,
                'spray_schedule_active': False,
            }

    def add_arguments(self, parser):
        parser.add_argument('username', type=str, help='Username to add sensor data for')
        parser.add_argument('--soil-moisture', type=float, default=65.5, help='Soil moisture percent')
        parser.add_argument('--temperature', type=float, default=28.5, help='Temperature °C')
        parser.add_argument('--humidity', type=float, default=72.3, help='Humidity percent')
        parser.add_argument('--n', type=float, default=0.42, help='Nitrogen (N) ppm')
        parser.add_argument('--p', type=float, default=0.38, help='Phosphorus (P) ppm')
        parser.add_argument('--k', type=float, default=0.55, help='Potassium (K) ppm')
        parser.add_argument('--water-pump-active', action='store_true', help='Set water pump as manually active')
        parser.add_argument('--spray-pump-active', action='store_true', help='Set spray pump as manually active')
        parser.add_argument('--water-schedule-active', action='store_true', help='Set water schedule as active')
        parser.add_argument('--spray-schedule-active', action='store_true', help='Set spray schedule as active')

    def handle(self, *args, **options):
        username = options['username']
        
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"User '{username}' not found"))
            return

        client = get_firestore_client()
        if not client:
            self.stdout.write(self.style.ERROR("Firestore not configured"))
            return

        user_id = str(user.id)
        
        # Get current pump status from Firestore
        current_pump_status = self.get_current_pump_status(user_id)
        
        # Get active season
        try:
            seasons_ref = client.collection('users').document(user_id).collection('seasons')
            active_seasons = seasons_ref.where('is_active', '==', True).limit(1).get()
            
            if not active_seasons:
                self.stdout.write(self.style.ERROR(f"No active season found for user '{username}'"))
                return
            
            season_doc = active_seasons[0]
            season_id = season_doc.id
            
            # Create sensor data
            sensor_data = {
                'timestamp': datetime.now(),
                'soil_moisture': options['soil_moisture'],
                'temperature': options['temperature'],
                'humidity': options['humidity'],
                'n': options['n'],
                'p': options['p'],
                'k': options['k'],
                'soil_ph': 6.8,
                # Add pump status for Raspberry Pi triggering - use command line options or current Firestore status
                'water_pump_active': options.get('water_pump_active', current_pump_status['water_pump_active']),
                'spray_pump_active': options.get('spray_pump_active', current_pump_status['spray_pump_active']),
                'water_schedule_active': options.get('water_schedule_active', current_pump_status['water_schedule_active']),
                'spray_schedule_active': options.get('spray_schedule_active', current_pump_status['spray_schedule_active']),
            }
            
            # Calculate pump status based on manual and schedule states
            water_manual = sensor_data['water_pump_active']
            water_sched = sensor_data['water_schedule_active']
            spray_manual = sensor_data['spray_pump_active']
            spray_sched = sensor_data['spray_schedule_active']
            
            sensor_data['water_pump_status'] = 'active' if water_manual else ('scheduled' if water_sched else 'inactive')
            sensor_data['spray_pump_status'] = 'active' if spray_manual else ('scheduled' if spray_sched else 'inactive')
            
            # Add to Firestore
            season_ref = seasons_ref.document(season_id)
            season_ref.collection('sensor_data').add(sensor_data)
            
            self.stdout.write(self.style.SUCCESS(
                f"✓ Added test sensor data for user '{username}' (Season ID: {season_id})\n"
                f"  Soil Moisture: {options['soil_moisture']}%\n"
                f"  Temperature: {options['temperature']}°C\n"
                f"  Humidity: {options['humidity']}%\n"
                f"  N/P/K: {options['n']}/{options['p']}/{options['k']} ppm\n"
                f"  Water Pump: {sensor_data['water_pump_status']} (Manual: {sensor_data['water_pump_active']}, Schedule: {sensor_data['water_schedule_active']})\n"
                f"  Spray Pump: {sensor_data['spray_pump_status']} (Manual: {sensor_data['spray_pump_active']}, Schedule: {sensor_data['spray_schedule_active']})"
            ))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error adding sensor data: {e}"))
