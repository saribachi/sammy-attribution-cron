import time, subprocess, sys
INTERVAL = 3600  # hourly
print("[sammy-attribution-cron] starting; interval", INTERVAL, "s", flush=True)
while True:
    print("\n===== classifier run =====", flush=True)
    try:
        subprocess.run([sys.executable, "governed_classifier.py", "--commit"], check=False)
    except Exception as e:
        print("run error:", e, flush=True)
    time.sleep(INTERVAL)
