#!/usr/bin/env python3

import contextlib
import datetime
import http.server
import json
import os
import threading
import time

import psutil
from prometheus_reporter import gpu_mem, gpu_throttle, gpu_util
from py3nvml.py3nvml import *

PORT = 9212

#
# GPU Stats
#


class GPUMultiMeta(object):
    # Only open one handle, and share that across all accesses.
    _init = False
    _id_handles = []

    def __init__(self, innerfuncs):
        self._fns = innerfuncs

    def __call__(self):
        if not self._init:
            self._init = True
            for i in range(nvmlDeviceGetCount()):
                handle = nvmlDeviceGetHandleByIndex(i)
                id = str(nvmlDeviceGetUUID(handle))
                self._id_handles.append((id, handle))

        rv = {}
        for id, h in self._id_handles:
            entry = {}
            for ifunc in self._fns:
                entry[ifunc.__name__] = ifunc(h)
            rv[id] = entry
        return rv

def gpu_procs(handle):
    now = datetime.datetime.now()

    procs = nvmlDeviceGetComputeRunningProcesses(handle)

    rv = []
    for p in procs:
        pid = p.pid

        try:
            mem = 0 if p.usedGpuMemory == None else p.usedGpuMemory

            # Check the user owning this process.
            proc = psutil.Process(pid=pid)
            pinfo = proc.as_dict(attrs=['pid', 'cmdline', 'name', 'username', 'create_time', 'cwd'])
            pinfo["gpu_mem"] = mem

            # Update the cache:
            rv.append(pinfo)
        except:
            pass

    return rv


_gpu_meta = GPUMultiMeta([gpu_mem, gpu_throttle, gpu_util, gpu_procs])

class GPUReporter(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()

        self.wfile.write(json.dumps(_gpu_meta()).encode())


def main():
    try:
        nvmlInit()
    except NVMLError_LibraryNotFound:
        print("NVML Library Not Found")
        return

    httpd = http.server.HTTPServer(("", PORT), GPUReporter)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("keyboard interrupt")
    finally:
        httpd.__exit__(None, None, None)
        nvmlShutdown()


if __name__ == "__main__":
    main()
