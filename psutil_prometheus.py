#!/usr/bin/env python3

import http.server
import threading
import time

import psutil

PORT = 9110

# Quick and dirty conversion from psutil data to prometheus input.
def metric_str(stats, family, *args, **kwargs):
    def _tostr(parts, val, axes):
        rv=[]
        if isinstance(val, list):
            for i, v in enumerate(val):
                rv.extend(_tostr(parts + ["{}=\"{}\"".format(axes[0], i)], v, axes[1:]))
        elif hasattr(val, "_fields"):
            for k in val._fields:
                rv.extend(_tostr(parts + ["{}=\"{}\"".format(axes[0], k)], getattr(val, k), axes[1:]))
        elif isinstance(val, dict):
            for k, v in val.items():
                rv.extend(_tostr(parts + ["{}=\"{}\"".format(axes[0], k)], v, axes[1:]))
        else:
            if "fmt" in kwargs:
                val = kwargs["fmt"](val)

            if parts:
                return ["{}{{{}}} {}".format(family, ",".join(parts), val)]
            else:
                return ["{} {}".format(family, val)]
        return rv
    return _tostr([], stats, args)

def metric_filter(val, keep):
    if isinstance(val, list):
        data = enumerate(val)
    elif hasattr(val, "_fields"):
        data = ((k, getattr(val, k)) for k in val._fields)
    elif isinstance(val, dict):
        data = val.items()
    return {k: v for k, v in data if k in keep}


handlers = []
# Some psutil calls require us to discard the first result
psutil.cpu_times_percent()

# CPU Stats
def cpu():
    parts = []
    for id, cpu in enumerate(psutil.cpu_times_percent(percpu=True)):
        cpustat = {k: getattr(cpu, k) for k in ["user", "system", "idle", "iowait"]}
        cpustat["allirq"] = cpu.irq + cpu.softirq
        cpustat["other"] = cpu.nice + cpu.steal + cpu.guest + cpu.guest_nice
        parts.append(cpustat)
    return metric_str(parts, "cpu", "id", "type", fmt=lambda x: x/100)
handlers.append(cpu)

# Interrupts Stats
handlers.append(lambda: metric_str(psutil.cpu_stats(),
                "irq", "type"))

# Virtual Memory
def virtual_memory():
    vmem = psutil.virtual_memory()
    parts = {}
    parts["used"] = vmem.percent/100
    parts["cached"] = vmem.cached/vmem.total
    return metric_str(parts, "vmem", "type")
handlers.append(virtual_memory)

# Swap percentage
def swap():
    swp = psutil.swap_memory()
    return metric_str({"used": swp.percent}, "swap", "type")
handlers.append(swap)

# Disk usage and stats
def disk():
    parts = {}
    ioc = psutil.disk_io_counters(perdisk=True)

    for p in psutil.disk_partitions():
        if p.mountpoint not in ["/boot"]:
            disk_stat = {}
            dioc = ioc[p.device[5:]]
            disk_stat["used"] = psutil.disk_usage(p.mountpoint).percent/100
            disk_stat["read_size"] = dioc.read_bytes/dioc.read_count
            disk_stat["write_size"] = dioc.write_bytes/dioc.write_count
            disk_stat["read_time"] = dioc.read_time/1000
            disk_stat["write_time"] = dioc.write_time/1000
            parts[p.mountpoint] = disk_stat
    return metric_str(parts, "disk", "path", "type")
handlers.append(disk)

def netio():
    parts = {}
    for k1, nic in psutil.net_io_counters(pernic=True).items():
        if k1 != "lo":
            parts[k1] = {"sent": nic.bytes_sent, "recv": nic.bytes_recv}
    return metric_str(parts, "network", "id", "type")
handlers.append(netio)

class StatsPrintHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(s):
        s.send_response(200)
        s.send_header("Content-type", "text/plain")
        s.end_headers()

        for h in handlers:
            s.wfile.write("\n".join(h()).encode())
            s.wfile.write("\n".encode())

if __name__ == "__main__":
    with http.server.HTTPServer(("", PORT), StatsPrintHandler) as httpd:
        httpd.serve_forever()
