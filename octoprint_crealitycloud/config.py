import json
import logging
import os

DEFAULT_STREAM_URL = "http://127.0.0.1/webcam/?action=stream"


def resolve_video_source(data):
    """
    Resolve the video source from the top-level keys of config.json
    (disableStream / externalUrl / enableRtspServer).

    :param data: dict from config.json (CrealityConfig.data(), may be empty)
    :return: dict with disableStream / enableRtspServer / source
    """
    data = data or {}
    source = (data.get("externalUrl") or "").strip() or DEFAULT_STREAM_URL
    return {
        "disableStream": bool(data.get("disableStream", False)),
        "enableRtspServer": bool(data.get("enableRtspServer", False)),
        "source": source,
    }


class CrealityConfig(object):
    def __init__(self, plugin) -> None:
        self._path = self.path = os.path.join(
            plugin.get_plugin_data_folder(), "config.json"
        )
        self._p2p_path = self.path = os.path.join(
            plugin.get_plugin_data_folder(), "p2pcfg.json"
        )
        self._p2pdata = {}
        self._data = {}
        self.load()

    def load(self):
        if os.path.exists(self._path):
            with open(self._path, "r") as f:
                try:
                    self._data = json.load(f)
                    f.close()
                except ValueError:
                    # corrupt JSON: drop it so it is re-created on next activation
                    os.remove(self._path)
        if os.path.exists(self._p2p_path):
            with open(self._p2p_path, "r") as f:
                try:
                    self._p2pdata = json.load(f)
                    f.close()
                except ValueError:
                    os.remove(self._p2p_path)

    def data(self):
        self.load()
        return self._data

    def p2p_data(self):
        self.load()
        return self._p2pdata

    def save(self, key, val):
        self._data[key] = val
        with open(self._path, "w") as f:
            json.dump(self._data, f)
            f.close()

    def save_p2p_config(self, key, val):
        self._p2pdata[key] = val
        with open(self._p2p_path, "w") as f:
            json.dump(self._p2pdata, f)
            f.close()
