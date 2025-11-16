import json
import os

DATA_FILE = "queue_data.json"

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"desks": {}, "tickets": {}, "clerks": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {"desks": {}, "tickets": {}, "clerks": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def ensure_config():
    """
    Check if services and desks are configured.
    If not, ask user for input and update the JSON file.
    Each desk will have a status field: 'empty' or 'occupied'.
    """
    data = load_data()

    if not data.get("desks") or not any(data["desks"].values()):
        print("No pre-set services/desks found.")
        services_count = int(input("Enter number of services to configure: "))
        for i in range(services_count):
            service = input(f"Enter name for service {i+1}: ").strip().lower()
            desk_count = int(input(f"Enter number of desks for service '{service}': "))
            # Each desk is stored as a dict with status
            data["desks"][service] = [
                {"id": str(d+1), "status": "empty"} for d in range(desk_count)
            ]
        data["tickets"] = {}
        data["clerks"] = {}
        save_data(data)
        print("Configuration saved to queue_data.json")
    else:
        print("Services/desks already configured.")

    return data

if __name__ == "__main__":
    ensure_config()
