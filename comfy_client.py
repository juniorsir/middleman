import requests, uuid, time, json, os, random, logging, base64, sys, sqlite3
import websocket
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# --- HARDCODED TO LOCALHOST ---
# This forces the script to ignore the .env file and talk directly to ComfyUI locally.
APP_URL = os.environ.get("COMFYUI_URL", "https://l0bxgzkcelmzk3-8888.proxy.runpod.net").rstrip('/')

NODE_TAGS_LYRICS = "94"  
NODE_SAMPLER = "3"       
NODE_LATENT = "98"       
NODE_UNET = "104"        
NODE_AUDIO_OUT = "107"   

def check_comfyui_health():
    try:
        res = requests.get(f"{APP_URL}/system_stats", timeout=5)
        return res.status_code == 200
    except requests.exceptions.RequestException:
        return False

def log_terminal_progress(song_num, total_songs, progress, message, is_done=False):
    bar_length = 25
    filled = int(bar_length * progress // 100)
    bar = '█' * filled + '-' * (bar_length - filled)
    sys.stdout.write(f"\r🎵 [Song {song_num}/{total_songs}] [{bar}] {progress}% | {message}\033[K")
    sys.stdout.flush()
    if is_done:
        sys.stdout.write("\n") 

def submit_workflow(workflow, client_id):
    url = f"{APP_URL}/prompt"
    payload = {"prompt": workflow, "client_id": client_id}
    res = requests.post(url, json=payload, timeout=20)
    res.raise_for_status()
    return res.json().get("prompt_id")

def get_audio_file(filename, subfolder="", folder_type="output"):
    url = f"{APP_URL}/view"
    params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    res = requests.get(url, params=params, timeout=30)
    res.raise_for_status()
    return res.content

def generate_music_stream(params, num_songs=1):
    client_id = str(uuid.uuid4())
    
    ws = websocket.WebSocket()
    ws_url = APP_URL.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?clientId={client_id}"
    
    try:
        # Since we are on localhost, there is no Cloudflare! Connection is instant.
        logger.info(f"Connecting directly to ComfyUI at {ws_url}...")
        ws.connect(ws_url)
        logger.info("✅ WebSocket connected successfully!")
    except Exception as e:
        logger.error(f"WebSocket connect failed: {e}")
        yield json.dumps({"status": "error", "message": f"WebSocket connect failed: {e}"}) + "\n"
        return

    try:
        with open("music_workflow.json", 'r') as f:
            base_workflow = json.load(f)
    except Exception as e:
        logger.error(f"Workflow setup error: {e}")
        yield json.dumps({"status": "error", "message": f"Workflow setup error: {e}"}) + "\n"
        return

    prompt_ids = []
    seed_map = {} 
    base_seed = params["seed"]
    
    logger.info(f"🚀 Queuing {num_songs} songs in ComfyUI...")
    
    for i in range(num_songs):
        current_seed = base_seed + i 
        workflow = json.loads(json.dumps(base_workflow)) 
        
        if NODE_TAGS_LYRICS in workflow:
            workflow[NODE_TAGS_LYRICS]["inputs"].update({
                "tags": params["tags"], "lyrics": params["lyrics"], 
                "duration": params["duration"], "seed": current_seed,
                "bpm": params["bpm"], "timesignature": params["timesignature"],
                "language": params["language"], "cfg_scale": params["cfg_scale"]
            })
        if NODE_SAMPLER in workflow: workflow[NODE_SAMPLER]["inputs"]["seed"] = current_seed
        if NODE_LATENT in workflow: workflow[NODE_LATENT]["inputs"]["seconds"] = params["duration"]
        if NODE_UNET in workflow: workflow[NODE_UNET]["inputs"]["unet_name"] = params["unet_name"]

        try:
            pid = submit_workflow(workflow, client_id)
            prompt_ids.append(pid)
            seed_map[pid] = current_seed
        except Exception as e:
            logger.error(f"\nFailed to submit song {i+1}: {e}")
            yield json.dumps({"status": "error", "message": f"Failed to submit song {i+1}: {e}"}) + "\n"
            return

    yield json.dumps({"status": "queued", "message": f"Successfully queued {num_songs} variations.", "total_songs": num_songs}) + "\n"

    completed_songs = 0
    
    try:
        while completed_songs < num_songs:
            out = ws.recv()
            if isinstance(out, str):
                message = json.loads(out)
                msg_type = message.get('type')
                data = message.get('data', {})
                
                current_pid = data.get('prompt_id')
                if current_pid not in prompt_ids:
                    continue 
                    
                active_prompt_id = current_pid
                current_song_num = prompt_ids.index(active_prompt_id) + 1

                if msg_type == 'executing':
                    node = data.get('node')
                    if node == NODE_TAGS_LYRICS:
                        log_terminal_progress(current_song_num, num_songs, 5, "Encoding Lyrics & Tags...")
                        yield json.dumps({"status": "generating", "song": current_song_num, "progress": 5, "message": f"Encoding..."}) + "\n"
                    elif node == NODE_SAMPLER:
                        log_terminal_progress(current_song_num, num_songs, 10, "Synthesizing Audio...")
                        yield json.dumps({"status": "generating", "song": current_song_num, "progress": 10, "message": f"Synthesizing..."}) + "\n"
                        
                elif msg_type == 'progress':
                    val = data.get('value', 0)
                    mx = data.get('max', 1)
                    pct = 10 + int((val / mx) * 80)
                    
                    log_terminal_progress(current_song_num, num_songs, pct, f"Step {val}/{mx}")
                    yield json.dumps({"status": "generating", "song": current_song_num, "progress": pct, "message": f"Step {val}/{mx}..."}) + "\n"
                    
                elif msg_type == 'executed' and data.get('node') == NODE_AUDIO_OUT:
                    audio_info = data['output']['audio'][0]
                    comfy_filename = audio_info['filename']
                    comfy_subfolder = audio_info.get('subfolder', '')
                    
                    song_uuid = str(uuid.uuid4())
                    
                    try:
                        conn = sqlite3.connect("music_database.db")
                        c = conn.cursor()
                        c.execute('''INSERT INTO songs (id, prompt, lyrics, seed, model, comfy_filename, comfy_subfolder) 
                                     VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                                  (song_uuid, params["tags"], params["lyrics"], seed_map[active_prompt_id], params.get("unet_name", "unknown"), comfy_filename, comfy_subfolder))
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        logger.error(f"Database error: {e}")
                    
                    song_url = f"/songs/{song_uuid}/stream"
                    log_terminal_progress(current_song_num, num_songs, 100, f"Saved in ComfyUI! (Seed: {seed_map[active_prompt_id]})", is_done=True)
                    logger.info(f"🔗 Song {current_song_num} Ready! API Stream URL: {song_url}")
                    
                    yield json.dumps({
                        "status": "song_complete", 
                        "song": current_song_num,
                        "progress": 100, 
                        "message": f"Song {current_song_num} Ready!",
                        "seed": seed_map[active_prompt_id],
                        "song_id": song_uuid,
                        "audio_url": song_url
                    }) + "\n"
                    
                    completed_songs += 1
                    
                elif msg_type == 'execution_error':
                    sys.stdout.write(f"\n❌ [Song {current_song_num}/{num_songs}] FAILED in ComfyUI.\n")
                    yield json.dumps({"status": "error", "song": current_song_num, "message": "Internal ComfyUI error."}) + "\n"
                    completed_songs += 1 
                    
        logger.info(f"🎉 All {num_songs} songs successfully generated and sent to frontend!")
        yield json.dumps({"status": "all_complete", "message": "All variations generated!"}) + "\n"

    except Exception as e:
        logger.error(f"\nConnection lost: {e}")
        yield json.dumps({"status": "error", "message": f"Connection lost: {e}"}) + "\n"
    finally:
        ws.close()

def simulate_music_stream(params, num_songs=1):
    logger.info(f"🧪 SIMULATION MODE: Faking {num_songs} songs...")
    
    if not os.path.exists("test.mp3"):
        yield json.dumps({"status": "error", "message": "test.mp3 not found in folder!"}) + "\n"
        return

    with open("test.mp3", "rb") as f:
        audio_bytes = f.read()
        b64_audio = base64.b64encode(audio_bytes).decode('utf-8')

    yield json.dumps({"status": "queued", "message": f"Successfully queued {num_songs} variations.", "total_songs": num_songs}) + "\n"

    for current_song_num in range(1, num_songs + 1):
        current_seed = params["seed"] + current_song_num
        
        log_terminal_progress(current_song_num, num_songs, 5, "Encoding Lyrics & Tags...")
        yield json.dumps({"status": "generating", "song": current_song_num, "progress": 5, "message": "Encoding..."}) + "\n"
        time.sleep(1) 
        
        log_terminal_progress(current_song_num, num_songs, 10, "Synthesizing Audio...")
        yield json.dumps({"status": "generating", "song": current_song_num, "progress": 10, "message": "Synthesizing..."}) + "\n"
        time.sleep(1)

        for pct in range(20, 100, 15):
            log_terminal_progress(current_song_num, num_songs, pct, f"Step {pct}/100")
            yield json.dumps({"status": "generating", "song": current_song_num, "progress": pct, "message": f"Step {pct}/100..."}) + "\n"
            time.sleep(0.8) 

        song_uuid = str(uuid.uuid4())
        
        try:
            conn = sqlite3.connect("music_database.db")
            c = conn.cursor()
            c.execute('''INSERT INTO songs (id, prompt, lyrics, seed, model, comfy_filename, comfy_subfolder) 
                         VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                      (song_uuid, params["tags"], params["lyrics"], current_seed, params.get("unet_name", "unknown"), "test.mp3", ""))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Database error: {e}")

        song_url = f"/songs/{song_uuid}/stream"
        
        log_terminal_progress(current_song_num, num_songs, 100, f"Saved MP3! (Seed: {current_seed})", is_done=True)
        logger.info(f"🔗 Song {current_song_num} Ready! API Stream URL: {song_url}")
        
        yield json.dumps({
            "status": "song_complete", 
            "song": current_song_num,
            "progress": 100, 
            "message": f"Song {current_song_num} Ready!",
            "seed": current_seed,
            "song_id": song_uuid,
            "audio_url": song_url,
            "audio_base64": b64_audio
        }) + "\n"
        
        time.sleep(1) 

    logger.info(f"🎉 SIMULATION COMPLETE: All {num_songs} songs sent to frontend!")
    yield json.dumps({"status": "all_complete", "message": "All variations generated!"}) + "\n"
