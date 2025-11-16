import time
import json
from typing import List, Dict, Optional, Tuple
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI()

DATA_FILE = "queue_data.json"

def init_data_file():
    """
    Initialize the JSON file if missing or empty.
    If not empty, verify structure and load without altering existing data.
    """
    default_structure = {
        "desks": {},
        "tickets": {}
    }

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # File missing or corrupted → create default
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(default_structure, f, indent=2)
        return default_structure

    # Verify structure without overwriting existing data
    if not isinstance(data, dict):
        return default_structure

    if "desks" not in data or not isinstance(data["desks"], dict):
        data["desks"] = {}

    if "tickets" not in data or not isinstance(data["tickets"], dict):
        data["tickets"] = {}

    return data

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"desks": {}, "tickets": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# Initialize JSON file at server start
init_data_file()

def load_html(filename: str) -> str:
    with open(f"templates/{filename}", "r", encoding="utf-8") as f:
        return f.read()
    
# --- Queue Management ---
class QueueSystem:
    def __init__(self):
        self.queues: Dict[str, List[str]] = {}
        self.ticket_counter: Dict[str, int] = {}
        self.assigned: Dict[Tuple[str, str], Optional[str]] = {}
        self.serving: Dict[Tuple[str, str], Optional[str]] = {}

    def generate_ticket(self, service: str) -> str:
        if service not in self.ticket_counter:
            self.ticket_counter[service] = 0
            self.queues[service] = []
        self.ticket_counter[service] += 1
        ticket = f"{service[:1].upper()}-{self.ticket_counter[service]}"
        self.queues[service].append(ticket)

        # Persist ticket
        data = load_data()
        if service not in data["tickets"]:
            data["tickets"][service] = []
        data["tickets"][service].append({"id": ticket, "status": "waiting"})
        save_data(data)

        return ticket

    def next_ticket(self, service: str) -> Optional[str]:
        if service not in self.queues or not self.queues[service]:
            return None
        return self.queues[service].pop(0)

    def queue_length(self, service: str) -> int:
        return len(self.queues.get(service, []))

    def set_assigned(self, service: str, desk: str, ticket: Optional[str]):
        self.assigned[(service, desk)] = ticket

    def get_assigned(self, service: str, desk: str) -> Optional[str]:
        return self.assigned.get((service, desk))

    def set_serving(self, service: str, desk: str, ticket: Optional[str]):
        self.serving[(service, desk)] = ticket

    def get_serving(self, service: str, desk: str) -> Optional[str]:
        return self.serving.get((service, desk))

queue_system = QueueSystem()


# --- Connection management ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                if connection in self.active_connections:
                    self.active_connections.remove(connection)


manager = ConnectionManager()


# --- HTML Pages ---
@app.get("/kiosk")
async def kiosk():
    return HTMLResponse(load_html("kiosk.html"))

@app.get("/clerk")
async def clerk_select():
    return HTMLResponse(load_html("clerk_select.html"))

# @app.get("/clerk/{service}/{desk}")
# async def clerk_page(service: str, desk: str):
#     data = load_data()
#     if service not in data["desks"]:
#         data["desks"][service] = []

#     # Prevent duplicate desk registration
#     if desk in data["desks"][service]:
#         return HTMLResponse(
#             """
#             <script>
#               alert("Desk {desk} for {service} is already registered. Please choose another desk number.");
#               window.location.href = "/clerk";
#             </script>
#             """.format(desk=desk, service=service),
#             status_code=200
#         )

#     # Register desk
#     data["desks"][service].append(desk)
#     save_data(data)

#     queue_system.set_assigned(service, desk, None)
#     queue_system.set_serving(service, desk, None)

#     return HTMLResponse(load_html("clerk_page.html"))


@app.get("/display")
async def display():
    return HTMLResponse(load_html("display.html"))


# --- Routes ---
@app.get("/ticket")
async def get_ticket(service: str = Query(...)):
    ticket = queue_system.generate_ticket(service)
    timestamp = time.strftime("%H:%M")
    payload = json.dumps({
        "type": "issued",
        "timestamp": timestamp,
        "service": service,
        "ticket": ticket,
        "queue_length": queue_system.queue_length(service),
    })
    await manager.broadcast(payload)
    return {"ticket": ticket}


@app.get("/next")
async def next_ticket(service: str = Query(...), desk: str = Query(...)):
    for (svc, _desk), assigned_ticket in queue_system.assigned.items():
        if svc == service and assigned_ticket is not None:
            return {"status": "busy", "message": f"Ticket {assigned_ticket} already assigned for {service}."}

    ticket = queue_system.next_ticket(service)
    timestamp = time.strftime("%H:%M")
    if not ticket:
        payload = json.dumps({
            "type": "empty",
            "timestamp": timestamp,
            "service": service,
            "message": f"No tickets waiting for {service}."
        })
        await manager.broadcast(payload)
        return {"status": "empty"}

    queue_system.set_assigned(service, desk, ticket)

    # Update JSON status
    data = load_data()
    for t in data["tickets"].get(service, []):
        if t["id"] == ticket:
            t["status"] = "assigned"
    save_data(data)

    payload = json.dumps({
        "type": "called",
        "timestamp": timestamp,
        "service": service,
        "ticket": ticket,
        "desk": desk,
    })
    await manager.broadcast(payload)
    return {"status": "ok", "ticket": ticket, "desk": desk}

@app.get("/clerk/{service}/{desk_id}")
async def clerk_select(service: str, desk_id: str, status: str = None):
    """
    Example: /clerk/deposit/4?status=selected
    """
    data = load_data()

    # Verify service exists
    if service not in data["desks"]:
        return JSONResponse({"error": f"Service '{service}' not found"}, status_code=404)

    # Find desk entry
    desk_entry = None
    for desk in data["desks"][service]:
        if desk["id"] == desk_id:
            desk_entry = desk
            break

    if not desk_entry:
        return JSONResponse({"error": f"Desk {desk_id} not found in service {service}"}, status_code=404)

    # Handle query parameter
    if status == "selected":
        if desk_entry["status"] == "empty":
            desk_entry["status"] = "occupied"
            save_data(data)
            # Redirect to explicit occupied route
            return RedirectResponse(url=f"/clerk/{service}/{desk_id}/occupied")
        else:
            # Already occupied → redirect to /clerk
            return RedirectResponse(url="/clerk")

    # Default response if no status param
    return JSONResponse({"service": service, "desk": desk_entry})

@app.get("/clerk/{service}/{desk_id}/occupied")
async def clerk_page(service: str, desk_id: str):
    """
    Clerk working page after desk is marked occupied.
    """
    # Serve the clerk_page.html file
    return HTMLResponse(load_html("clerk_page.html"))

@app.get("/confirm")
async def confirm_ticket(service: str = Query(...), desk: str = Query(...), ticket: str = Query(...)):
    assigned = queue_system.get_assigned(service, desk)
    timestamp = time.strftime("%H:%M")
    if assigned != ticket:
        return {"status": "error", "message": "Ticket mismatch or no ticket assigned."}

    queue_system.set_serving(service, desk, ticket)
    queue_system.set_assigned(service, desk, None)

    # Update JSON status
    data = load_data()
    for t in data["tickets"].get(service, []):
        if t["id"] == ticket:
            t["status"] = "serving"
    save_data(data)

    payload = json.dumps({
        "type": "serving",
        "timestamp": timestamp,
        "service": service,
        "ticket": ticket,
        "desk": desk,
    })
    await manager.broadcast(payload)
    return {"status": "ok"}


@app.get("/done")
async def done_ticket(service: str = Query(...), desk: str = Query(...), ticket: str = Query(...)):
    current_serving = queue_system.get_serving(service, desk)
    timestamp = time.strftime("%H:%M")
    if current_serving != ticket:
        return {"status": "error", "message": "No matching serving ticket for this desk."}

    queue_system.set_serving(service, desk, None)

    # Update JSON status
    data = load_data()
    for t in data["tickets"].get(service, []):
        if t["id"] == ticket:
            t["status"] = "done"
    save_data(data)

    payload = json.dumps({
        "type": "done",
        "timestamp": timestamp,
        "service": service,
        "ticket": ticket,
        "desk": desk,
    })
    await manager.broadcast(payload)
    return {"status": "ok"}

@app.get("/state")
async def get_state():
    data = load_data()
    return data


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: int):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "__ping__":
                continue
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


# --- Dev entrypoint ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
