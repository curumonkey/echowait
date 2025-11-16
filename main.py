import time
import json, uuid
from typing import Dict, List, Tuple, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from contextlib import asynccontextmanager

app = FastAPI()

DATA_FILE = "queue_data.json"

import json

DATA_FILE = "queue_data.json"

def init_data_file():
    """
    Initialize the JSON file if missing or empty.
    Normalize ticket statuses: if 'assigned', change to 'waiting'.
    """
    default_structure = {
        "desks": {},
        "tickets": {},
        "sessions": {}
    }

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(default_structure, f, indent=2)
        print("[init_data_file] Created new data file with default structure.")
        return default_structure

    if "desks" not in data or not isinstance(data["desks"], dict):
        data["desks"] = {}
    if "tickets" not in data or not isinstance(data["tickets"], dict):
        data["tickets"] = {}
    if "sessions" not in data or not isinstance(data["sessions"], dict):
        data["sessions"] = {}

    normalized_count = 0
    for service, tickets in data["tickets"].items():
        if not isinstance(tickets, list):
            continue
        for ticket in tickets:
            if ticket.get("status") == "assigned":
                ticket["status"] = "waiting"
                normalized_count += 1
                print(f"[init_data_file] Normalized ticket {ticket['id']} ({service}) → waiting")

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"[init_data_file] Completed normalization. {normalized_count} ticket(s) updated.")
    return data


def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"desks": {}, "tickets": {}, "sessions": {}}

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
        # In‑memory queues per service
        self.queues: Dict[str, List[str]] = {}
        # Ticket counters per service
        self.ticket_counter: Dict[str, int] = {}
        # Assigned tickets per (service, desk)
        self.assigned: Dict[Tuple[str, str], Optional[str]] = {}
        # Serving tickets per (service, desk)
        self.serving: Dict[Tuple[str, str], Optional[str]] = {}

        # Restore state from JSON when server starts
        self.restore_state()

    def restore_state(self):
        """Rehydrate queues, assigned, and serving from JSON persistence."""
        data = load_data()

        # Restore queues from waiting tickets
        for service, tickets in data.get("tickets", {}).items():
            self.queues[service] = [t["id"] for t in tickets if t["status"] == "waiting"]
            # Restore ticket counter to max id number
            if tickets:
                max_num = max(int(t["id"].split("-")[1]) for t in tickets)
                self.ticket_counter[service] = max_num

        # Restore desk assignments
        for service, desks in data.get("desks", {}).items():
            for desk in desks:
                current_ticket = desk.get("current_ticket")
                if current_ticket:
                    self.assigned[(service, desk["id"])] = current_ticket
                    # Check ticket status to decide if serving
                    ticket_info = next(
                        (t for t in data["tickets"].get(service, []) if t["id"] == current_ticket),
                        None
                    )
                    if ticket_info and ticket_info["status"] == "serving":
                        self.serving[(service, desk["id"])] = current_ticket

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

    def add_ticket(self, service: str, ticket_id: str):
        """Add an existing ticket back into the waiting queue (e.g. from JSON)."""
        if service not in self.queues:
            self.queues[service] = []
        if ticket_id not in self.queues[service]:
            self.queues[service].append(ticket_id)

    def next_ticket(self, service: str) -> Optional[str]:
        if service not in self.queues or not self.queues[service]:
            return None
        return self.queues[service].pop(0)

    def queue_length(self, service: str) -> int:
        return len(self.queues.get(service, []))

    def set_assigned(self, service: str, desk: str, ticket: Optional[str]):
        self.assigned[(service, desk)] = ticket
        # Persist desk assignment
        data = load_data()
        for d in data["desks"].get(service, []):
            if d["id"] == desk:
                d["current_ticket"] = ticket
        save_data(data)

    def get_assigned(self, service: str, desk: str) -> Optional[str]:
        return self.assigned.get((service, desk))

    def set_serving(self, service: str, desk: str, ticket: Optional[str]):
        self.serving[(service, desk)] = ticket

        # Persist serving state
        data = load_data()

        # Update ticket status
        for t in data["tickets"].get(service, []):
            if t["id"] == ticket:
                t["status"] = "serving"

        # Update desk state
        for d in data["desks"].get(service, []):
            if d["id"] == desk:
                d["current_ticket"] = ticket   # <-- persist ticket ID here
                d["status"] = "occupied"

        save_data(data)


    def get_serving(self, service: str, desk: str) -> Optional[str]:
        return self.serving.get((service, desk))


queue_system = QueueSystem()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    data = init_data_file()  # normalize JSON and return it

    # Rebuild queue_system from tickets in JSON
    for service, tickets in data.get("tickets", {}).items():
        for ticket in tickets:
            if ticket.get("status") == "waiting":
                queue_system.add_ticket(service, ticket["id"])
                print(f"[lifespan] Restored ticket {ticket['id']} into {service} queue")

    yield

    # Shutdown logic (optional)
    print("[lifespan] Server shutting down.")

app = FastAPI(lifespan=lifespan)

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
    data = load_data()

    # Collect waiting tickets for this service
    waiting_tickets = [t for t in data["tickets"].get(service, []) if t["status"] == "waiting"]
    waiting_count = len(waiting_tickets)

    # Case A: Only 1 ticket waiting
    if waiting_count == 1:
        single_ticket = waiting_tickets[0]["id"]
        # Check if that single ticket is already assigned
        for (svc, _desk), assigned_ticket in queue_system.assigned.items():
            if svc == service and assigned_ticket == single_ticket:
                return {
                    "status": "busy",
                    "ticket": assigned_ticket,
                    "message": f"Ticket {assigned_ticket} already assigned for {service}."
                }

    # Case B: More than 1 tickets waiting
    # → allow each desk to get the next unassigned ticket
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

    # Assign ticket to desk
    queue_system.set_assigned(service, desk, ticket)

    # Update JSON status
    for t in data["tickets"].get(service, []):
        if t["id"] == ticket:
            t["status"] = "assigned"
    save_data(data)

    # Broadcast event
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
async def clerk_select(service: str, desk_id: str, request: Request, status: str = Query(None)):
    data = load_data()
    session_id = request.cookies.get("session_id")

    # Find desk
    desks = data["desks"].get(service, [])
    desk = next((d for d in desks if d["id"] == desk_id), None)
    if not desk:
        return JSONResponse({"error": "Desk not found"}, status_code=404)

    # Case 1: Desk occupied
    if desk["status"] == "occupied":
        # Find which session owns this desk
        owner_session = None
        for sid, info in data["sessions"].items():
            if info["service"] == service and info["desk_id"] == desk_id:
                owner_session = sid
                break

        if owner_session and owner_session == session_id:
            # Current browser owns this desk → Back to desk
            return RedirectResponse(url=f"/clerk/{service}/{desk_id}/occupied")
        else:
            # Occupied by someone else → send back to setup page
            return RedirectResponse(url="/clerk")

    # Case 2: Desk empty → assign to this browser
    new_session = str(uuid.uuid4())
    desk["status"] = "occupied"
    data["sessions"][new_session] = {"service": service, "desk_id": desk_id}
    save_data(data)

    response = RedirectResponse(url=f"/clerk/{service}/{desk_id}/occupied")
    response.set_cookie("session_id", new_session)
    return response

@app.get("/clerk/{service}/{desk_id}/occupied")
async def clerk_page(service: str, desk_id: str, request: Request):
    """
    Clerk working page after desk is marked occupied.
    Only accessible if session cookie matches.
    """
    data = load_data()
    session_id = request.cookies.get("session_id")

    if not session_id or session_id not in data.get("sessions", {}):
        return RedirectResponse(url="/clerk")

    session_info = data["sessions"][session_id]
    if session_info["service"] != service or session_info["desk_id"] != desk_id:
        return RedirectResponse(url="/clerk")

    # Serve clerk page
    return HTMLResponse(load_html("clerk_page.html"))

@app.get("/confirm")
async def confirm_ticket(
    service: str = Query(...),
    desk: str = Query(...),
    ticket: str = Query(...)
):
    assigned = queue_system.get_assigned(service, desk)
    timestamp = time.strftime("%H:%M")

    if assigned != ticket:
        return {"status": "error", "message": "Ticket mismatch or no ticket assigned."}

    # Mark as serving in memory
    queue_system.set_serving(service, desk, ticket)
    queue_system.set_assigned(service, desk, None)

    # Update JSON status
    data = load_data()

    # Update ticket status and persist desk info
    for t in data["tickets"].get(service, []):
        if t["id"] == ticket:
            t["status"] = "serving"
            t["desk"] = desk   # <-- persist desk here

    # Update desk state with current_ticket
    for d in data["desks"].get(service, []):
        if d["id"] == desk:
            d["status"] = "occupied"
            d["current_ticket"] = ticket

    save_data(data)

    # Broadcast event
    payload = json.dumps({
        "type": "serving",
        "timestamp": timestamp,
        "service": service,
        "ticket": ticket,
        "desk": desk,
    })
    await manager.broadcast(payload)

    return {"status": "ok", "ticket": ticket, "desk": desk}


@app.get("/done")
async def done_ticket(
    service: str = Query(...),
    desk: str = Query(...),
    ticket: str = Query(...)
):
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
            t["desk"] = None   # <-- clear desk info
    for d in data["desks"].get(service, []):
        if d["id"] == desk:
            d["current_ticket"] = None
            d["status"] = "occupied"  # or "empty" if you want to free the desk
    save_data(data)

    payload = json.dumps({
        "type": "done",
        "timestamp": timestamp,
        "service": service,
        "ticket": ticket,
        "desk": desk,
    })
    await manager.broadcast(payload)

    return {"status": "ok", "ticket": ticket, "desk": desk}


@app.get("/state")
async def get_state(request: Request):
    data = load_data()
    session_id = request.cookies.get("session_id")
    return JSONResponse({
        "desks": data.get("desks", {}),
        "tickets": data.get("tickets", {}),
        "sessions": data.get("sessions", {}),
        "currentSession": session_id
    })

@app.get("/clerk_state")
async def clerk_state(service: str = Query(...), desk: str = Query(...)):
    data = load_data()
    desk_info = next((d for d in data["desks"].get(service, []) if d["id"] == desk), None)
    if not desk_info:
        return {"service": service, "desk": desk, "assigned": None, "serving": None}

    current_ticket = desk_info.get("current_ticket")
    if current_ticket:
        # Find ticket status
        ticket_info = next((t for t in data["tickets"].get(service, []) if t["id"] == current_ticket), None)
        if ticket_info and ticket_info["status"] == "serving":
            return {"service": service, "desk": desk, "serving": current_ticket}
        elif ticket_info and ticket_info["status"] == "assigned":
            return {"service": service, "desk": desk, "assigned": current_ticket}

    return {"service": service, "desk": desk, "assigned": None, "serving": None}


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
    data = init_data_file()
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

