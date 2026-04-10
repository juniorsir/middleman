from fastapi import FastAPI, Form, HTTPException, Depends, Header, Path, Request, Response, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from comfy_client import generate_music_stream, check_comfyui_health, upload_file_to_comfy
import json, os, random, sqlite3, requests, logging, uuid
from dotenv import load_dotenv

load_dotenv()
APP_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip('/')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def init_db():
    conn = sqlite3.connect("music_database.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS songs (
                    id TEXT PRIMARY KEY,
                    prompt TEXT,
                    lyrics TEXT,
                    seed INTEGER,
                    model TEXT,
                    comfy_filename TEXT,
                    comfy_subfolder TEXT,
                    plays INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    conn.commit()
    conn.close()

init_db()
app = FastAPI(title="AceStep Music Bridge API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
    expose_headers=["*"],
)

API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "super-secret-music-key-123")

def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid API Key")
    return x_api_key

@app.get("/health")
async def health_check():
    comfy_online = check_comfyui_health()
    return JSONResponse(content={
        "status": "healthy" if comfy_online else "degraded",
        "api_security": "enabled",
        "comfyui_reachable": comfy_online
    }, status_code=200 if comfy_online else 503)

def load_models():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(current_dir, "models.json")
    try:
        with open(json_path, "r") as f:
            data = json.load(f)
            return data.get("models", [])
    except FileNotFoundError:
        logger.error(f"Could not find models.json at {json_path}")
        return []
    except json.JSONDecodeError:
        logger.error(f"models.json is badly formatted. check for missing comas!")
        return []

@app.get("/models")
async def list_models(api_key: str = Depends(verify_api_key)):
    models = load_models()
    if not models:
        raise HTTPException(status_code=500, detail="No models configured.")
    return {"models": models}

@app.get("/songs/trending")
def get_trending_songs(limit: int = 10):
    conn = sqlite3.connect("music_database.db")
    c = conn.cursor()
    c.execute("SELECT id, prompt, seed, plays, created_at FROM songs ORDER BY plays DESC LIMIT ?", (limit,))
    
    songs = []
    for row in c.fetchall():
        songs.append({
            "id": row[0], "prompt": row[1], "seed": row[2], 
            "url": f"/songs/{row[0]}/stream",
            "plays": row[3], "created_at": row[4]
        })
    conn.close()
    return {"trending": songs}


@app.get("/songs/{song_id}/stream")
def stream_song_from_comfy(song_id: str = Path(...)):
    conn = sqlite3.connect("music_database.db")
    c = conn.cursor()
    c.execute("UPDATE songs SET plays = plays + 1 WHERE id = ?", (song_id,))
    conn.commit()
    c.execute("SELECT comfy_filename, comfy_subfolder FROM songs WHERE id = ?", (song_id,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(status_code=404, detail="Song not found")
        
    comfy_filename, comfy_subfolder = row[0], row[1]
    
    if comfy_filename == "test.mp3":
        def iter_local_file():
            with open("test.mp3", "rb") as f:
                while chunk := f.read(8192):
                    yield chunk
        return StreamingResponse(iter_local_file(), media_type="audio/mpeg")
        
    comfy_view_url = f"{APP_URL}/view?filename={comfy_filename}&subfolder={comfy_subfolder}&type=output"
    
    def iterfile():
        # --- FIXED: Use a Session with browser-like headers to avoid 502 Bad Gateway ---
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Referer": f"{APP_URL}/",
            "Accept": "*/*",
            "Connection": "keep-alive"
        }
        
        # We need the clearance cookie again to bypass Cloudflare for the download
        session.get(APP_URL, headers=headers, timeout=10)
        
        with session.get(comfy_view_url, stream=True, headers=headers) as r:
            if r.status_code != 200:
                logger.error(f"Failed to download audio from ComfyUI. Status: {r.status_code}")
                return
            for chunk in r.iter_content(chunk_size=8192):
                yield chunk

    return StreamingResponse(iterfile(), media_type="audio/mpeg")

@app.post("/generate-music")
async def api_generate_music(
    prompt: str = Form(..., description="Tags: e.g., 'Rock: A powerful track...'"),
    model_id: str = Form(..., description="The ID from /models, e.g., 'acestep-turbo'"),
    lyrics: str = Form("", description="Lyrics for the song"),
    duration: int = Form(120, description="Duration of song in seconds"),
    bpm: int = Form(120, description="Beats per minute"),
    timesignature: str = Form("4", description="Time signature, e.g., '4' for 4/4"),
    language: str = Form("en", description="Language code, e.g., 'en'"),
    cfg_scale: float = Form(2.0, description="CFG Scale for prompt adherence"),
    song_type: str = Form(None, description="Set to 'instrumental' to remove vocals"),
    seed: int = Form(None, description="Leave blank for random"),
    num_songs: int = Form(6, description="How many similar variations to generate (Max 6)"),
    audio_ref: UploadFile = File(None, description="Optional audio file for reference"),
    api_key: str = Depends(verify_api_key)
):
    logger.info("============== NEW REQUEST RECEIVED ==============")
    logger.info(f"Model: {model_id} | Songs: {num_songs} | Instrumental: {song_type}")
    logger.info(f"Prompt: {prompt}")

    comfy_ref_filename = None
    if audio_ref and audio_ref.filename:
        try:
            logger.info(f"🎤 Received Audio Reference: {audio_ref.filename}")
            audio_bytes = await audio_ref.read()
            
            # Extract the original extension (.mp3, .wav, .flac) to allow all types
            ext = os.path.splitext(audio_ref.filename)[1].lower()
            if not ext: ext = ".mp3" # Fallback if no extension
            
            clean_filename = f"ref_{uuid.uuid4().hex[:8]}{ext}"
            comfy_ref_filename = upload_file_to_comfy(audio_bytes, clean_filename)
            logger.info(f"✅ Uploaded reference audio to ComfyUI: {comfy_ref_filename}")
        except Exception as e:
            logger.error(f"Failed to upload reference audio: {e}")
            raise HTTPException(status_code=500, detail="Failed to process reference audio.")
    else:
        logger.info("📄 No Audio Reference provided. Generating Text-to-Audio.")
            
    available_models = load_models()
    selected_model = next((m for m in available_models if m["id"] == model_id), None)

    if not selected_model:
        raise HTTPException(status_code=400, detail=f"Model '{model_id}' is invalid. Please check /models endpoint.")

    unet_filename = selected_model.get("unet_name")
    is_instrumental = song_type and song_type.strip().lower() == "instrumental"

    params = {
        "tags": f"{prompt}, instrumental, no vocals" if is_instrumental else prompt,
        "lyrics": "" if is_instrumental else lyrics,
        "duration": duration,
        "bpm": bpm,
        "timesignature": timesignature,
        "language": language,
        "cfg_scale": cfg_scale,
        "seed": seed or random.randint(1, 999999999999999),
        "unet_name": unet_filename,
        "reference_audio": comfy_ref_filename
    }

    try:
        # Add anti-buffering headers so Cloudflare/Nginx sends data instantly!
        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no" 
        }

        return StreamingResponse(
            generate_music_stream(params, num_songs=num_songs), 
            media_type="application/x-ndjson",
            headers=headers
        )
        
    except Exception as e:
        logger.critical(f"Unhandled error initiating stream: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error starting the generator")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
