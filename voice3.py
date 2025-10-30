import streamlit as st
import os
import re
import time
import random
import logging
import tempfile
from datetime import datetime

# --- Audio / TTS ---
import pygame
from gtts import gTTS
from pydub import AudioSegment

# --- Recording / DSP ---
import numpy as np
import sounddevice as sd
import soundfile as sf
import noisereduce as nr
from scipy.signal import butter, lfilter

# --- Optional STT backends ---
import speech_recognition as sr
try:
    from faster_whisper import WhisperModel  # recommended
    _FW_AVAILABLE = True
except Exception:
    _FW_AVAILABLE = False

# ==========================================
# Logging
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# Streamlit Page
# ==========================================
st.set_page_config(page_title="Travel Voice Assistant", page_icon="🌍", layout="wide")

# ==========================================
# Session State
# ==========================================
if 'messages' not in st.session_state:
    st.session_state.messages = []
if 'user_data' not in st.session_state:
    st.session_state.user_data = {
        'destination': None,
        'travelers': None,
        'budget': None,
        'interests': [],
        'confirmed': None
    }
if 'conversation_active' not in st.session_state:
    st.session_state.conversation_active = False
if 'conversation_ended' not in st.session_state:
    st.session_state.conversation_ended = False
if 'greeted' not in st.session_state:
    st.session_state.greeted = False
if 'logs' not in st.session_state:
    st.session_state.logs = []
if 'waiting_for_input' not in st.session_state:
    st.session_state.waiting_for_input = False
if 'retry_count' not in st.session_state:
    st.session_state.retry_count = 0
if 'last_question_type' not in st.session_state:
    st.session_state.last_question_type = None

# ==========================================
# Helpers
# ==========================================
def add_log(message, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {level}: {message}"
    st.session_state.logs.append(log_entry)
    logger.info(message)
    if len(st.session_state.logs) > 50:
        st.session_state.logs.pop(0)

# ==========================================
# Pygame mixer (init once)
# ==========================================
if not getattr(st.session_state, "_mixer_inited", False):
    try:
        pygame.mixer.quit()
        pygame.mixer.init(frequency=44100, size=-16, channels=1, buffer=1024)
        st.session_state._mixer_inited = True
        add_log("✅ Pygame mixer initialized successfully")
    except Exception as e:
        add_log(f"❌ Failed to initialize pygame: {e}", "ERROR")

# ==========================================
# TTS (gTTS single-shot + speed-up)
# ==========================================

def _speed_up(audio: AudioSegment, speed=1.15):
    return audio._spawn(audio.raw_data, overrides={"frame_rate": int(audio.frame_rate * speed)}).set_frame_rate(audio.frame_rate)


def speak(text: str) -> bool:
    try:
        add_log(f"TTS (gTTS): '{text[:60]}...'")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            tmp = fp.name
        gTTS(text=text, lang='en', slow=False, tld='com').save(tmp)
        audio = AudioSegment.from_file(tmp)
        faster = _speed_up(audio, speed=1.15)
        faster.export(tmp, format="mp3")
        pygame.mixer.music.load(tmp)
        pygame.mixer.music.set_volume(1.0)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(25)
        pygame.mixer.music.unload()
        os.unlink(tmp)
        return True
    except Exception as e:
        add_log(f"TTS error (gTTS): {e}", "ERROR")
        return False

# ==========================================
# Recording + Cleanup (LIVE, no VAD)
# ==========================================
FS = 16000                 # mono 16 kHz end-to-end
CHUNK_SECONDS = 3.5        # capture small, steady chunks
MAX_RECORD_SEC = 12        # (unused now but kept for safety / future)

# High-pass (rumble removal)

def _butter_highpass(cut, fs, order=4):
    b, a = butter(order, cut/(0.5*fs), btype='highpass')
    return b, a


def _highpass(x: np.ndarray, fs=FS, cut=130.0):
    b, a = _butter_highpass(cut, fs)
    return lfilter(b, a, x).astype(np.float32)

# Bounded noise reduction

def _safe_denoise(x: np.ndarray, fs=FS) -> np.ndarray:
    if x.size == 0:
        return x
    try:
        x = nr.reduce_noise(
            y=x, sr=fs, stationary=False,
            n_fft=1024, win_length=1024, hop_length=256,
            n_jobs=1, use_tqdm=False
        ).astype(np.float32)
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    except MemoryError:
        add_log("noisereduce skipped (MemoryError)", "WARNING")
    except Exception as e:
        add_log(f"noisereduce warning: {e}", "WARNING")
    return x


def _capture_chunk(seconds: float = CHUNK_SECONDS, device=None):
    """Capture a fixed-length audio chunk (no VAD)."""
    sd.default.samplerate = FS
    sd.default.channels = 1
    sd.default.dtype = "int16"
    if device is not None:
        sd.default.device = (device, None)  # (input, output)

    add_log(f"🎧 Capturing {seconds:.1f}s audio chunk...")
    frames = sd.rec(int(seconds * FS), dtype='int16')
    sd.wait()

    if frames is None or frames.size == 0:
        add_log("No audio captured", "WARNING")
        return None

    x = frames.flatten().astype(np.float32) / 32768.0

    # quick silence gate to skip empty chunks
    if float(np.mean(np.abs(x))) < 0.002:
        add_log("Chunk skipped: too quiet")
        return None

    # High-pass + safe denoise
    x = _highpass(x, FS, 130.0)
    x = _safe_denoise(x, FS)

    # Normalize safely
    peak = float(np.max(np.abs(x))) if x.size else 1.0
    if peak < 1e-6:
        peak = 1.0
    x = (x / peak * 0.98).astype(np.float32)

    tmp_wav = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
    sf.write(tmp_wav, x, FS)
    return tmp_wav

# ==========================================
# STT (Prefer faster-whisper; fallback to Google SR on wav)
# ==========================================
if _FW_AVAILABLE and 'whisper_model' not in st.session_state:
    # Light and robust; CPU INT8 is fine for short utterances
    st.session_state.whisper_model = WhisperModel("base", device="cpu", compute_type="int8")


def listen_live():
    """Capture a small chunk (no VAD), then transcribe (Whisper or Google SR)."""
    try:
        wav = _capture_chunk()
        if not wav:
            return None

        # Prefer Faster-Whisper
        if _FW_AVAILABLE:
            model = st.session_state.whisper_model
            segments, info = model.transcribe(wav, language="en")
            text = " ".join(s.text.strip() for s in segments).strip()
            add_log(f"✅ Whisper: '{text}'" if text else "✅ Whisper: (no text)")
            return text if text else None

        # Fallback to SpeechRecognition (offline file)
        r = sr.Recognizer()
        with sr.AudioFile(wav) as source:
            audio = r.record(source)
        try:
            text = r.recognize_google(audio, language="en-US")
            add_log(f"✅ Google SR: '{text}'")
            return text.strip()
        except sr.UnknownValueError:
            return None
        except Exception as e:
            add_log(f"SR error: {e}", "ERROR")
            return None
    except Exception as e:
        add_log(f"listen_live() fatal: {e}", "ERROR")
        return None

# ==========================================
# NLU / Conversation (unchanged from your logic)
# ==========================================

def extract_info(user_input):
    if not user_input:
        return
    user_input_lower = user_input.lower()
    add_log(f"Extracting info from: '{user_input}'")
    destinations = {
        'paris': ['paris', 'france', 'eiffel', 'french capital', 'city of lights'],
        'london': ['london', 'uk', 'england', 'britain', 'big ben', 'british'],
        'tokyo': ['tokyo', 'japan', 'japanese capital'],
        'new york': ['new york', 'nyc', 'manhattan', 'ny city'],
        'dubai': ['dubai', 'uae', 'emirates', 'burj'],
        'bali': ['bali', 'indonesia', 'balinese'],
        'maldives': ['maldives', 'maldive', 'male'],
        'switzerland': ['switzerland', 'swiss', 'zurich', 'geneva'],
        'italy': ['italy', 'italian', 'rome', 'venice', 'florence', 'milan'],
        'spain': ['spain', 'spanish', 'madrid', 'barcelona'],
        'greece': ['greece', 'greek', 'athens', 'santorini', 'mykonos'],
        'thailand': ['thailand', 'bangkok', 'phuket', 'thai'],
        'singapore': ['singapore', 'singapura'],
        'australia': ['australia', 'sydney', 'melbourne', 'aussie'],
        'india': ['india', 'goa', 'kerala', 'rajasthan', 'delhi', 'mumbai', 'jaipur'],
        'mexico': ['mexico', 'cancun', 'mexican'],
        'canada': ['canada', 'toronto', 'vancouver', 'canadian'],
        'iceland': ['iceland', 'reykjavik', 'icelandic'],
        'norway': ['norway', 'oslo', 'norwegian'],
        'hawaii': ['hawaii', 'honolulu', 'maui', 'hawaiian'],
        'turkey': ['turkey', 'istanbul', 'turkish'],
        'egypt': ['egypt', 'cairo', 'pyramids', 'egyptian'],
        'vietnam': ['vietnam', 'hanoi', 'vietnamese'],
        'peru': ['peru', 'machu picchu', 'peruvian'],
        'portugal': ['portugal', 'lisbon', 'portuguese'],
        'amsterdam': ['amsterdam', 'netherlands', 'dutch'],
        'brazil': ['brazil', 'rio', 'brazilian'],
        'austria': ['austria', 'vienna', 'austrian'],
        'germany': ['germany', 'berlin', 'munich', 'german'],
        'croatia': ['croatia', 'dubrovnik', 'croatian']
    }

    if not st.session_state.user_data['destination']:
        for dest, patterns in destinations.items():
            if any(pattern in user_input_lower for pattern in patterns):
                st.session_state.user_data['destination'] = dest.title()
                add_log(f"✅ Destination extracted: {dest.title()}")
                break

    if not st.session_state.user_data['travelers']:
        patterns = [
            r'\b(\d+)\s*(?:people|person|persons|travelers|travellers|pax|passenger|passengers)\b',
            r'\b(?:party of|group of|team of)\s*(\d+)\b',
            r'\b(\d+)\s*(?:adults?|kids?|children|members)\b',
            r'\b(?:we are|there are|will be)\s*(\d+)\b'
        ]
        for pattern in patterns:
            numbers = re.findall(pattern, user_input_lower)
            if numbers:
                count = int(numbers[0])
                st.session_state.user_data['travelers'] = f"{count} {'person' if count == 1 else 'people'}"
                add_log(f"✅ Travelers extracted: {count} people")
                break

    if not st.session_state.user_data['travelers']:
        solo = ['solo', 'alone', 'myself', 'just me', 'by myself', 'single', 'only me', 'one person']
        couple = ['couple', 'two of us', 'my partner', 'wife', 'husband', 'girlfriend', 'boyfriend', 'spouse', 'fiance', 'with my', 'me and my']
        family = ['family', 'kids', 'children', 'son', 'daughter', 'parents']
        friends = ['friends', 'buddies', 'group', 'gang', 'crew']
        if any(word in user_input_lower for word in solo):
            st.session_state.user_data['travelers'] = '1 person (Solo)'
            add_log("✅ Travelers: Solo")
        elif any(word in user_input_lower for word in couple):
            st.session_state.user_data['travelers'] = '2 people (Couple)'
            add_log("✅ Travelers: Couple")
        elif any(word in user_input_lower for word in family):
            st.session_state.user_data['travelers'] = 'Family group'
            add_log("✅ Travelers: Family")
        elif any(word in user_input_lower for word in friends):
            st.session_state.user_data['travelers'] = 'Friends group'
            add_log("✅ Travelers: Friends")

    if not st.session_state.user_data['budget']:
        luxury = ['luxury', 'premium', 'high-end', 'lavish', 'expensive', 'best', 'five star', '5 star', 'upscale', 'deluxe', 'first class', 'splurge']
        moderate = ['moderate', 'reasonable', 'average', 'standard', 'mid-range', 'medium', 'comfortable', 'decent', 'middle', 'normal']
        budget = ['budget', 'cheap', 'affordable', 'economical', 'low cost', 'inexpensive', 'frugal', 'backpacker', 'save money', 'tight budget']
        if any(word in user_input_lower for word in luxury):
            st.session_state.user_data['budget'] = 'Luxury'
            add_log("✅ Budget: Luxury")
        elif any(word in user_input_lower for word in moderate):
            st.session_state.user_data['budget'] = 'Moderate'
            add_log("✅ Budget: Moderate")
        elif any(word in user_input_lower for word in budget):
            st.session_state.user_data['budget'] = 'Budget-friendly'
            add_log("✅ Budget: Budget-friendly")


def get_response(user_input):
    add_log("Generating response...")
    extract_info(user_input)
    data = st.session_state.user_data
    user_input_lower = user_input.lower() if user_input else ""

    confirm_yes = ['yes', 'yeah', 'yep', 'sure', 'definitely', 'absolutely', 'of course', 'sounds good', 'perfect', 'great', "let's do it", 'interested', 'proceed', 'book', 'okay', 'ok', 'sounds great', "let's go", "i'm in", 'count me in', 'sign me up', 'go ahead', 'affirmative']
    confirm_no = ['no', 'nah', 'nope', 'not interested', 'maybe later', 'not sure', 'let me think', 'not now', 'cancel', 'not really', "i'll pass", 'not yet', 'hold on', 'negative']

    if data['destination'] and data['travelers'] and data['confirmed'] is None:
        add_log("Checking confirmation...")
        if any(word in user_input_lower for word in confirm_yes):
            data['confirmed'] = True
            st.session_state.conversation_ended = True
            st.session_state.conversation_active = False
            add_log("✅ Confirmed!")
            endings = [
                f"Amazing! I'm so excited for your {data['destination']} adventure! One of our travel experts will call you within 24 hours with a personalized package. Get ready for an unforgettable trip!",
                f"Fantastic! Your {data['destination']} journey is going to be incredible! Expect a call from our specialist tomorrow with exclusive deals and insider tips. Thank you for choosing us!",
                f"Wonderful! I can't wait for you to experience {data['destination']}! Our team will reach out within a day with your custom itinerary. Safe travels ahead!"
            ]
            return random.choice(endings)
        elif any(word in user_input_lower for word in confirm_no):
            data['confirmed'] = False
            st.session_state.conversation_ended = True
            st.session_state.conversation_active = False
            add_log("❌ Declined")
            endings = [
                "No problem at all! Take your time thinking it over. We're here whenever you're ready. Have a great day!",
                "That's totally fine! Travel is a big decision. Feel free to reach out anytime. Thanks for chatting!",
                "Completely understand! Come back whenever you'd like to explore options. Wishing you well!"
            ]
            return random.choice(endings)

    if not data['destination']:
        st.session_state.last_question_type = 'destination'
        questions = [
            "Where would you love to travel? Paris? Bali? Tokyo? Or somewhere else entirely?",
            "What's your dream destination? Beach paradise, mountain adventure, or city exploration?",
            "Tell me your ideal spot! European getaway, Asian adventure, or tropical escape?"
        ]
        add_log("Asking destination")
        return random.choice(questions)
    elif not data['travelers']:
        st.session_state.last_question_type = 'travelers'
        questions = [
            f"{data['destination']} is beautiful! Who's joining you? Traveling solo, with someone, or as a group?",
            f"Great pick! {data['destination']} is amazing! How many people are coming along?",
            f"Love it! {data['destination']} is perfect! Is this a solo trip, couple's getaway, or family vacation?"
        ]
        add_log("Asking travelers")
        return random.choice(questions)
    elif not data['budget']:
        st.session_state.last_question_type = 'budget'
        questions = [
            f"Perfect! So {data['travelers']} heading to {data['destination']}. What's your budget style? Luxury, moderate, or budget-friendly?",
            f"Awesome! {data['travelers']} in {data['destination']} sounds fun! Thinking premium experience or keeping it economical?",
            f"Nice! {data['travelers']} exploring {data['destination']}! High-end luxury or comfortable mid-range?"
        ]
        add_log("Asking budget")
        return random.choice(questions)
    elif data['destination'] and data['travelers'] and data['budget'] and data['confirmed'] is None:
        st.session_state.last_question_type = 'confirmation'
        add_log("Confirming details")
        confirmations = [
            f"Perfect! Let me confirm: {data['destination']}, {data['travelers']}, {data['budget']} style. Sound right? Ready to book?",
            f"Great! So that's {data['destination']} for {data['travelers']} with a {data['budget']} budget. All good? Should we proceed?",
            f"Excellent! {data['destination']}, {data['travelers']}, {data['budget']} experience. Does that work? Shall I connect you with our expert?"
        ]
        return random.choice(confirmations)

    follow_ups = [
        "Could you share more details?",
        "Tell me a bit more about that!",
        "Interesting! What else should I know?",
        "Got it! Anything else to add?"
    ]
    return random.choice(follow_ups)

# ==========================================
# UI
# ==========================================
col1, col2 = st.columns([3, 1])
with col1:
    st.title("🌍 Smart Travel Voice Assistant")
    st.markdown("### *Natural Conversation • Enhanced Recognition*")
with col2:
    if st.button("🗑️ Clear Logs"):
        st.session_state.logs = []
        st.rerun()

st.markdown("---")

main_col, log_col = st.columns([2, 1])
with main_col:
    # Simplified Trip Details (no st.success columns)
    with st.expander("📊 Trip Details", expanded=True):
        data = st.session_state.user_data
        st.markdown(
            f"""
            **🎯 Destination:** {data['destination'] or '_Not set_'}  
            **👥 Travelers:** {data['travelers'] or '_Not set_'}  
            **💰 Budget:** {data['budget'] or '_Not set_'}
            """
        )
        if data['confirmed'] is not None:
            if data['confirmed']:
                st.markdown("✅ **Confirmed!** You'll receive a call within 24 hours")
            else:
                st.markdown("❌ **Declined** — Come back anytime!")

    st.subheader("💬 Conversation")
    chat_container = st.container()
    with chat_container:
        for message in st.session_state.messages:
            if message["role"] == "user":
                st.markdown(f'<div class="user-message"><strong>🗣️ You:</strong><br>{message["content"]}</div>', unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="assistant-message"><strong>🤖 Assistant:</strong><br>{message["content"]}</div>', unsafe_allow_html=True)

    st.markdown("---")

    if not st.session_state.conversation_active and not st.session_state.conversation_ended:
        st.info("👋 **Ready to plan your dream vacation?** Click START!")
        if st.button("🎙️ START CONVERSATION", type="primary", use_container_width=True):
            add_log("🚀 Starting...")
            st.session_state.conversation_active = True
            st.session_state.retry_count = 0
            if not st.session_state.greeted:
                greetings = [
                    "Hey! Welcome to our travel agency! I'm your AI travel buddy, and I'm super excited to help plan your next adventure! Let's chat about where you'd like to go!",
                    "Hello there! Thanks for stopping by! I'm here to help you discover amazing destinations. Let's have a quick chat and find your perfect trip!",
                    "Hi! Welcome! I'm your personal travel assistant, ready to help you plan something incredible! Let's talk about your dream vacation!"
                ]
                greeting = random.choice(greetings)
                st.session_state.messages.append({"role": "assistant", "content": greeting})
                speak(greeting)
                st.session_state.greeted = True
                st.session_state.waiting_for_input = True
            st.rerun()

    elif st.session_state.conversation_active and not st.session_state.conversation_ended:
        if st.session_state.waiting_for_input:
            st.markdown('<p class="listening-indicator">🎤 LIVE — Listening... (no VAD)</p>', unsafe_allow_html=True)
            user_input = listen_live()
            if user_input and user_input.strip():
                st.session_state.retry_count = 0
                st.session_state.messages.append({"role": "user", "content": user_input})
                add_log(f"User: '{user_input}'")
                response = get_response(user_input)
                st.session_state.messages.append({"role": "assistant", "content": response})
                speak(response)
                st.session_state.waiting_for_input = not st.session_state.conversation_ended
                st.rerun()
            else:
                # No clear speech in this chunk; keep listening silently
                time.sleep(0.1)
                st.rerun()

        if st.button("⏹️ STOP", type="secondary", use_container_width=True):
            add_log("⏹️ Stopped")
            st.session_state.conversation_active = False
            st.session_state.conversation_ended = True
            st.session_state.waiting_for_input = False
            st.rerun()

    elif st.session_state.conversation_ended:
        st.success("✅ **Done!**")
        if st.button("🔄 New Conversation", use_container_width=True):
            st.session_state.messages = []
            st.session_state.user_data = {
                'destination': None,
                'travelers': None,
                'budget': None,
                'interests': [],
                'confirmed': None
            }
            st.session_state.conversation_active = False
            st.session_state.conversation_ended = False
            st.session_state.greeted = False
            st.session_state.waiting_for_input = False
            st.session_state.retry_count = 0
            st.rerun()

with log_col:
    st.subheader("📋 System Log")
    log_text = "\n".join(st.session_state.logs[-40:]) if st.session_state.logs else "Ready..."
    st.markdown(f'<div class="log-container">{log_text}</div>', unsafe_allow_html=True)
    st.markdown("---")
    st.markdown(
        """
        ### 💡 Tips for Best Results

        **🎤 Voice Recognition:**
        - Live listening in short chunks (no VAD)
        - Normal speaking pace
        - Reduce background noise

        **✨ Features:**
        - Fixed-length live capture (no end-on-silence)
        - Bounded noise reduction (no RAM blow-ups)
        - Faster-Whisper transcription (fallback to Google SR)
        - Natural TTS with slight speed-up
        - Smart, low-friction loop
        """
    )

# ============ Minimal CSS kept ============
st.markdown(
    """
    <style>
    .user-message {background: linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:20px;border-radius:20px;margin:15px 0;box-shadow:0 4px 15px rgba(102,126,234,.4)}
    .assistant-message {background: linear-gradient(135deg,#f093fb 0%,#f5576c 100%);color:white;padding:20px;border-radius:20px;margin:15px 0;box-shadow:0 4px 15px rgba(245,87,108,.4)}
    .log-container {background-color:#0d1117;color:#58a6ff;padding:15px;border-radius:10px;max-height:350px;overflow-y:auto;font-family:Consolas,Monaco,monospace;font-size:11px;line-height:1.6;border:1px solid #30363d}
    .stButton button {font-size:18px;font-weight:bold;padding:18px;border-radius:12px;transition:all .3s ease}
    .stButton button:hover {transform: translateY(-3px);box-shadow:0 8px 20px rgba(0,0,0,.3)}
    .listening-indicator {animation:pulse 1.5s infinite;color:#ff4444;font-weight:bold}
    @keyframes pulse {0%,100%{opacity:1} 50%{opacity:.4}}
    </style>
    """,
    unsafe_allow_html=True,
)
