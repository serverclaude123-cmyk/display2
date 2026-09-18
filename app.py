# Power Meter — remote dashboard.
#
# Reads live meter data straight off the same HiveMQ Cloud MQTT broker the
# ESP32 boards already use (ModbusGateway publishes, both this app and the
# Guition display subscribe) — no new data path on the device side, this
# app just adds another subscriber that happens to run on the internet
# instead of on the LAN. Publishing "ON"/"OFF" to the cmd topic reaches the
# gateway exactly the same way a confirmed tap on the touchscreen or the
# local dashboard does.
#
# Credentials come from Streamlit secrets, not from this file — see
# .streamlit/secrets.toml.example (a nested [mqtt] table: host/port/
# username/password/topic/tls). If this folder is ever pushed to a public
# GitHub repo for Streamlit Community Cloud deployment, the actual
# secrets.toml must NOT go with it (see .gitignore); paste the same
# content into the app's own Settings -> Secrets panel instead.

import json
import ssl
import threading
import time
from collections import deque

import pandas as pd
import paho.mqtt.client as mqtt
import streamlit as st

HISTORY_MAXLEN = 500  # ~ a few hours at one MQTT message per ~2s

st.set_page_config(page_title="Power Meter — Live Dashboard", layout="wide")


def _mqtt_secret(key, default=None):
    try:
        val = st.secrets["mqtt"][key]
    except Exception:
        val = default
    if val is None:
        st.error(
            f"Missing secret `mqtt.{key}`. Set it under a `[mqtt]` table in "
            "`.streamlit/secrets.toml` (local runs) or the app's Settings -> "
            "Secrets panel (Streamlit Community Cloud) — see "
            "`.streamlit/secrets.toml.example`."
        )
        st.stop()
    return val


TOPIC_STATE = _mqtt_secret("topic")
# No separate cmd-topic secret needed for the existing setup — the project's
# convention everywhere else is "<...>/state" paired with "<...>/cmd", so
# derive it the same way unless a cmd_topic secret overrides it.
TOPIC_CMD = _mqtt_secret("cmd_topic", TOPIC_STATE.replace("/state", "/cmd"))


@st.cache_resource
def get_mqtt_state():
    state = {
        "latest": None,
        "history": deque(maxlen=HISTORY_MAXLEN),
        "lock": threading.Lock(),
        "connected": False,
    }

    def on_connect(client, userdata, flags, rc):
        state["connected"] = rc == 0
        if rc == 0:
            client.subscribe(TOPIC_STATE)

    def on_disconnect(client, userdata, rc):
        state["connected"] = False

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            return
        data["_t"] = time.time()
        with state["lock"]:
            state["latest"] = data
            state["history"].append(data)

    client = mqtt.Client(client_id=f"streamlit-dashboard-{int(time.time())}")
    client.username_pw_set(_mqtt_secret("username"), _mqtt_secret("password"))
    if _mqtt_secret("tls", True):
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)  # matches the ESP32 boards' setInsecure() — no CA pinned
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect(_mqtt_secret("host"), int(_mqtt_secret("port")), keepalive=30)
    client.loop_start()  # background thread; the cache_resource singleton keeps it alive across reruns

    state["client"] = client
    return state


mqtt_state = get_mqtt_state()

st.title("⚡ Power Meter — Live Dashboard")
st.caption(f"MQTT topic `{TOPIC_STATE}` — published by the ModbusGateway board")


@st.fragment(run_every=2)
def live_tiles():
    with mqtt_state["lock"]:
        latest = mqtt_state["latest"]
        connected = mqtt_state["connected"]

    st.caption(f"Broker: {'🟢 connected' if connected else '🔴 disconnected'}")

    if latest is None:
        st.info("Waiting for the first MQTT message from the gateway...")
        return

    age_s = time.time() - latest["_t"]
    if age_s > 15:
        st.warning(f"Last reading is {age_s:.0f}s old — the gateway or its Wi-Fi/Modbus link may be down.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Voltage", f"{latest.get('volt', 0):.1f} V")
    c2.metric("Current", f"{latest.get('curr', 0):.2f} A")
    c3.metric("Power", f"{latest.get('power', 0):.3f} kW")
    c4.metric("Energy", f"{latest.get('kwh', 0):.1f} kWh")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Power Factor", f"{latest.get('pf', 0):.2f}")
    c6.metric("Frequency", f"{latest.get('freq', 0):.2f} Hz")
    c7.metric("Temperature", f"{latest.get('temp', 0):.1f} °C")
    c8.metric("Breaker", "CLOSED" if latest.get("breaker") else "OPEN")


live_tiles()

st.divider()
st.subheader("Breaker control")
bc1, bc2 = st.columns(2)
if bc1.button("CLOSE (ON)", use_container_width=True, type="primary"):
    mqtt_state["client"].publish(TOPIC_CMD, "ON")
    st.toast("Sent ON command")
if bc2.button("OPEN (OFF)", use_container_width=True):
    mqtt_state["client"].publish(TOPIC_CMD, "OFF")
    st.toast("Sent OFF command")

st.divider()
st.subheader("Live trend (this session only)")
st.caption(
    "Built from MQTT messages received while this app has been running — "
    "not backed by the display board's SD card, so it resets on redeploy/restart."
)


@st.fragment(run_every=5)
def trend_charts():
    with mqtt_state["lock"]:
        hist = list(mqtt_state["history"])

    if len(hist) < 2:
        st.info("Collecting readings...")
        return

    df = pd.DataFrame(hist)
    df["time"] = pd.to_datetime(df["_t"], unit="s")
    df = df.set_index("time")

    tc1, tc2, tc3 = st.columns(3)
    with tc1:
        st.caption("Voltage (V)")
        st.line_chart(df[["volt"]], height=200)
    with tc2:
        st.caption("Current (A)")
        st.line_chart(df[["curr"]], height=200)
    with tc3:
        st.caption("Power (kW)")
        st.line_chart(df[["power"]], height=200)


trend_charts()
