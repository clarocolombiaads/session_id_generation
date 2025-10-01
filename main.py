from fastapi import FastAPI, Query, Request, HTTPException
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone
from fastapi.middleware.cors import CORSMiddleware
from zoneinfo import ZoneInfo
import os, time, httpx
from fastapi import Body

app = FastAPI()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

LOCAL_TZ = ZoneInfo("America/Bogota")
SESSION_DURATION_MS = 28500000
SESSION_DURATION_HOURS = SESSION_DURATION_MS / (1000 * 60 * 60)    # expira en 1 hora


# Middleware CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://tienda.claro.com.co", "https://tienda.claro.com.co/"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def safe_supabase_call(func, retries=3, delay=2):
    for i in range(retries):
        try:
            return func()
        except (httpx.ReadError, httpx.TimeoutException) as e:
            print(f"⚠️ Error en conexión Supabase: {e} (intento {i+1}/{retries})")
            if i < retries - 1:
                time.sleep(delay)
                continue
            raise e



@app.post("/session/create")
def create_session(request: Request, payload: dict = Body(...)):
    session_id = payload.get("session_id")
    now_local = datetime.now(LOCAL_TZ)

    if session_id:
        # 🔎 Buscar por session_id existente
        existing = safe_supabase_call(lambda: supabase.table("session_ids")
            .select("*")
            .eq("session_id", session_id)
            .execute()
        )

        if existing.data:
            session = existing.data[0]

            created_at = datetime.fromisoformat(session["created_at"]).astimezone(LOCAL_TZ)
            last_activity = datetime.now(LOCAL_TZ)

            duration_ms = int((last_activity - created_at).total_seconds() * 1000)
            session_hours = duration_ms / (1000 * 60 * 60)

            if session.get("ended_by"):
                return {
                    "session_id": session["session_id"],
                    "created_at": session["created_at"],
                    "expires_at": None,
                    "reused": False,
                    "ended": True,
                    "reason": session["ended_by"]
                }

            if session_hours >= SESSION_DURATION_HOURS:
                safe_supabase_call(lambda: supabase.table("session_ids")
                    .update({"ended_by": "expired_time"})
                    .eq("session_id", session_id)
                    .execute()
                )
                return {
                    "session_id": session["session_id"],
                    "created_at": session["created_at"],
                    "expires_at": None,
                    "reused": False,
                    "ended": True,
                    "reason": "expired_time"
                }

            # ✅ Aún activa → sesión reutilizada
            return {
                "session_id": session["session_id"],
                "created_at": session["created_at"],
                "expires_at": int((created_at + timedelta(hours=SESSION_DURATION_HOURS)).timestamp() * 1000),
                "reused": True,
                "ended": False,
                "reason": None
            }

    # 🚀 Si no hay session_id válido → crear nueva
    new_session = safe_supabase_call(lambda: supabase.table("session_ids").insert({
        "created_at": now_local.isoformat(),
        "last_activity": now_local.isoformat(),
        "ip_address": request.client.host,
        "duration_ms": 0,
        "ended_by": None
    }).execute()).data[0]

    expires_at = int((now_local + timedelta(hours=SESSION_DURATION_HOURS)).timestamp() * 1000)

    return {
        "session_id": new_session["session_id"],
        "created_at": new_session["created_at"],
        "expires_at": expires_at,
        "reused": False,
        "ended": False,
        "reason": None
    }
# --- Endpoint expirar sesión ---

@app.post("/session/expire")
def expire_session(data: dict):

    session_id = data.get("session_id")
    reason = data.get("reason")

    if not session_id or not reason:
        raise HTTPException(status_code=400, detail="Faltan parámetros: session_id o reason")

    # Buscar sesión en Supabase
    response = supabase.table("session_ids").select("*").eq("session_id", session_id).execute()
    if not response.data:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    session = response.data[0]
    now_local = datetime.now(LOCAL_TZ)
    # Parsear created_at (naive → UTC-5)
    created_at = datetime.fromisoformat(session["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone(timedelta(hours=-5)))  # Bogotá

    # Parsear last_activity (ya aware con +00)
    # 2. Hora actual en Bogotá
    now_local = datetime.now(LOCAL_TZ)

    created_at = datetime.fromisoformat(session["created_at"])
    if created_at.tzinfo is None:  # si viene naive, ajustamos a Bogotá
        created_at = created_at.replace(tzinfo=LOCAL_TZ)

    # last_activity es AHORA (no lo que hay en la tabla)
    last_activity = now_local

    # 3. Calcular duración en ms
    duration_ms = int((last_activity - created_at).total_seconds() * 1000)

    # Marcar sesión como expirada
    safe_supabase_call(lambda: supabase.table("session_ids")
        .update({
            "ended_by": reason,
            "duration_ms": duration_ms
        })
        .eq("session_id", session_id)
        .execute()
    )

    return {
        "session_id": session_id,
        "ended_by": reason,
        "duration_ms": duration_ms
    }


@app.post("/session/update")
def update_session(request: Request, payload: dict = Body(...)):
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    # Buscar sesión
    existing = safe_supabase_call(lambda: supabase.table("session_ids")
        .select("*")
        .eq("session_id", session_id)
        .execute()
    )

    if not existing.data:
        raise HTTPException(status_code=404, detail="Session not found")

    session = existing.data[0]

    # Calcular duración
    created_at = datetime.fromisoformat(session["created_at"]).astimezone(LOCAL_TZ)
    now_local = datetime.now(LOCAL_TZ)
    duration_ms = int((now_local - created_at).total_seconds() * 1000)

    # Actualizar en DB
    safe_supabase_call(lambda: supabase.table("session_ids")
        .update({
            "last_activity": now_local.isoformat(),
            "duration_ms": duration_ms
        })
        .eq("session_id", session_id)
        .execute()
    )

    return {
        "session_id": session["session_id"],
        "last_activity": now_local.isoformat(),
        "duration_ms": duration_ms
    }
