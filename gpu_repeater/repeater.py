import atexit
import itertools
import os
import datetime
import requests

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, jsonify

CLIENT_PORT = 9212
SERVER_PORT = 8271
UPDATE_PERIOD = 60

# Get the list of other servers from the environment variable:
SERVERS = [s for s in os.environ.get("SLACK_BOT_OTHER_SERVERS").split(",") if s]

global GPU_RESPONSE
GPU_RESPONSE = {k: (None, None) for k in SERVERS}

app = Flask(__name__)
@app.route("/")
def page_index():
    return app.send_static_file('index.html')

@app.route("/script.js")
def page_script():
    return app.send_static_file('script.js')

@app.route("/style.css")
def page_style():
    return app.send_static_file('style.css')


@app.route("/update")
def update():
    return jsonify(GPU_RESPONSE)


def update(server):
    global GPU_RESPONSE

    r = requests.get('http://{}:{}'.format(server, CLIENT_PORT))
    if r.status_code == 200:
        now = datetime.datetime.now()
        GPU_RESPONSE[server] = (r.json(), now)


def main():
    assert SERVERS
    NEXT_SERVER = itertools.cycle(SERVERS)

    scheduler = BackgroundScheduler()
    scheduler.start()
    scheduler.add_job(
        func=lambda: update(next(NEXT_SERVER)),
        trigger=IntervalTrigger(seconds=UPDATE_PERIOD//len(SERVERS)),
        id='update_server',
        name='Query the server for running jobs',
        replace_existing=True)
    # Shut down the scheduler when exiting the app
    atexit.register(lambda: scheduler.shutdown())

    app.run(host='0.0.0.0', port=SERVER_PORT)

if __name__ == "__main__":
    main()
