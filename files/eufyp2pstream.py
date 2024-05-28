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

video_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
audio_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
backchannel_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

EVENT_CONFIGURATION: dict = {
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

class Connector:
    def __init__(self, run_event):
        video_sock.bind(("0.0.0.0", 63336))
        video_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        video_sock.settimeout(1) # timeout for listening
        video_sock.listen()
        audio_sock.bind(("0.0.0.0", 63337))
        audio_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        audio_sock.settimeout(1) # timeout for listening
        audio_sock.listen()
        backchannel_sock.bind(("0.0.0.0", 63338))
        backchannel_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        backchannel_sock.settimeout(1) # timeout for listening
        backchannel_sock.listen()
        self.ws = None
        self.run_event = run_event
        self.devices = {}

    def stop(self):
        try:                                                                                            
            video_sock.shutdown(socket.SHUT_RDWR)                                                 
        except OSError:                                                                                 
            print("Error shutdown socket")                                                
        video_sock.close()
        try:                 
            audio_sock.shutdown(socket.SHUT_RDWR)
        except OSError:      
            print("Error shutdown socket")
        audio_sock.close()
        try:                 
            backchannel_sock.shutdown(socket.SHUT_RDWR)
        except OSError:      
            print("Error shutdown socket")
        backchannel_sock.close()

    def setWs(self, ws: EufySecurityWebSocket):
       
