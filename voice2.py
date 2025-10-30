import os
import streamlit as st
import streamlit.components.v1 as components
import re
import random

# --- Cloud vs Local -----------------------------------------------------------
IS_CLOUD = os.environ.get("STREAMLIT_SERVER_HEADLESS", "0") == "1"

# --- Safe essentials (ok on Cloud) -------------------------------------------
import numpy as np
from gtts import gTTS
from pydub import AudioSegment
import soundfile as sf
import logging
from datetime import datetime
import tempfile
import io
import base64

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Page config
st.set_page_config(page_title="Travel Voice Assistant", page_icon="🌍", layout="wide")

# Initialize session state
if 'logs' not in st.session_state:
    st.session_state.logs = []

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

if 'last_question_type' not in st.session_state:
    st.session_state.last_question_type = None

if 'last_transcript' not in st.session_state:
    st.session_state.last_transcript = None

if 'current_audio' not in st.session_state:
    st.session_state.current_audio = None

def add_log(message, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {level}: {message}"
    st.session_state.logs.append(log_entry)
    logger.info(message)
    if len(st.session_state.logs) > 200:
        st.session_state.logs.pop(0)

def _speed_up(audio: AudioSegment, speed=1.15):
    return audio._spawn(audio.raw_data, overrides={"frame_rate": int(audio.frame_rate * speed)}).set_frame_rate(audio.frame_rate)

def generate_audio(text: str) -> str:
    """Generate audio and return base64 encoded string"""
    try:
        add_log(f"Generating TTS: '{text[:60]}...'")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            tmp = fp.name
        
        gTTS(text=text, lang='en', slow=False, tld='com').save(tmp)
        
        # Speed up
        audio = AudioSegment.from_file(tmp)
        faster = _speed_up(audio, speed=1.15)
        faster.export(tmp, format="mp3")
        
        # Read and encode
        with open(tmp, "rb") as f:
            audio_bytes = f.read()
        
        os.unlink(tmp)
        
        # Return base64 encoded audio
        audio_base64 = base64.b64encode(audio_bytes).decode()
        return audio_base64
    except Exception as e:
        add_log(f"TTS error: {e}", "ERROR")
        return None

def browser_speech_component():
    """Component for browser-based speech recognition with manual submit"""
    
    # HTML/JS for Web Speech API
    html_code = """
    <div style="padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 15px; color: white;">
        <div style="text-align: center; margin-bottom: 20px;">
            <h3>🎤 Voice Input</h3>
            <button id="startBtn" onclick="startListening()" 
                    style="background: white; color: #667eea; border: none; padding: 15px 30px; 
                           border-radius: 10px; font-size: 16px; font-weight: bold; cursor: pointer; 
                           margin: 10px;">
                🎙️ Start Speaking
            </button>
            <button id="stopBtn" onclick="stopListening()" 
                    style="background: #ff4444; color: white; border: none; padding: 15px 30px; 
                           border-radius: 10px; font-size: 16px; font-weight: bold; cursor: pointer; 
                           margin: 10px; display: none;">
                ⏹️ Stop
            </button>
        </div>
        <div id="status" style="text-align: center; font-size: 18px; margin: 15px 0; font-weight: bold;">
            Click "Start Speaking" to begin
        </div>
        <div id="transcript" style="background: rgba(255,255,255,0.2); padding: 15px; 
                                     border-radius: 10px; min-height: 80px; font-size: 18px; font-weight: 500;">
            Your speech will appear here...
        </div>
        <div style="text-align: center; margin-top: 20px;">
            <button id="submitBtn" onclick="submitTranscript()" 
                    style="background: #00ff88; color: #333; border: none; padding: 15px 40px; 
                           border-radius: 10px; font-size: 18px; font-weight: bold; cursor: pointer; 
                           display: none;">
                ✅ Submit
            </button>
        </div>
    </div>
    
    <script>
        let recognition;
        let isListening = false;
        let finalTranscript = '';
        
        // Check if browser supports speech recognition
        if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            recognition = new SpeechRecognition();
            recognition.continuous = true;
            recognition.interimResults = true;
            recognition.lang = 'en-US';
            
            recognition.onstart = function() {
                isListening = true;
                finalTranscript = '';
                document.getElementById('status').innerText = '🎤 Listening... Speak now!';
                document.getElementById('status').style.color = '#00ff88';
                document.getElementById('transcript').innerText = 'Listening...';
                document.getElementById('startBtn').style.display = 'none';
                document.getElementById('stopBtn').style.display = 'inline-block';
                document.getElementById('submitBtn').style.display = 'none';
            };
            
            recognition.onresult = function(event) {
                let interimTranscript = '';
                
                for (let i = event.resultIndex; i < event.results.length; i++) {
                    const transcript = event.results[i][0].transcript;
                    if (event.results[i].isFinal) {
                        finalTranscript += transcript + ' ';
                    } else {
                        interimTranscript += transcript;
                    }
                }
                
                document.getElementById('transcript').innerText = 
                    (finalTranscript + interimTranscript).trim() || 'Listening...';
            };
            
            recognition.onend = function() {
                isListening = false;
                
                if (finalTranscript.trim()) {
                    document.getElementById('status').innerText = '✅ Speech captured! Click Submit to send.';
                    document.getElementById('status').style.color = '#00ff88';
                    document.getElementById('transcript').innerText = finalTranscript.trim();
                    document.getElementById('submitBtn').style.display = 'inline-block';
                } else {
                    document.getElementById('status').innerText = '❌ No speech detected. Try again!';
                    document.getElementById('status').style.color = '#ff4444';
                    document.getElementById('transcript').innerText = 'Your speech will appear here...';
                }
                
                document.getElementById('startBtn').style.display = 'inline-block';
                document.getElementById('stopBtn').style.display = 'none';
            };
            
            recognition.onerror = function(event) {
                console.error('Speech recognition error:', event.error);
                document.getElementById('status').innerText = '❌ Error: ' + event.error;
                document.getElementById('status').style.color = '#ff4444';
                document.getElementById('startBtn').style.display = 'inline-block';
                document.getElementById('stopBtn').style.display = 'none';
                isListening = false;
            };
        } else {
            document.getElementById('status').innerText = '❌ Speech recognition not supported in this browser. Please use Chrome or Edge.';
            document.getElementById('status').style.color = '#ff4444';
        }
        
        function startListening() {
            if (recognition && !isListening) {
                finalTranscript = '';
                document.getElementById('transcript').innerText = 'Starting...';
                recognition.start();
            }
        }
        
        function stopListening() {
            if (recognition && isListening) {
                recognition.stop();
            }
        }
        
        function submitTranscript() {
            const text = document.getElementById('transcript').innerText;
            if (text && text !== 'Your speech will appear here...' && text !== 'Listening...') {
                // Send to Streamlit
                window.parent.postMessage({
                    type: 'streamlit:setComponentValue',
                    value: text
                }, '*');
                
                // Reset UI
                document.getElementById('transcript').innerText = 'Sent! Speak again or wait for response...';
                document.getElementById('submitBtn').style.display = 'none';
                document.getElementById('status').innerText = '📤 Message sent! Processing...';
                document.getElementById('status').style.color = '#fff';
                finalTranscript = '';
            }
        }
    </script>
    """
    
    # Render component with static key
    transcript = components.html(html_code, height=350)
    return transcript

def play_audio_browser(audio_base64):
    """Play audio in browser using HTML5 audio element"""
    if audio_base64:
        audio_html = f"""
        <audio autoplay>
            <source src="data:audio/mp3;base64,{audio_base64}" type="audio/mp3">
        </audio>
        <script>
            // Ensure audio plays
            document.querySelector('audio').play();
        </script>
        """
        components.html(audio_html, height=0)

def extract_info(user_input):
    """Enhanced extraction with fuzzy matching and context awareness"""
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
        ]
        
        for pattern in patterns:
            numbers = re.findall(pattern, user_input_lower)
            if numbers:
                count = int(numbers[0])
                st.session_state.user_data['travelers'] = f"{count} {'person' if count == 1 else 'people'}"
                add_log(f"✅ Travelers extracted: {count} people")
                break
    
    if not st.session_state.user_data['travelers']:
        solo = ['solo', 'alone', 'myself', 'just me', 'by myself', 'single']
        couple = ['couple', 'two of us', 'my partner', 'wife', 'husband']
        family = ['family', 'kids', 'children']
        
        if any(word in user_input_lower for word in solo):
            st.session_state.user_data['travelers'] = '1 person (Solo)'
        elif any(word in user_input_lower for word in couple):
            st.session_state.user_data['travelers'] = '2 people (Couple)'
        elif any(word in user_input_lower for word in family):
            st.session_state.user_data['travelers'] = 'Family group'
    
    if not st.session_state.user_data['budget']:
        luxury = ['luxury', 'premium', 'high-end', 'expensive', 'five star', '5 star']
        moderate = ['moderate', 'reasonable', 'average', 'standard', 'mid-range']
        budget = ['budget', 'cheap', 'affordable', 'economical', 'low cost']
        
        if any(word in user_input_lower for word in luxury):
            st.session_state.user_data['budget'] = 'Luxury'
        elif any(word in user_input_lower for word in moderate):
            st.session_state.user_data['budget'] = 'Moderate'
        elif any(word in user_input_lower for word in budget):
            st.session_state.user_data['budget'] = 'Budget-friendly'

def get_response(user_input):
    """Generate natural, conversational responses"""
    add_log("Generating response...")
    extract_info(user_input)
    data = st.session_state.user_data
    
    user_input_lower = user_input.lower() if user_input else ""
    
    confirm_yes = ['yes', 'yeah', 'yep', 'sure', 'definitely', 'absolutely', 'okay', 'ok']
    confirm_no = ['no', 'nah', 'nope', 'not interested', 'maybe later']
    
    if data['destination'] and data['travelers'] and data['confirmed'] is None:
        if any(word in user_input_lower for word in confirm_yes):
            data['confirmed'] = True
            st.session_state.conversation_ended = True
            st.session_state.conversation_active = False
            return f"Amazing! I'm so excited for your {data['destination']} adventure! One of our travel experts will call you within 24 hours. Get ready for an unforgettable trip!"
        
        elif any(word in user_input_lower for word in confirm_no):
            data['confirmed'] = False
            st.session_state.conversation_ended = True
            st.session_state.conversation_active = False
            return "No problem at all! Take your time. We're here whenever you're ready. Have a great day!"
    
    if not data['destination']:
        return "Where would you love to travel? Paris? Bali? Tokyo? Or somewhere else?"
    
    elif not data['travelers']:
        return f"{data['destination']} is beautiful! Who's joining you? Traveling solo, with someone, or as a group?"
    
    elif not data['budget']:
        return f"Perfect! So {data['travelers']} heading to {data['destination']}. What's your budget style? Luxury, moderate, or budget-friendly?"
    
    elif data['destination'] and data['travelers'] and data['budget'] and data['confirmed'] is None:
        return f"Great! Let me confirm: {data['destination']}, {data['travelers']}, {data['budget']} style. Sound right? Ready to book?"
    
    return "Could you share more details?"

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
    }
    .assistant-message {
        background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
        color: white;
        padding: 20px;
        border-radius: 20px;
        margin: 15px 0;
        box-shadow: 0 4px 15px rgba(245, 87, 108, 0.4);
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
</style>
""", unsafe_allow_html=True)

# Header
st.title("🌍 Smart Travel Voice Assistant")
st.markdown("### *Browser Voice Recognition • Works on Cloud!*")
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
        st.info("👋 **Ready to plan your dream vacation?** Click START!")
        
        if st.button("🎙️ START CONVERSATION", type="primary", use_container_width=True):
            add_log("🚀 Starting...")
            st.session_state.conversation_active = True
            
            if not st.session_state.greeted:
                greeting = "Hey! Welcome to our travel agency! I'm your AI travel buddy. Let's chat about where you'd like to go!"
                st.session_state.messages.append({"role": "assistant", "content": greeting})
                
                # Generate and play audio
                audio_b64 = generate_audio(greeting)
                if audio_b64:
                    st.session_state.current_audio = audio_b64
                
                st.session_state.greeted = True
                st.session_state.waiting_for_input = True
            
            st.rerun()
    
    elif st.session_state.conversation_active and not st.session_state.conversation_ended:
        # Play current audio if available
        if st.session_state.current_audio:
            play_audio_browser(st.session_state.current_audio)
            st.session_state.current_audio = None
        
        # Voice input
        st.info("🎤 **Speak, then click Submit button to send your message**")
        transcript = browser_speech_component()
        
        # Process transcript - check if it's a string and not empty
        if transcript and isinstance(transcript, str) and transcript.strip():
            user_input = transcript.strip()
            
            # Avoid processing the same input twice
            if user_input != st.session_state.last_transcript:
                st.session_state.last_transcript = user_input
                st.session_state.messages.append({"role": "user", "content": user_input})
                add_log(f"User: '{user_input}'")
                
                response = get_response(user_input)
                st.session_state.messages.append({"role": "assistant", "content": response})
                
                # Generate audio for response
                audio_b64 = generate_audio(response)
                if audio_b64:
                    st.session_state.current_audio = audio_b64
                
                st.session_state.waiting_for_input = not st.session_state.conversation_ended
                st.rerun()
        
        # Stop button
        if st.button("⏹️ STOP", type="secondary", use_container_width=True):
            add_log("⏹️ Stopped")
            st.session_state.conversation_active = False
            st.session_state.conversation_ended = True
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
            st.session_state.current_audio = None
            st.session_state.last_transcript = None
            st.rerun()

with log_col:
    st.subheader("📋 System Log")
    
    log_text = "\n".join(st.session_state.logs[-40:]) if st.session_state.logs else "Ready..."
    
    st.markdown(f'<div class="log-container">{log_text}</div>', unsafe_allow_html=True)
    
    st.markdown("---")
    st.markdown("""
    ### 💡 Browser Voice Features
    
    **🎤 Voice Input:**
    - Uses browser's Web Speech API
    - Click microphone button to speak
    - Works on Chrome, Edge, Safari
    
    **🔊 Voice Output:**
    - Auto-plays bot responses
    - High-quality gTTS voices
    - Automatic playback
    
    **✨ Features:**
    - Smart destination matching
    - Context-aware responses
    - Natural conversation flow
    - Works on Streamlit Cloud!
    
    **📱 Compatibility:**
    - ✅ Chrome/Edge (Best)
    - ✅ Safari (iOS/Mac)
    - ❌ Firefox (Limited)
    """)