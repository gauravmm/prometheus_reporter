#!/usr/bin/env python3

import contextlib
import http.server
import json
import os
import threading
import time

from prometheus_reporter import (gpu_mem, gpu_throttle, gpu_util)
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
                for k, v in ifunc(h).items():
                    assert k not in entry
                    entry[k] = v
            rv[id] = entry
        return rv


# For future implementation: GPU user tracking
"""
    procs = nvmlDeviceGetComputeRunningProcesses(handle)

    for p in procs:
        try:
            name = str(nvmlSystemGetProcessName(p.pid))
        except NVMLError as err:
            if (err.value == NVML_ERROR_NOT_FOUND):
                # probably went away
                continue
            else:
                name = handleError(err)

        if (p.usedGpuMemory == None):
            mem = 'N\A'
        else:
            mem = '%d MiB' % (p.usedGpuMemory / 1024 / 1024)
        strResult += '      <used_memory>' + mem + '</used_memory>\n'
"""

_gpu_meta = GPUMultiMeta([gpu_mem, gpu_throttle, gpu_util])

class GPUReporter(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
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
