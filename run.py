import time, subprocess, sys
INTERVAL = 3600  # hourly
print("[sammy-attribution-cron] starting; interval", INTERVAL, "s", flush=True)
# Ticket watch runs alongside, polling every minute; restarted hourly if it died.
watch = subprocess.Popen([sys.executable, "-u", "ticket_watch.py"])
while True:
    if watch.poll() is not None and watch.returncode != 0:
        print("ticket watch exited", watch.returncode, "- restarting", flush=True)
        watch = subprocess.Popen([sys.executable, "-u", "ticket_watch.py"])
    print("\n===== classifier run =====", flush=True)
    try:
        subprocess.run([sys.executable, "governed_classifier.py", "--commit"], check=False)
    except Exception as e:
        print("run error:", e, flush=True)
    try:
        subprocess.run([sys.executable, "weekly_sweep.py"], check=False)
    except Exception as e:
        print("sweep error:", e, flush=True)
    time.sleep(INTERVAL)
