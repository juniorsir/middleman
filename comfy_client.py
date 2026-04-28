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
    url = f"{APP_URL}/upload/image" 
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
        error_info = res.json()
        logger.error(f"--- COMFYUI VALIDATION ERROR ---\n{json.dumps(error_info, indent=2)}")
        raise Exception(f"ComfyUI rejected the request.")
        
    return res.json().get("prompt_id")

def load_workflow(file):
    with open(file, "r") as f:
        return json.load(f)

def get_node_id(workflow, class_type):
    for k, v in workflow.items():
        if v.get("class_type") == class_type:
            return str(k)
    return None

def generate_music_stream(params, num_songs=1):
    client_id = str(uuid.uuid4())
    ws = websocket.WebSocket()
    ws_url = APP_URL.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?clientId={client_id}"
    
    try:
        ws.connect(ws_url)
    except Exception as e:
        logger.error(f"WebSocket connect failed: {e}")
        yield json.dumps({"status": "error", "message": "WebSocket connection failed."}) + "\n"
        return

    # 🔥 SELECT WORKFLOW
    if params.get("reference_audio"):
        base_workflow = load_workflow("music_workflow.json")
        mode = "audio"
    else:
        base_workflow = load_workflow("text2song.json")
        mode = "text"

    # 🔍 NODE DETECTION
    NODE_TEXT = get_node_id(base_workflow, "TextEncodeAceStepAudio1.5")
    print(f"DEBUG: Found Text Node ID: {NODE_TEXT}")
    NODE_SAMPLER = get_node_id(base_workflow, "KSampler")
    NODE_LATENT = get_node_id(base_workflow, "EmptyAceStep1.5LatentAudio")
    NODE_UNET = get_node_id(base_workflow, "UNETLoader")
    NODE_AUDIO_OUT = get_node_id(base_workflow, "SaveAudioMP3") or get_node_id(base_workflow, "SaveAudio")
    NODE_VHS = get_node_id(base_workflow, "VHS_LoadAudioUpload")
    NODE_VAE_ENCODE = get_node_id(base_workflow, "VAEEncodeAudio")

    base_seed = params["seed"]
    prompt_ids = []
    seed_map = {}

    for i in range(num_songs):
        seed = base_seed + i
        wf = json.loads(json.dumps(base_workflow))

        if NODE_TEXT:
            wf[NODE_TEXT]["inputs"].update({
                "tags": params.get("tags") or "high quality instrumental", "lyrics": params["lyrics"],
                "duration": params["duration"], "seed": seed,
                "bpm": params["bpm"], "timesignature": params["timesignature"],
                "language": params["language"], "cfg_scale": params["cfg_scale"]
            })

        if NODE_SAMPLER: 
                    wf[NODE_SAMPLER]["inputs"]["seed"] = seed
                    wf[NODE_SAMPLER]["inputs"]["denoise"] = params.get("denoise", 0.55)
            
        if NODE_UNET: wf[NODE_UNET]["inputs"]["unet_name"] = params["unet_name"]

        if mode == "text":
            wf[NODE_SAMPLER]["inputs"]["latent_image"] = [NODE_LATENT, 0]
            wf[NODE_LATENT]["inputs"]["seconds"] = params["duration"]
        else:
            wf[NODE_VHS]["inputs"]["audio"] = params["reference_audio"]
            wf[NODE_SAMPLER]["inputs"]["latent_image"] = [NODE_VAE_ENCODE, 0]

        pid = submit_workflow(wf, client_id)
        prompt_ids.append(pid)
        seed_map[pid] = seed

    yield json.dumps({"status": "queued", "total": num_songs}) + "\n"

    completed = 0
    try:
        while completed < num_songs:
            msg = json.loads(ws.recv())
            t, data = msg.get("type"), msg.get("data", {})
            pid = data.get("prompt_id")
            
            if pid not in prompt_ids: continue
            idx = prompt_ids.index(pid) + 1

            if t == "executing":
                node_executing = str(data.get("node"))
                msg_text = "Encoding..." if node_executing == NODE_TEXT else "Synthesizing..."
                log_terminal_progress(idx, num_songs, 10, msg_text)
                yield json.dumps({"status": "generating", "song": idx, "progress": 10, "message": msg_text}) + "\n"

            elif t == "progress":
                val, mx = data.get("value", 0), data.get("max", 1)
                pct = 15 + int((val / mx) * 80)
                log_terminal_progress(idx, num_songs, pct, f"Step {val}/{mx}")
                yield json.dumps({"status": "generating", "song": idx, "progress": pct}) + "\n"

            elif t == "executed" and str(data.get("node")) == str(NODE_AUDIO_OUT):
                # 1. Extract the audio filename from ComfyUI response
                audio_output = data['output']['audio'][0]
                comfy_filename = audio_output['filename']
                comfy_subfolder = audio_output.get('subfolder', '')

                # 2. Generate a unique ID and save to database
                song_uuid = str(uuid.uuid4())
                try:
                    conn = sqlite3.connect("music_database.db")
                    c = conn.cursor()
                    c.execute('''INSERT INTO songs (id, prompt, lyrics, seed, model, comfy_filename, comfy_subfolder) 
                                 VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                              (song_uuid, params["tags"], params["lyrics"], seed_map[pid], params.get("unet_name", "unknown"), comfy_filename, comfy_subfolder))
                    conn.commit()
                    conn.close()
                except Exception as e:
                    logger.error(f"Database error: {e}")

                # 3. Create the API URL for the frontend
                song_url = f"/songs/{song_uuid}/stream"
                log_terminal_progress(idx, num_songs, 100, "Done!", is_done=True)
                
                yield json.dumps({
                    "status": "song_complete",
                    "song": idx,
                    "seed": seed_map[pid],
                    "song_id": song_uuid,
                    "audio_url": song_url
                }) + "\n"
                completed += 1

    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        yield json.dumps({"status": "error", "message": "Connection lost."}) + "\n"
    finally:
        ws.close()
