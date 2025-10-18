# ESP32 Plant Monitor (MicroPython) - LCD 16x2 I2C @3V3
# DHT@15, LDR@32, SOIL@33, LEDsoil@25, LEDenv@26, LCD I2C SDA=21 SCL=22

import time, json, network
from machine import Pin, ADC, I2C
import dht
from umqtt.simple import MQTTClient

# ==== WiFi & MQTT ====
WIFI_SSID = "Wokwi-GUEST"
WIFI_PASS = ""
MQTT_HOST = "test.mosquitto.org"
MQTT_PORT = 1883
CLIENT_ID = b"esp32_plant_monitor_sync"

# ==== MQTT Topics ====
TOPIC_DATA = b"plant/data"
TOPIC_TEMP = b"plant/temperature"
TOPIC_HUM = b"plant/humidity"
TOPIC_SOIL = b"plant/soil_moisture"
TOPIC_LIGHT = b"plant/light_level"
TOPIC_PUMP = b"plant/pump_status"
TOPIC_LAMP = b"plant/light_status"
TOPIC_MODE = b"plant/mode"
TOPIC_THRESHOLDS = b"plant/thresholds"
TOPIC_CMD = b"plant/command"

# ==== GPIO Map ====
PIN_DHT = 15
PIN_SOIL = 33
PIN_LDR = 32
PIN_LED_SOIL = 25
PIN_LED_ENV = 26

# ==== Thresholds & State ====
mode = "AUTO"
thresholds = {
    "temperature_min": 18.0,
    "temperature_max": 30.0,
    "soil_moisture_min": 30.0,
    "humidity_min": 40.0,
    "light_level_min": 30.0
}
pump_on = False
lamp_on = False

# ==== ADC Calibration ====
ADC_SOIL_DRY = 3500
ADC_SOIL_WET = 1200

# ==== Init Sensors ====
dht_sensor = dht.DHT22(Pin(PIN_DHT))
soil_adc = ADC(Pin(PIN_SOIL)); soil_adc.atten(ADC.ATTN_11DB)
ldr_adc = ADC(Pin(PIN_LDR)); ldr_adc.atten(ADC.ATTN_11DB)
led_soil = Pin(PIN_LED_SOIL, Pin.OUT)
led_env = Pin(PIN_LED_ENV, Pin.OUT)

# ==== LCD Driver (được giữ nguyên từ bạn) ====
class I2cLcd:
    LCD_CLR = 0x01
    LCD_HOME = 0x02
    LCD_ENTRY = 0x04
    LCD_DISPLAY = 0x08
    LCD_FUNCTION = 0x20
    LCD_SET_DDRAM = 0x80
    ENTRY_LEFT = 0x02
    DISPLAY_ON = 0x04
    CURSOR_OFF = 0x00
    BLINK_OFF = 0x00
    MODE_4BIT = 0x00
    LINES_2 = 0x08
    DOTS_5x8 = 0x00
    EN = 0b00000100
    RS = 0b00000001
    BL = 0b00001000
    def __init__(self, i2c, addr, rows=2, cols=16):
        self.i2c, self.addr, self.rows, self.cols = i2c, addr, rows, cols
        self._write4(0x03); time.sleep_ms(5)
        self._write4(0x03); time.sleep_ms(5)
        self._write4(0x03); time.sleep_ms(5)
        self._write4(0x02)
        self._cmd(self.LCD_FUNCTION | self.MODE_4BIT | self.LINES_2 | self.DOTS_5x8)
        self._cmd(self.LCD_DISPLAY | self.DISPLAY_ON | self.CURSOR_OFF)
        self.clear(); self._cmd(self.LCD_ENTRY | self.ENTRY_LEFT)
    def _exp(self, d): self.i2c.writeto(self.addr, bytes([d | self.BL]))
    def _pulse(self, d): self._exp(d | self.EN); time.sleep_us(1); self._exp(d & ~self.EN); time.sleep_us(50)
    def _write4(self, n): d = (n << 4) & 0xF0; self._exp(d); self._pulse(d)
    def _cmd(self, c): self._write4(c >> 4); self._write4(c & 0x0F)
    def write_char(self, ch):
        h = (ord(ch) & 0xF0) | self.RS
        l = ((ord(ch) << 4) & 0xF0) | self.RS
        self._exp(h); self._pulse(h); self._exp(l); self._pulse(l)
    def clear(self): self._cmd(self.LCD_CLR); time.sleep_ms(2)
    def move_to(self, col, row): self._cmd(self.LCD_SET_DDRAM | (col + [0x00,0x40][row]))
    def putstr(self, s): [self.write_char(ch) for ch in s if ch != '\n']

i2c = I2C(0, scl=Pin(22), sda=Pin(21))
lcd = I2cLcd(i2c, 0x27, 2, 16)
lcd.putstr("ESP32 Plant Init")

# ==== Helpers ====
def adc_to_percent(raw, wet=ADC_SOIL_WET, dry=ADC_SOIL_DRY):
    r = min(max(raw, wet), dry)
    pct = 100 * (dry - r) / (dry - wet)
    return round(pct, 1)

def update_leds():
    led_soil.value(1 if pump_on else 0)
    led_env.value(1 if lamp_on else 0)

def wifi_connect():
    sta = network.WLAN(network.STA_IF); sta.active(True)
    sta.connect(WIFI_SSID, WIFI_PASS)
    for _ in range(40):
        if sta.isconnected():
            print("✅ WiFi:", sta.ifconfig()); return True
        time.sleep(0.25)
    print("❌ WiFi failed"); return False

# ==== MQTT ====
client = MQTTClient(CLIENT_ID, MQTT_HOST, port=MQTT_PORT, keepalive=60)

def on_message(topic, msg):
    global mode, thresholds, pump_on, lamp_on
    try:
        t, s = topic.decode(), msg.decode()
        print("📩", t, "→", s)
        if t == "plant/mode":
            mode = s.strip().upper()
        elif t == "plant/thresholds":
            thresholds.update(json.loads(s))
            print("⚙️ Thresholds updated:", thresholds)
        elif t == "plant/command":
            cmd = s.strip().upper()
            if cmd == "PUMP_ON": pump_on = True
            elif cmd == "PUMP_OFF": pump_on = False
            elif cmd == "LIGHT_ON": lamp_on = True
            elif cmd == "LIGHT_OFF": lamp_on = False
            update_leds()
    except Exception as e:
        print("❌ MQTT Error:", e)

def mqtt_setup():
    client.set_callback(on_message)
    client.connect()
    for tp in (TOPIC_MODE, TOPIC_THRESHOLDS, TOPIC_CMD):
        client.subscribe(tp)
    print("✅ MQTT Connected:", MQTT_HOST)

# ==== Main Loop ====
wifi_ok = wifi_connect()
if wifi_ok:
    mqtt_setup()

while True:
    try:
        # đọc cảm biến
        dht_sensor.measure()
        t = dht_sensor.temperature()
        h = dht_sensor.humidity()
        soil_raw = soil_adc.read()
        light_raw = ldr_adc.read()
        soil_pct = adc_to_percent(soil_raw)
        light_pct = round(light_raw / 4095 * 100, 1)

        # logic AUTO
        if mode == "AUTO":
            pump_on = (soil_pct < thresholds["soil_moisture_min"]) or (h < thresholds["humidity_min"])
            lamp_on = (t > thresholds["temperature_max"]) or (light_pct < thresholds["light_level_min"])

        update_leds()

        # hiển thị LCD
        lcd.clear()
        lcd.move_to(0,0)
        lcd.putstr(f"T:{t:>4.1f} H:{h:>4.1f}")
        lcd.move_to(0,1)
        lcd.putstr(f"S:{soil_pct:>3.0f}% L:{light_pct:>3.0f}%")

        # publish MQTT
        client.publish(TOPIC_TEMP, str(t))
        client.publish(TOPIC_HUM, str(h))
        client.publish(TOPIC_SOIL, str(soil_raw))
        client.publish(TOPIC_LIGHT, str(light_raw))
        pkt = {
            "temperature": t,
            "humidity": h,
            "soil_moisture": soil_raw,
            "light_level": light_raw,
            "pump_status": "ON" if pump_on else "OFF",
            "light_status": "ON" if lamp_on else "OFF",
            "mode": mode
        }
        client.publish(TOPIC_DATA, json.dumps(pkt))

        client.check_msg()
        time.sleep(3)

    except Exception as e:
        print("⚠️ Loop err:", e)
        time.sleep(0.5)
