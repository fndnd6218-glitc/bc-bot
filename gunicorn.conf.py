import threading

workers = 1
threads = 1
preload_app = False
timeout = 180
graceful_timeout = 120
# Avoid token-bearing URLs in access/error output. Application logs never include secrets.
accesslog = None
errorlog = "-"


def post_worker_init(worker):
    from cloud import app
    threading.Thread(target=app.bootstrap, daemon=True).start()
