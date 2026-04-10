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
    ws.connect(ws_url)

    # 🔥 SELECT WORKFLOW
    if params.get("reference_audio"):
        base_workflow = load_workflow("music_workflow.json")  # NEW (audio)
        mode = "audio"
    else:
        base_workflow = load_workflow("text2song.json")  # OLD (text)
        mode = "text"

    # 🔍 NODE DETECTION
    NODE_TEXT = get_node_id(base_workflow, "TextEncodeAceStepAudio1.5")
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

        # ---- TEXT PARAMS ----
        if NODE_TEXT:
            wf[NODE_TEXT]["inputs"].update({
                "tags": params["tags"],
                "lyrics": params["lyrics"],
                "duration": params["duration"],
                "seed": seed,
                "bpm": params["bpm"],
                "timesignature": params["timesignature"],
                "language": params["language"],
                "cfg_scale": params["cfg_scale"]
            })

        if NODE_SAMPLER:
            wf[NODE_SAMPLER]["inputs"]["seed"] = seed

        if NODE_UNET:
            wf[NODE_UNET]["inputs"]["unet_name"] = params["unet_name"]

        # 🔀 MODE-SPECIFIC LOGIC
        if mode == "text":
            wf[NODE_SAMPLER]["inputs"]["latent_image"] = [NODE_LATENT, 0]
            wf[NODE_LATENT]["inputs"]["seconds"] = params["duration"]

        else:  # audio mode
            wf[NODE_VHS]["inputs"]["audio"] = params["reference_audio"]
            wf[NODE_SAMPLER]["inputs"]["latent_image"] = [NODE_VAE_ENCODE, 0]

        pid = submit_workflow(wf, client_id)
        prompt_ids.append(pid)
        seed_map[pid] = seed

    yield json.dumps({"status": "queued", "total": num_songs}) + "\n"

    completed = 0

    while completed < num_songs:
        msg = json.loads(ws.recv())
        t = msg.get("type")
        data = msg.get("data", {})

        pid = data.get("prompt_id")
        if pid not in prompt_ids:
            continue

        idx = prompt_ids.index(pid) + 1

        if t == "progress":
            val = data.get("value", 0)
            mx = data.get("max", 1)
            pct = int((val / mx) * 100)

            yield json.dumps({
                "status": "generating",
                "song": idx,
                "progress": pct
            }) + "\n"

        elif t == "executed" and str(data.get("node")) == str(NODE_AUDIO_OUT):
            yield json.dumps({
                "status": "song_complete",
                "song": idx,
                "seed": seed_map[pid]
            }) + "\n"

            completed += 1

    ws.close()
