import os
import streamlit as st
import re
import random

# --- Cloud vs Local -----------------------------------------------------------
# Streamlit Cloud runs headless; no server mic/speaker available.
IS_CLOUD = os.environ.get("STREAMLIT_SERVER_HEADLESS", "0") == "1"
ENABLE_AUDIO = not IS_CLOUD   # local mic/recording stack (sounddevice/etc.)
ENABLE_TTS   = not IS_CLOUD   # local speaker playback via pygame

# --- Safe essentials (ok on Cloud) -------------------------------------------
import numpy as np
import speech_recognition as sr
from gtts import gTTS
from pydub import AudioSegment

# --- Import soundfile for both modes -----------------------------------------
import soundfile as sf

# --- Risky on Cloud: import only if enabled ----------------------------------
# TTS / pygame
if ENABLE_TTS and not IS_CLOUD:
    try:
        import pygame
        os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"
    except Exception:
        ENABLE_TTS = False

# Audio capture/DSP stack (LOCAL ONLY)
if ENABLE_AUDIO:
    try:
        import webrtcvad
        import sounddevice as sd
        import noisereduce as nr
        import soundfile as sf
        from scipy.signal import butter, lfilter
    except Exception:
        ENABLE_AUDIO = False

# --- Logging -----------------------------------------------------------------
import logging
from datetime import datetime
import tempfile
import io

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

if 'logs' not in st.session_state:
    st.session_state.logs = []

def add_log(message, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {level}: {message}"
    st.session_state.logs.append(log_entry)
    logger.info(message)
    if len(st.session_state.logs) > 200:
        st.session_state.logs.pop(0)

# --- Pygame mixer init (LOCAL ONLY) ------------------------------------------
if ENABLE_TTS and not IS_CLOUD and not getattr(st.session_state, "_mixer_inited", False):
    try:
        import sys
        from contextlib import redirect_stderr
        import io
        
        # Suppress ALSA errors during pygame init
        f = io.StringIO()
        with redirect_stderr(f):
            pygame.mixer.quit()
            pygame.mixer.init(frequency=44100, size=-16, channels=1, buffer=1024)
            pygame.mixer.music.set_volume(1.0)
        
        st.session_state._mixer_inited = True
        add_log("✅ Pygame mixer initialized successfully")
    except Exception as e:
        add_log(f"❌ Failed to initialize pygame: {e}", "ERROR")
        ENABLE_TTS = False
elif IS_CLOUD:
    add_log("ℹ️ Running on Streamlit Cloud - audio output disabled")

# Page config
st.set_page_config(page_title="Travel Voice Assistant", page_icon="🌍", layout="wide")

# Initialize session state
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

if 'waiting_for_input' not in st.session_state:
    st.session_state.waiting_for_input = False

if 'retry_count' not in st.session_state:
    st.session_state.retry_count = 0

if 'last_question_type' not in st.session_state:
    st.session_state.last_question_type = None

def _speed_up(audio: AudioSegment, speed=1.15):
    # speed >1.0 = faster
    return audio._spawn(audio.raw_data, overrides={"frame_rate": int(audio.frame_rate * speed)}).set_frame_rate(audio.frame_rate)

def speak(text: str) -> bool:
    """TTS output - only works locally. On cloud, just displays the text."""
    if IS_CLOUD:
        # On cloud, just log it - user will see it in chat
        add_log(f"Assistant response: '{text[:60]}...'")
        return True
    
    if not ENABLE_TTS:
        add_log("TTS disabled")
        return False
    
    try:    
        add_log(f"TTS (gTTS): '{text[:60]}...'")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            tmp = fp.name
        gTTS(text=text, lang='en', slow=False, tld='com').save(tmp)

        # OPTIONAL: speed up ~15% to feel less sluggish
        audio = AudioSegment.from_file(tmp)
        faster = _speed_up(audio, speed=1.15)
        faster.export(tmp, format="mp3")

        # Suppress ALSA errors
        import sys
        from contextlib import redirect_stderr
        import io as io_module
        
        f = io_module.StringIO()
        with redirect_stderr(f):
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

# Simple text input function for cloud mode (no WebRTC needed)
def cloud_text_input():
    """Text input for cloud deployment - no audio capture needed"""
    st.info("🌐 Cloud Mode: Please type your response below")
    
    user_text = st.text_input("Your message:", key=f"text_input_{len(st.session_state.messages)}")
    
    if st.button("Send", key=f"send_btn_{len(st.session_state.messages)}"):
        if user_text and user_text.strip():
            text = user_text.strip()
            st.session_state.messages.append({"role": "user", "content": text})
            add_log(f"User typed: {text}")
            reply = get_response(text)
            st.session_state.messages.append({"role": "assistant", "content": reply})
            add_log(f"Assistant: {reply}")
            st.rerun()

def listen():
    """Local mic capture only. On Streamlit Cloud, returns None (we use text input there)."""
    if IS_CLOUD:
        add_log("listen() skipped on Cloud; using text input instead", "WARNING")
        return None

    try:
        import pyaudio  # Check if PyAudio is available
    except ImportError:
        add_log("PyAudio not available - cannot use microphone", "ERROR")
        return None

    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 4000
    recognizer.dynamic_energy_threshold = True
    recognizer.dynamic_energy_adjustment_damping = 0.15
    recognizer.dynamic_energy_ratio = 1.5
    recognizer.pause_threshold = 1.0
    recognizer.phrase_threshold = 0.3
    recognizer.non_speaking_duration = 0.8

    try:
        add_log("Opening microphone...")
        with sr.Microphone(sample_rate=48000, chunk_size=8192) as source:
            try:
                add_log("🎧 Calibrating for ambient noise...")
                recognizer.adjust_for_ambient_noise(source, duration=1.2)
            except Exception as e:
                add_log(f"Ambient calibration skipped: {e}", "WARNING")

            add_log("🎤 NOW LISTENING - Please speak clearly!")
            audio = recognizer.listen(source, timeout=20, phrase_time_limit=25)

        add_log("✅ Audio captured; processing...")

        try:
            text = recognizer.recognize_google(audio, language='en-US', show_all=False)
            add_log(f"✅ Recognized (Google): '{text}'")
            return text.strip()
        except (sr.UnknownValueError, sr.RequestError) as e:
            add_log(f"Primary recognition failed: {e}", "WARNING")

        # Try alternatives
        try:
            results = recognizer.recognize_google(audio, language='en-US', show_all=True)
            if isinstance(results, dict) and 'alternative' in results and results['alternative']:
                best = results['alternative'][0].get('transcript', '').strip()
                if best:
                    add_log(f"✅ Recognized (Google alt): '{best}'")
                    return best
        except Exception as e2:
            add_log(f"Alternative recognition failed: {e2}", "WARNING")

        # Optional offline fallback (if pocketsphinx installed)
        try:
            import pocketsphinx  # noqa
            text = recognizer.recognize_sphinx(audio)
            if text and text.strip():
                add_log(f"✅ Recognized (Sphinx): '{text}'")
                return text.strip()
        except Exception:
            pass

        return "unclear"

    except sr.WaitTimeoutError:
        add_log("⏱️ No speech detected within timeout", "WARNING")
        return None
    except Exception as e:
        add_log(f"❌ Error in listen(): {e}", "ERROR")
        return None

def extract_info(user_input):
    """Enhanced extraction with fuzzy matching and context awareness"""
    if not user_input:
        return
    
    user_input_lower = user_input.lower()
    add_log(f"Extracting info from: '{user_input}'")
    
    # Comprehensive destination database with variations
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
    
    # Check destinations
    if not st.session_state.user_data['destination']:
        for dest, patterns in destinations.items():
            if any(pattern in user_input_lower for pattern in patterns):
                st.session_state.user_data['destination'] = dest.title()
                add_log(f"✅ Destination extracted: {dest.title()}")
                break
    
    # Enhanced number extraction
    if not st.session_state.user_data['travelers']:
        # Try multiple patterns
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
    
    # Solo/couple/family/group patterns
    if not st.session_state.user_data['travelers']:
        solo = ['solo', 'alone', 'myself', 'just me', 'by myself', 'single', 'only me', 'one person']
        couple = ['couple', 'two of us', 'my partner', 'wife', 'husband', 'girlfriend', 
                 'boyfriend', 'spouse', 'fiance', 'with my', 'me and my']
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
    
    # Enhanced budget extraction
    if not st.session_state.user_data['budget']:
        luxury = ['luxury', 'premium', 'high-end', 'lavish', 'expensive', 'best', 
                 'five star', '5 star', 'upscale', 'deluxe', 'first class', 'splurge']
        moderate = ['moderate', 'reasonable', 'average', 'standard', 'mid-range', 
                   'medium', 'comfortable', 'decent', 'middle', 'normal']
        budget = ['budget', 'cheap', 'affordable', 'economical', 'low cost', 
                 'inexpensive', 'frugal', 'backpacker', 'save money', 'tight budget']
        
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
    """Generate natural, conversational responses"""
    
    add_log("Generating response...")
    extract_info(user_input)
    data = st.session_state.user_data
    
    user_input_lower = user_input.lower() if user_input else ""
    
    # Expanded confirmation keywords
    confirm_yes = ['yes', 'yeah', 'yep', 'sure', 'definitely', 'absolutely', 'of course', 
                   'sounds good', 'perfect', 'great', 'let\'s do it', 'interested', 
                   'proceed', 'book', 'okay', 'ok', 'sounds great', 'let\'s go',
                   'i\'m in', 'count me in', 'sign me up', 'go ahead', 'affirmative']
    
    confirm_no = ['no', 'nah', 'nope', 'not interested', 'maybe later', 'not sure', 
                  'let me think', 'not now', 'cancel', 'not really', 'i\'ll pass', 
                  'not yet', 'hold on', 'negative']
    
    # Confirmation stage
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
    
    # Progressive questioning
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
    
    # Context-aware follow-ups
    follow_ups = [
        "Could you share more details?",
        "Tell me a bit more about that!",
        "Interesting! What else should I know?",
        "Got it! Anything else to add?"
    ]
    
    return random.choice(follow_ups)

# Enhanced CSS
st.markdown("""
<style>
    .user-message {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: 20px;
        border-radius: 20px;
        margin: 15px 0;
        box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
        animation: slideIn 0.3s ease-out;
    }
    .assistant-message {
        background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
        color: white;
        padding: 20px;
        border-radius: 20px;
        margin: 15px 0;
        box-shadow: 0 4px 15px rgba(245, 87, 108, 0.4);
        animation: slideIn 0.3s ease-out;
    }
    @keyframes slideIn {
        from {
            opacity: 0;
            transform: translateY(20px);
        }
        to {
            opacity: 1;
            transform: translateY(0);
        }
    }
    .log-container {
        background-color: #0d1117;
        color: #58a6ff;
        padding: 15px;
        border-radius: 10px;
        max-height: 350px;
        overflow-y: auto;
        font-family: 'Consolas', 'Monaco', monospace;
        font-size: 11px;
        line-height: 1.6;
        border: 1px solid #30363d;
    }
    .stButton button {
        font-size: 18px;
        font-weight: bold;
        padding: 18px;
        border-radius: 12px;
        transition: all 0.3s ease;
    }
    .stButton button:hover {
        transform: translateY(-3px);
        box-shadow: 0 8px 20px rgba(0,0,0,0.3);
    }
    .listening-indicator {
        animation: pulse 1.5s infinite;
        color: #ff4444;
        font-weight: bold;
    }
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.4; }
    }
</style>
""", unsafe_allow_html=True)

# Header
col1, col2 = st.columns([3, 1])
with col1:
    st.title("🌍 Smart Travel Voice Assistant")
    st.markdown("### *Natural Conversation • Enhanced Recognition*")

with col2:
    if st.button("🗑️ Clear Logs"):
        st.session_state.logs = []
        st.rerun()

st.markdown("---")

# Main layout
main_col, log_col = st.columns([2, 1])

with main_col:
    # Info display
    with st.expander("📊 Trip Details", expanded=True):
        data = st.session_state.user_data
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if data['destination']:
                st.success(f"🎯 **Destination**\n\n{data['destination']}")
            else:
                st.info("🎯 **Destination**\n\nNot set")
        
        with col2:
            if data['travelers']:
                st.success(f"👥 **Travelers**\n\n{data['travelers']}")
            else:
                st.info("👥 **Travelers**\n\nNot set")
        
        with col3:
            if data['budget']:
                st.success(f"💰 **Budget**\n\n{data['budget']}")
            else:
                st.info("💰 **Budget**\n\nNot set")
        
        if data['confirmed'] is not None:
            if data['confirmed']:
                st.success("✅ **Confirmed!** You'll receive a call within 24 hours")
            else:
                st.warning("❌ **Declined** - Come back anytime!")
    
    # Chat display
    st.subheader("💬 Conversation")
    
    chat_container = st.container()
    with chat_container:
        for message in st.session_state.messages:
            if message["role"] == "user":
                st.markdown(f'<div class="user-message"><strong>🗣️ You:</strong><br>{message["content"]}</div>', 
                           unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="assistant-message"><strong>🤖 Assistant:</strong><br>{message["content"]}</div>', 
                           unsafe_allow_html=True)
    
    st.markdown("---")
    
    # Controls
    if not st.session_state.conversation_active and not st.session_state.conversation_ended:
        if IS_CLOUD:
            st.info("👋 **Ready to plan your dream vacation?** Click START to begin! (Cloud Mode: Text input)")
        else:
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
            if IS_CLOUD:
                # CLOUD: use text input instead of microphone
                cloud_text_input()
            else:
                # LOCAL: use system microphone
                st.markdown(
                    '<p class="listening-indicator">🎤 LISTENING... Speak now!</p>',
                    unsafe_allow_html=True
                )
                user_input = listen()

                if user_input and user_input != "unclear":
                    st.session_state.retry_count = 0
                    st.session_state.messages.append({"role": "user", "content": user_input})
                    add_log(f"User: '{user_input}'")
                    response = get_response(user_input)
                    st.session_state.messages.append({"role": "assistant", "content": response})
                    speak(response)
                    st.session_state.waiting_for_input = not st.session_state.conversation_ended
                    st.rerun()

                elif user_input == "unclear":
                    st.session_state.retry_count += 1
                    
                    if st.session_state.retry_count <= 2:
                        prompts = [
                            "Sorry, didn't catch that. Could you repeat?",
                            "I missed that. One more time please?",
                            "Audio unclear. Try again?"
                        ]
                    else:
                        prompts = ["Please speak louder and clearer. I'm listening!"]
                        st.session_state.retry_count = 0
                    
                    prompt = random.choice(prompts)
                    speak(prompt)
                    st.session_state.messages.append({"role": "assistant", "content": prompt})
                    st.session_state.waiting_for_input = True
                    st.rerun()

                else:
                    st.session_state.retry_count += 1
                    
                    if st.session_state.retry_count <= 2:
                        prompts = [
                            "Are you there? Please speak!",
                            "I'm listening. Go ahead!",
                            "Ready when you are!"
                        ]
                    else:
                        prompts = ["Still here! Speak clearly when ready!"]
                        st.session_state.retry_count = 0
                    
                    prompt = random.choice(prompts)
                    speak(prompt)
                    st.session_state.messages.append({"role": "assistant", "content": prompt})
                    st.session_state.waiting_for_input = True
                    st.rerun()

        # Stop button (visible during active conversation)
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
    if IS_CLOUD:
        st.markdown("""
        ### 💡 Cloud Mode Active
        
        **💬 Text Input:**
        - Type your responses in the text box
        - Click "Send" to submit
        - All conversation is text-based
        
        **✨ Features:**
        - Smart destination matching
        - Context-aware responses
        - Natural conversation flow
        - Trip detail extraction
        
        **📱 Status:**
        - 💬 = Text conversation
        - ✅ = Information captured
        - 🌐 = Cloud deployment
        """)
    else:
        st.markdown("""
        ### 💡 Tips for Best Results
        
        **🎤 Voice Recognition:**
        - Wait for calibration (2 seconds)
        - Speak clearly after "NOW LISTENING"
        - Normal speaking pace
        - Reduce background noise
        
        **✨ Features:**
        - Multiple recognition engines
        - Natural sentence pausing
        - Context-aware responses
        - Smart retry logic
        - Enhanced audio quality
        
        **📱 Status:**
        - 🎤 = Listening
        - 🗣️ = Speaking
        - ✅ = Success
        - ⚠️ = Retry needed
        """)