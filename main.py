import network
import time
import ujson
from machine import Pin
from mfrc522 import MFRC522
from umqtt.simple import MQTTClient

# --- 1. NETWORK SETUP ---
SSID, PASSWORD = "EdNet", "Huawei@123"
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect(SSID, PASSWORD)

print("[DEBUG] Connecting to WiFi", end="")
while not wlan.isconnected():
    print(".", end="")  # Printing dots while connecting
    time.sleep(0.5)
print("\n[DEBUG] WiFi Connected! IP:", wlan.ifconfig()[0])

# --- 2. CONFIG & TOPICS ---
TEAM_ID = "team_rukundo_20266" 
MQTT_BROKER = "157.173.101.159"

TOPIC_TOPUP = "rfid/{}/card/topup".format(TEAM_ID)
TOPIC_STATUS = "rfid/{}/card/status".format(TEAM_ID)
TOPIC_BALANCE = "rfid/{}/card/balance".format(TEAM_ID)

# Global variables
pending_amount = 0
target_uid = ""
should_write = False
KEY = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
BLOCK_ADDR = 8

# Hardware setup (sck, mosi, miso, rst, cs)
print("[DEBUG] Initializing MFRC522 Reader...")
rdr = MFRC522(sck=14, mosi=13, miso=12, rst=4, cs=5)

# --- 3. MQTT CALLBACK ---
def on_message(topic, msg):
    global pending_amount, target_uid, should_write
    try:
        data = ujson.loads(msg)
        target_uid = data.get('uid', "").strip()
        pending_amount = int(data.get('amount', 0))
        should_write = True
        print("\n[DEBUG] MQTT Message Received")
        print("        Target UID:", target_uid)
        print("        Top-up Amt:", pending_amount)
    except Exception as e:
        print("[DEBUG] MQTT Parse Error:", e)

print("[DEBUG] Connecting to MQTT Broker: {}".format(MQTT_BROKER))
client = MQTTClient(TEAM_ID, MQTT_BROKER)
client.set_callback(on_message)
client.connect()
client.subscribe(TOPIC_TOPUP)
print("[DEBUG] Subscribed to:", TOPIC_TOPUP)

print("\n--- SYSTEM LIVE: Place card on reader ---")

# --- 4. MAIN LOOP ---
while True:
    try:
        client.check_msg()
        (stat, tag_type) = rdr.request(rdr.REQIDL)
        
        if stat == rdr.OK:
            (stat, raw_uid) = rdr.anticoll()
            if stat == rdr.OK:
                current_uid_str = "0x%02x%02x%02x%02x" % (raw_uid[0], raw_uid[1], raw_uid[2], raw_uid[3])
                print("\n[DEBUG] Card Detected:", current_uid_str)
                
                # 1. Select the tag
                if rdr.select_tag(raw_uid) == rdr.OK:
                    print("[DEBUG] Card Selected Successfully")
                    
                    # 2. Authenticate
                    if rdr.auth(rdr.AUTHENT1A, BLOCK_ADDR, KEY, raw_uid) == rdr.OK:
                        print("[DEBUG] Authentication Successful (Block {})".format(BLOCK_ADDR))
                        
                        block_data = rdr.read(BLOCK_ADDR)
                        if block_data is not None:
                            current_balance = (block_data[0]<<24 | block_data[1]<<16 | block_data[2]<<8 | block_data[3])
                            print("[DEBUG] Current Balance Read:", current_balance)
                            
                            # 3. WRITE LOGIC
                            if should_write:
                                if current_uid_str.lower() == target_uid.lower() or target_uid in ["", "Waiting..."]:
                                    print("[DEBUG] UID Match! Proceeding with Physical Write...")
                                    new_balance = current_balance + pending_amount
                                    
                                    buf = [0]*16
                                    buf[0:4] = [(new_balance >> 24) & 0xFF, (new_balance >> 16) & 0xFF, (new_balance >> 8) & 0xFF, new_balance & 0xFF]
                                    
                                    if rdr.write(BLOCK_ADDR, buf) == rdr.OK:
                                        print("[DEBUG] SUCCESS: Data written to card chip.")
                                        client.publish(TOPIC_BALANCE, ujson.dumps({"uid": current_uid_str, "new balance": new_balance}))
                                        current_balance = new_balance
                                        should_write = False
                                    else:
                                        print("[DEBUG] ERROR: Hardware write failed.")
                                else:
                                    print("[DEBUG] UID MISMATCH: Card on reader is NOT the one selected on dashboard.")
                                    should_write = False

                            # 4. SEND STATUS
                            payload = ujson.dumps({"uid": current_uid_str, "balance": current_balance, "team": TEAM_ID})
                            client.publish(TOPIC_STATUS, payload)
                        else:
                            print("[DEBUG] ERROR: Could not read block data.")
                    else:
                        print("[DEBUG] ERROR: Authentication Failed (Check Key/Sector)")
                    
                    rdr.stop_crypto1()
                
                time.sleep(1) # Delay to prevent duplicate reads
                
    except Exception as e:
        print("\n[DEBUG] Global Loop Error:", e)
        time.sleep(1)
    
    time.sleep(0.1)
