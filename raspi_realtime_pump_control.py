"""Run on the Pi to apply manual requests and timed spraying schedules."""
import argparse
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, db
from daily_watering import DailyWatering, WeatherRain


class PumpController:
    def __init__(self, control, status, pumps):
        self.control = control
        self.status = status
        self.pumps = pumps
        self.lock = threading.Lock()
        self.requests = {}
        self.running_id = None
        self.timer = None
        self.water_timer = None
        self.daily_water_active = False
        self.daily_watering = DailyWatering(control.child('water_pump/daily_schedule'), WeatherRain())

    def _apply_outputs(self):
        for name, pump in self.pumps.items():
            enabled = (self.requests.get(name) or {}).get('enabled') is True
            if name == 'water_pump' and self.daily_water_active:
                enabled = True
            if name == 'spray_pump' and self.running_id is not None:
                enabled = True
            pump.on() if enabled else pump.off()

    def _report_outputs(self):
        for name, pump in self.pumps.items():
            self.status.child(name).update({
                'active': bool(pump.value),
                'updated_at': {'.sv': 'timestamp'},
            })

    def apply_event(self, event):
        # Read current state for both full snapshots and nested stream patches.
        try:
            requests = self.control.get() or {}
            with self.lock:
                self.requests = requests
                schedule = (requests.get('spray_pump') or {}).get('schedule') or {}
                if self.running_id and (schedule.get('enabled') is not True or schedule.get('id') != self.running_id):
                    self.timer.cancel()
                    self.running_id = None
                self._apply_outputs()
            self._report_outputs()
        except Exception:
            with self.lock:
                if self.water_timer:
                    self.water_timer.cancel()
                self.daily_water_active = False
                if self.timer:
                    self.timer.cancel()
                self.running_id = None
                self.requests = {}
                for pump in self.pumps.values():
                    pump.off()
            logging.exception('Pump control failed; outputs turned off')

    def tick(self, now=None):
        """Claim each saved schedule once; expired schedules are never replayed."""
        now = now or datetime.now(timezone.utc)
        self.daily_watering.tick(self.start_daily_water,
                                busy=self.daily_water_active or any(pump.value for pump in self.pumps.values()), now=now)
        try:
            with self.lock:
                schedule = (self.requests.get('spray_pump') or {}).get('schedule') or {}
                schedule_id = schedule.get('id')
                if self.running_id or schedule.get('enabled') is not True or not schedule_id:
                    return
                if schedule.get('executed_id') == schedule_id:
                    return
                start = datetime.fromisoformat(schedule['start_datetime'])
                duration = int(schedule['duration'])
                if start.utcoffset() is None or not 1 <= duration <= 3600:
                    return
                elapsed = (now - start).total_seconds()
                if elapsed < 0 or elapsed >= duration:
                    return
                reference = self.control.child('spray_pump/schedule')
                execution_token = uuid.uuid4().hex
                claim_started = time.monotonic()

                def claim(current):
                    if (current and current.get('id') == schedule_id
                            and current.get('enabled') is True
                            and current.get('executed_id') != schedule_id):
                        return {**current, 'executed_id': schedule_id, 'execution_token': execution_token}
                    return current

                # Persist the claim before activating, preventing replay on reconnect/restart.
                claimed = reference.transaction(claim)
                if not claimed or claimed.get('execution_token') != execution_token:
                    return
                # Account for network time spent claiming the schedule.
                remaining = duration - elapsed - (time.monotonic() - claim_started)
                self.requests['spray_pump']['schedule'] = claimed
                if remaining <= 0:
                    return
                self.running_id = schedule_id
                self._apply_outputs()
                self.timer = threading.Timer(remaining, self.finish_schedule, args=(schedule_id,))
                self.timer.daemon = True
                self.timer.start()
            self._report_outputs()
        except Exception:
            logging.exception('Could not run spraying schedule')

    def finish_schedule(self, schedule_id):
        # Turn off before any Firebase write, so loss of internet cannot extend a spray.
        with self.lock:
            if self.running_id != schedule_id:
                return
            self.running_id = None
            self._apply_outputs()
        try:
            self._report_outputs()
        except Exception:
            logging.exception('Could not report completed spraying schedule')

    def close(self):
        with self.lock:
            if self.water_timer:
                self.water_timer.cancel()
            self.daily_water_active = False
            if self.timer:
                self.timer.cancel()
            self.running_id = None
            for pump in self.pumps.values():
                pump.off()

    def start_daily_water(self):
        with self.lock:
            if any(pump.value for pump in self.pumps.values()):
                return False
            self.daily_water_active = True
            self._apply_outputs()
            self.water_timer = threading.Timer(5, self.finish_daily_water)
            self.water_timer.daemon = True
            self.water_timer.start()
        self._report_outputs()
        return True

    def finish_daily_water(self):
        with self.lock:
            self.daily_water_active = False
            self._apply_outputs()
        try:
            self._report_outputs()
        except Exception:
            logging.exception('Could not report completed daily watering')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id', required=True, help='Django user ID used by the web app')
    parser.add_argument('--database-url', default=os.environ.get('FIREBASE_DATABASE_URL'))
    parser.add_argument('--credentials', default=str(Path(__file__).with_name('firebase-service-account.json')))
    parser.add_argument('--water-pin', type=int, default=17)
    parser.add_argument('--spray-pin', type=int, default=18)
    parser.add_argument('--active-low', action='store_true', help='Use for relays that turn on at LOW')
    args = parser.parse_args()
    if not args.database_url:
        parser.error('Provide --database-url or set FIREBASE_DATABASE_URL')
    from gpiozero import OutputDevice

    logging.basicConfig(level=logging.INFO)
    firebase_admin.initialize_app(credentials.Certificate(args.credentials),
                                  {'databaseURL': args.database_url, 'httpTimeout': 10})
    pumps = {}
    listener = None
    try:
        for name, pin in [('water_pump', args.water_pin), ('spray_pump', args.spray_pin)]:
            pumps[name] = OutputDevice(pin, active_high=not args.active_low, initial_value=False)
        controller = PumpController(
            db.reference(f'users/{args.user_id}/pump_control'),
            db.reference(f'users/{args.user_id}/pump_status'), pumps)
        listener = controller.control.listen(controller.apply_event)
        logging.info('Listening for pump requests for user %s', args.user_id)
        while True:
            controller.tick()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if pumps and 'controller' in locals():
            controller.close()
        if listener is not None:
            listener.close()
        for name, pump in pumps.items():
            pump.off()
            pump.close()
            try:
                db.reference(f'users/{args.user_id}/pump_status/{name}').update({'active': False})
            except Exception:
                logging.exception('Could not report shutdown status')


if __name__ == '__main__':
    main()
