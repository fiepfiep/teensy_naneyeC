"""Saleae Logic 2 control through its MCP server, and reading its binary exports.

Logic 2 (2.4.x) serves MCP as plain JSON-RPC over HTTP on port 10530. Talking to it
directly keeps captures scriptable from the same Python as everything else, with no
dependency on the gRPC automation package.

    from naneye.saleae import Logic
    logic = Logic()
    cap = logic.start_triggered(channels=[0, 1, 2, 3], rate=250e6, trigger=1,
                                after_s=0.5)
    ...                       # make the device do the thing
    logic.wait(cap)
    chans = logic.export_digital(cap, "build/capture")   # {channel: DigitalTrace}
"""

from __future__ import annotations

import json
import os
import struct
import urllib.request
from dataclasses import dataclass

import numpy as np

DEFAULT_URL = "http://127.0.0.1:10530/mcp"


class SaleaeError(RuntimeError):
    pass


class Logic:
    """Minimal client for the Logic 2 MCP server."""

    def __init__(self, url: str = DEFAULT_URL, timeout: float = 600.0):
        self.url = url
        self.timeout = timeout
        self._id = 0

    def call(self, tool: str, args: dict | None = None):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                           "params": {"name": tool, "arguments": args or {}}}).encode()
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            reply = json.loads(resp.read())
        if "error" in reply:
            raise SaleaeError(f"{tool}: {reply['error']}")
        result = reply.get("result", {})
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content", []))
            raise SaleaeError(f"{tool}: {text}")
        if "structuredContent" in result:
            return result["structuredContent"]
        texts = [c.get("text", "") for c in result.get("content", [])]
        try:
            return json.loads(texts[0]) if texts else {}
        except (ValueError, IndexError):
            return {"text": " ".join(texts)}

    def devices(self) -> list:
        return self.call("get_devices", {}).get("devices", [])

    def start_triggered(self, channels, rate: float, trigger: int, after_s: float,
                        rising: bool = True, threshold_v: float = 3.3,
                        device_id: str | None = None, analog=(),
                        analog_rate: float | None = None) -> int:
        """Arm a capture that fires on an edge of `trigger` and records `after_s` after it.

        `analog` adds analog channels (the Logic Pro inputs are dual-use); give
        `analog_rate` with them.
        """
        chans = {"digitalChannels": list(channels)}
        if analog:
            chans["analogChannels"] = list(analog)
        cfg = {
            "logicChannels": chans,
            "digitalSampleRate": int(rate),
            "digitalThresholdVolts": threshold_v,
        }
        if analog:
            cfg["analogSampleRate"] = int(analog_rate)
        args = {
            "logicDeviceConfiguration": cfg,
            "captureConfiguration": {
                "digitalCaptureMode": {
                    "triggerType": 1 if rising else 2,
                    "triggerChannelIndex": trigger,
                    "afterTriggerSeconds": after_s,
                },
            },
        }
        if device_id:
            args["deviceId"] = device_id
        return self._capture_id(self.call("start_capture", args))

    def start_timed(self, channels, rate: float, seconds: float,
                    threshold_v: float = 3.3, analog=(),
                    analog_rate: float | None = None) -> int:
        chans = {"digitalChannels": list(channels)}
        cfg = {"logicChannels": chans, "digitalSampleRate": int(rate),
               "digitalThresholdVolts": threshold_v}
        if analog:
            chans["analogChannels"] = list(analog)
            cfg["analogSampleRate"] = int(analog_rate)
        args = {"logicDeviceConfiguration": cfg,
                "captureConfiguration": {"timedCaptureMode": {"durationSeconds": seconds}}}
        return self._capture_id(self.call("start_capture", args))

    def add_analyzer(self, capture_id: int, name: str, settings: dict,
                     label: str | None = None) -> int:
        """Add a protocol analyser (it also appears in the Logic 2 window). Returns its id.

        Settings are plain Python values; the MCP server wants each wrapped by type.
        """
        def typed(v):
            if isinstance(v, bool):
                return {"boolValue": v}
            if isinstance(v, (int, float)):
                return {"numberValue": v}
            return {"stringValue": str(v)}

        args = {"captureId": capture_id, "analyzerName": name,
                "settings": {k: typed(v) for k, v in settings.items()}}
        if label:
            args["analyzerLabel"] = label
        r = self.call("add_analyzer", args)
        for key in ("analyzerId", "id"):
            if key in r:
                return int(r[key])
        raise SaleaeError(f"add_analyzer returned no id: {r}")

    def export_table(self, capture_id: int, path: str) -> str:
        """Export every analyser's decoded results as one CSV; returns the path."""
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.call("export_data_table_csv", {"captureId": capture_id, "filepath": path})
        return path

    @staticmethod
    def _capture_id(result) -> int:
        for key in ("captureId", "id"):
            if key in result:
                return int(result[key])
        raise SaleaeError(f"start_capture returned no capture id: {result}")

    def wait(self, capture_id: int):
        return self.call("wait_capture", {"captureId": capture_id})

    def stop(self, capture_id: int):
        return self.call("stop_capture", {"captureId": capture_id})

    def close(self, capture_id: int):
        return self.call("close_capture", {"captureId": capture_id})

    def save(self, capture_id: int, path: str):
        return self.call("save_capture", {"captureId": capture_id,
                                          "filepath": os.path.abspath(path)})

    def export_analog(self, capture_id: int, directory: str, channels) -> dict:
        """Export analog channels as binary and load them: {channel: AnalogTrace}."""
        directory = os.path.abspath(directory)
        os.makedirs(directory, exist_ok=True)
        self.call("export_raw_data_binary", {
            "captureId": capture_id, "directory": directory, "analogDownsampleRatio": 1,
            "logicChannels": {"analogChannels": list(channels)}})
        out = {}
        for name in sorted(os.listdir(directory)):
            if name.startswith("analog_") and name.endswith(".bin"):
                ch = int(name[len("analog_"):-len(".bin")])
                out[ch] = read_analog_bin(os.path.join(directory, name))
        return out

    def export_digital(self, capture_id: int, directory: str, channels=None) -> dict:
        """Export digital channels as binary and load them: {channel: DigitalTrace}."""
        directory = os.path.abspath(directory)
        os.makedirs(directory, exist_ok=True)
        args = {"captureId": capture_id, "directory": directory, "analogDownsampleRatio": 1}
        if channels is not None:
            args["logicChannels"] = {"digitalChannels": list(channels)}
        self.call("export_raw_data_binary", args)
        out = {}
        for name in sorted(os.listdir(directory)):
            if name.startswith("digital_") and name.endswith(".bin"):
                ch = int(name[len("digital_"):-len(".bin")])
                out[ch] = read_digital_bin(os.path.join(directory, name))
        return out


@dataclass
class DigitalTrace:
    """One digital channel: its level before the first transition, and transition times."""

    initial: int
    begin: float
    end: float
    times: np.ndarray  # seconds, float64, each a level change

    def level_at(self, t) -> np.ndarray:
        """Logic level at time(s) t."""
        n = np.searchsorted(self.times, t, side="right")
        return ((self.initial + n) & 1).astype(np.uint8)

    def edges(self, rising: bool = True) -> np.ndarray:
        """Times of rising (or falling) edges."""
        # Transition k leaves the line at level (initial + k + 1) & 1.
        after = (self.initial + np.arange(1, len(self.times) + 1)) & 1
        return self.times[after == (1 if rising else 0)]


def read_digital_bin(path: str) -> DigitalTrace:
    """Read a Logic 2 digital binary export (format version 0).

    <SALEAE> | int32 version | int32 type (0 = digital) | uint32 initial state |
    double begin | double end | uint64 n | double[n] transition times
    """
    with open(path, "rb") as f:
        ident = f.read(8)
        if ident != b"<SALEAE>":
            raise SaleaeError(f"{path}: not a Saleae export (starts {ident!r})")
        version, kind = struct.unpack("<ii", f.read(8))
        if version != 0 or kind != 0:
            raise SaleaeError(f"{path}: unsupported export version {version} type {kind}")
        initial, = struct.unpack("<I", f.read(4))
        begin, end = struct.unpack("<dd", f.read(16))
        n, = struct.unpack("<Q", f.read(8))
        times = np.fromfile(f, dtype="<f8", count=n)
    if len(times) != n:
        raise SaleaeError(f"{path}: expected {n} transitions, read {len(times)}")
    return DigitalTrace(initial=int(initial), begin=begin, end=end, times=times)


@dataclass
class AnalogTrace:
    begin: float
    sample_rate: float
    samples: np.ndarray  # volts, float32

    def times(self) -> np.ndarray:
        return self.begin + np.arange(len(self.samples)) / self.sample_rate

    def at(self, t0: float, t1: float):
        """(times, volts) between t0 and t1."""
        i0 = max(0, int((t0 - self.begin) * self.sample_rate))
        i1 = min(len(self.samples), int((t1 - self.begin) * self.sample_rate) + 1)
        t = self.begin + np.arange(i0, i1) / self.sample_rate
        return t, self.samples[i0:i1]


def read_analog_bin(path: str) -> AnalogTrace:
    """Read a Logic 2 analog binary export (format version 0).

    <SALEAE> | int32 version | int32 type (1 = analog) | double begin |
    uint64 sample rate | uint64 downsample | uint64 n | float32[n] volts
    """
    with open(path, "rb") as f:
        ident = f.read(8)
        if ident != b"<SALEAE>":
            raise SaleaeError(f"{path}: not a Saleae export (starts {ident!r})")
        version, kind = struct.unpack("<ii", f.read(8))
        if version != 0 or kind != 1:
            raise SaleaeError(f"{path}: unsupported export version {version} type {kind}")
        begin, = struct.unpack("<d", f.read(8))
        rate, downsample, n = struct.unpack("<QQQ", f.read(24))
        samples = np.fromfile(f, dtype="<f4", count=n)
    return AnalogTrace(begin=begin, sample_rate=float(rate) / max(downsample, 1),
                       samples=samples)
