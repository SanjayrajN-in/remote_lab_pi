#!/usr/bin/env python3
import sys
import os

# Load environment variables from .env file
from dotenv import load_dotenv
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
load_dotenv(env_path)

# Import lgpio FIRST before eventlet patches anything!
# Try multiple GPIO libraries as fallback
try:
    import lgpio
    RELAY_PIN = 26
    GPIO_MODE = "lgpio"
except Exception as e:
    print(f"lgpio import failed: {e}")
    lgpio = None
    RELAY_PIN = None
    GPIO_MODE = None

# Try gpiod as fallback
try:
    import gpiod
    GPIO_MODE = GPIO_MODE or "gpiod"
except:
    gpiod = None

# Try RPi.GPIO as another fallback
try:
    import RPi.GPIO as GPIO
    GPIO_MODE = GPIO_MODE or "rpi"
except:
    GPIO = None

# Try gpio utility as last resort (always available on RPi)
import subprocess

import eventlet
eventlet.monkey_patch()

import os, time, subprocess, threading, queue, tempfile, re, random, json, math, asyncio, hmac, functools, collections, glob
import numpy as np
from scipy.signal import medfilt, savgol_filter
import requests
from flask import Flask, send_from_directory, request, jsonify, render_template, abort, session, redirect, url_for, copy_current_request_context
from flask_socketio import SocketIO, emit

# Optional: serial usage guarded (so app still runs if pyserial not available)
try:
    import serial
    from serial.tools import list_ports
except Exception as e:
    serial = None
    list_ports = None

import struct

# ---------- OSCILLOSCOPE CONFIG ----------
OSC_PORT = None
OSC_BAUD = 921600
OSC_SAMPLE_RATE = 40000
OSC_HISTORY_SIZE = OSC_SAMPLE_RATE * 10  # 10 seconds, matches STM32 dual-channel scope
OSC_EXPECTED_COUNT = 256  # samples per packet (HALF_BUF_LEN in firmware) - exact match required
osc_history_ch1 = np.zeros(OSC_HISTORY_SIZE, dtype=np.float64)
osc_history_ch2 = np.zeros(OSC_HISTORY_SIZE, dtype=np.float64)
osc_hist_idx = 0
osc_lock = threading.Lock()
osc_ser = None
osc_stop = threading.Event()
osc_stats_counters = {"packets_ok": 0, "packets_rejected": 0}

osc_settings = {
    'trig_v': 1.65,
    'hyst': 0.15,
    'rising': True,
    'samples': 1000,
    'smooth': False,
    'freeze': False,
    'pre_trigger': 200,
    'trig_src': 0  # 0 = CH1, 1 = CH2
}

OSC_KNOWN_VID_PID = [
    (0x0483, 0x5740),  # STM32 CDC ACM (the actual scope board)
    (0x10c4, 0xea60),  # legacy ESP32/Arduino: CP210x
    (0x1a86, 0x7523),  # legacy ESP32/Arduino: CH340
]

def _osc_port_override():
    """Admin-configured override (a /dev/serial/by-id/... path) for the
    Oscilloscope's port, read lazily so this works even at the module-level
    call below, before admin_config's top-level import further down runs."""
    try:
        from admin_config import get_effective_ui_config
        return (get_effective_ui_config().get('defaults', {}).get('osc_port') or '').strip()
    except Exception:
        return ''


def detect_osc_port():
    """Detect the scope board's USB VID:PID (STM32 CDC ACM 0483:5740, with legacy
    ESP32/Arduino USB-serial bridges kept as fallback). Requires an exact vid+pid
    pair match AND a /dev/ttyACM* path, since the scope always enumerates as CDC
    ACM while student/teacher MCU boards (CP210x/CH340 bridges) enumerate as
    /dev/ttyUSB* — this keeps a teacher/student board from ever being misdetected
    as the oscilloscope just because it shares a VID or PID with one of these pairs.

    An admin-configured 'osc_port' override (a stable by-id path) skips this
    scan entirely — needed once more than one attached device could match the
    whitelist above, or when raw /dev/ttyACM* indices get confusing next to
    other ACM devices on the same Pi (e.g. a debug probe's virtual COM port)."""
    global OSC_PORT
    if list_ports is None: return None
    override = _osc_port_override()
    if override:
        resolved = os.path.realpath(override)
        if os.path.exists(resolved):
            OSC_PORT = resolved
            print(f"[OSC] Using admin-configured scope port: {override} -> {OSC_PORT}")
            return OSC_PORT
        print(f"[OSC] Configured scope port {override} not found on this Pi, falling back to auto-detect")
    ports = list_ports.comports()
    for p in ports:
        if not re.match(r'^/dev/ttyACM\d+$', p.device):
            continue
        if (p.vid, p.pid) in OSC_KNOWN_VID_PID:
             OSC_PORT = p.device
             print(f"[OSC] Auto-detected scope board at {OSC_PORT}")
             return OSC_PORT
    return None


def _reset_osc_connection():
    """Force osc_worker to re-resolve the Oscilloscope's port on its next
    loop iteration, so changing the admin's port override takes effect
    immediately instead of requiring a service restart."""
    global OSC_PORT, osc_ser
    OSC_PORT = None
    if osc_ser is not None:
        try:
            osc_ser.close()
        except Exception:
            pass
        osc_ser = None


detect_osc_port()

from werkzeug.utils import secure_filename

from admin_config import (
    CONTROL_KEYS, get_effective_ui_config, get_student_ui_config, load_ui_config, save_ui_config,
    is_control_enabled, has_admin_password_configured, password_locked_by_env,
    set_admin_password, verify_admin_password, admin_required, get_or_create_secret_key,
    add_required_control, delete_required_control, update_required_control,
    add_serial_port, delete_serial_port, update_serial_port,
)

# ---------- CONFIG ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # base path relative to script location
UPLOAD_DIR = os.path.join(BASE_DIR, 'data', 'uploads')
DEFAULT_FW_DIR = os.path.join(BASE_DIR, 'data', 'default_fw')  # contains esp32_default.bin etc
SOP_DIR = os.path.join(BASE_DIR, 'data', 'sop')      # contains exp.pdf
# Debug symbols (.elf) live separately from UPLOAD_DIR's flashed .bin/.hex/etc
# -- flashing never produces or needs an ELF, so this is always a distinct
# upload. A single fixed filename is fine: like every other debug session
# global here (_debug_gdb, active_sessions), a Lab Pi only ever serves one
# active booking at a time.
DEBUG_UPLOAD_DIR = os.path.join(BASE_DIR, 'data', 'debug_uploads')
DEBUG_ELF_PATH = os.path.join(DEBUG_UPLOAD_DIR, 'firmware.elf')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DEFAULT_FW_DIR, exist_ok=True)
os.makedirs(SOP_DIR, exist_ok=True)
os.makedirs(DEBUG_UPLOAD_DIR, exist_ok=True)

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['SECRET_KEY'] = get_or_create_secret_key()
# async_handlers=False: each client's events run one at a time, in arrival
# order. With the default (a new green thread per event) a send_command press
# and release arriving milliseconds apart could reach the serial port swapped.
# Handlers must therefore stay quick — slow work (flashing, GDB/OpenOCD) runs
# in a background task instead so it doesn't hold up later commands.
socketio = SocketIO(app, async_mode='eventlet', async_handlers=False)

# Global active sessions for authorization
active_sessions = {}
latest_sensor_data = {}  # conn_id -> latest parsed sensor dict, for CRO page polling

# ---------- MULTI-PORT SERIAL CONNECTIONS ----------
# conn_id -> {'ser': serial.Serial, 'stop': threading.Event, 'port': str, 'baud': int}
# conn_id is a serial-port profile id from admin_config's ui_config['serial_ports'].
serial_connections = {}
serial_connections_lock = threading.Lock()


def _primary_conn_id():
    """The serial-port profile that generic slider/button commands are sent to."""
    for p in get_effective_ui_config().get('serial_ports', []):
        if p.get('is_primary_target'):
            return p['id']
    return 'default'


def _open_connection(conn_id, port, baud):
    """(Re)open a serial connection for conn_id, replacing any existing one under the same id."""
    if serial is None:
        return False, 'pyserial not available on server'
    # The Oscilloscope is a wholly separate, always-on system (its own worker,
    # its own auto-detected port, its own settings) — students can never connect,
    # disconnect, or reconfigure it through the Serial Monitor/Plotter, so no
    # serial-port profile (however it got configured) may ever open its port.
    if OSC_PORT and port == OSC_PORT:
        return False, f'{port} is the Oscilloscope\'s port and cannot be used here'
    with serial_connections_lock:
        existing = serial_connections.pop(conn_id, None)
        if existing:
            try:
                existing['stop'].set()
                if existing['ser'] and existing['ser'].is_open:
                    existing['ser'].close()
            except Exception:
                pass
        try:
            ser_obj = serial.Serial(port, baud, timeout=1)
        except Exception as e:
            return False, str(e)
        stop_event = threading.Event()
        serial_connections[conn_id] = {'ser': ser_obj, 'stop': stop_event, 'port': port, 'baud': baud}
    eventlet.spawn(serial_reader_worker, conn_id, ser_obj, stop_event)
    return True, None


def _close_connection(conn_id):
    with serial_connections_lock:
        conn = serial_connections.pop(conn_id, None)
    if not conn:
        return
    try:
        conn['stop'].set()
        if conn['ser'] and conn['ser'].is_open:
            conn['ser'].close()
    except Exception:
        pass


def sync_serial_profiles():
    """Reconcile live connections with admin-configured serial port profiles:
    open auto-connect profiles with a fixed port, close ones that were
    deleted, disabled, or had auto-connect turned off. Call after any
    admin settings save so changes take effect without a restart.
    (_open_connection refuses the Oscilloscope's own port regardless.)"""
    profiles = {p['id']: p for p in get_effective_ui_config().get('serial_ports', [])}
    with serial_connections_lock:
        current_ids = list(serial_connections.keys())
    for conn_id in current_ids:
        profile = profiles.get(conn_id)
        if profile is None or not profile.get('auto_connect') or not profile.get('port'):
            _close_connection(conn_id)
    for conn_id, profile in profiles.items():
        if not profile.get('auto_connect') or not profile.get('port'):
            continue
        with serial_connections_lock:
            already_open = conn_id in serial_connections
        if not already_open:
            _open_connection(conn_id, profile['port'], int(profile.get('baud', 115200)))

# ---------- MASTER PI HEARTBEAT CONFIGURATION ----------
# Load from environment variables
LAB_PI_ID = os.environ.get('VLAB_PI_ID', 'Master')
LAB_PI_NAME = os.environ.get('VLAB_PI_NAME', 'Lab Pi Node 1')
LAB_PI_MAC = os.environ.get('VLAB_PI_MAC', '')
MASTER_URL = os.environ.get('MASTER_URL', 'http://192.168.1.5:5000').strip()
MASTER_API_KEY = os.environ.get('MASTER_API_KEY', '').strip()
HEARTBEAT_INTERVAL = 30  # seconds
HEARTBEAT_RETRY = 5


def _verify_master_request():
    """Confirm a POST to one of the /api/lab-pi/* command endpoints really came
    from the Admin/Master Pi (which must send the same MASTER_API_KEY back in an
    X-Master-Api-Key header), not just anyone on the network who knows the URL.
    If MASTER_API_KEY isn't configured on this Pi yet, allow through (matches
    today's behavior) but warn loudly — once it's set, enforcement is automatic."""
    if not MASTER_API_KEY:
        print("[SECURITY] MASTER_API_KEY is not set in .env — /api/lab-pi/* command "
              "endpoints are accepting unauthenticated requests. Set MASTER_API_KEY "
              "(and have the Admin Pi send it in an X-Master-Api-Key header) to close this.")
        return True
    provided = request.headers.get('X-Master-Api-Key', '')
    return hmac.compare_digest(provided, MASTER_API_KEY)

# Where "return to homepage" links should send students - the booking
# portal lives on the Admin/Master Pi, not on this Lab Pi.
BOOKING_PAGE_URL = f"{MASTER_URL}/my_bookings"

@app.context_processor
def inject_booking_page_url():
    return {'booking_page_url': BOOKING_PAGE_URL}

# Session tracking for heartbeat
current_session_key = None

def send_heartbeat():
    """Send heartbeat to Master Pi (Admin Pi)"""
    global current_session_key
    
    if not MASTER_URL:
        print("No MASTER_URL configured, skipping heartbeat")
        return False
    
    # Get local IP address
    local_ip = "192.168.1.5"  # Default, will be overridden
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((MASTER_URL.replace('http://', '').split(':')[0], 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        pass
    
    # Get system metrics
    cpu_percent = 0.0
    ram_percent = 0.0
    temperature = 0.0
    
    try:
        import psutil
        cpu_percent = psutil.cpu_percent(interval=0.1)
        ram = psutil.virtual_memory()
        ram_percent = ram.percent
        
        # Try to get temperature
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                # Try different temperature sensors
                for key in ['cpu_thermal', 'cpu', 'coretemp', 'scpi_sensors']:
                    if key in temps and temps[key]:
                        temperature = temps[key][0].current
                        break
        except:
            pass
    except Exception as e:
        print(f"[Heartbeat] Error getting system metrics: {e}")
    
    # Get battery status from DFRobot UPS
    battery_percent = 0
    battery_voltage = 0.0
    ac_connected = False
    charging = False
    
    try:
        # Use DYNAMIC path - detect user and project directory automatically
        import subprocess
        result = subprocess.run(['whoami'], capture_output=True, text=True)
        current_user = result.stdout.strip()
        
        # Try both possible locations: ~/lab-pi/ and ~/admin-pi/
        possible_paths = [
            f"/home/{current_user}/lab-pi/battery_status.json",
            f"/home/{current_user}/admin-pi/battery_status.json",
            os.path.expanduser("~/lab-pi/battery_status.json"),
            os.path.expanduser("~/admin-pi/battery_status.json")
        ]
        
        battery_file = None
        for path in possible_paths:
            if os.path.exists(path):
                battery_file = path
                break
        
        if battery_file:
            with open(battery_file, 'r') as f:
                battery_data = json.load(f)
                battery_percent = battery_data.get('soc', 0)
                battery_voltage = battery_data.get('voltage', 0.0)
                ac_status = battery_data.get('ac_status', 'ON_BATTERY')
                charging_status = battery_data.get('charging_status', 'DISCHARGING')
                ac_connected = (ac_status != 'ON_BATTERY')
                charging = (charging_status == 'CHARGING')
                print(f"[Heartbeat] Battery: {battery_percent}% ({battery_voltage}V) - {ac_status} - File: {battery_file}")
        else:
            print(f"[Heartbeat] Battery file not found in any location: {possible_paths}")
    except Exception as e:
        print(f"[Heartbeat] Error reading battery status: {e}")
    
    heartbeat_data = {
        'lab_pi_id': LAB_PI_ID,
        'name': LAB_PI_NAME,
        'ip_address': local_ip,
        'mac_address': LAB_PI_MAC,
        'status': 'ONLINE',
        'session_active': current_session_key is not None,
        'current_session_key': current_session_key,
        # System metrics (matching Admin Pi field names)
        'cpu_usage': cpu_percent,
        'ram_usage': ram_percent,
        'temperature': temperature,
        # Battery metrics (matching Admin Pi field names)
        'battery_soc': battery_percent,
        'battery_voltage': battery_voltage,
        'battery_ac_status': 'AC_CONNECTED' if ac_connected else 'ON_BATTERY',
        'battery_charging': charging
    }
    
    headers = {'X-Lab-Pi-Id': LAB_PI_ID, 'X-Master-Api-Key': MASTER_API_KEY}
    
    for attempt in range(HEARTBEAT_RETRY):
        try:
            response = requests.post(
                f"{MASTER_URL}/api/lab-pi/heartbeat",
                json=heartbeat_data,
                headers=headers,
                timeout=5
            )
            if response.status_code in [200, 201]:
                print(f"[Heartbeat] Sent successfully to {MASTER_URL}")
                
                # Check if Admin Pi sent a new session
                try:
                    resp_data = response.json()
                    if resp_data.get('new_session') and resp_data.get('session'):
                        session_info = resp_data['session']
                        session_key = session_info.get('session_key')
                        
                        if session_key and session_key not in active_sessions:
                            # Parse end_time to get duration
                            end_time_str = session_info.get('end_time')
                            duration = 30  # default 30 minutes
                            if end_time_str:
                                try:
                                    from datetime import datetime
                                    end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
                                    start_time = datetime.fromisoformat(session_info.get('start_time', datetime.utcnow().isoformat()).replace('Z', '+00:00'))
                                    duration = int((end_time - start_time).total_seconds() / 60)
                                except:
                                    pass
                            
                            # Create local session with board_type
                            active_sessions[session_key] = {
                                'start_time': time.time(),
                                'duration': duration,
                                'expires_at': time.time() + (duration * 60),
                                'user_email': session_info.get('user_email'),
                                'booking_id': session_info.get('booking_id'),
                                'board_type': session_info.get('board_type', 'arduino')
                            }
                            current_session_key = session_key
                            print(f"[Heartbeat] New session created: {session_key} (duration: {duration} min)")
                    
                    # Check for board_type update from Admin Pi - real-time sync
                    new_board_type = resp_data.get('board_type')
                    if new_board_type:
                        # Get current session's board_type
                        current_board = active_sessions.get(current_session_key, {}).get('board_type', 'arduino') if current_session_key else None
                        
                        # If board_type changed, update it and notify frontend
                        if current_session_key and new_board_type != current_board:
                            active_sessions[current_session_key]['board_type'] = new_board_type
                            print(f"[Heartbeat] Board type updated to: {new_board_type}")
                            
                            # Emit SocketIO event to notify frontend
                            try:
                                from flask_socketio import emit
                                emit('board_type_updated', {'board_type': new_board_type}, namespace='/')
                                print(f"[SocketIO] Emitted board_type_updated: {new_board_type}")
                            except Exception as e:
                                print(f"[SocketIO] Error emitting board_type_updated: {e}")
                except Exception as e:
                    print(f"[Heartbeat] Error processing response: {e}")
                
                return True
            else:
                print(f"[Heartbeat] Failed with status {response.status_code}: {response.text}")
        except requests.exceptions.RequestException as e:
            print(f"[Heartbeat] Attempt {attempt + 1} failed: {e}")
        
        if attempt < HEARTBEAT_RETRY - 1:
            time.sleep(2)  # Wait before retry
    
    print(f"[Heartbeat] Failed after {HEARTBEAT_RETRY} attempts")
    return False

def register_with_master():
    """Register this Lab Pi with the Master Pi"""
    if not MASTER_URL:
        print("No MASTER_URL configured, skipping registration")
        return False
    
    # Get local IP address
    local_ip = "192.168.1.5"
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((MASTER_URL.replace('http://', '').split(':')[0], 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        pass
    
    registration_data = {
        'lab_pi_id': LAB_PI_ID,
        'name': LAB_PI_NAME,
        'ip_address': local_ip,
        'mac_address': LAB_PI_MAC,
        'experiment_id': int(os.environ.get('EXPERIMENT_ID', 1)),
        'location': os.environ.get('LOCATION', '')
    }
    
    headers = {'X-Lab-Pi-Id': LAB_PI_ID, 'X-Master-Api-Key': MASTER_API_KEY}
    
    for attempt in range(HEARTBEAT_RETRY):
        try:
            response = requests.post(
                f"{MASTER_URL}/api/lab-pi/register",
                json=registration_data,
                headers=headers,
                timeout=5
            )
            if response.status_code in [200, 201]:
                print(f"[Registration] Successfully registered with {MASTER_URL}")
                return True
            elif response.status_code == 409:
                # Already registered, that's fine
                print(f"[Registration] Already registered with {MASTER_URL}")
                return True
            else:
                print(f"[Registration] Failed with status {response.status_code}: {response.text}")
        except requests.exceptions.RequestException as e:
            print(f"[Registration] Attempt {attempt + 1} failed: {e}")
        
        if attempt < HEARTBEAT_RETRY - 1:
            time.sleep(2)
    
    print(f"[Registration] Failed after {HEARTBEAT_RETRY} attempts")
    return False

def heartbeat_loop():
    """Background thread to send heartbeats to Master Pi"""
    # Wait a bit for the server to start
    time.sleep(5)
    
    # First, try to register with master
    print("[Heartbeat] Starting registration with Master Pi...")
    register_with_master()
    
    # Then send heartbeats periodically
    while True:
        try:
            send_heartbeat()
        except Exception as e:
            print(f"[Heartbeat] Error: {e}")
        
        time.sleep(HEARTBEAT_INTERVAL)

# Start heartbeat thread
heartbeat_thread = None

# ---------- RELAY CONTROL ----------
# Keep a persistent handle to GPIO chip to prevent issues with repeated calls
gpio_handle = None
chip = None
line = None  # For gpiod

def init_gpio():
    """Initialize GPIO chip handle"""
    global gpio_handle, chip, line, GPIO_MODE
    
    # If already initialized and handle is valid, return success
    if GPIO_MODE == "lgpio" and gpio_handle is not None and gpio_handle > 0:
        return True
    if GPIO_MODE == "gpiod" and chip is not None:
        return True
    if GPIO_MODE == "rpi" and GPIO is not None:
        return True
    
    if RELAY_PIN is None:
        print("[ERROR] init_gpio: RELAY_PIN is None")
        return False
    
    print(f"[GPIO] Trying to initialize GPIO (RELAY_PIN={RELAY_PIN})...")
    
    # Try lgpio first
    if lgpio is not None:
        try:
            gpio_handle = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_output(gpio_handle, RELAY_PIN)
            GPIO_MODE = "lgpio"
            print(f"[GPIO] ✓ Initialized with lgpio - Pin {RELAY_PIN}")
            return True
        except Exception as e:
            print(f"[GPIO] lgpio failed: {e}")
            gpio_handle = None  # Reset on failure
    
    # Try gpiod second
    if gpiod is not None:
        try:
            chip = gpiod.Chip("gpiochip0")
            line = chip.get_line(RELAY_PIN)
            line.request(consumer="lab-pi", type=gpiod.LINE_REQ_DIR_OUT)
            GPIO_MODE = "gpiod"
            print(f"[GPIO] ✓ Initialized with gpiod - Pin {RELAY_PIN}")
            return True
        except Exception as e:
            print(f"[GPIO] gpiod failed: {e}")
    
    # Try RPi.GPIO as last resort
    if GPIO is not None:
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(RELAY_PIN, GPIO.OUT)
            GPIO_MODE = "rpi"
            print(f"[GPIO] ✓ Initialized with RPi.GPIO - Pin {RELAY_PIN}")
            return True
        except Exception as e:
            print(f"[GPIO] RPi.GPIO failed: {e}")
    
    # Try gpio utility (shell command) as final fallback
    try:
        result = subprocess.run(['gpio', '-g', 'mode', str(RELAY_PIN), 'out'], 
                              capture_output=True, timeout=5)
        if result.returncode == 0:
            GPIO_MODE = "shell"
            print(f"[GPIO] ✓ Initialized with gpio shell - Pin {RELAY_PIN}")
            return True
    except Exception as e:
        print(f"[GPIO] gpio shell failed: {e}")
    
    print("[ERROR] init_gpio: No GPIO library available or all failed")
    return False

def relay_on():
    """Turn the relay ON (power supply to experiments)"""
    global gpio_handle, chip, line, GPIO_MODE
    print(f"[DEBUG] relay_on called, GPIO_MODE={GPIO_MODE}, gpio_handle={gpio_handle}")
    
    # Reset GPIO handle if it was busy or invalid
    if GPIO_MODE == "lgpio":
        try:
            if gpio_handle:
                try:
                    lgpio.gpiochip_close(gpio_handle)
                except:
                    pass
            gpio_handle = None
            GPIO_MODE = None
        except:
            pass
    
    if not init_gpio():
        print("[ERROR] relay_on: GPIO init failed")
        return False
    print(f"[DEBUG] After init_gpio, GPIO_MODE={GPIO_MODE}, gpio_handle={gpio_handle}")
    try:
        if GPIO_MODE == "lgpio":
            lgpio.gpio_write(gpio_handle, RELAY_PIN, 0)  # ACTIVE LOW
        elif GPIO_MODE == "gpiod":
            line.set_value(0)  # ACTIVE LOW
        elif GPIO_MODE == "rpi":
            GPIO.output(RELAY_PIN, GPIO.LOW)  # ACTIVE LOW
        elif GPIO_MODE == "shell":
            subprocess.run(['gpio', '-g', 'write', str(RELAY_PIN), '0'], check=True)
        print("[RELAY] ON - Power supply enabled")
        return True
    except Exception as e:
        print(f"[ERROR] relay_on: {e}")
        return False

def relay_off():
    """Turn the relay OFF (power supply to experiments off)"""
    global gpio_handle, chip, line, GPIO_MODE
    print(f"[DEBUG] relay_off called, GPIO_MODE={GPIO_MODE}, gpio_handle={gpio_handle}")
    
    # Reset GPIO handle if it was busy or invalid
    if GPIO_MODE == "lgpio":
        try:
            if gpio_handle:
                try:
                    lgpio.gpiochip_close(gpio_handle)
                except:
                    pass
            gpio_handle = None
            GPIO_MODE = None
        except:
            pass
    
    if not init_gpio():
        print("[ERROR] relay_off: GPIO init failed")
        return False
    print(f"[DEBUG] After init_gpio, GPIO_MODE={GPIO_MODE}, gpio_handle={gpio_handle}")
    try:
        if GPIO_MODE == "lgpio":
            lgpio.gpio_write(gpio_handle, RELAY_PIN, 1)  # ACTIVE LOW
        elif GPIO_MODE == "gpiod":
            line.set_value(1)  # ACTIVE LOW
        elif GPIO_MODE == "rpi":
            GPIO.output(RELAY_PIN, GPIO.HIGH)  # ACTIVE LOW
        elif GPIO_MODE == "shell":
            subprocess.run(['gpio', '-g', 'write', str(RELAY_PIN), '1'], check=True)
        print("[RELAY] OFF - Power supply disabled")
        return True
    except Exception as e:
        print(f"[ERROR] relay_off: {e}")
        return False

# ---------- UTIL ----------
def list_serial_ports():
    if list_ports is None:
        return []
    all_ports = list_ports.comports()
    # Only show USB serial adapters (student boards); hide onboard UARTs like /dev/ttyAMA*
    # and exclude the Oscilloscope port
    return [
        p.device for p in all_ports
        if p.device != OSC_PORT and re.match(r'^/dev/tty(USB|ACM)\d+$', p.device)
    ]

def _resolved_flash_port(explicit_port):
    """Resolve which device path Flash/Factory-Reset should target: the explicit
    port the client sent (getFlashPort() on the browser side, resolved from the
    Primary-target profile), or — if that's blank — the Primary-target profile's
    own fixed port from admin config. Deliberately never falls back to "whichever
    port the OS enumerated first": that's how a Teacher MCU port ended up getting
    factory-reset instead of the Student MCU — enumeration order isn't board
    identity. Returns (port, error_message); error_message is None on success."""
    explicit_port = (explicit_port or '').strip()
    if explicit_port:
        return explicit_port, None
    primary = next((p for p in get_effective_ui_config().get('serial_ports', []) if p.get('is_primary_target')), None)
    if primary and primary.get('port'):
        return primary['port'], None
    return None, 'No target port resolved — connect to the primary Serial Monitor port first, or set a fixed port for it in Admin Settings.'


def list_by_id_ports(exclude_resolved=None):
    """Stable /dev/serial/by-id paths — these stay pointed at the correct
    physical device across reboots/replugs, unlike /dev/ttyUSB0/ttyACM0 which
    are just assigned by detection order. If a board's bridge chip has no
    unique serial (common with CH340), it won't get its own by-id entry once a
    second identical board is attached — callers always allow typing a custom
    path (e.g. a /dev/serial/by-path/... one) as a fallback for that case."""
    by_id_dir = '/dev/serial/by-id'
    if not os.path.isdir(by_id_dir):
        return []
    choices = []
    for name in sorted(os.listdir(by_id_dir)):
        full = os.path.join(by_id_dir, name)
        resolved = os.path.realpath(full)
        if exclude_resolved and resolved == exclude_resolved:
            continue
        choices.append({'path': full, 'label': name, 'resolved': resolved})
    return choices


def list_admin_port_choices():
    """By-id choices for the generic Serial Port and Debug probe pickers —
    excludes whatever raw device the Oscilloscope currently owns, since
    neither a serial-port profile nor the debug probe may ever be the same
    physical device as the always-on Oscilloscope worker."""
    return list_by_id_ports(exclude_resolved=OSC_PORT)

@app.route('/')
def index():
    # Provide default values for session variables
    return render_template('index.html', session_duration=0, session_end_time=0, board_type='arduino', ui_config=get_student_ui_config())

@app.route('/experiment')
def experiment():
    session_key = request.args.get('key')
    session_end_time_param = request.args.get('end_time')  # Optional: session end time from admin-pi
    
    print(f"[Experiment] Loading experiment page, session_key={session_key}, end_time_param={session_end_time_param}")
    
    if not session_key:
        return render_template('expired_session.html')

    # For testing purposes: if session key is 'testpop123', create a temporary session
    if session_key == 'testpop123' and session_key not in active_sessions:
        active_sessions[session_key] = {
            'start_time': time.time(),
            'duration': 0.08,  # 5 seconds
            'expires_at': time.time() + (0.08 * 60)
        }

    # Clean up expired sessions
    current_time = time.time()
    expired_keys = [k for k, v in active_sessions.items() if current_time > v['expires_at']]
    for k in expired_keys:
        del active_sessions[k]
        # Turn relay OFF when session expires
        subprocess.run(['python3', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'relay_control.py'), 'off'], capture_output=True)

    # If session doesn't exist but we have session_end_time param, create it
    if session_key not in active_sessions and session_end_time_param:
        try:
            session_end_time_ms = int(session_end_time_param)
            expires_at = session_end_time_ms / 1000
            duration_minutes = (expires_at - time.time()) / 60
            
            print(f"[Experiment] Creating session from URL: expires_at={expires_at}, duration_minutes={duration_minutes}, current_time={current_time}")
            
            if duration_minutes > 0:
                # Fetch board_type from Admin Pi via heartbeat or use default
                board_type = 'arduino'
                try:
                    if MASTER_URL:
                        resp = requests.get(f"{MASTER_URL}/api/lab-pi/{LAB_PI_ID}/board-config", timeout=3)
                        if resp.status_code == 200:
                            board_type = resp.json().get('board_type', 'arduino')
                except:
                    pass
                
                active_sessions[session_key] = {
                    'start_time': time.time(),
                    'duration': duration_minutes,
                    'expires_at': expires_at,
                    'board_type': board_type
                }
                print(f"[Session] Created from URL param: {session_key}, expires at {expires_at}, board_type: {board_type}")
            else:
                print(f"[Session] Session from URL already expired: duration_minutes={duration_minutes}")
        except (ValueError, TypeError) as e:
            print(f"[Session] Error parsing session_end_time: {e}")

    if session_key not in active_sessions:
        print(f"[Experiment] Session not found: {session_key}, active_sessions={list(active_sessions.keys())}")
        return render_template('expired_session.html')
    
    print(f"[Experiment] Session found: {session_key}, expires_at={active_sessions[session_key]['expires_at']}")
    duration = active_sessions[session_key]['duration']
    session_end_time = int(active_sessions[session_key]['expires_at'] * 1000)  # JS milliseconds
    board_type = active_sessions[session_key].get('board_type', 'arduino')
    return render_template('index.html', session_duration=duration, session_end_time=session_end_time, board_type=board_type, ui_config=get_student_ui_config())

@app.route('/add_session', methods=['POST'])
def add_session():
    data = request.get_json()
    session_key = data.get('session_key')
    duration = data.get('duration', 5)
    if session_key:
        active_sessions[session_key] = {
            'start_time': time.time(),
            'duration': duration,
            'expires_at': time.time() + (duration * 60)
        }
        # Do NOT turn relay ON automatically when session starts
    return jsonify({'status': 'added'})

@app.route('/api/lab-pi/session-start', methods=['POST'])
def api_lab_pi_session_start():
    """
    Receive session start command from Admin Pi (Master).
    Called when a user starts an experiment session.
    """
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    global current_session_key

    data = request.get_json()
    session_key = data.get('session_key')
    booking_id = data.get('booking_id')
    user_email = data.get('user_email')
    session_end_time_ms = data.get('session_end_time')  # End time in JS milliseconds
    
    if not session_key:
        return jsonify({'error': 'Missing session_key'}), 400
    
    # Calculate duration from session_end_time if provided, otherwise use default
    if session_end_time_ms:
        # Convert JS milliseconds to Unix timestamp
        expires_at = session_end_time_ms / 1000
        duration = (expires_at - time.time()) / 60  # Duration in minutes
    else:
        # Default session duration from Admin Pi
        duration = 30  # Default 30 minutes
        expires_at = time.time() + (duration * 60)
    
    # Create local session
    active_sessions[session_key] = {
        'start_time': time.time(),
        'duration': duration,
        'expires_at': expires_at,
        'user_email': user_email,
        'booking_id': booking_id
    }
    
    current_session_key = session_key
    
    print(f"[Session] Started: {session_key} for user {user_email}, expires at {expires_at}")
    
    return jsonify({'status': 'success', 'session_key': session_key})

@app.route('/remove_session', methods=['POST'])
def remove_session():
    data = request.get_json()
    session_key = data.get('session_key')
    if session_key in active_sessions:
        del active_sessions[session_key]
        # Turn relay OFF automatically when session is removed to save power
        relay_off()
    return jsonify({'status': 'removed'})

@app.route('/api/lab-pi/session-end', methods=['POST'])
def api_lab_pi_session_end():
    """
    Receive session end command from Admin Pi (Master).
    Called when a session is terminated or expires.
    """
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    global current_session_key

    data = request.get_json()
    session_key = data.get('session_key')
    
    if session_key and session_key in active_sessions:
        del active_sessions[session_key]
        if current_session_key == session_key:
            current_session_key = None
        # Turn relay OFF when session ends
        relay_off()
        # A debug session still holds the USB probe open even after the
        # booking that started it ends — free it for the next student.
        _teardown_debug_session()
        print(f"[Session] Ended: {session_key}")

    return jsonify({'status': 'success'})

@app.route('/api/lab-pi/update-config', methods=['POST'])
def api_lab_pi_update_config():
    """
    Receive real-time configuration updates from Admin Pi.
    Used for instant board_type, experiment, and SOP updates without service restart.
    """
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json()
    
    # Update board_type if provided
    if 'board_type' in data:
        board_type = data['board_type']
        
        # Update all active sessions with new board_type
        if current_session_key and current_session_key in active_sessions:
            active_sessions[current_session_key]['board_type'] = board_type
            print(f"[Config] Board type updated to: {board_type}")
            
            # Emit SocketIO event to notify frontend immediately
            try:
                from flask import Flask as FlaskClass
                from flask_socketio import emit
                socketio.emit('board_type_updated', {'board_type': board_type})
                print(f"[Config] Emitted board_type_updated via SocketIO")
            except Exception as e:
                print(f"[Config] SocketIO emit error: {e}")
        
        # Also store in a global variable for immediate access
        global current_board_type
        current_board_type = board_type
    
    # Update experiment_id if provided
    if 'experiment_id' in data:
        global current_experiment_id
        current_experiment_id = data['experiment_id']
        print(f"[Config] Experiment ID updated to: {current_experiment_id}")
    
    # Update SOP file if provided
    if 'sop_file' in data:
        global current_sop_file
        current_sop_file = data['sop_file']
        print(f"[Config] SOP file updated to: {current_sop_file}")
    
    return jsonify({'status': 'success', 'message': 'Configuration updated'})

# Global variables for current config
current_board_type = 'arduino'
current_experiment_id = None
current_sop_file = None

# ---------- ADMIN AUTH & UI CONFIG ----------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    setup_mode = not has_admin_password_configured()
    error = None
    if request.method == 'POST':
        if setup_mode:
            password = request.form.get('password', '')
            password_confirm = request.form.get('password_confirm', '')
            if len(password) < 8:
                error = 'Password must be at least 8 characters.'
            elif password != password_confirm:
                error = 'Passwords do not match.'
            else:
                set_admin_password(password)
                session['is_admin'] = True
                return redirect(url_for('admin_settings'))
        else:
            password = request.form.get('password', '')
            if verify_admin_password(password):
                session['is_admin'] = True
                next_path = request.args.get('next') or url_for('admin_settings')
                return redirect(next_path)
            error = 'Incorrect password.'
    return render_template('admin_login.html', setup_mode=setup_mode, error=error)

@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))

@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    saved = False
    if request.method == 'POST':
        new_controls = {key: (request.form.get(f'control_{key}') == 'on') for key, _ in CONTROL_KEYS}
        main_view = request.form.get('main_view')
        if main_view not in ('plotter', 'oscilloscope'):
            main_view = 'plotter'
        required_prefixes = [
            kw.strip() for kw in (request.form.get('serial_plotter_required_prefixes') or '').split(',')
            if kw.strip()
        ]
        new_defaults = {
            'main_view': main_view,
            'dynamic_controls_visible': request.form.get('dynamic_controls_visible') == 'on',
            'serial_plotter_allow_port_switch': request.form.get('serial_plotter_allow_port_switch') == 'on',
            'serial_plotter_default_port_id': (request.form.get('serial_plotter_default_port_id') or '').strip(),
            'serial_plotter_required_prefixes': required_prefixes,
        }
        experiment_name = (request.form.get('experiment_name') or '').strip()
        cfg = save_ui_config(new_controls, new_defaults, experiment_name=experiment_name)
        socketio.emit('ui_config_updated', get_student_ui_config())
        sync_serial_profiles()
        saved = True

    cfg = get_effective_ui_config()
    return render_template(
        'admin_settings.html', cfg=cfg, control_keys=CONTROL_KEYS, available_ports=list_admin_port_choices(),
        env_password_locked=password_locked_by_env(), saved=saved, password_error=None, port_error=None,
    )

@app.route('/admin/settings/password', methods=['POST'])
@admin_required
def admin_change_password():
    if password_locked_by_env():
        return redirect(url_for('admin_settings'))
    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    new_password_confirm = request.form.get('new_password_confirm', '')
    if not verify_admin_password(current_password):
        error = 'Current password is incorrect.'
    elif len(new_password) < 8:
        error = 'New password must be at least 8 characters.'
    elif new_password != new_password_confirm:
        error = 'New passwords do not match.'
    else:
        set_admin_password(new_password)
        return redirect(url_for('admin_settings'))

    cfg = get_effective_ui_config()
    return render_template(
        'admin_settings.html', cfg=cfg, control_keys=CONTROL_KEYS, available_ports=list_admin_port_choices(),
        env_password_locked=password_locked_by_env(), saved=False, password_error=error, port_error=None,
    )

def _required_control_from_form(form):
    rc_type = form.get('rc_type', '')
    label = (form.get('rc_label') or '').strip()
    if rc_type not in ('slider', 'button', 'readout') or not label:
        return None
    control = {'type': rc_type, 'label': label}
    # Which serial-port profile this control talks to. Blank = the primary
    # target (default, unchanged behavior); set explicitly to lock a control
    # to a specific board — e.g. a required slider pinned to the Teacher MCU
    # while a student's own controls keep going to the primary/student MCU.
    control['portId'] = (form.get('rc_port_id') or '').strip()
    if rc_type == 'slider':
        try:
            control['min'] = float(form.get('rc_min', 0))
        except (TypeError, ValueError):
            control['min'] = 0
        try:
            control['max'] = float(form.get('rc_max', 1023))
        except (TypeError, ValueError):
            control['max'] = 1023
        try:
            control['precision'] = max(0, min(2, int(form.get('rc_precision', 0))))
        except (TypeError, ValueError):
            control['precision'] = 0
        control['dataKey'] = label.lower().replace(' ', '_')
        control['cmdFormat'] = (form.get('rc_cmd_format') or '').strip() or '{value}'
    elif rc_type == 'button':
        control['onCmd'] = form.get('rc_on_cmd', '1')
        control['offCmd'] = form.get('rc_off_cmd', '0')
        # 'toggle' (default) flips between onCmd/offCmd per click; 'momentary'
        # sends pressCmd while held down and releaseCmd when let go.
        mode = form.get('rc_button_mode', 'toggle')
        control['buttonMode'] = mode if mode in ('toggle', 'momentary') else 'toggle'
        control['pressCmd'] = form.get('rc_press_cmd', '1')
        control['releaseCmd'] = form.get('rc_release_cmd', '0')
    elif rc_type == 'readout':
        control['dataKey'] = (form.get('rc_data_key') or label.lower().replace(' ', '_')).strip()
        # 'unit' is a LaTeX source string (e.g. ^\circ\text{C}), rendered client-side with KaTeX.
        control['unit'] = form.get('rc_unit', '')
        control['decimals'] = form.get('rc_decimals', '')
    return control


@app.route('/admin/settings/controls/add', methods=['POST'])
@admin_required
def admin_add_required_control():
    control = _required_control_from_form(request.form)
    if control:
        add_required_control(control)
        socketio.emit('ui_config_updated', get_student_ui_config())
    return redirect(url_for('admin_settings'))

@app.route('/admin/settings/controls/edit', methods=['POST'])
@admin_required
def admin_edit_required_control():
    control_id = request.form.get('control_id', '')
    control = _required_control_from_form(request.form)
    if control_id and control:
        update_required_control(control_id, control)
        socketio.emit('ui_config_updated', get_student_ui_config())
    return redirect(url_for('admin_settings'))

@app.route('/admin/settings/controls/delete', methods=['POST'])
@admin_required
def admin_delete_required_control():
    control_id = request.form.get('control_id', '')
    if control_id:
        delete_required_control(control_id)
        socketio.emit('ui_config_updated', get_student_ui_config())
    return redirect(url_for('admin_settings'))

def _truthy(value):
    """True for an HTML checkbox's 'on' (request.form) or a real JSON boolean
    (request.get_json(), from the Master's admin API) — same field, two callers."""
    return value in ('on', True)


def _serial_port_profile_from_form(form):
    try:
        baud = int(form.get('sp_baud', 115200))
    except (TypeError, ValueError):
        baud = 115200
    return {
        'label': (form.get('sp_label') or '').strip(),
        'port': (form.get('sp_port') or '').strip(),
        'baud': baud,
        'student_visible': _truthy(form.get('sp_student_visible')),
        'auto_connect': _truthy(form.get('sp_auto_connect')),
        'allow_disconnect': _truthy(form.get('sp_allow_disconnect')),
        'is_primary_target': _truthy(form.get('sp_is_primary_target')),
    }


def _osc_port_conflicts(port_value):
    # The Oscilloscope owns its own auto-detected port and is never something
    # students connect to or reconfigure via Serial Monitor/Plotter — reject
    # any profile that would collide with it instead of silently failing later.
    return bool(port_value and OSC_PORT and port_value == OSC_PORT)


def _osc_port_conflict_response(port_value):
    if not _osc_port_conflicts(port_value):
        return None
    cfg = get_effective_ui_config()
    return render_template(
        'admin_settings.html', cfg=cfg, control_keys=CONTROL_KEYS, available_ports=list_admin_port_choices(),
        env_password_locked=password_locked_by_env(), saved=False, password_error=None,
        port_error=f'{port_value} is the Oscilloscope\'s port (auto-detected) and cannot be reused here.',
    )


@app.route('/admin/settings/ports/add', methods=['POST'])
@admin_required
def admin_add_serial_port():
    profile = _serial_port_profile_from_form(request.form)
    if profile['label']:
        conflict = _osc_port_conflict_response(profile['port'])
        if conflict:
            return conflict
        add_serial_port(profile)
        socketio.emit('ui_config_updated', get_student_ui_config())
        sync_serial_profiles()
    return redirect(url_for('admin_settings'))

@app.route('/admin/settings/ports/edit', methods=['POST'])
@admin_required
def admin_edit_serial_port():
    port_id = request.form.get('port_id', '')
    profile = _serial_port_profile_from_form(request.form)
    if port_id and profile['label']:
        conflict = _osc_port_conflict_response(profile['port'])
        if conflict:
            return conflict
        update_serial_port(port_id, profile)
        # Port/baud may have changed under the same id — force a reconnect with
        # the new settings instead of leaving a stale connection open.
        _close_connection(port_id)
        socketio.emit('ui_config_updated', get_student_ui_config())
        sync_serial_profiles()
    return redirect(url_for('admin_settings'))

@app.route('/admin/settings/ports/delete', methods=['POST'])
@admin_required
def admin_delete_serial_port():
    port_id = request.form.get('port_id', '')
    if port_id:
        delete_serial_port(port_id)
        socketio.emit('ui_config_updated', get_student_ui_config())
        sync_serial_profiles()
    return redirect(url_for('admin_settings'))


# ---------- MASTER-FACING ADMIN CONFIG API (Tier A — X-Master-Api-Key only) ----------
# JSON equivalents of the /admin/settings* HTML routes above, so the Master PC's
# admin console can edit this Lab Pi's UI/layout config without an admin ever
# visiting this Pi's own IP. Never called by a browser — see LAB_PI_API_CONTRACT.md §5.

@app.route('/api/admin/ui-config', methods=['GET'])
def api_admin_get_ui_config():
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    cfg = get_effective_ui_config()
    cfg['control_keys'] = [{'key': key, 'label': label} for key, label in CONTROL_KEYS]
    cfg['available_ports'] = list_admin_port_choices()
    # Unlike available_ports, the Oscilloscope's own port picker must be able
    # to show (and re-select) whatever device is currently OSC_PORT itself.
    cfg['osc_available_ports'] = list_by_id_ports()
    return jsonify(cfg)


@app.route('/api/admin/ui-config', methods=['POST'])
def api_admin_save_ui_config():
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json() or {}
    new_controls = {key: bool(data.get('controls', {}).get(key, True)) for key, _ in CONTROL_KEYS}
    defaults = data.get('defaults', {}) or {}
    main_view = defaults.get('main_view')
    if main_view not in ('plotter', 'oscilloscope'):
        main_view = 'plotter'
    required_prefixes = [kw.strip() for kw in defaults.get('serial_plotter_required_prefixes', []) if str(kw).strip()]
    new_defaults = {
        'main_view': main_view,
        'dynamic_controls_visible': bool(defaults.get('dynamic_controls_visible')),
        'serial_plotter_allow_port_switch': bool(defaults.get('serial_plotter_allow_port_switch')),
        'serial_plotter_default_port_id': (defaults.get('serial_plotter_default_port_id') or '').strip(),
        'serial_plotter_required_prefixes': required_prefixes,
        'debug_board_id': (defaults.get('debug_board_id') or '').strip(),
        'debug_port': (defaults.get('debug_port') or '').strip(),
        'osc_board_id': (defaults.get('osc_board_id') or '').strip(),
        'osc_port': (defaults.get('osc_port') or '').strip(),
    }
    experiment_name = (data.get('experiment_name') or '').strip()
    osc_port_changed = new_defaults['osc_port'] != load_ui_config().get('defaults', {}).get('osc_port', '')
    cfg = save_ui_config(new_controls, new_defaults, experiment_name=experiment_name)
    if osc_port_changed:
        _reset_osc_connection()
    socketio.emit('ui_config_updated', get_student_ui_config())
    sync_serial_profiles()
    return jsonify(cfg)


@app.route('/api/admin/controls', methods=['POST'])
def api_admin_add_control():
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    control = _required_control_from_form(request.get_json() or {})
    if not control:
        return jsonify({'error': 'invalid control'}), 400
    control = add_required_control(control)
    socketio.emit('ui_config_updated', get_student_ui_config())
    return jsonify(control)


@app.route('/api/admin/controls/<control_id>', methods=['PUT'])
def api_admin_edit_control(control_id):
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    control = _required_control_from_form(request.get_json() or {})
    if not control:
        return jsonify({'error': 'invalid control'}), 400
    updated = update_required_control(control_id, control)
    if updated is None:
        return jsonify({'error': 'not found'}), 404
    socketio.emit('ui_config_updated', get_student_ui_config())
    return jsonify(updated)


@app.route('/api/admin/controls/<control_id>', methods=['DELETE'])
def api_admin_delete_control(control_id):
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    delete_required_control(control_id)
    socketio.emit('ui_config_updated', get_student_ui_config())
    return jsonify({'status': 'success'})


@app.route('/api/admin/ports', methods=['POST'])
def api_admin_add_port():
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    profile = _serial_port_profile_from_form(request.get_json() or {})
    if not profile['label']:
        return jsonify({'error': 'label is required'}), 400
    if _osc_port_conflicts(profile['port']):
        return jsonify({'error': f'{profile["port"]} is the Oscilloscope\'s port (auto-detected) and cannot be reused here.'}), 400
    profile = add_serial_port(profile)
    socketio.emit('ui_config_updated', get_student_ui_config())
    sync_serial_profiles()
    return jsonify(profile)


@app.route('/api/admin/ports/<port_id>', methods=['PUT'])
def api_admin_edit_port(port_id):
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    profile = _serial_port_profile_from_form(request.get_json() or {})
    if not profile['label']:
        return jsonify({'error': 'label is required'}), 400
    if _osc_port_conflicts(profile['port']):
        return jsonify({'error': f'{profile["port"]} is the Oscilloscope\'s port (auto-detected) and cannot be reused here.'}), 400
    updated = update_serial_port(port_id, profile)
    if updated is None:
        return jsonify({'error': 'not found'}), 404
    # Port/baud may have changed under the same id — force a reconnect with
    # the new settings instead of leaving a stale connection open.
    _close_connection(port_id)
    socketio.emit('ui_config_updated', get_student_ui_config())
    sync_serial_profiles()
    return jsonify(updated)


@app.route('/api/admin/ports/<port_id>', methods=['DELETE'])
def api_admin_delete_port(port_id):
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    delete_serial_port(port_id)
    socketio.emit('ui_config_updated', get_student_ui_config())
    sync_serial_profiles()
    return jsonify({'status': 'success'})

@app.route('/test_gpio', methods=['GET'])
def test_gpio():
    """Test GPIO initialization and return debug info"""
    debug_info = {
        'lgpio_available': lgpio is not None,
        'gpiod_available': gpiod is not None,
        'rpi_gpio_available': GPIO is not None,
        'relay_pin': RELAY_PIN,
        'gpio_mode': GPIO_MODE,
        'gpio_handle': gpio_handle is not None,
        'chip': chip is not None
    }
    
    # Try to initialize GPIO
    init_result = init_gpio()
    debug_info['init_result'] = init_result
    debug_info['gpio_mode'] = GPIO_MODE
    
    # Try to turn on relay
    if init_result:
        relay_on()
        relay_off()
    
    return jsonify(debug_info)

@app.route('/toggle_relay', methods=['POST'])
def toggle_relay():
    """Toggle the relay ON or OFF using external script"""
    data = request.get_json()
    state = data.get('state')
    session_key = data.get('session_key')
    bypass_auth = data.get('bypass', False)
    
    # Check if session is valid
    if not bypass_auth and session_key not in active_sessions:
        print(f"[WARNING] toggle_relay called without valid session: {session_key}")
    
    if state not in ['on', 'off']:
        return jsonify({'status': 'error', 'message': 'Invalid state'}), 400
    
    # Call the relay control script
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'relay_control.py')
    try:
        result = subprocess.run(
            ['python3', script_path, state],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print(f"[RELAY] {state.upper()} - {result.stdout.strip()}")
            return jsonify({'status': state})
        else:
            print(f"[ERROR] relay {state} failed: {result.stderr}")
            return jsonify({'status': 'error', 'message': f'Relay {state} failed: Check GPIO connection'}), 500
    except Exception as e:
        print(f"[ERROR] toggle_relay: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/chart')
def chart():
    # Clean up expired sessions
    current_time = time.time()
    expired_keys = [k for k, v in active_sessions.items() if current_time > v['expires_at']]
    for k in expired_keys:
        del active_sessions[k]
        # Turn relay OFF when session expires
        subprocess.run(['python3', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'relay_control.py'), 'off'], capture_output=True)

    session_key = request.args.get('key')
    if not session_key or session_key not in active_sessions:
        return render_template('expired_session.html')
    return render_template('chart.html')

@app.route('/newchart')
def newchart():
    # Clean up expired sessions
    current_time = time.time()
    expired_keys = [k for k, v in active_sessions.items() if current_time > v['expires_at']]
    for k in expired_keys:
        del active_sessions[k]
        subprocess.run(['python3', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'relay_control.py'), 'off'], capture_output=True)

    session_key = request.args.get('key')
    board_type = 'arduino'
    
    # Try to get board_type from session if it exists
    if session_key and session_key in active_sessions:
        board_type = active_sessions[session_key].get('board_type', 'arduino')
    
    return render_template('newchart.html', board_type=board_type, ui_config=get_student_ui_config())

@app.route('/oscilloscope')
def oscilloscope():
    # Clean up expired sessions
    current_time = time.time()
    expired_keys = [k for k, v in active_sessions.items() if current_time > v['expires_at']]
    for k in expired_keys:
        del active_sessions[k]
        subprocess.run(['python3', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'relay_control.py'), 'off'], capture_output=True)

    session_key = request.args.get('key')
    if not session_key or session_key not in active_sessions:
        return render_template('expired_session.html')
    
    return render_template('oscilloscope.html')

@app.route('/api/latest-sensor-data')
def api_latest_sensor_data():
    """Return latest sensor data for CRO page polling"""
    return jsonify(latest_sensor_data)

@app.route('/api/ui-config')
def api_ui_config():
    """The admin-configured UI restrictions (which controls/boards are
    enabled, serial port profiles, required controls, experiment name) as
    JSON, so the Master PC can render this Lab Pi's real experiment page
    instead of this Lab Pi doing it. Master-only — never a browser, since
    get_student_ui_config() is what's safe to hand to a student's page, but
    the fetch itself should only ever be made server-side by the Master."""
    if not _verify_master_request():
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify(get_student_ui_config())

@app.route('/camera')
def camera():
    # Clean up expired sessions
    current_time = time.time()
    expired_keys = [k for k, v in active_sessions.items() if current_time > v['expires_at']]
    for k in expired_keys:
        del active_sessions[k]
        # Turn relay OFF when session expires
        subprocess.run(['python3', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'relay_control.py'), 'off'], capture_output=True)

    session_key = request.args.get('key')
    if not session_key or session_key not in active_sessions:
        return render_template('expired_session.html')
    return render_template('camera.html')

@app.route('/homepage')
def homepage():
    return render_template('index.html', ui_config=get_student_ui_config())

@app.route('/ports')
def ports_rest():
    return jsonify({'ports': list_serial_ports()})

# ---------- FLASH ----------
def _flash_commands(board, port, fw_path):
    """Argv lists (never a shell string) for each board's flash command, so
    `port` — which comes straight from the client — can never be parsed as
    shell syntax no matter what a caller puts in it."""
    python_exec = sys.executable
    return {
        'esp32': [python_exec, '-m', 'esptool', '--chip', 'esp32', '--port', port, '--baud', '115200', '--before', 'default_reset', 'write_flash', '0x10000', fw_path],
        'esp8266': [python_exec, '-m', 'esptool', '--chip', 'esp8266', '--port', port, '--baud', '115200', '--before', 'default_reset', 'write_flash', '0x00000', fw_path],
        'arduino': ['avrdude', '-v', '-p', 'atmega328p', '-c', 'arduino', '-P', port, '-b115200', '-D', '-U', f'flash:w:{fw_path}:i'],
        'attiny': ['avrdude', '-v', '-p', 'attiny85', '-c', 'usbasp', '-P', port, '-U', f'flash:w:{fw_path}:i'],
        'stm32': ['openocd', '-f', 'interface/stlink.cfg', '-f', 'target/stm32f4x.cfg', '-c', f'program {fw_path} 0x08000000 verify reset exit'],
        'nucleo_f446re': ['openocd', '-f', 'interface/stlink.cfg', '-f', 'target/stm32f4x.cfg', '-c', f'program {fw_path} 0x08000000 verify reset exit'],
        'black_pill': ['openocd', '-f', 'interface/stlink.cfg', '-f', 'target/stm32f4x.cfg', '-c', f'program {fw_path} 0x08000000 verify reset exit'],
        'msp430': ['mspdebug', 'rf2500', f'prog {fw_path}'],
        'tiva': ['openocd', '-f', 'board/ti_ek-tm4c123gxl.cfg', '-c', f'program {fw_path} verify reset exit'],
        'tms320f28377s': ['python3', 'dsp/flash_tool.py', fw_path],
        'generic': ['echo', f'No flashing command configured for {board}. Firmware at {fw_path}'],
    }


@app.route('/flash', methods=['POST'])
def flash():
    if not is_control_enabled('flash_firmware'):
        return jsonify({'status': 'error', 'message': 'Flash Firmware is disabled for this session'}), 403
    if current_session_key is None:
        return jsonify({'status': 'error', 'message': 'No active experiment session'}), 403
    board = request.form.get('board', 'generic')
    if not is_control_enabled('board_select'):
        board = current_board_type or board
    port, port_err = _resolved_flash_port(request.form.get('port', ''))
    if port_err:
        return jsonify({'status': port_err}), 400
    fw = request.files.get('firmware')
    if not fw:
        return jsonify({'status': 'No firmware uploaded'}), 400
    fname = secure_filename(fw.filename)
    dest = os.path.join(UPLOAD_DIR, fname)
    fw.save(dest)

    commands = _flash_commands(board, port, dest)
    cmd = commands.get(board, commands['generic'])
    socketio.start_background_task(run_flash_command, cmd, fname)
    return jsonify({'status': f'Flashing started for {board}', 'command': ' '.join(cmd)})


@app.route('/debug/upload-elf', methods=['POST'])
def debug_upload_elf():
    """Save the ELF a student uploads for GDB symbol/source-level debugging.
    Deliberately never accepts a filesystem path from the browser (see
    handle_debug_load_symbols) -- students have no visibility into this Pi's
    filesystem, so the only path a session ever debugs against is this fixed
    one, written here from an actual upload."""
    if current_session_key is None:
        return jsonify({'status': 'No active experiment session'}), 403
    elf = request.files.get('elf')
    if not elf or not elf.filename:
        return jsonify({'status': 'No ELF file uploaded'}), 400
    if not elf.filename.lower().endswith(('.elf', '.out')):
        return jsonify({'status': 'Expected a .elf (or .out) file with debug symbols'}), 400
    elf.save(DEBUG_ELF_PATH)
    return jsonify({'status': 'ok', 'filename': secure_filename(elf.filename)})


FLASH_TIMEOUT_SEC = 90
# OpenOCD output that means the probe/flash failed even if it exited 0.
# ("Error: SRST error" is normal for the Tiva ICDI and deliberately not here.)
_FLASH_FAILURE_PATTERNS = ('Error TX Data', 'open failed')
# TI Stellaris/Tiva In-Circuit Debug Interface (EK-TM4C123GXL onboard probe)
_ICDI_USB_ID = ('1cbe', '00fd')


def _usb_reset_icdi():
    """Software-replug a wedged ICDI probe (USBDEVFS_RESET) so the next
    OpenOCD can talk to it. Needs write access to /dev/bus/usb/BBB/DDD,
    which the plugdev group has via 60-openocd.rules -- no sudo. Only
    resets when exactly one ICDI is attached, so it never knocks another
    board's probe off the bus. Returns (ok, human-readable message)."""
    import fcntl
    USBDEVFS_RESET = 0x5514  # _IO('U', 20)
    matches = []
    for dev in glob.glob('/sys/bus/usb/devices/*'):
        try:
            with open(os.path.join(dev, 'idVendor')) as f:
                vid = f.read().strip()
            with open(os.path.join(dev, 'idProduct')) as f:
                pid = f.read().strip()
            if (vid, pid) != _ICDI_USB_ID:
                continue
            with open(os.path.join(dev, 'busnum')) as f:
                bus = int(f.read())
            with open(os.path.join(dev, 'devnum')) as f:
                devnum = int(f.read())
            matches.append(f'/dev/bus/usb/{bus:03d}/{devnum:03d}')
        except (OSError, ValueError):
            continue
    if len(matches) != 1:
        return False, f'USB reset skipped: found {len(matches)} ICDI probes (need exactly 1)'
    try:
        fd = os.open(matches[0], os.O_WRONLY)
        try:
            fcntl.ioctl(fd, USBDEVFS_RESET, 0)
        finally:
            os.close(fd)
    except OSError as e:
        return False, f'USB reset of {matches[0]} failed: {e}'
    # Give the probe time to re-enumerate before OpenOCD opens it again.
    eventlet.sleep(2)
    return True, f'USB reset of ICDI probe {matches[0]} OK'


def _run_flash_once(cmd):
    """Run one flash attempt, streaming output.
    Returns (rc, timed_out, failure_pattern_or_None, last_output_lines)."""
    tail = collections.deque(maxlen=15)
    hit = None
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    timed_out = False
    try:
        with eventlet.Timeout(FLASH_TIMEOUT_SEC):
            for line in iter(p.stdout.readline, ''):
                if line is None:
                    continue
                line = line.strip()
                tail.append(line)
                if hit is None:
                    hit = next((pat for pat in _FLASH_FAILURE_PATTERNS if pat in line), None)
                socketio.emit('flashing_status', line)
            p.wait()
    except eventlet.Timeout:
        timed_out = True
        p.kill()
        p.wait()
    return p.returncode, timed_out, hit, list(tail)


def run_flash_command(cmd, filename=None):
    try:
        # The debugger's OpenOCD holds the same probe the flasher needs.
        _release_debug_probe('flashing firmware')
        socketio.emit('flashing_status', f"Starting: {' '.join(cmd)}")
        is_openocd = os.path.basename(cmd[0]) == 'openocd'
        for attempt in (1, 2):
            rc, timed_out, hit, tail = _run_flash_once(cmd)
            if rc == 0 and not timed_out and hit is None:
                socketio.emit('flashing_status', f'✅ Flashing completed successfully (file: {filename})')
                return
            # A wedged ICDI (e.g. after the debugger crashed) fails every
            # libusb transfer until it's replugged -- try that in software once.
            if attempt == 1 and is_openocd and hit == 'Error TX Data':
                reset_ok, result = _usb_reset_icdi()
                socketio.emit('flashing_status', f'Probe not responding (Error TX Data); {result}')
                if reset_ok:
                    socketio.emit('flashing_status', 'Retrying flash once...')
                    continue
            break
        if timed_out:
            reason = f'timed out after {FLASH_TIMEOUT_SEC}s'
        elif hit is not None:
            reason = f'"{hit}" in output'
        else:
            reason = 'non-zero exit'
        last = '\n'.join(tail[-8:])
        socketio.emit('flashing_status',
                      f'❌ Flashing FAILED ({reason}, exit code {rc}) (file: {filename})\nLast output:\n{last}')
    except Exception as e:
        socketio.emit('flashing_status', f'Error while flashing: {e}')

# ---------- FACTORY RESET ENDPOINT ----------
# Expects JSON or form { "board": "esp32" }
# Finds corresponding default firmware file under DEFAULT_FW_DIR and calls run_flash_command
@app.route('/factory_reset', methods=['POST'])
def factory_reset():
    if not is_control_enabled('factory_reset'):
        return jsonify({'error': 'Factory Reset is disabled for this session'}), 403
    if current_session_key is None:
        return jsonify({'error': 'No active experiment session'}), 403
    try:
        data = request.get_json(force=True)
    except:
        data = request.form.to_dict()
    board = (data.get('board') or 'generic').lower()
    if not is_control_enabled('board_select'):
        board = (current_board_type or board).lower()

    # mapping from board -> default filename in DEFAULT_FW_DIR
    default_map = {
        'esp32': 'esp32_default.bin',
        'esp8266': 'esp32_default.bin',
        'arduino': 'arduino_default.hex',
        'attiny': 'attiny_default.hex',
        'stm32': 'stm32_default.bin',
        'nucleo_f446re': 'stm32_default.bin',
        'black_pill': 'stm32_default.bin',
        'msp430': 'generic_default.bin',
        'tiva': 'tiva_default.out',
        'tms320f28377s': 'tms320f28377s_default.out',
        'generic': 'generic_default.bin'
    }

    fname = default_map.get(board, default_map['generic'])
    fpath = os.path.join(DEFAULT_FW_DIR, fname)
    if not os.path.isfile(fpath):
        return jsonify({'error': f'Default firmware not found for board {board}: expected {fpath}'}), 404

    # choose command based on board (similar to /flash)
    port, port_err = _resolved_flash_port(data.get('port'))
    if port_err:
        return jsonify({'error': port_err}), 400
    commands = _flash_commands(board, port, fpath)
    cmd = commands.get(board, commands['generic'])
    socketio.start_background_task(run_flash_command, cmd, fname)
    return jsonify({'status': f'Factory reset started for {board}', 'command': ' '.join(cmd)})

# ---------- SOP DOWNLOAD ----------
# Serve SOP file(s) from static/sop directory. Example: GET /sop/exp.pdf
@app.route('/sop/<path:filename>')
def serve_sop(filename):
    # security: only allow files inside SOP_DIR
    safe_path = os.path.join(SOP_DIR, filename)
    if not os.path.isfile(safe_path):
        abort(404)
    return send_from_directory(SOP_DIR, filename, as_attachment=True)

# ---------- SOP UPLOAD ----------
@app.route('/upload-sop', methods=['POST'])
def upload_sop():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400
    
    filename = secure_filename(file.filename)
    os.makedirs(SOP_DIR, exist_ok=True)
    file.save(os.path.join(SOP_DIR, filename))
    print(f'[SOP UPLOAD] Received: {filename}')
    return jsonify({'success': True, 'filename': filename})

# ---------- OSCILLOSCOPE WORKER ----------
# ---------- OSCILLOSCOPE WORKER ----------
def clean_osc_data(data):
    if len(data) < 7: return data
    try:
        d = medfilt(data, kernel_size=3)
        d = savgol_filter(d, window_length=7, polyorder=3)
        return d
    except:
        return data

def osc_checksum16(data: bytes) -> int:
    # Must match the firmware's checksum16(): plain 16-bit additive sum,
    # wrapping the same way a uint16_t does on the MCU.
    return sum(data) & 0xFFFF

def find_osc_trigger(data, level, hysteresis, rising=True):
    pre_trigger = osc_settings['pre_trigger']
    if len(data) < pre_trigger + 10: return None
    armed = False
    for i in range(pre_trigger, len(data) - 1):
        if rising:
            if not armed and data[i] < (level - hysteresis): armed = True
            if armed and data[i - 1] < level <= data[i]: return i
        else:
            if not armed and data[i] > (level + hysteresis): armed = True
            if armed and data[i - 1] > level >= data[i]: return i
    return None

def get_latest_osc(history_arr, n):
    global osc_hist_idx
    n = min(n, OSC_HISTORY_SIZE)
    with osc_lock:
        start = (osc_hist_idx - n) % OSC_HISTORY_SIZE
        if start < osc_hist_idx:
            return history_arr[start:osc_hist_idx].copy()
        return np.concatenate([history_arr[start:], history_arr[:osc_hist_idx]])

def measure_osc_frequency(data):
    if len(data) < 64: return None
    try:
        n = len(data)
        nfft = n * 4
        windowed = (data - np.mean(data)) * np.hanning(n)
        fft = np.abs(np.fft.rfft(windowed, n=nfft))
        freqs = np.fft.rfftfreq(nfft, 1.0 / OSC_SAMPLE_RATE)
        fft[0] = 0
        peak = np.argmax(fft)
        return float(freqs[peak]) if peak > 0 else None
    except:
        return None

def osc_worker():
    global osc_history_ch1, osc_history_ch2, osc_hist_idx, osc_ser, OSC_PORT
    print(f"[OSC] Worker started. Port: {OSC_PORT}")

    raw = bytearray()
    VREF = 3.3
    last_emit_time = 0

    while not osc_stop.is_set():
        if OSC_PORT is None:
            detect_osc_port()
            eventlet.sleep(1)
            continue

        try:
            if osc_ser is None or not osc_ser.is_open:
                osc_ser = serial.Serial(OSC_PORT, OSC_BAUD, timeout=0.01)
                print(f"[OSC] Connected to {OSC_PORT}")

            # --- 1. DRAIN SERIAL (High Speed) ---
            # Wait for at least 1KB or 10ms to reduce overhead
            if osc_ser.in_waiting < 1024:
                eventlet.sleep(0.01)

            avail = osc_ser.in_waiting
            if avail > 0:
                chunk = osc_ser.read(avail)
                raw += chunk

                # Packet format: header(0xAA 0x55) + count(u16) + count*2 data
                # bytes (interleaved CH1,CH2,CH1,CH2...) + 2-byte checksum trailer.
                while True:
                    idx = raw.find(b'\xAA\x55')
                    if idx == -1:
                        if len(raw) > 1: raw = raw[-1:]
                        break

                    if idx > 0: raw = raw[idx:]
                    if len(raw) < 4: break

                    count = struct.unpack_from('<H', raw, 2)[0]
                    # Reject anything that isn't exactly the packet size the
                    # firmware sends. A loose 0<count<=1024 check let
                    # misaligned/corrupted "packets" through to the plot.
                    if count != OSC_EXPECTED_COUNT:
                        osc_stats_counters["packets_rejected"] += 1
                        raw = raw[2:]          # drop just the false header, keep searching
                        continue

                    pkt_len = 4 + count * 2 + 2  # +2 for the trailing checksum
                    if len(raw) < pkt_len: break

                    payload = bytes(raw[4:4 + count * 2])
                    expected_chk = struct.unpack_from('<H', raw, 4 + count * 2)[0]
                    actual_chk = osc_checksum16(payload)

                    if actual_chk != expected_chk:
                        # Corrupted/misaligned packet - this used to produce
                        # impossible voltage spikes. Discard instead of plotting it.
                        osc_stats_counters["packets_rejected"] += 1
                        raw = raw[2:]
                        continue

                    samples = np.frombuffer(payload, dtype='<u2')
                    raw = raw[pkt_len:]
                    osc_stats_counters["packets_ok"] += 1

                    volts = samples.astype(np.float64) * (VREF / 4095.0)
                    # Split the interleaved data (CH1, CH2, CH1, CH2...)
                    v1 = volts[0::2]
                    v2 = volts[1::2]
                    n = len(v1)
                    if n == 0:
                        continue

                    with osc_lock:
                        end = osc_hist_idx + n
                        if end <= OSC_HISTORY_SIZE:
                            osc_history_ch1[osc_hist_idx:end] = v1
                            osc_history_ch2[osc_hist_idx:end] = v2
                        else:
                            split = OSC_HISTORY_SIZE - osc_hist_idx
                            osc_history_ch1[osc_hist_idx:]  = v1[:split]
                            osc_history_ch1[:n - split] = v1[split:]
                            osc_history_ch2[osc_hist_idx:]  = v2[:split]
                            osc_history_ch2[:n - split] = v2[split:]
                        osc_hist_idx = end % OSC_HISTORY_SIZE

            # --- 2. EMIT (Throttled Snapshot @ 10 FPS) ---
            now = time.time()
            if now - last_emit_time > 0.1: # 100ms delay for "Slower Screening"
                last_emit_time = now
                if not osc_settings['freeze']:
                    disp_n = osc_settings['samples']
                    pad = 16
                    search_n = min(disp_n + osc_settings['pre_trigger'] + 1024 + pad, OSC_HISTORY_SIZE)
                    d1 = get_latest_osc(osc_history_ch1, search_n)
                    d2 = get_latest_osc(osc_history_ch2, search_n)

                    if len(d1) >= disp_n:
                        trig_data = d1 if osc_settings.get('trig_src', 0) == 0 else d2
                        trig_idx = find_osc_trigger(trig_data, osc_settings['trig_v'], osc_settings['hyst'], osc_settings['rising'])

                        if trig_idx is not None and (trig_idx - osc_settings['pre_trigger']) >= pad:
                            start = trig_idx - osc_settings['pre_trigger']
                            slice_start = start - pad
                            slice_end = start + disp_n + pad

                            if slice_end <= len(d1):
                                raw1 = d1[slice_start:slice_end].copy()
                                raw2 = d2[slice_start:slice_end].copy()
                                if osc_settings['smooth']:
                                    raw1 = clean_osc_data(raw1)
                                    raw2 = clean_osc_data(raw2)
                                display1 = raw1[pad:pad + disp_n]
                                display2 = raw2[pad:pad + disp_n]
                                triggered = True
                            else:
                                display1 = d1[-disp_n:].copy()
                                display2 = d2[-disp_n:].copy()
                                triggered = False
                        else:
                            display1 = d1[-disp_n:].copy()
                            display2 = d2[-disp_n:].copy()
                            triggered = False

                        payload = {
                            'ch1': display1.tolist(),
                            'ch2': display2.tolist(),
                            'triggered': triggered,
                            'ts': time.time(),
                        }
                        for display, prefix in [(display1, 'ch1_'), (display2, 'ch2_')]:
                            if len(display) == 0: continue
                            vmin = float(np.min(display))
                            vmax = float(np.max(display))
                            payload[prefix + 'vmin'] = vmin
                            payload[prefix + 'vmax'] = vmax
                            payload[prefix + 'vpp'] = vmax - vmin
                            payload[prefix + 'freq'] = measure_osc_frequency(display)
                            payload[prefix + 'dc'] = float(np.mean(display))

                        socketio.emit('osc_data', payload)

            eventlet.sleep(0.001)

        except Exception as e:
            print(f"[OSC] Error: {e}")
            if osc_ser:
                try: osc_ser.close()
                except: pass
            osc_ser = None
            eventlet.sleep(1)

# ---------- SOCKET HANDLERS ----------
@socketio.on('update_osc_settings')
def handle_update_osc_settings(data):
    global osc_settings
    osc_settings.update(data)
    print(f"[OSC] Settings updated: {osc_settings}")

@socketio.on('osc_auto_level')
def handle_osc_auto_level():
    history = osc_history_ch1 if osc_settings.get('trig_src', 0) == 0 else osc_history_ch2
    data = get_latest_osc(history, osc_settings['samples'])
    if len(data) > 10:
        mid = float((np.min(data) + np.max(data)) / 2.0)
        osc_settings['trig_v'] = round(mid, 2)
        socketio.emit('osc_settings_sync', {'trig_v': osc_settings['trig_v']})


# ---------- SERIAL READER ----------
def serial_reader_worker(conn_id, serial_obj, stop_event):
    try:
        while not stop_event.is_set():
            line = serial_obj.readline()
            if not line:
                continue
            try:
                text = line.decode(errors='replace').strip()
            except:
                text = str(line)
            socketio.emit('feedback', {'conn_id': conn_id, 'text': text})

            # --- parse serial line into sensor_data for chart ---
            if any(sep in text for sep in [':', '=', '@', '>', '#', '^', '!', '$', '*', '%', '~', '\\', '|', '+', '-', ';', ',']) and any(c.isdigit() for c in text):
                # Flexible parsing similar to client-side
                trimmed = re.sub(r'^\d{1,2}:\d{2}:\d{2}\s*', '', text.strip())  # remove timestamp

                # Admin-configurable required prefixes (e.g. "Temperature") a line must
                # start with to be treated as plotter data; empty list = no requirement.
                # The prefix is only a gate, not stripped out — it's commonly the data
                # key itself (e.g. "Temperature: 23.5"), so removing it would leave a
                # bare number with no key to attach it to.
                required_prefixes = load_ui_config().get('defaults', {}).get('serial_plotter_required_prefixes', [])
                if required_prefixes:
                    prefix_match = next((kw for kw in required_prefixes if trimmed.lower().startswith(kw.lower())), None)
                    if prefix_match is None:
                        continue

                # Split on |, ;, or , to handle various serial formats like 'V: 16.15 | I: 4.18'
                pairGroups = re.split(r'[|;,]', trimmed)
                data = {}
                for group in pairGroups:
                    if not group.strip():
                        continue
                    normalized = re.sub(r'[:=>@#>^!$*~\\|+%\s&]+', ' ', group).strip()
                    tokens = re.split(r'\s+', normalized)
                    for i in range(0, len(tokens), 2):
                        if i + 1 < len(tokens):
                            k = tokens[i].strip().lower()
                            rawv = tokens[i + 1].strip()
                            try:
                                num = float(re.sub(r'[^\d\.\-+eE]', '', rawv))
                                if not math.isnan(num):
                                    data[k] = num
                            except:
                                pass
                # Keep original keys as sent by Arduino - no predefined mappings
                if data:
                    latest_sensor_data[conn_id] = data
                    socketio.start_background_task(send_sensor_data_to_clients, conn_id, data)
    except Exception as e:
        socketio.emit('feedback', {'conn_id': conn_id, 'text': f'[serial worker stopped] {e}'})

# ---------- SOCKET HANDLERS ----------
@socketio.on('connect')
def on_connect():
    from flask import request
    print("[DEBUG] Client connected:", request.sid)
    emit('ports_list', list_serial_ports())
    cfg = get_student_ui_config()
    student_ports = cfg.get('serial_ports', [])
    # Excludes the plotter-only stub for a hidden default port (student_visible=False,
    # see get_student_ui_config) — its real device path/baud must never reach the client.
    student_ids = {p['id'] for p in student_ports if p.get('student_visible', True)}
    with serial_connections_lock:
        active = {cid: {'port': c['port'], 'baud': c['baud']} for cid, c in serial_connections.items() if cid in student_ids}
    emit('serial_ports_config', {'ports': student_ports, 'active': active})
    emit('feedback', {'conn_id': None, 'text': 'Server: socket connected'})

@socketio.on('list_ports')
def handle_list_ports():
    emit('ports_list', list_serial_ports())

@socketio.on('connect_serial')
def handle_connect_serial(data):
    data = data or {}
    conn_id = data.get('conn_id') or 'default'
    # serial_monitor_section only hides the card in the UI; serial_connect is the sole
    # functional gate, so auto-connect (or a manual reconnect) still works when hidden.
    if not is_control_enabled('serial_connect'):
        emit('serial_status', {'status': 'error', 'message': 'Serial Monitor connect is disabled for this session', 'conn_id': conn_id})
        return
    port = data.get('port')
    baud = int(data.get('baud', 115200))
    if not port:
        emit('serial_status', {'status': 'error', 'message': 'No port selected', 'conn_id': conn_id})
        return
    ok, err = _open_connection(conn_id, port, baud)
    if ok:
        emit('serial_status', {'status': 'connected', 'port': port, 'baud': baud, 'conn_id': conn_id})
    else:
        emit('serial_status', {'status': 'error', 'message': err, 'conn_id': conn_id})

@socketio.on('disconnect_serial')
def handle_disconnect_serial(data=None):
    conn_id = (data or {}).get('conn_id') or 'default'
    _close_connection(conn_id)
    emit('serial_status', {'status': 'disconnected', 'conn_id': conn_id})

@socketio.on('send_command')
def handle_send_command(data):
    data = data or {}
    conn_id = data.get('conn_id') or _primary_conn_id()
    cmd = data.get('cmd', '')
    out = cmd + ("\n" if not cmd.endswith("\n") else "")
    with serial_connections_lock:
        conn = serial_connections.get(conn_id)
    try:
        if conn and conn['ser'] and conn['ser'].is_open:
            conn['ser'].write(out.encode())
            emit('feedback', {'conn_id': conn_id, 'text': f'SENT> {cmd}'})
        else:
            emit('feedback', {'conn_id': conn_id, 'text': f'[no-serial] {cmd}'})
    except Exception as e:
        emit('feedback', {'conn_id': conn_id, 'text': f'[send error] {e}'})

@socketio.on('reset_serial')
def handle_reset_serial(data):
    """Hardware-reset the MCU on conn_id via the USB-serial adapter's DTR/RTS
    lines, without touching firmware — same mechanism esptool/avrdude use
    before flashing. Works whether or not conn_id is already connected: if
    there's a live connection, the pulse rides on it; otherwise a temporary
    connection is opened using the given port/baud just long enough to send
    the pulse, then closed — so resetting never requires connecting first.
    Only works if the board has an auto-reset circuit wiring DTR and/or RTS
    to its RESET pin (true for Arduino/FTDI and most ESP32 dev boards);
    otherwise the pulse is a harmless no-op."""
    data = data or {}
    conn_id = data.get('conn_id') or _primary_conn_id()
    if not is_control_enabled('serial_connect'):
        emit('feedback', {'conn_id': conn_id, 'text': '[reset] Serial Monitor connect is disabled for this session'})
        return

    with serial_connections_lock:
        conn = serial_connections.get(conn_id)

    temp_ser = None
    if conn and conn['ser'] and conn['ser'].is_open:
        ser = conn['ser']
    else:
        if serial is None:
            emit('feedback', {'conn_id': conn_id, 'text': '[reset] pyserial not available on server'})
            return
        port = (data.get('port') or '').strip()
        if not port:
            emit('feedback', {'conn_id': conn_id, 'text': '[reset] Not connected and no port given — connect once, or pick a port, first'})
            return
        if OSC_PORT and port == OSC_PORT:
            emit('feedback', {'conn_id': conn_id, 'text': f'[reset] {port} is the Oscilloscope\'s port and cannot be used here'})
            return
        try:
            baud = int(data.get('baud') or 115200)
            temp_ser = serial.Serial(port, baud, timeout=1)
            ser = temp_ser
        except Exception as e:
            emit('feedback', {'conn_id': conn_id, 'text': f'[reset error] {e}'})
            return

    try:
        ser.dtr = False
        ser.rts = False
        time.sleep(0.1)
        ser.dtr = True
        ser.rts = True
        emit('feedback', {'conn_id': conn_id, 'text': '[reset] Reset pulse sent to MCU'})
    except Exception as e:
        emit('feedback', {'conn_id': conn_id, 'text': f'[reset error] {e}'})
    finally:
        if temp_ser:
            try:
                temp_ser.close()
            except Exception:
                pass

# ---------- HARDWARE DEBUGGER (GDB/OpenOCD) ----------
# Browser <-> Master <-> here, over the same session-scoped SocketIO relay
# every other event on this page already uses (see Master's
# _relay_from_browser and RELAYED_EVENTS) — no separate service or protocol.
# Only one debug session at a time: the probe is physical hardware, and a
# Lab Pi only ever has one active booking anyway (current_session_key).
from debug.boards import get_board
from debug.gdb_bridge import GdbSession
from debug.mock_gdb import MockGdbSession
from debug.openocd_manager import OpenOCDManager
from debug.ws_protocol import handle_command as _handle_debug_command

DEBUG_MOCK = os.environ.get('DEBUG_MOCK') == '1'
_DEBUG_GDB_PORT = int(os.environ.get('DEBUG_GDB_PORT', '3333'))
_debug_gdb = None
_debug_openocd = None


def _probe_serial_from_by_id_path(path):
    """Best-effort extraction of a USB device's serial number from a
    /dev/serial/by-id/... symlink name, for OpenOCD's 'adapter serial'
    option. udev's usb-serial by-id names are conventionally
    'usb-<vendor>_<model>_<serial>[-ifXX]' -- strip a trailing -ifNN
    interface suffix and take the last underscore-separated segment. Not
    guaranteed for nonstandard bridges; if OpenOCD rejects the result,
    clear the admin's Debug port field to fall back to auto-selection."""
    if not path:
        return None
    name = re.sub(r'-if\d+$', '', os.path.basename(path))
    parts = name.rsplit('_', 1)
    return parts[-1] if len(parts) == 2 else None


def _emit_debug_event(payload):
    # Called from the reader greenthread, outside any request context, so
    # the request-bound `emit()` helper (which assumes flask.request.sid)
    # doesn't apply — the server instance's own .emit() works from anywhere.
    # No explicit room: broadcasting is correct since only one student's
    # browser is ever actually listening on this Lab Pi at a time.
    socketio.emit('debug_event', payload)


# Debug handlers can block for seconds (OpenOCD startup, GDB round trips,
# reset retries), which with async_handlers=False would stall the student's
# serial commands behind them. They're queued to one worker instead, so they
# stay off the socket loop but still run in the order they were sent.
_debug_jobs = eventlet.queue.Queue()


def _debug_worker():
    while True:
        job = _debug_jobs.get()
        try:
            job()
        except Exception as e:
            print(f"[Debug] Unhandled error in debug job: {e}")


def _debug_job(handler):
    """Run a socket handler on the debug worker. The request context is
    copied so emit() still replies to the sender."""
    @functools.wraps(handler)
    def wrapper(*args):
        _debug_jobs.put(copy_current_request_context(lambda: handler(*args)))
    return wrapper


def _teardown_debug_session():
    global _debug_gdb, _debug_openocd
    if _debug_gdb is not None:
        try:
            _debug_gdb.stop()
        except Exception as e:
            print(f"[Debug] Error stopping GDB session: {e}")
        _debug_gdb = None
    if _debug_openocd is not None:
        try:
            _debug_openocd.stop()
        except Exception as e:
            print(f"[Debug] Error stopping OpenOCD: {e}")
        _debug_openocd = None


def _on_gdb_exit_callback():
    """GdbSession on_exit hook: GDB died on its own (e.g. an internal error
    while loading symbols). Tear down on the debug worker -- the callback
    runs on the session's own reader greenthread, which stop() would kill
    -- and free OpenOCD so it stops holding the ICDI probe."""
    def on_exit(rc):
        gdb = _debug_gdb
        def job():
            if gdb is None or _debug_gdb is not gdb:
                return  # already torn down (debug_stop / failed debug_start)
            print(f"[Debug] GDB exited unexpectedly (exit code {rc}); tearing down debug session")
            _teardown_debug_session()
            _emit_debug_event({'event': 'error', 'message': (
                f'The debugger (GDB) crashed (exit code {rc}). The debug session has been '
                'closed and the probe released -- click Connect to start a new session.')})
            _emit_debug_event({'event': 'session_stopped'})
        _debug_jobs.put(job)
    return on_exit


def _release_debug_probe(reason):
    """Stop any running debug session before something else (flash, factory
    reset) opens the same probe -- OpenOCD can't share the ICDI. Runs on the
    debug worker so it doesn't race an in-flight debug command; blocks the
    caller until it's done."""
    if _debug_gdb is None and _debug_openocd is None:
        return
    done = eventlet.event.Event()
    def job():
        try:
            if _debug_gdb is not None or _debug_openocd is not None:
                _teardown_debug_session()
                _emit_debug_event({'event': 'console', 'text': f'debug session closed: {reason}\n'})
                _emit_debug_event({'event': 'session_stopped'})
        finally:
            done.send()
    _debug_jobs.put(job)
    done.wait(timeout=30)


@socketio.on('debug_start')
@_debug_job
def handle_debug_start(data):
    global _debug_gdb, _debug_openocd
    data = data or {}
    # The admin's configured MCU (Lab Pi UI settings -> Debug) always wins
    # over whatever the browser sent, since it's set once per Pi and the
    # browser's value is just a same-string default derived from board_type.
    debug_defaults = get_effective_ui_config().get('defaults', {})
    board_id = (debug_defaults.get('debug_board_id') or '').strip() or data.get('board_id')
    if _debug_gdb is not None:
        emit('debug_event', {'event': 'error', 'message': 'A debug session is already active on this board'})
        return
    try:
        board = get_board(board_id)
    except ValueError as e:
        emit('debug_event', {'event': 'error', 'message': str(e)})
        return

    probe_path = (debug_defaults.get('debug_port') or '').strip()
    probe_serial = _probe_serial_from_by_id_path(probe_path) if probe_path else None

    gdb = None
    try:
        if DEBUG_MOCK:
            gdb = MockGdbSession(board, _DEBUG_GDB_PORT, on_event=_emit_debug_event)
        else:
            _debug_openocd = OpenOCDManager(board, gdb_port=_DEBUG_GDB_PORT, adapter_serial=probe_serial)
            _debug_openocd.start()
            gdb = GdbSession(board, _DEBUG_GDB_PORT, on_event=_emit_debug_event,
                             on_exit=_on_gdb_exit_callback())
        # Set before start(), not after: GDB auto-halts on attach (before our
        # own deliberate reset+halt even runs) and that fires an unsolicited
        # 'stopped' debug_event immediately -- the browser reacts to *any*
        # 'stopped' by requesting registers/disasm/locals/watches right away
        # (see the 'stopped' case in templates/index.html). If _debug_gdb
        # were still None at that point, every one of those bounces back as
        # a bogus "No active debug session" error even though the session
        # is, from the student's perspective, mid-connect and about to work.
        _debug_gdb = gdb
        gdb.start()
    except Exception as e:
        _debug_gdb = None
        if gdb is not None:
            try:
                gdb.stop()
            except Exception:
                pass
        if _debug_openocd is not None:
            _debug_openocd.stop()
            _debug_openocd = None
        emit('debug_event', {'event': 'error', 'message': f'failed to start debug session: {e}'})
        return

    emit('debug_event', {'event': 'session_started', 'board_id': board.id, 'mock': DEBUG_MOCK})


@socketio.on('debug_stop')
@_debug_job
def handle_debug_stop(data=None):
    _teardown_debug_session()
    emit('debug_event', {'event': 'session_stopped'})


@socketio.on('debug_load_symbols')
@_debug_job
def handle_debug_load_symbols(data=None):
    # Always the fixed path from debug_upload_elf() -- never a client-supplied
    # path (students have no visibility into this Pi's filesystem to type one).
    if _debug_gdb is None:
        emit('debug_event', {'event': 'error', 'message': 'No active debug session'})
        return
    if not os.path.isfile(DEBUG_ELF_PATH):
        emit('debug_event', {'event': 'error', 'message': 'No ELF uploaded yet -- choose one in the Symbols section first'})
        return
    try:
        _debug_gdb.load_symbols(DEBUG_ELF_PATH)
        emit('debug_event', {'event': 'console', 'text': 'symbols loaded\n'})
    except Exception as e:
        emit('debug_event', {'event': 'error', 'message': f'failed to load symbols: {e}'})


@socketio.on('debug_command')
@_debug_job
def handle_debug_command(data):
    data = data or {}
    if _debug_gdb is None:
        emit('debug_event', {'event': 'error', 'message': 'No active debug session'})
        return
    try:
        reply = _handle_debug_command(_debug_gdb, data)
    except Exception as e:
        reply = {'event': 'error', 'message': str(e)}
    if reply is not None:
        emit('debug_event', reply)


@socketio.on('waveform_config')
def handle_waveform_config(cfg):
    conn_id = _primary_conn_id()
    shape = cfg.get('shape'); freq = cfg.get('freq'); amp = cfg.get('amp')
    msg = f'WAVE {shape} FREQ {freq} AMP {amp}'
    emit('feedback', {'conn_id': conn_id, 'text': f'[waveform] {msg}'})
    with serial_connections_lock:
        conn = serial_connections.get(conn_id)
    try:
        if conn and conn['ser'] and conn['ser'].is_open:
            conn['ser'].write((msg + "\n").encode())
    except Exception as e:
        emit('feedback', {'conn_id': conn_id, 'text': f'[waveform send error] {e}'})

def send_sensor_data_to_clients(conn_id, data):
    try:
        with app.app_context():
            with serial_connections_lock:
                conn = serial_connections.get(conn_id)
            port = conn['port'] if conn else None
            socketio.emit('sensor_data', {'conn_id': conn_id, 'port': port, 'data': data}, namespace='/')
    except Exception as e:
        print("[ERROR] Failed to emit sensor_data:", e)


# ---------- MAIN ----------
if __name__ == '__main__':
    import socket
    def check_port(port, name):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        if result == 0:
            print(f"✓ {name} is running on port {port}")
            return True
        else:
            print(f"✗ {name} is NOT running on port {port}")
            return False

    print("========================================")
    print("Virtual Lab Server Starting...")
    print("========================================")
    
    # Start heartbeat thread to communicate with Master Pi
    print(f"[Heartbeat] Starting heartbeat thread (Lab Pi ID: {LAB_PI_ID})")
    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    
    # Start Oscilloscope worker
    eventlet.spawn(osc_worker)
    eventlet.spawn(_debug_worker)

    # Auto-connect any admin-configured serial port profiles
    print("[Serial] Syncing admin-configured serial port profiles...")
    sync_serial_profiles()

    audio_running = check_port(9000, "Audio server")
    if not audio_running:
        print("\n⚠️  Audio service not detected!")
        print("   To enable audio, run:")
        print("   sudo systemctl enable audio_stream.service")
        print("   sudo systemctl start audio_stream.service")

    print("\nStarting Flask server on port 10000...")
    print("========================================")

    try:
        socketio.run(app, host='0.0.0.0', port=10000)
    finally:
        print("Main server stopped")
