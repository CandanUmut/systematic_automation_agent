from automation import pru_db
pru_db.put("deadbeef", "SGVsbG8gU1RIUw==")   # "Hello STHS"
print("PRU →", pru_db.get("deadbeef"))