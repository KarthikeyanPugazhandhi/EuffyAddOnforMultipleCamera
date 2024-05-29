from websocket import EufySecurityWebSocket
import aiohttp
import asyncio
import json
import socket
import threading
import time
import sys
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from queue import Queue

RECV_CHUNK_SIZE = 4096

EVENT_CONFIGURATION = {
    "livestream video data": {
        "name": "video_data",
        "value": "buffer",
        "type": "event",
    },
    "livestream audio data": {
        "name": "audio_data",
        "value": "buffer",
        "type": "event",
    },
}

START_P2P_LIVESTREAM_MESSAGE = {
    "messageId": "start_livestream",
    "command": "device.start_livestream",
    "serialNumber": None,
}

STOP_P2P_LIVESTREAM_MESSAGE = {
    "messageId": "stop_livestream",
    "command": "device.stop_livestream",
    "serialNumber": None,
}

START_TALKBACK = {
    "messageId": "start_talkback",
    "command": "device.start_talkback",
    "serialNumber": None,
}

SEND_TALKBACK_AUDIO_DATA = {
    "messageId": "talkback_audio_data",
    "command": "device.talkback_audio_data",
    "serialNumber": None,
    "buffer": None
}

STOP_TALKBACK = {
    "messageId": "stop_talkback",
    "command": "device.stop_talkback",
    "serialNumber": None,
}

SET_API_SCHEMA = {
    "messageId": "set_api_schema",
    "command": "set_api_schema",
    "schemaVersion": 13,
}

P2P_LIVESTREAMING_STATUS = "p2pLiveStreamingStatus"

START_LISTENING_MESSAGE = {"messageId": "start_listening", "command": "start_listening"}

TALKBACK_RESULT_MESSAGE = {"messageId": "talkback_audio_data", "errorCode": "device_talkback_not_running"}

DRIVER_CONNECT_MESSAGE = {"messageId": "driver_connect", "command": "driver.connect"}

run_event = threading.Event()

def exit_handler(signum, frame):
    print(f'Signal handler called with signal {signum}')
    run_event.set()

# Install signal handler
signal.signal(signal.SIGINT, exit_handler)

class ClientAcceptThread(threading.Thread):
    def __init__(self, socket, run_event, name, ws, serialno):
        threading.Thread.__init__(self)
        self.socket = socket
        self.queues = []
        self.run_event = run_event
        self.name = name
        self.ws = ws
        self.serialno = serialno
        self.my_threads = []

    def update_threads(self):
        my_threads_before = len(self.my_threads)
        for thread in self.my_threads:
            if not thread.is_alive():
                self.queues.remove(thread.queue)
        self.my_threads = [t for t in self.my_threads if t.is_alive()]
        if self.ws and my_threads_before > 0 and len(self.my_threads) == 0:
            if self.name == "BackChannel":
                print("All clients died (BackChannel): ", self.name)
                sys.stdout.flush()
            else:
                print("All clients died. Stopping Stream: ", self.name)
                sys.stdout.flush()

                msg = STOP_P2P_LIVESTREAM_MESSAGE.copy()
                msg["serialNumber"] = self.serialno
                asyncio.run(self.ws.send_message(json.dumps(msg)))

    def run(self):
        print("Accepting connection for ", self.name)
        msg = STOP_TALKBACK.copy()
        msg["serialNumber"] = self.serialno
        asyncio.run(self.ws.send_message(json.dumps(msg)))
        while not self.run_event.is_set():
            self.update_threads()
            sys.stdout.flush()
            try:
                client_sock, client_addr = self.socket.accept()
                print("New connection added: ", client_addr, " for ", self.name)
                sys.stdout.flush()

                if self.name == "BackChannel":
                    client_sock.setblocking(True)
                    print("Starting BackChannel")
                    thread = ClientRecvThread(client_sock, run_event, self.name, self.ws, self.serialno)
                    thread.start()
                else:
                    client_sock.setblocking(False)
                    thread = ClientSendThread(client_sock, run_event, self.name, self.ws, self.serialno)
                    self.queues.append(thread.queue)
                    if self.ws:
                        msg = START_P2P_LIVESTREAM_MESSAGE.copy()
                        msg["serialNumber"] = self.serialno
                        asyncio.run(self.ws.send_message(json.dumps(msg)))
                    self.my_threads.append(thread)
                    thread.start()
            except socket.timeout:
                pass

class ClientSendThread(threading.Thread):
    def __init__(self, client_sock, run_event, name, ws, serialno):
        threading.Thread.__init__(self)
        self.client_sock = client_sock
        self.queue = Queue(100)
        self.run_event = run_event
        self.name = name
        self.ws = ws
        self.serialno = serialno

    def run(self):
        print("Thread running: ", self.name)
        sys.stdout.flush()

        try:
            while not self.run_event.is_set():
                if self.queue.empty():
                    # Send something to know if socket is dead
                    self.client_sock.sendall(bytearray(0))
                    time.sleep(0.1)
                else:
                    sys.stdout.flush()
                    self.client_sock.sendall(
                        bytearray(self.queue.get(True)["data"])
                    )
                    sys.stdout.flush()
        except socket.error as e:
            print("Connection lost", self.name, e)
            pass
        except socket.timeout:
            print("Timeout on socket for ", self.name)
            pass
        try:
            self.client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            print("Error shutdown socket: ", self.name)
        self.client_sock.close()
        print("Thread stopping: ", self.name)
        sys.stdout.flush()

class ClientRecvThread(threading.Thread):
    def __init__(self, client_sock, run_event, name, ws, serialno):
        threading.Thread.__init__(self)
        self.client_sock = client_sock
        self.run_event = run_event
        self.name = name
        self.ws = ws
        self.serialno = serialno

    def run(self):
        msg = START_TALKBACK.copy()
        msg["serialNumber"] = self.serialno
        asyncio.run(self.ws.send_message(json.dumps(msg)))
        try:
            curr_packet = bytearray()
            while not self.run_event.is_set():
                try:
                    data = self.client_sock.recv(RECV_CHUNK_SIZE)
                    curr_packet += bytearray(data)
                    if len(data) > 0 and len(data) < RECV_CHUNK_SIZE:
                        msg = SEND_TALKBACK_AUDIO_DATA.copy()
                        msg["serialNumber"] = self.serialno
                        msg["buffer"] = list(bytes(curr_packet))
                        asyncio.run(self.ws.send_message(json.dumps(msg)))
                        curr_packet = bytearray()
                except BlockingIOError:
                    # Resource temporarily unavailable (errno EWOULDBLOCK)
                    pass
        except socket.error as e:
            print("Connection lost", self.name, e)
            pass
        except socket.timeout:
            print("Timeout on socket for ", self.name)
            pass
        try:
            self.client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            print("Error shutdown socket: ", self.name)
        self.client_sock.close()
        msg = STOP_TALKBACK.copy()
        msg["serialNumber"] = self.serialno
        asyncio.run(self.ws.send_message(json.dumps(msg)))

class DeviceHandler:
    def __init__(self, serialno, ws):
        self.serialno = serialno
        self.ws = ws
        self.video_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.audio_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.backchannel_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        self.setup_sockets()

        self.video_thread = ClientAcceptThread(self.video_sock, run_event, "Video", ws, serialno)
        self.audio_thread = ClientAcceptThread(self.audio_sock, run_event, "Audio", ws, serialno)
        self.backchannel_thread = ClientAcceptThread(self.backchannel_sock, run_event, "BackChannel", ws, serialno)

        self.start_threads()

    def setup_sockets(self):
        self.video_sock.bind(("0.0.0.0", 0))
        self.video_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.video_sock.settimeout(1)
        self.video_sock.listen()

        self.audio_sock.bind(("0.0.0.0", 0))
        self.audio_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.audio_sock.settimeout(1)
        self.audio_sock.listen()

        self.backchannel_sock.bind(("0.0.0.0", 0))
        self.backchannel_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.backchannel_sock.settimeout(1)
        self.backchannel_sock.listen()

    def start_threads(self):
        self.video_thread.start()
        self.audio_thread.start()
        self.backchannel_thread.start()

    def stop(self):
        self.stop_socket(self.video_sock)
        self.stop_socket(self.audio_sock)
        self.stop_socket(self.backchannel_sock)

    @staticmethod
    def stop_socket(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            print("Error shutdown socket")
        sock.close()

class Connector:
    def __init__(self, run_event):
        self.devices = {}
        self.ws = None
        self.run_event = run_event

    def stop(self):
        for device in self.devices.values():
            device.stop()

    def set_ws(self, ws: EufySecurityWebSocket):
        self.ws = ws

    async def on_open(self):
        print(" on_open - executed")

    async def on_close(self):
        print(" on_close - executed")
        self.run_event.set()
        self.ws = None
        self.stop()
        os._exit(-1)

    async def on_error(self, message):
        print(f" on_error - executed - {message}")

    async def on_message(self, message):
        payload = message.json()
        message_type = payload["type"]
        if message_type == "result":
            message_id = payload["messageId"]
            print(f"on_message result: {payload}")
            sys.stdout.flush()
            if message_id == START_LISTENING_MESSAGE["messageId"]:
                message_result = payload[message_type]
                states = message_result["state"]
                for state in states["devices"]:
                    serialno = state["serialNumber"]
                    self.devices[serialno] = DeviceHandler(serialno, self.ws)
            if message_id == TALKBACK_RESULT_MESSAGE["messageId"] and "errorCode" in payload:
                error_code = payload["errorCode"]
                if error_code == "device_talkback_not_running":
                    for device in self.devices.values():
                        msg = START_TALKBACK.copy()
                        msg["serialNumber"] = device.serialno
                        asyncio.run(self.ws.send_message(json.dumps(msg)))

        if message_type == "event":
            message = payload[message_type]
            event_type = message["event"]
            serialno = message["serialNumber"]
            sys.stdout.flush()
            if event_type in ["livestream audio data", "livestream video data"]:
                event_value = message[EVENT_CONFIGURATION[event_type]["value"]]
                event_data_type = EVENT_CONFIGURATION[event_type]["type"]
                if event_data_type == "event":
                    thread_name = "Audio" if event_type == "livestream audio data" else "Video"
                    device = self.devices[serialno]
                    thread = device.audio_thread if thread_name == "Audio" else device.video_thread
                    for queue in thread.queues:
                        if queue.full():
                            print(f"{thread_name} queue full.")
                            queue.get(False)
                        queue.put(event_value)
            if event_type == "livestream error":
                print("Livestream Error!")
                if self.ws and serialno in self.devices:
                    device = self.devices[serialno]
                    msg = START_P2P_LIVESTREAM_MESSAGE.copy()
                    msg["serialNumber"] = device.serialno
                    asyncio.run(self.ws.send_message(json.dumps(msg)))

# Websocket connector
c = Connector(run_event)

async def init_websocket():
    ws: EufySecurityWebSocket = EufySecurityWebSocket(
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
        await ws.send_message(json.dumps(SET_API_SCHEMA))
        await ws.send_message(json.dumps(DRIVER_CONNECT_MESSAGE))
        while not run_event.is_set():
            await asyncio.sleep(1000)
    except Exception as ex:
        print(ex)
        print("init_websocket failed. Exiting.")
    os._exit(-1)

loop = asyncio.get_event_loop()
loop.run_until_complete(init_websocket())
