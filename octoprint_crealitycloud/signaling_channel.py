import asyncio
import time
import websocket
import threading
import json
import logging

class WebSocketConnectionException(Exception):
    pass

class WebSocketClient:

    def __init__(self, url, queue, token ,subprotocols=None, waitsecs=120, on_giveup=None):
        self._logger = logging.getLogger("octoprint.plugins.crealitycloud")
        self._mutex = threading.RLock()
        self._user_agent = "crealitycloud"
        self._url = url
        self._queue = queue
        self.reconnect_count = 0
        self._closing = False
        self._on_giveup = on_giveup
        self.subprotocols = subprotocols
        self.token = token

        self.ws = websocket.WebSocketApp(
            self._url,
            on_message=self.on_message,
            on_open=self.on_open,
            on_close=self.on_close,
            on_error=self.on_error,
            #header=header,
            subprotocols=self.subprotocols
        )
        wst = threading.Thread(target=self.ws.run_forever)
        wst.daemon = True
        wst.start()

        for i in range(waitsecs * 10):  # Give it up to 120s for ws hand-shaking to finish
            if self.connected():
                return
            time.sleep(0.1)
        self.close()
        raise WebSocketConnectionException('Not connected to websocket server after {}s'.format(waitsecs))

    def send(self, data, as_binary=False):
        self._logger.info("send" + str(data))
        with self._mutex:
            if self.connected():
                if as_binary:
                    self.ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
                else:
                    self.ws.send(data)

    def connected(self):
        with self._mutex:
            return self.ws.sock and self.ws.sock.connected

    def close(self):
        with self._mutex:
            self._closing = True
            self.ws.keep_running = False
            self.ws.close()

    def on_error(self, ws, error):
        # only the current socket may trigger a reconnect, so a stale
        # callback from an already-replaced socket is ignored
        if self._closing or ws is not self.ws:
            return
        self._logger.error("websocket error: %r" % (error,))
        self._reconnect()

    def on_message(self, ws, msg):
        self._logger.info("recv++++++++++++" + str(msg))
        try:
            data = json.loads(msg)
            action = data["action"]
        except (TypeError, ValueError, KeyError) as e:
            self._logger.error("bad websocket message %r: %r" % (msg, e))
            return
        if action != "join":
            self._queue.put(msg)


    def on_close(self, ws, status, msg):
        if self._closing or ws is not self.ws:
            return
        self._logger.info("websocket closed: status=%r msg=%r" % (status, msg))
        self._reconnect()

    def on_open(self, ws):
        self.reconnect_count = 0
        data = {
                        "action": "join",
                        "to": "server",
                        "clientCtx":{
                                        "device_brand":"raspberry",
                                        "os_version":"linux",
                                        "platform_type":1,
                                        "app_version":"v1.1.2"
                                    },
                        "token":{
                                    "jwtToken":self.token
                        }

                }
        ws.send(json.dumps(data))

    def _reconnect(self):
        giveup = False
        with self._mutex:
            if self._closing:
                return
            if self.reconnect_count >= 100:
                self._logger.error("websocket reconnect limit (100) reached, giving up")
                giveup = True
            else:
                self.reconnect_count += 1
                attempt = self.reconnect_count
                old = self.ws
        if giveup:
            # shut the webrtc service down so the next token can restart it cleanly
            if self._on_giveup is not None:
                try:
                    self._on_giveup()
                except Exception as e:
                    self._logger.error("on_giveup callback failed: %r" % (e,))
            return
        self._logger.info("reconnecting websocket, attempt %d" % attempt)
        try:
            old.keep_running = False
        except Exception:
            pass
        ws = websocket.WebSocketApp(
            self._url,
            on_message=self.on_message,
            on_open=self.on_open,
            on_close=self.on_close,
            on_error=self.on_error,
            subprotocols=self.subprotocols)
        self.ws = ws
        try:
            wst = threading.Thread(target=ws.run_forever)
            wst.daemon = True
            wst.start()
        except Exception as e:
            self._logger.error("websocket reconnect failed: %r" % (e,))
            ws.close()

    def token_update(self, token):
        self.token = str(token)
