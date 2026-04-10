import requests, uuid, time, json, os, random, logging, base64, sys, sqlite3
import websocket
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

APP_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip('/')

def check_comfyui_health():
    try:
        res = requests.get(f"{APP_URL}/system_stats", timeout=5)
        return res.status_code == 200
    except requests.exceptions.RequestException:
        return False

def upload_file_to_comfy(file_bytes, filename):
    url = f"{APP_URL}/upload/image" # ComfyUI uses the image endpoint for audio too
    files = {"image": (filename, file_bytes)}
    data = {"overwrite": "true"}
    res = requests.post(url, files=files, data=data)
    res.raise_for_status()
    return res.json()["name"]
    
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
    
    if res.status_code != 200:
        # This will print the EXACT reason ComfyUI rejected the prompt
        error_info = res.json()
        logger.error(f"--- COMFYUI VALIDATION ERROR ---")
        logger.error(json.dumps(error_info, indent=2))
        logger.error(f"---------------------------------")
        raise Exception(f"ComfyUI rejected the request: {error_info.get('message', 'Check logs')}")
        
    return res.json().get("prompt_id")

def generate_music_stream(params, num_songs=1):
    client_id = str(uuid.uuid4())
    
    ws = websocket.WebSocket()
    ws_url = APP_URL.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?clientId={client_id}"
    
    try:
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
            
            # --- SAFETY SHIELD: PREVENT KeyError: '3' Crahses ---
            if "nodes" in base_workflow:
                logger.error("🚨 CRITICAL ERROR: music_workflow.json is in UI FORMAT!")
                logger.error("You MUST use the API Format JSON. The script cannot continue.")
                yield json.dumps({"status": "error", "message": "Server configuration error: Workflow is in UI format. Please update music_workflow.json to API format."}) + "\n"
                return

    except Exception as e:
        logger.error(f"Workflow setup error: {e}")
        yield json.dumps({"status": "error", "message": f"Workflow setup error: {e}"}) + "\n"
        return

    # ==========================================
    # --- AUTO-DETECT NODE IDS DYNAMICALLY ---
    # ==========================================
    def get_node_id(class_type):
        for k, v in base_workflow.items():
            if isinstance(v, dict) and v.get("class_type") == class_type:
                return str(k)
        return None

    NODE_TAGS_LYRICS = get_node_id("TextEncodeAceStepAudio1.5")
    NODE_SAMPLER = get_node_id("KSampler")
    NODE_LATENT_EMPTY = get_node_id("EmptyAceStep1.5LatentAudio")
    NODE_UNET = get_node_id("UNETLoader")
    NODE_AUDIO_OUT = get_node_id("SaveAudioMP3") or get_node_id("SaveAudio") 
    NODE_VHS_AUDIO = get_node_id("VHS_LoadAudioUpload")
    NODE_VAE_ENCODE = get_node_id("VAEEncodeAudio")

    if not NODE_SAMPLER:
        logger.error("🚨 Could not find a KSampler node in the workflow JSON!")
        yield json.dumps({"status": "error", "message": "Invalid workflow JSON: Missing KSampler."}) + "\n"
        return

    prompt_ids = []
    seed_map = {} 
    base_seed = params["seed"]
    
    logger.info(f"🚀 Queuing {num_songs} songs in ComfyUI...")
    
    for i in range(num_songs):
        current_seed = base_seed + i 
        workflow = json.loads(json.dumps(base_workflow)) 
        
        # 1. Update text encoding parameters
        if NODE_TAGS_LYRICS and NODE_TAGS_LYRICS in workflow:
            workflow[NODE_TAGS_LYRICS]["inputs"].update({
                "tags": params["tags"], "lyrics": params["lyrics"], 
                "duration": params["duration"], "seed": current_seed,
                "bpm": params["bpm"], "timesignature": params["timesignature"],
                "language": params["language"], "cfg_scale": params["cfg_scale"]
            })
            
        # 2. Update model and latent duration
        if NODE_SAMPLER in workflow: workflow[NODE_SAMPLER]["inputs"]["seed"] = current_seed
        if NODE_LATENT_EMPTY and NODE_LATENT_EMPTY in workflow: 
            workflow[NODE_LATENT_EMPTY]["inputs"]["seconds"] = params["duration"]
        if NODE_UNET and NODE_UNET in workflow: 
            workflow[NODE_UNET]["inputs"]["unet_name"] = params["unet_name"]

        # 3. DYNAMIC AUDIO ROUTING
        reference_audio = params.get("reference_audio")
        
        if reference_audio and NODE_VHS_AUDIO in workflow:
            # Mode: Audio-to-Audio (Upload exists)
            logger.info(f"Mode: Audio-to-Audio (Ref: {reference_audio})")
            workflow[NODE_VHS_AUDIO]["inputs"]["audio"] = reference_audio
            if NODE_VAE_ENCODE:
                workflow[NODE_SAMPLER]["inputs"]["latent_image"] = [NODE_VAE_ENCODE, 0]
        else:
            # Mode: Text-to-Audio (No upload)
            logger.info(f"Mode: Text-to-Audio")
            
            # 1. Connect Sampler back to the Empty Latent node
            if NODE_LATENT_EMPTY:
                workflow[NODE_SAMPLER]["inputs"]["latent_image"] = [NODE_LATENT_EMPTY, 0]
            
            # 2. DELETE unused audio nodes so ComfyUI stops validating them
            nodes_to_remove = [NODE_VAE_ENCODE, NODE_VHS_AUDIO, "111", "112"]
            for nid in nodes_to_remove:
                if nid and nid in workflow:
                    del workflow[nid]
            
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
                    node = str(data.get('node'))
                    if node == str(NODE_TAGS_LYRICS):
                        log_terminal_progress(current_song_num, num_songs, 5, "Encoding Lyrics & Tags...")
                        yield json.dumps({"status": "generating", "song": current_song_num, "progress": 5, "message": f"Encoding..."}) + "\n"
                    elif node == str(NODE_SAMPLER):
                        log_terminal_progress(current_song_num, num_songs, 10, "Synthesizing Audio...")
                        yield json.dumps({"status": "generating", "song": current_song_num, "progress": 10, "message": f"Synthesizing..."}) + "\n"
                        
                elif msg_type == 'progress':
                    val = data.get('value', 0)
                    mx = data.get('max', 1)
                    pct = 10 + int((val / mx) * 80)
                    
                    log_terminal_progress(current_song_num, num_songs, pct, f"Step {val}/{mx}")
                    yield json.dumps({"status": "generating", "song": current_song_num, "progress": pct, "message": f"Step {val}/{mx}..."}) + "\n"
                    
                elif msg_type == 'executed' and str(data.get('node')) == str(NODE_AUDIO_OUT):
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
