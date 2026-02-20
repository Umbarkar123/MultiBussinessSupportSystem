import pytz
from datetime import datetime

IST = pytz.timezone('Asia/Kolkata')
now_utc = datetime.utcnow()
now_ist = datetime.now(IST)
now_ist_naive = now_ist.replace(tzinfo=None)

print(f"UTC Time: {now_utc}")
print(f"IST Time: {now_ist}")
print(f"IST Naive (stored in DB): {now_ist_naive}")

# Mimic the scheduler check
scheduled_time = now_ist_naive # Mimic a just-submitted form
if scheduled_time <= now_ist_naive:
    print("✅ Success: Scheduler would trigger this call immediately!")
else:
    print("❌ Failure: Scheduler would wait.")
