#!/usr/bin/env python
"""

WARNING: THIS IS OBSOLETE, NOW THE SERVER IS STARTED USING 'serve.wsgi'

"""

import os
import signal
import subprocess
import sys
import time
import traceback

try:
    from urllib2 import urlopen, URLError  # python2
except ImportError:
    from urllib.request import urlopen  # python3
    from urllib.error import URLError

if not hasattr(subprocess.Popen, 'send_signal'):
    def send_signal(self, sig):
        os.kill(self.pid, sig)
    subprocess.Popen.send_signal = send_signal

DATABASE = "/data/db/data"
# the database is currently found there

# The server hangs while connecting to trac, so we poll it and
# restart if needed.

HTTP_TIMEOUT = 60
POLL_INTERVAL = 180
KILL_WAIT = 5

open("keepalive", "w").write(str(os.getpid()))

p = None
try:
    # Start mongodb
    mongo_process = subprocess.Popen(["mongod", "--port=21002",
                                      "--dbpath=" + DATABASE],
                                     stderr=subprocess.STDOUT)

    # Run the server
    while True:

        if not os.path.exists("keepalive"):
            break

        if p is None or p.poll() is not None:
            # The subprocess died.
            restart = True
        else:
            try:
                print("Testing url...")
                urlopen("http://patchbot.sagemath.org/", timeout=HTTP_TIMEOUT)
                print("    ...good")
                restart = False
            except URLError as e:
                msg = "    ...bad {}".format(e)
                print(msg)
                restart = True

        if restart:
            if p is not None and p.poll() is None:
                print("SIGTERM")
                p.send_signal(signal.SIGTERM)
                time.sleep(KILL_WAIT)
                if p.poll() is None:
                    print("SIGKILL")
                    p.kill()
                    time.sleep(KILL_WAIT)

            print("Starting server...")
            p = subprocess.Popen([sys.executable, "serve.py", "--port=21100"])
            open("server.pid", "w").write(str(p.pid))
            print("    ...done.")
        time.sleep(POLL_INTERVAL)

finally:
    traceback.print_exc()
    mongo_process.send_signal(signal.SIGTERM)
    if p is not None and p.poll() is None:
        p.kill()
