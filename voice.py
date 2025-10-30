import streamlit as st
import speech_recognition as sr
from gtts import gTTS
import os
import tempfile
import time
import re
import pygame
import logging
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize pygame for audio playback
try:
    pygame.mixer.init()
    logger.info("✅ Pygame mixer initialized successfully")
except Exception as e:
    logger.error(f"❌ Failed to initialize pygame: {e}")

# Page config
st.set_page_config(page_title="Travel Voice Assistant", page_icon="🌍", layout="wide")

# Initialize session state
if 'messages' not in st.session_state:
    st.session_state.messages = []
    logger.info("Initialized messages list")

if 'user_data' not in st.session_state:
    st.session_state.user_data = {
        'destination': None,
        'travelers': None,
        'budget': None,
        'interests': [],
        'confirmed': None
    }
    logger.info("Initialized user_data dictionary")

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

if 'auto_listen' not in st.session_state:
    st.session_state.auto_listen = False

def add_log(message, level="INFO"):
    """Add log message to session state for display"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {level}: {message}"
    st.session_state.logs.append(log_entry)
    logger.info(message)
    
    # Keep only last 50 logs
    if len(st.session_state.logs) > 50:
        st.session_state.logs.pop(0)

def speak(text):
    """Convert text to speech and play it"""
    try:
        add_log(f"Speaking: '{text[:50]}...'")
        
        tts = gTTS(text=text, lang='en', slow=False)
        
        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as fp:
            temp_file = fp.name
            tts.save(temp_file)
        
        add_log(f"Audio file created: {temp_file}")
        
        # Play the audio using pygame
        pygame.mixer.music.load(temp_file)
        pygame.mixer.music.play()
        
        # Wait for audio to finish
        while pygame.mixer.music.get_busy():
            time.sleep(0.1)
        
        add_log("Audio playback completed")
        
        # Clean up
        pygame.mixer.music.unload()
        time.sleep(0.2)
        
        try:
            os.unlink(temp_file)
            add_log("Temporary audio file deleted")
        except Exception as e:
            add_log(f"Warning: Could not delete temp file: {e}", "WARNING")
        
        return True
        
    except Exception as e:
        add_log(f"Error in speech synthesis: {e}", "ERROR")
        st.error(f"Speech error: {e}")
        return False

def listen():
    """Listen to user and convert speech to text"""
    recognizer = sr.Recognizer()
    
    try:
        add_log("Opening microphone...")
        
        with sr.Microphone() as source:
            add_log("Adjusting for ambient noise...")
            recognizer.adjust_for_ambient_noise(source, duration=0.5)
            
            add_log("🎤 Listening for speech...")
            audio = recognizer.listen(source, timeout=10, phrase_time_limit=15)
            
            add_log("Audio captured, processing...")
            text = recognizer.recognize_google(audio)
            
            add_log(f"✅ Recognized: '{text}'")
            return text
            
    except sr.WaitTimeoutError:
        add_log("⏱️ Timeout: No speech detected", "WARNING")
        return None
        
    except sr.UnknownValueError:
        add_log("❌ Could not understand audio", "WARNING")
        return "unclear"
        
    except sr.RequestError as e:
        add_log(f"❌ API Error: {e}", "ERROR")
        return None
        
    except Exception as e:
        add_log(f"❌ Unexpected error in listen(): {e}", "ERROR")
        return None

def extract_info(user_input):
    """Extract information from natural conversation"""
    if not user_input:
        return
    
    user_input_lower = user_input.lower()
    add_log(f"Extracting info from: '{user_input}'")
    
    # Extract destination
    destinations = ['paris', 'london', 'tokyo', 'new york', 'dubai', 'bali', 'maldives', 
                   'switzerland', 'italy', 'spain', 'greece', 'thailand', 'singapore',
                   'australia', 'japan', 'india', 'mexico', 'canada', 'brazil', 'egypt',
                   'amsterdam', 'iceland', 'norway', 'hawaii', 'miami', 'los angeles',
                   'turkey', 'portugal', 'austria', 'germany', 'france', 'croatia',
                   'sweden', 'denmark', 'bali', 'phuket', 'goa', 'kerala', 'rome',
                   'barcelona', 'prague', 'vietnam', 'cambodia', 'peru', 'china']
    
    for dest in destinations:
        if dest in user_input_lower:
            if not st.session_state.user_data['destination']:
                st.session_state.user_data['destination'] = dest.title()
                add_log(f"✅ Destination extracted: {dest.title()}")
                break
    
    # Extract number of travelers
    numbers = re.findall(r'\b(\d+)\s*(people|person|travelers|travellers|pax|of us)\b', user_input_lower)
    if numbers and not st.session_state.user_data['travelers']:
        st.session_state.user_data['travelers'] = f"{numbers[0][0]} people"
        add_log(f"✅ Travelers extracted: {numbers[0][0]} people")
    
    # Solo/couple/family
    if not st.session_state.user_data['travelers']:
        if any(word in user_input_lower for word in ['solo', 'alone', 'myself', 'just me']):
            st.session_state.user_data['travelers'] = '1 (Solo)'
            add_log("✅ Travelers extracted: Solo")
        elif any(word in user_input_lower for word in ['couple', 'two of us', 'my partner', 'my wife', 'my husband', 'girlfriend', 'boyfriend']):
            st.session_state.user_data['travelers'] = '2 (Couple)'
            add_log("✅ Travelers extracted: Couple")
        elif 'family' in user_input_lower:
            st.session_state.user_data['travelers'] = 'Family group'
            add_log("✅ Travelers extracted: Family")
    
    # Extract budget
    if not st.session_state.user_data['budget']:
        if any(word in user_input_lower for word in ['luxury', 'premium', 'high-end', 'lavish', 'expensive', 'best']):
            st.session_state.user_data['budget'] = 'Luxury'
            add_log("✅ Budget extracted: Luxury")
        elif any(word in user_input_lower for word in ['moderate', 'reasonable', 'average', 'standard', 'mid-range', 'medium']):
            st.session_state.user_data['budget'] = 'Moderate'
            add_log("✅ Budget extracted: Moderate")
        elif any(word in user_input_lower for word in ['budget', 'cheap', 'affordable', 'economical', 'low cost']):
            st.session_state.user_data['budget'] = 'Budget-friendly'
            add_log("✅ Budget extracted: Budget-friendly")

def get_response(user_input):
    """Generate natural responses based on conversation context"""
    
    add_log("Generating response...")
    extract_info(user_input)
    data = st.session_state.user_data
    
    user_input_lower = user_input.lower() if user_input else ""
    
    # Confirmation keywords
    confirm_yes = ['yes', 'yeah', 'sure', 'definitely', 'absolutely', 'of course', 
                   'sounds good', 'let\'s do it', 'interested', 'proceed', 'book', 'yep', 'ok', 'okay']
    confirm_no = ['no', 'nah', 'not interested', 'maybe later', 'not sure', 
                  'let me think', 'not now', 'cancel', 'nope']
    
    # Handle confirmation stage
    if data['destination'] and data['travelers'] and data['confirmed'] is None:
        add_log("Checking for confirmation response...")
        
        if any(word in user_input_lower for word in confirm_yes):
            data['confirmed'] = True
            st.session_state.conversation_ended = True
            st.session_state.conversation_active = False
            add_log("✅ User confirmed - ending conversation")
            
            return f"""Fantastic! I'm so excited for your trip to {data['destination']}! 
            
Our travel expert will contact you within 24 hours with a personalized package including the best hotels, activities, and experiences tailored just for you!

Thank you so much for choosing our travel agency. Have a wonderful day and get ready for an amazing adventure!"""
        
        elif any(word in user_input_lower for word in confirm_no):
            data['confirmed'] = False
            st.session_state.conversation_ended = True
            st.session_state.conversation_active = False
            add_log("❌ User declined - ending conversation")
            
            return """No problem at all! I completely understand. Travel is a big decision. 

If you'd like to explore different options or have any questions in the future, feel free to reach out anytime. We're always here to help!

Thank you for your time, and have a great day!"""
    
    # Build conversation based on missing info
    if not data['destination']:
        add_log("Asking for destination")
        return """So, where would you love to travel? You can tell me any destination like Paris, Bali, Tokyo, New York, or anywhere else you're dreaming about!"""
    
    elif not data['travelers']:
        add_log("Asking for number of travelers")
        return f"""Great choice! {data['destination']} is absolutely stunning! Now, who's going on this adventure with you? Is it just you traveling solo, or are you going with your partner, family, or friends?"""
    
    elif not data['budget']:
        travelers_info = data['travelers']
        add_log("Asking for budget")
        return f"""Perfect! So {travelers_info} will be exploring {data['destination']}. That sounds amazing!

Now let's talk about your budget. Are you looking for a luxury experience, a comfortable mid-range trip, or a budget-friendly adventure?"""
    
    elif data['destination'] and data['travelers'] and data['budget'] and data['confirmed'] is None:
        add_log("All info collected - asking for confirmation")
        
        return f"""Wonderful! Let me confirm everything:

You want to visit {data['destination']} with {data['travelers']}, and you're looking for a {data['budget']} experience.

This is going to be an incredible trip! I can create a customized package with handpicked accommodations, amazing activities, and local insider tips.

So, are you ready to move forward? Should I have our travel expert prepare your personalized vacation package? Just say yes or no."""
    
    add_log("Generating general response")
    return """Tell me more about what you're thinking! I'm here to help create your perfect vacation."""

# Custom CSS
st.markdown("""
<style>
    .user-message {
        background-color: #e3f2fd;
        padding: 15px;
        border-radius: 15px;
        margin: 10px 0;
        border-left: 5px solid #2196F3;
    }
    .assistant-message {
        background-color: #f1f8e9;
        padding: 15px;
        border-radius: 15px;
        margin: 10px 0;
        border-left: 5px solid #8BC34A;
    }
    .log-container {
        background-color: #f5f5f5;
        padding: 10px;
        border-radius: 5px;
        max-height: 300px;
        overflow-y: auto;
        font-family: monospace;
        font-size: 12px;
    }
    .stButton button {
        font-size: 18px;
        font-weight: bold;
        padding: 15px;
    }
</style>
""", unsafe_allow_html=True)

# Header
col1, col2 = st.columns([3, 1])
with col1:
    st.title("🌍 Travel Agency Voice Assistant")
    st.markdown("### *Auto-Continuous Voice Conversation*")

with col2:
    if st.button("🗑️ Clear Logs"):
        st.session_state.logs = []
        st.rerun()

st.markdown("---")

# Main layout
main_col, log_col = st.columns([2, 1])

with main_col:
    # Collected info display
    with st.expander("📊 Collected Information", expanded=True):
        data = st.session_state.user_data
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if data['destination']:
                st.success(f"🎯 {data['destination']}")
            else:
                st.info("🎯 No destination")
        
        with col2:
            if data['travelers']:
                st.success(f"👥 {data['travelers']}")
            else:
                st.info("👥 No travelers")
        
        with col3:
            if data['budget']:
                st.success(f"💰 {data['budget']}")
            else:
                st.info("💰 No budget")
        
        if data['confirmed'] is not None:
            if data['confirmed']:
                st.success("✅ Booking Confirmed!")
            else:
                st.warning("❌ User Declined")
    
    # Chat display
    st.subheader("💬 Conversation")
    
    chat_container = st.container()
    with chat_container:
        for message in st.session_state.messages:
            if message["role"] == "user":
                st.markdown(f'<div class="user-message"><strong>👤 You:</strong><br>{message["content"]}</div>', 
                           unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="assistant-message"><strong>🤖 Assistant:</strong><br>{message["content"]}</div>', 
                           unsafe_allow_html=True)
    
    st.markdown("---")
    
    # Control section
    if not st.session_state.conversation_active and not st.session_state.conversation_ended:
        st.info("👋 Welcome! Click START to begin your travel planning journey!")
        
        if st.button("🎙️ START CONVERSATION", type="primary", use_container_width=True):
            add_log("🚀 Starting conversation...")
            st.session_state.conversation_active = True
            
            # Initial greeting
            if not st.session_state.greeted:
                greeting = """Hello! Welcome to our travel agency! I'm your personal travel assistant, and I'm so excited to help you plan an unforgettable trip today!

Let's have a chat about your dream vacation. I'll ask you a few questions to understand what you're looking for, and then we'll see if we can create the perfect travel package for you."""
                
                st.session_state.messages.append({"role": "assistant", "content": greeting})
                add_log("Greeting added to messages")
                
                # Speak greeting
                speak(greeting)
                st.session_state.greeted = True
                st.session_state.waiting_for_input = True
            
            st.rerun()
    
    elif st.session_state.conversation_active and not st.session_state.conversation_ended:
        
        # Auto-listen section
        if st.session_state.waiting_for_input:
            st.warning("🎤 **AUTO-LISTENING IN PROGRESS...**")
            
            # Automatically listen
            user_input = listen()
            
            if user_input and user_input != "unclear":
                # Display user message
                st.session_state.messages.append({"role": "user", "content": user_input})
                add_log(f"User said: '{user_input}'")
                
                # Generate response
                response = get_response(user_input)
                st.session_state.messages.append({"role": "assistant", "content": response})
                add_log(f"Assistant responding...")
                
                # Speak response
                speak(response)
                
                # Check if conversation should end
                if not st.session_state.conversation_ended:
                    st.session_state.waiting_for_input = True
                else:
                    st.session_state.waiting_for_input = False
                
                st.rerun()
            
            elif user_input == "unclear":
                prompt = "Sorry, I couldn't hear you clearly. Could you please repeat that?"
                speak(prompt)
                st.session_state.messages.append({"role": "assistant", "content": prompt})
                st.session_state.waiting_for_input = True
                st.rerun()
            
            else:
                prompt = "I didn't hear anything. Are you still there? Please speak again."
                speak(prompt)
                st.session_state.messages.append({"role": "assistant", "content": prompt})
                st.session_state.waiting_for_input = True
                st.rerun()
        
        # Stop button
        if st.button("⏹️ STOP CONVERSATION", type="secondary", use_container_width=True):
            add_log("⏹️ User stopped conversation")
            st.session_state.conversation_active = False
            st.session_state.conversation_ended = True
            st.session_state.waiting_for_input = False
            st.rerun()
    
    elif st.session_state.conversation_ended:
        st.success("✅ **Conversation Ended**")
        
        if st.button("🔄 Start New Conversation", use_container_width=True):
            add_log("🔄 Resetting conversation...")
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
            st.rerun()

with log_col:
    st.subheader("📋 System Logs")
    
    log_text = "\n".join(st.session_state.logs[-30:]) if st.session_state.logs else "No logs yet..."
    
    st.markdown(f'<div class="log-container">{log_text}</div>', unsafe_allow_html=True)
    
    st.markdown("---")
    st.markdown("""
    ### 📝 How It Works:
    1. Click **START CONVERSATION**
    2. Greeting plays automatically
    3. **Listens automatically** after each response
    4. Speak naturally when you see "🎤 AUTO-LISTENING"
    5. Continues until you confirm yes/no
    
    **✨ No button pressing needed!**
    
    **Status:**
    - ✅ Success
    - 🎤 Listening
    - ❌ Errors
    - 💬 Speaking
    """)