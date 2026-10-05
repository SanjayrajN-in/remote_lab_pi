#!/usr/bin/env python3
"""
Lab Pi Session Poller - Auto-discovery version
Polls Admin Pi for active session and controls hardware automatically.

Features:
- Auto-discovers lab_pi_id from hostname (no manual config needed)
- Polls every 5 seconds
- Controls relay based on session status

Usage:
    python3 lab_pi_session_poller.py
    
Or with custom admin URL:
    ADMIN_PI_URL=http://192.168.1.100:5000 python3 lab_pi_session_poller.py

Install as service on Lab Pi:
    sudo cp systemd/lab_pi_session_poller.service /etc/systemd/system/
    sudo systemctl enable lab_pi_session_poller
    sudo systemctl start lab_pi_session_poller
"""

import os
import sys
import time
import socket
import requests
import threading
from datetime import datetime

def _authed_get(url, **kw):
    kw.setdefault('headers', {})['X-Master-Api-Key'] = os.environ.get('MASTER_API_KEY', '')
    return requests.get(url, **kw)


# Configuration - reads from environment or .env file
# Default to localhost for testing
ADMIN_PI_URL = os.environ.get('ADMIN_PI_URL', 'http://127.0.0.1:5000')

# Try to get hostname as auto-discovered lab_pi_id
def get_lab_pi_id():
    """Auto-discover lab_pi_id from hostname"""
    hostname = socket.gethostname()
    return hostname

LAB_PI_ID = os.environ.get('LAB_PI_ID', get_lab_pi_id())
POLL_INTERVAL = 5  # seconds

# GPIO Setup (for Relay Control)
try:
    import lgpio
    RELAY_PIN = 26
    gpio_handle = None
    
    def init_gpio():
        global gpio_handle
        try:
            gpio_handle = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_output(gpio_handle, RELAY_PIN)
            return True
        except Exception as e:
            print(f"GPIO init failed: {e}")
            return False
    
    def relay_on():
        if gpio_handle:
            lgpio.gpio_write(gpio_handle, RELAY_PIN, 0)
            print(f"[{datetime.now()}] Relay ON")
    
    def relay_off():
        if gpio_handle:
            lgpio.gpio_write(gpio_handle, RELAY_PIN, 1)
            print(f"[{datetime.now()}] Relay OFF")
            
except ImportError:
    print("lgpio not available, relay control disabled (or running on non-Pi)")
    RELAY_PIN = None
    gpio_handle = None
    def init_gpio(): return True
    def relay_on(): print(f"[{datetime.now()}] Relay ON (simulated)")
    def relay_off(): print(f"[{datetime.now()}] Relay OFF (simulated)")


class SessionPoller:
    def __init__(self, admin_url, lab_pi_id):
        self.admin_url = admin_url.rstrip('/')
        self.lab_pi_id = lab_pi_id
        self.current_session_key = None
        self.session_end_time = None
        self.hardware_running = False
        self.running = True
        
    def poll(self):
        """Poll Admin Pi for active session"""
        try:
            url = f"{self.admin_url}/api/lab-pi/{self.lab_pi_id}/active-session"
            response = _authed_get(url, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                status = data.get('status')
                
                if status == 'running':
                    session_key = data.get('session_key')
                    end_time_str = data.get('end_time')
                    
                    # Parse end time
                    if end_time_str:
                        self.session_end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
                    else:
                        self.session_end_time = None
                    
                    # New session started?
                    if session_key != self.current_session_key:
                        print(f"[{datetime.now()}] New session: {session_key}")
                        self.current_session_key = session_key
                        self.start_hardware()
                        
                    # Check if session time has ended
                    if self.session_end_time:
                        now = datetime.now(self.session_end_time.tzinfo)
                        if now >= self.session_end_time:
                            print(f"[{datetime.now()}] Session time ended")
                            self.stop_hardware()
                            
                elif status == 'stopped':
                    if self.current_session_key or self.hardware_running:
                        print(f"[{datetime.now()}] No active session")
                        self.stop_hardware()
                        
            elif response.status_code == 404:
                print(f"[{datetime.now()}] Lab Pi '{self.lab_pi_id}' not registered on Admin. Please add it in Admin Pi and map to an experiment.")
                time.sleep(10)  # Wait longer if not registered
                
        except requests.exceptions.RequestException as e:
            print(f"[{datetime.now()}] Poll error: {e}")
        except Exception as e:
            print(f"[{datetime.now()}] Error: {e}")
            
    def start_hardware(self):
        """Start hardware (relay on)"""
        if not self.hardware_running:
            relay_on()
            self.hardware_running = True
            
    def stop_hardware(self):
        """Stop hardware (relay off)"""
        if self.hardware_running:
            relay_off()
            self.hardware_running = False
        self.current_session_key = None
        self.session_end_time = None
        
    def run(self):
        """Main polling loop"""
        print(f"=" * 50)
        print(f"Lab Pi Session Poller")
        print(f"=" * 50)
        print(f"Lab Pi ID: {self.lab_pi_id} (auto-discovered from hostname)")
        print(f"Admin URL: {self.admin_url}")
        print(f"Poll interval: {POLL_INTERVAL}s")
        print(f"=" * 50)
        
        init_gpio()
        
        while self.running:
            self.poll()
            time.sleep(POLL_INTERVAL)
            
        # Cleanup
        self.stop_hardware()
        if gpio_handle:
            lgpio.gpiochip_close(gpio_handle)


def main():
    poller = SessionPoller(ADMIN_PI_URL, LAB_PI_ID)
    try:
        poller.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
        poller.running = False


if __name__ == '__main__':
    main()
