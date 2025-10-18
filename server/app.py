from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from config import Config
from extensions import db, mqtt
from models import SensorData, ThresholdSettings
import json
from datetime import datetime, timedelta
import logging
import sys
import argparse
from gesture_control import start_gesture_thread

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ---------- Flask setup ----------
app = Flask(__name__, static_folder='static', template_folder='templates')
app.config.from_object(Config)
socketio = SocketIO(app, cors_allowed_origins="*")
db.init_app(app)
mqtt.init_app(app)

from routes import main_bp
app.register_blueprint(main_bp)

# ---------- MQTT Topics ----------
TOPIC_DATA = "plant/data"
TOPIC_TEMPERATURE = "plant/temperature"
TOPIC_HUMIDITY = "plant/humidity"
TOPIC_SOIL_MOISTURE = "plant/soil_moisture"
TOPIC_LIGHT_LEVEL = "plant/light_level"
TOPIC_PUMP_STATUS = "plant/pump_status"
TOPIC_LIGHT_STATUS = "plant/light_status"
TOPIC_MODE = "plant/mode"
TOPIC_THRESHOLDS = "plant/thresholds"
TOPIC_COMMAND = "plant/command"

# ---------- States ----------
current_state = {
    "temperature": 0,
    "humidity": 0,
    "soil_moisture": 0,
    "light_level": 0,
    "pump_status": "OFF",
    "light_status": "OFF",
    "mode": "AUTO"
}

thresholds = {
    "temperature_min": 18.0,
    "temperature_max": 30.0,
    "soil_moisture_min": 30,
    "humidity_min": 40.0,
    "light_level_min": 30
}

last_db_save_time = datetime.now()
DB_SAVE_INTERVAL = 60
gesture_thread = None

# ---------- Helper: ADC to Percent ----------
def soil_adc_to_percent(value):
    """Độ ẩm đất: ADC cao -> khô, ADC thấp -> ẩm"""
    try:
        value = float(value)
        ADC_SOIL_DRY = 3500
        ADC_SOIL_WET = 1200
        lo, hi = min(ADC_SOIL_WET, ADC_SOIL_DRY), max(ADC_SOIL_WET, ADC_SOIL_DRY)
        value = max(lo, min(hi, value))
        pct = 100.0 * (ADC_SOIL_DRY - value) / (ADC_SOIL_DRY - ADC_SOIL_WET)
        return round(max(0, min(100, pct)), 2)
    except Exception as e:
        logger.error(f"Soil convert error: {e}")
        return 0


def light_adc_to_percent(value):
    """Ánh sáng: ADC cao -> sáng, ADC thấp -> tối"""
    try:
        value = float(value)
        pct = (value / 4095.0) * 100.0
        return round(max(0, min(100, pct)), 2)
    except Exception as e:
        logger.error(f"Light convert error: {e}")
        return 0

# ---------- MQTT connect ----------
@mqtt.on_connect()
def handle_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("✅ Connected to MQTT broker")
        for t in [
            TOPIC_DATA, TOPIC_TEMPERATURE, TOPIC_HUMIDITY, TOPIC_SOIL_MOISTURE,
            TOPIC_LIGHT_LEVEL, TOPIC_PUMP_STATUS, TOPIC_LIGHT_STATUS,
            TOPIC_MODE, TOPIC_THRESHOLDS
        ]:
            mqtt.subscribe(t)
    else:
        logger.error(f"❌ Failed to connect MQTT, code {rc}")

# ---------- Save sensor data ----------
def save_sensor_data_to_db(temperature, humidity, soil_moisture, light_level, timestamp=None):
    global last_db_save_time
    current_time = datetime.now()
    if (current_time - last_db_save_time).total_seconds() >= DB_SAVE_INTERVAL:
        try:
            if timestamp is None:
                timestamp = current_time
            elif isinstance(timestamp, str):
                try:
                    timestamp = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    timestamp = current_time

            new_data = SensorData(
                temperature=temperature,
                humidity=humidity,
                soil_moisture=soil_moisture,
                light_level=light_level,
                timestamp=timestamp
            )
            with app.app_context():
                db.session.add(new_data)
                db.session.commit()

            last_db_save_time = current_time
            logger.info(f"✅ Sensor data saved at {current_time}")
        except Exception as e:
            logger.error(f"❌ DB save error: {e}")
            with app.app_context():
                db.session.rollback()

# ---------- Handle MQTT messages ----------
@mqtt.on_message()
def handle_message(client, userdata, message):
    topic = message.topic
    payload = message.payload.decode()
    logger.info(f"📩 {topic}: {payload}")

    try:
        if topic == TOPIC_DATA:
            data = json.loads(payload)
            timestamp = data.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

            if "temperature" in data:
                current_state["temperature"] = data["temperature"]
            if "humidity" in data:
                current_state["humidity"] = data["humidity"]
            if "soil_moisture" in data:
                current_state["soil_moisture"] = soil_adc_to_percent(data["soil_moisture"])
            if "light_level" in data:
                current_state["light_level"] = light_adc_to_percent(data["light_level"])

            save_sensor_data_to_db(
                data.get("temperature", 0),
                data.get("humidity", 0),
                soil_adc_to_percent(data.get("soil_moisture", 0)),
                light_adc_to_percent(data.get("light_level", 0)),
                timestamp
            )
            socketio.emit('sensor_data_update', current_state)

        elif topic == TOPIC_TEMPERATURE:
            val = float(payload)
            current_state["temperature"] = val
            socketio.emit('temperature_update', {"value": val})
            socketio.emit('sensor_data_update', current_state)


        elif topic == TOPIC_HUMIDITY:
            val = float(payload)
            current_state["humidity"] = val
            socketio.emit('humidity_update', {"value": val})
            socketio.emit('sensor_data_update', current_state)


        elif topic == TOPIC_SOIL_MOISTURE:
            raw_val = int(payload)
            soil_pct = soil_adc_to_percent(raw_val)
            current_state["soil_moisture"] = soil_pct
            socketio.emit('soil_moisture_update', {"value": soil_pct})
            socketio.emit('sensor_data_update', current_state)


        elif topic == TOPIC_LIGHT_LEVEL:
            raw_val = int(payload)
            light_pct = light_adc_to_percent(raw_val)
            current_state["light_level"] = light_pct
            socketio.emit('light_level_update', {"value": light_pct})
            socketio.emit('sensor_data_update', current_state)


        elif topic == TOPIC_PUMP_STATUS:
            current_state["pump_status"] = payload
            socketio.emit('pump_status_update', {"status": payload})

        elif topic == TOPIC_LIGHT_STATUS:
            current_state["light_status"] = payload
            socketio.emit('light_status_update', {"status": payload})

        elif topic == TOPIC_MODE:
            current_state["mode"] = payload
            socketio.emit('mode_update', {"mode": payload})

        elif topic == TOPIC_THRESHOLDS:
            threshold_data = json.loads(payload)
            thresholds.update(threshold_data)
            update_thresholds_in_db(threshold_data)
            socketio.emit('thresholds_update', threshold_data)

        # Lưu định kỳ
        if all(current_state[k] != 0 for k in ["temperature", "humidity", "soil_moisture", "light_level"]):
            save_sensor_data_to_db(
                current_state["temperature"],
                current_state["humidity"],
                current_state["soil_moisture"],
                current_state["light_level"]
            )
    except Exception as e:
        logger.error(f"❌ MQTT message error: {e}")

# ---------- Update thresholds ----------
def update_thresholds_in_db(threshold_data):
    try:
        with app.app_context():
            settings = ThresholdSettings.query.first() or ThresholdSettings()
            for key, value in threshold_data.items():
                if hasattr(settings, key): setattr(settings, key, value)
            db.session.add(settings)
            db.session.commit()
            logger.info("✅ Thresholds updated in DB")
    except Exception as e:
        logger.error(f"❌ Threshold DB error: {e}")
        with app.app_context():
            db.session.rollback()

# ---------- SocketIO ----------
@socketio.on('connect')
def handle_websocket_connect():
    logger.info(f"Client connected: {request.sid}")
    socketio.emit('initial_state', {
        "current_state": current_state,
        "thresholds": thresholds
    }, room=request.sid)

@socketio.on('disconnect')
def handle_websocket_disconnect():
    logger.info(f"Client disconnected: {request.sid}")

# @socketio.on('set_mode')
# def handle_set_mode(data):
#     mode = data.get('mode', 'AUTO')
#     logger.info(f"Setting mode to {mode}")
#     mqtt.publish(TOPIC_MODE, mode, retain=True)
@socketio.on('set_mode')
def handle_set_mode(data):
    mode = data.get('mode', 'AUTO')
    logger.info(f"Setting mode to {mode}")
    try:
        mqtt.publish(TOPIC_MODE, mode, retain=True)
        return {"ok": True, "mode": mode}
    except Exception as e:
        logger.error(f"Failed to set mode: {e}")
        return {"ok": False, "message": str(e)}


@socketio.on('set_thresholds')
def handle_set_thresholds(data):
    logger.info(f"Setting thresholds to {data}")
    try:
        mqtt.publish(TOPIC_THRESHOLDS, json.dumps(data), retain=True)
        # Phản hồi ngay cho client (ACK)
        return {"ok": True, "message": "Đã gửi ngưỡng mới lên thiết bị"}
    except Exception as e:
        logger.error(f"Failed to set thresholds: {e}")
        return {"ok": False, "message": f"Lỗi: {str(e)}"}


@socketio.on('send_command')
def handle_send_command(data):
    cmd = data.get('command')
    if cmd:
        logger.info(f"Sending command: {cmd}")
        mqtt.publish(TOPIC_COMMAND, cmd)

# ---------- Gesture recognition ----------
def start_gesture_recognition(test_mode=False):
    global gesture_thread
    try:
        logger.info("Starting gesture recognition thread")
        gesture_thread = start_gesture_thread(test_mode)
        logger.info("Gesture recognition thread started")
    except Exception as e:
        logger.error(f"Failed to start gesture recognition: {str(e)}")

# ---------- DB init ----------
def init_database():
    with app.app_context():
        db.create_all()
        settings = ThresholdSettings.query.first()
        if not settings:
            settings = ThresholdSettings(
                temperature_min=18.0,
                temperature_max=30.0,
                soil_moisture_min=30,
                humidity_min=40.0,
                light_level_min=30
            )
            db.session.add(settings)
            db.session.commit()
        thresholds.update({
            "temperature_min": settings.temperature_min,
            "temperature_max": settings.temperature_max,
            "soil_moisture_min": settings.soil_moisture_min,
            "humidity_min": settings.humidity_min,
            "light_level_min": settings.light_level_min
        })

# ---------- CLI ----------
def parse_arguments():
    parser = argparse.ArgumentParser(description='Plant Monitoring System')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--app', action='store_true', help='Run only web app')
    group.add_argument('--cam', action='store_true', help='Run gesture camera only')
    return parser.parse_args()

# ---------- Main ----------
if __name__ == '__main__':
    args = parse_arguments()
    if args.cam:
        import gesture_control
        gesture_control.run_gesture_detection(test_mode=True)
    else:
        init_database()
        if not args.app:
            start_gesture_recognition()
        socketio.run(app, debug=Config.DEBUG, host='0.0.0.0', port=5001)
