from websocket import EufySecurityWebSocket
import aiohttp
import asyncio
import json
import socket
import threading
import time
import sys
import signal
import os
from queue import Queue

RECV_CHUNK_SIZE = 4096

# Define a class to encapsulate camera-related information
class Camera:
    def __init__(self, serial_number):
        self.serial_number = serial_number
        self.video_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.audio_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.backchannel_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.video_thread = None
        self.audio_thread = None
        self.backchannel_thread = None
        self.video_queue = Queue(100)
        self.audio_queue = Queue(100)

# Global variables
cameras = {}  # Dictionary to store camera objects by serial number
run_event = threading.Event()

# Signal handler
def exit_handler(signum, frame):
    print(f'Signal handler called with signal {signum}')
    run_event.set()

# Install signal handler
signal.signal(signal.SIGINT, exit_handler)

# Define classes for client threads
class ClientAcceptThread(threading.Thread):
    def __init__(self, socket, run_event, name, ws, camera):
        threading.Thread.__init__(self)
        self.socket = socket
        self.run_event = run_event
        self.name = name
        self.ws = ws
        self.camera = camera

    def run(self):
        print("Accepting connection for", self.name)
        msg = STOP_TALKBACK.copy()
        msg["serialNumber"] = self.camera.serial_number
        asyncio.run(self.ws.send_message(json.dumps(msg)))
        while not self.run_event.is_set():
            try:
                client_sock, client_addr = self.socket.accept()
                print("New connection added:", client_addr, "for", self.name)
                if self.name == "BackChannel":
                    client_sock.setblocking(True)
                    print("Starting BackChannel")
                    thread = ClientRecvThread(client_sock, run_event, self.name, self.ws, self.camera)
                    thread.start()
                else:
                    client_sock.setblocking(False)
                    thread = ClientSendThread(client_sock, run_event, self.name, self.ws, self.camera)
                    if self.ws:
                        msg = START_P2P_LIVESTREAM_MESSAGE.copy()
                        msg["serialNumber"] = self.camera.serial_number
                        asyncio.run(self.ws.send_message(json.dumps(msg)))
                    thread.start()
            except socket.timeout:
                pass

class ClientSendThread(threading.Thread):
    def __init__(self, client_sock, run_event, name, ws, camera):
        threading.Thread.__init__(self)
        self.client_sock = client_sock
        self.run_event = run_event
        self.name = name
        self.ws = ws
        self.camera = camera

    def run(self):
        print("Thread running:", self.name)
        try:
            while not self.run_event.is_set():
                if self.name == "Video":
                    queue = self.camera.video_queue
                elif self.name == "Audio":
                    queue = self.camera.audio_queue
                if not queue.empty():
                    data = queue.get()
                    self.client_sock.sendall(bytearray(data))
                else:
                    # Send something to know if socket is dead
                    self.client_sock.sendall(bytearray(0))
                    time.sleep(0.1)
        except socket.error as e:
            print("Connection lost", self.name, e)
        except socket.timeout:
            print("Timeout on socket for", self.name)
        try:
            self.client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            print("Error shutdown socket:", self.name)
        self.client_sock.close()
        print("Thread stopping:", self.name)

class ClientRecvThread(threading.Thread):
    def __init__(self, client_sock, run_event, name, ws, camera):
        threading.Thread.__init__(self)
        self.client_sock = client_sock
        self.run_event = run_event
        self.name = name
        self.ws = ws
        self.camera = camera

    def run(self):
        msg = START_TALKBACK.copy()
        msg["serialNumber"] = self.camera.serial_number
        asyncio.run(self.ws.send_message(json.dumps(msg)))
        try:
            curr_packet = bytearray()
            while not self.run_event.is_set():
                data = self.client_sock.recv(RECV_CHUNK_SIZE)
                if not data:
                    break
                curr_packet += bytearray(data)
                if len(data) < RECV_CHUNK_SIZE:
                    if self.name == "Audio":
                        queue = self.camera.audio_queue
                    elif self.name == "Video":
                        queue = self.camera.video_queue
                    msg = SEND_TALKBACK_AUDIO_DATA.copy()
                    msg["serialNumber"] = self.camera.serial_number
                    msg["buffer"] = list(bytes(curr_packet))
                                       asyncio.run(self.ws.send_message(json.dumps(msg)))
                    curr_packet = bytearray()
        except socket.error as e:
            print("Connection lost", self.name, e)
        except socket.timeout:
            print("Timeout on socket for", self.name)
        try:
            self.client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            print("Error shutdown socket:", self.name)
        self.client_sock.close()
        msg = STOP_TALKBACK.copy()
        msg["serialNumber"] = self.camera.serial_number
        asyncio.run(self.ws.send_message(json.dumps(msg)))

# Define a class for WebSocket connection management
class Connector:
    def __init__(self, run_event):
        self.run_event = run_event
        self.ws = None

    def set_ws(self, ws):
        self.ws = ws

    async def on_open(self):
        print("on_open - executed")

    async def on_close(self):
        print("on_close - executed")
        self.run_event.set()

    async def on_error(self, message):
        print("on_error - executed -", message)

    async def on_message(self, message):
        payload = message.json()
        message_type = payload["type"]
        if message_type == "result":
            message_id = payload["messageId"]
            if message_id == START_LISTENING_MESSAGE["messageId"]:
                message_result = payload[message_type]
                states = message_result["state"]
                for state in states["devices"]:
                    serial_number = state["serialNumber"]
                    cameras[serial_number] = Camera(serial_number)
                    video_thread = ClientAcceptThread(cameras[serial_number].video_socket, run_event, "Video", self.ws, cameras[serial_number])
                    audio_thread = ClientAcceptThread(cameras[serial_number].audio_socket, run_event, "Audio", self.ws, cameras[serial_number])
                    backchannel_thread = ClientAcceptThread(cameras[serial_number].backchannel_socket, run_event, "BackChannel", self.ws, cameras[serial_number])
                    cameras[serial_number].video_thread = video_thread
                    cameras[serial_number].audio_thread = audio_thread
                    cameras[serial_number].backchannel_thread = backchannel_thread
                    video_thread.start()
                    audio_thread.start()
                    backchannel_thread.start()
            if message_id == TALKBACK_RESULT_MESSAGE["messageId"] and "errorCode" in payload:
                error_code = payload["errorCode"]
                if error_code == "device_talkback_not_running":
                    msg = START_TALKBACK.copy()
                    for camera in cameras.values():
                        msg["serialNumber"] = camera.serial_number
                        asyncio.run(self.ws.send_message(json.dumps(msg)))

        if message_type == "event":
            message = payload[message_type]
            event_type = message["event"]
            if event_type in ("livestream audio data", "livestream video data"):
                camera_serial = message.get("serialNumber")
                if camera_serial in cameras:
                    camera = cameras[camera_serial]
                    queue = camera.audio_queue if event_type == "livestream audio data" else camera.video_queue
                    event_value = message.get("buffer")
                    queue.put(event_value)
            elif event_type == "livestream error":
                print("Livestream Error!")
                for camera in cameras.values():
                    msg = START_P2P_LIVESTREAM_MESSAGE.copy()
                    msg["serialNumber"] = camera.serial_number
                    asyncio.run(self.ws.send_message(json.dumps(msg))))

# Websocket connector
c = Connector(run_event)

async def init_websocket():
    ws = EufySecurityWebSocket(
        "402f1039-eufy-security-ws",
        sys.argv[1],
        aiohttp.ClientSession(),
        c.on_open,
        c.on_message,
        c.on_close,
        c.on_error,
    )
    c.set_ws(ws)
    try:
        await ws.connect()
        await ws.send_message(json.dumps(START_LISTENING_MESSAGE))
        while not run_event.is_set():
            await asyncio.sleep(1000)
    except Exception as ex:
        print(ex)
        print("init_websocket failed. Exiting.")

# Main function
loop = asyncio.get_event_loop()
loop.run_until_complete(init_websocket())

