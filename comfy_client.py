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
        ws.connect(ws_url)
    except Exception as e:
        logger.error(f"WebSocket connect failed: {e}")
        yield json.dumps({"status": "error", "message": "WebSocket connection failed."}) + "\n"
        return

    try:
        with open("music_workflow.json", 'r') as f:
            base_workflow = json.load(f)
    except Exception as e:
        logger.error(f"Workflow setup error: {e}")
        yield json.dumps({"status": "error", "message": "Workflow file missing."}) + "\n"
        return

    # -------------------------------
    # 🔍 NODE DETECTION (STRICT)
    # -------------------------------
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

    logger.info(f"Detected Nodes -> Sampler:{NODE_SAMPLER}, EmptyLatent:{NODE_LATENT_EMPTY}, VAEEncode:{NODE_VAE_ENCODE}, AudioInput:{NODE_VHS_AUDIO}")

    # -------------------------------
    # 🧹 CLEAN BROKEN REFERENCES
    # -------------------------------
    def clean_workflow(workflow):
        valid_nodes = set(workflow.keys())
        for node_id, node in workflow.items():
            inputs = node.get("inputs", {})
            for key, value in list(inputs.items()):
                if isinstance(value, list) and len(value) == 2:
                    ref = str(value[0])
                    if ref not in valid_nodes:
                        logger.warning(f"Removed broken link {node_id}.{key} -> {ref}")
                        del inputs[key]

    # -------------------------------
    # 🚨 VALIDATION
    # -------------------------------
    if not NODE_SAMPLER:
        raise Exception("KSampler not found")

    base_seed = params["seed"]
    prompt_ids = []
    seed_map = {}

    # -------------------------------
    # 🔁 MAIN LOOP
    # -------------------------------
    for i in range(num_songs):
        current_seed = base_seed + i
        workflow = json.loads(json.dumps(base_workflow))

        # ---- update params ----
        if NODE_TAGS_LYRICS in workflow:
            workflow[NODE_TAGS_LYRICS]["inputs"].update({
                "tags": params["tags"],
                "lyrics": params["lyrics"],
                "duration": params["duration"],
                "seed": current_seed,
                "bpm": params["bpm"],
                "timesignature": params["timesignature"],
                "language": params["language"],
                "cfg_scale": params["cfg_scale"]
            })

        if NODE_SAMPLER in workflow:
            workflow[NODE_SAMPLER]["inputs"]["seed"] = current_seed

        if NODE_LATENT_EMPTY in workflow:
            workflow[NODE_LATENT_EMPTY]["inputs"]["seconds"] = params["duration"]

        if NODE_UNET in workflow:
            workflow[NODE_UNET]["inputs"]["unet_name"] = params["unet_name"]

        # -------------------------------
        # 🔀 ROUTING (FIXED)
        # -------------------------------
        reference_audio = params.get("reference_audio")

        if reference_audio and NODE_VHS_AUDIO and NODE_VAE_ENCODE:
            logger.info(f"Mode: Audio-to-Audio | Ref: {reference_audio}")

            workflow[NODE_VHS_AUDIO]["inputs"]["audio"] = reference_audio
            workflow[NODE_SAMPLER]["inputs"]["latent_image"] = [NODE_VAE_ENCODE, 0]

        else:
            logger.info("Mode: Text-to-Audio")

            if not NODE_LATENT_EMPTY:
                raise Exception("Empty latent node missing")

            # FORCE correct connection
            workflow[NODE_SAMPLER]["inputs"]["latent_image"] = [NODE_LATENT_EMPTY, 0]

            # 🔥 IMPORTANT FIX: DO NOT DELETE NODES
            # Instead disable safely
            if NODE_VHS_AUDIO in workflow:
                workflow[NODE_VHS_AUDIO]["inputs"].clear()

        # -------------------------------
        # 🧹 CLEAN GRAPH (CRITICAL)
        # -------------------------------
        clean_workflow(workflow)

        # -------------------------------
        # 🚀 SUBMIT
        # -------------------------------
        try:
            pid = submit_workflow(workflow, client_id)
            prompt_ids.append(pid)
            seed_map[pid] = current_seed
        except Exception as e:
            logger.error(f"Submission failed: {e}")
            yield json.dumps({"status": "error", "message": str(e)}) + "\n"
            return

    yield json.dumps({
        "status": "queued",
        "message": f"{num_songs} songs queued",
        "total_songs": num_songs
    }) + "\n"

    completed_songs = 0

    # -------------------------------
    # 📡 WEBSOCKET LOOP
    # -------------------------------
    try:
        while completed_songs < num_songs:
            out = ws.recv()

            if not isinstance(out, str):
                continue

            message = json.loads(out)
            msg_type = message.get('type')
            data = message.get('data', {})

            pid = data.get('prompt_id')
            if pid not in prompt_ids:
                continue

            song_index = prompt_ids.index(pid) + 1

            if msg_type == 'progress':
                val = data.get('value', 0)
                mx = data.get('max', 1)
                pct = 10 + int((val / mx) * 80)

                log_terminal_progress(song_index, num_songs, pct, f"Step {val}/{mx}")

                yield json.dumps({
                    "status": "generating",
                    "song": song_index,
                    "progress": pct
                }) + "\n"

            elif msg_type == 'executed' and str(data.get('node')) == str(NODE_AUDIO_OUT):
                audio_info = data['output']['audio'][0]
                filename = audio_info['filename']
                subfolder = audio_info.get('subfolder', '')

                song_id = str(uuid.uuid4())

                try:
                    conn = sqlite3.connect("music_database.db")
                    c = conn.cursor()
                    c.execute(
                        '''INSERT INTO songs 
                        (id, prompt, lyrics, seed, model, comfy_filename, comfy_subfolder)
                        VALUES (?, ?, ?, ?, ?, ?, ?)''',
                        (song_id, params["tags"], params["lyrics"],
                         seed_map[pid], params.get("unet_name", "unknown"),
                         filename, subfolder)
                    )
                    conn.commit()
                    conn.close()
                except Exception as e:
                    logger.error(f"DB error: {e}")

                log_terminal_progress(song_index, num_songs, 100, "Done", True)

                yield json.dumps({
                    "status": "song_complete",
                    "song": song_index,
                    "seed": seed_map[pid],
                    "song_id": song_id
                }) + "\n"

                completed_songs += 1

    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        yield json.dumps({"status": "error", "message": str(e)}) + "\n"

    finally:
        ws.close()
