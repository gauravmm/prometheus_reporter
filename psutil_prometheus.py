#!/usr/bin/env python3

import http.server
import os
import threading
import time
import contextlib

import psutil
from py3nvml.py3nvml import *

PORT = 9110

# Quick and dirty conversion from nested lists, dicts, or namedtuples to prometheus data.
def _tostr(data, name, labels, **kwargs):
    def _tostr_inner(parts, val, axes):
        rv=[]
        if isinstance(val, list):
            for i, v in enumerate(val):
                rv.extend(_tostr_inner(parts + ["{}=\"{}\"".format(axes[0], i)], v, axes[1:]))
        elif hasattr(val, "_fields"):
            for k in val._fields:
                rv.extend(_tostr_inner(parts + ["{}=\"{}\"".format(axes[0], k)], getattr(val, k), axes[1:]))
        elif isinstance(val, dict):
            for k, v in val.items():
                rv.extend(_tostr_inner(parts + ["{}=\"{}\"".format(axes[0], k)], v, axes[1:]))
        else:
            if "fmt" in kwargs:
                val = kwargs["fmt"](val)

            if parts:
                return ["{}{{{}}} {}".format(name, ",".join(parts), val)]
            else:
                return ["{} {}".format(name, val)]
        return rv
    return _tostr_inner([], data, labels)

global ALL_METRICS
ALL_METRICS = []

def not_implemented():
    raise RuntimeError("Not Implemented!")

class Metric(object):
    def __init__(self, name, help, query=not_implemented, 
                 labels=[], typ="gauge", fmt=lambda x: x, unit=None):
        assert typ in ["gauge", "summary", "counter", "histogram"]

        self.name = name
        self.help = help
        self.labels = labels
        self.tostr = _tostr
        self.typ = typ
        self.query = query
        self.unit = unit
        self.fmt = fmt

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def get(self):
        rv = []
        rv.append("# HELP {} {}".format(self.name, self.help))
        rv.append("# TYPE {} {}".format(self.name, self.typ))
        if self.unit is not None:
            rv.append("# UNIT {} {}".format(self.name, self.unit))
        return rv + self.tostr(self.query(), self.name, self.labels, fmt=self.fmt)

    def register(self):
        global ALL_METRICS
        ALL_METRICS.append(self)

# GPU Metrics only work if NVML can be loaded
class GPUMetric(Metric):
    _NVMLInitAttempted = False
    _NVMLInitSuccess = False

    def __init__(self, *args, **kwargs):
        super().__init__("gpu", "gpu statistics", gpu, ["id", "type"])

    def __enter__(self):
        super().__enter__()
        if not self._NVMLInitAttempted:
            self._NVMLInitAttempted = True
            # Try to initialize NVML:
            try:
                nvmlInit()
                self._NVMLInitSuccess = True
            except NVMLError_LibraryNotFound:
                self._NVMLInitSuccess = False
        return self

    def __exit__(self, *a):
        if self._NVMLInitSuccess:
            nvmlShutdown()
        return super().__exit__(*a)
    
    def get(self):
        if self._NVMLInitSuccess:
            return super().get()
        else:
            return ["# WARNING `{}` not available; NVML not found.".format(self.name)]



Metric("load", "one-minute average of run-queue length, the classic unix system load",
        lambda: os.getloadavg()[0]).register()


# Uptime
def uptime():
    with open('/proc/uptime', 'r') as f:
        return float(f.readline().split()[0])

Metric("uptime", "time since last boot",
        uptime, unit="seconds").register()


# CPU Stats
def cpu():
    parts = []
    for id, cpu in enumerate(psutil.cpu_times_percent(percpu=True)):
        cpustat = {k: getattr(cpu, k) for k in ["user", "system", "idle", "iowait"]}
        cpustat["allirq"] = cpu.irq + cpu.softirq
        cpustat["other"] = cpu.nice + cpu.steal + cpu.guest + cpu.guest_nice
        parts.append(cpustat)
    return parts

class CPUMetric(Metric):
    def __init__(self):
        super().__init__("cpu", "cpu allocation", cpu, ["id", "type"], fmt=lambda x: x/100, unit="percent")

    def __enter__(self):
        # This psutil call requires us to discard the first result
        psutil.cpu_times_percent()
        return super().__enter__()

CPUMetric().register()


# Interrupts Stats
Metric("irq", "number of interrupts", psutil.cpu_stats, ["type"], typ="counter").register()


# Virtual Memory
def virtual_memory():
    vmem = psutil.virtual_memory()
    parts={}
    parts["used"] = vmem.percent/100
    parts["cached"] = vmem.cached/vmem.total
    return parts
Metric("vmem", "virtual memory statistics", virtual_memory, ["type"], unit="percent").register()

# Swap percentage
Metric("swap", "swap memory", lambda: {"used": psutil.swap_memory().percent}, ["type"], unit="percent").register()


# Disk usage and stats
def disk_meta(fn):
    def _retfunc():
        parts={}
        ioc = psutil.disk_io_counters(perdisk=True)

        for p in psutil.disk_partitions():
            if p.mountpoint not in ["/boot"]:
                parts[p.mountpoint] = fn(p, ioc[p.device[5:]])
        return parts
    return _retfunc

disk_usage = disk_meta(lambda p, dioc: psutil.disk_usage(p.mountpoint).percent/100)
Metric("disk_usage", "fraction of disk used", disk_usage, ["path"], unit="percent").register()

class RunningDelta(object):
    def __init__(self):
        self.prev = 0
    def __call__(self, x):
        rv = x - self.prev
        self.prev = x
        return rv

class DiskRequestSizer(object):
    def __init__(self):
        self.rbD = RunningDelta()
        self.rcD = RunningDelta()
        self.wbD = RunningDelta()
        self.wcD = RunningDelta()
    
    def __call__(self, p, disk_io_count):
        rv = {}
        rb = self.rbD(disk_io_count.read_bytes)
        rc = self.rcD(disk_io_count.read_count)
        if rc > 0:
            rv["read"] = rb/rc

        wb = self.wbD(disk_io_count.write_bytes)
        wc = self.wcD(disk_io_count.write_count)
        if rc > 0:
            rv["write"] = wb/wc

        return rv

disk_req_size = disk_meta(DiskRequestSizer())
Metric("disk_req_size", "sliding-window average size of requests to the disk", disk_req_size, ["path", "direction"], typ="histogram", unit="bytes").register()

disk_time = disk_meta(lambda p, dioc: {"read": dioc.read_time/1000, "write": dioc.write_time/1000})
Metric("disk_time", "time spent waiting for disk to respond", disk_time, ["path", "direction"], unit="seconds").register()

def netio():
    parts={}
    for k1, nic in psutil.net_io_counters(pernic=True).items():
        if k1 != "lo":
            parts[k1] = {"sent": nic.bytes_sent, "recv": nic.bytes_recv}
    return parts
Metric("network", "network i/o", netio, ["id", "type"], unit="bytes").register()

# GPU Stats
def gpu():
    parts = {}
    for i in range(nvmlDeviceGetCount()):
        handle = nvmlDeviceGetHandleByIndex(i)
        # Get UUID:
        id = str(nvmlDeviceGetUUID(handle))

        # Gather stats:
        p = {}
        try:
            memInfo = nvmlDeviceGetMemoryInfo(handle)
            p["mem_used"] = str(memInfo.used / memInfo.total)

            memBar1 = nvmlDeviceGetBAR1MemoryInfo(handle)
            p["mem_mmap"] = str(memBar1.bar1Total / memInfo.total)
            p["mem_mmap_used"] = str(memBar1.bar1Used / memBar1.bar1Total)
        except NVMLError as err:
            print("NVML error : memInfo")

        try:
            util = nvmlDeviceGetUtilizationRates(handle)
            p["gpu_util"] = str(util.gpu/100)
            p["mem_util"] = str(util.memory/100)
        except NVMLError as err:
            print("NVML error : device utilization")

        try:
            p["temp"] = str(nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU))
        except NVMLError as err:
            print("NVML error : temperature")

        try:
            p["temp"] = str(nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU))
        except NVMLError as err:
            print("NVML error : temperature")

        parts[id] = p
    return parts

GPUMetric("gpu", "gpu performance metrics", gpu, ["id", "type"]).register()


class StatsPrintHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, metrics, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.do_GET = lambda s: self.do_GET(metrics, s)

    def do_GET(s):
        s.send_response(200)
        s.send_header("Content-type", "text/plain")
        s.end_headers()

        for m in metrics:
            s.wfile.write("\n".join(m.get()).encode())
            s.wfile.write("\n".encode())

if __name__ == "__main__":
    # Open context for all metrics:
    try:
        with contextlib.ExitStack() as metric_stack:
            metrics = [metric_stack.enter_context(m) for m in ALL_METRICS]
            with http.server.HTTPServer(("", PORT), lambda *a, **kwa: StatsPrintHandler(metrics, *a, **kwa)) as httpd:
                httpd.serve_forever()
    except KeyboardInterrupt:
        print("keyboard interrupt")
        pass
