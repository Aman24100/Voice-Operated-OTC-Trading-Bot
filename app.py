import os  # For accessing environment variables
import re  # Regular expression operations for text processing
import uuid  # Generating unique identifiers for sessions
import logging  # Application logging
import threading  # Thread-based parallelism for state management
import time  # Time-related functions
from datetime import datetime, timezone  # Date/time handling
from flask import Flask, request, jsonify, render_template  # Web framework components
import ccxt  # Cryptocurrency exchange trading library
from word2number import w2n  # type: ignore # Convert number words to numeric values
from dotenv import load_dotenv  # Environment variable loader

# Load environment variables from .env file
load_dotenv()

# Initialize Flask application
app = Flask(__name__)

# Configure logging settings
logging.basicConfig(
    level=logging.INFO,  # Set logging level to INFO
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',  # Log message format
    handlers=[logging.StreamHandler(), logging.FileHandler('voice_trading.log')]  # Output handlers
)
logger = logging.getLogger(__name__)  # Create logger instance

# Global state management
conversation_state = {}  # Dictionary to store conversation states keyed by call ID
state_lock = threading.Lock()  # Thread lock for safe concurrent access to conversation_state

# Keywords that indicate user is correcting previous input
correction_keywords = ["i meant", "actually", "instead", "change to", "correct to", "no,", "not", "mistake", "wrong"]

# Mapping of exchange IDs to display names
exchange_display_names = {
    'okx': 'OKX',
    'bybit': 'Bybit',
    'deribit': 'Deribit',
    'binance': 'Binance'
}

# Common mispronunciations and their corrections
mispronunciations = {
    'e t h': 'ETH',
    'b t c': 'BTC',
    'u s d t': 'USDT',
    'u s d': 'USD',
    'u s DTPL': 'USDT',
    'bit coin': 'BTC',
    'ether': 'ETH'
}

# Helper Functions ------------------------------------------------------------

def extract_exchange(transcript):
    """
    Extract exchange name from voice transcript.
    Handles variations in how exchanges might be pronounced.
    Returns exchange ID (e.g., 'binance') or None if not found.
    """
    transcript_l = transcript.lower()  # Convert to lowercase for case-insensitive matching
    # Map exchange IDs to their possible spoken variations
    exchanges = {
        'okx': ['okx', 'okay ex', 'okay x', 'o k x', 'ok ex', 'ok x'],
        'bybit': ['bybit', 'buy bit', 'by bit'],
        'deribit': ['deribit', 'dairy bit', 'deri bit'],
        'binance': ['binance', 'finance', 'by nance']
    }
    # Check each exchange variant
    for exc, variants in exchanges.items():
        for variant in variants:
            if variant in transcript_l:
                return exc
    return None

def extract_trading_pair(transcript):
    """
    Extract trading pair from transcript (e.g., BTC/USDT).
    Handles different separators (slash, dash, space) and mispronunciations.
    Returns normalized pair string or None if not found.
    """
    logger.info(f"Processing transcript for trading pair: '{transcript}'")
    
    # Convert to uppercase for consistent processing
    transcript = transcript.upper()
    
    # Replace spoken separators with actual symbols
    transcript = re.sub(r'\bSLASH\b', '/', transcript)
    transcript = re.sub(r'\bDASH\b', '-', transcript)
    transcript = re.sub(r'\bHYPHEN\b', '-', transcript)
    
    # Correct common mispronunciations
    for mis, correct in mispronunciations.items():
        transcript = transcript.replace(mis.upper(), correct)
    
    # Define patterns for different trading pair formats
    patterns = [
        r'\b([A-Z0-9]{2,6})\s*[/\- ]\s*([A-Z0-9]{2,6})\b',  # Standard format with separators
        r'trade\s+([A-Z0-9]{2,6})\s+with\s+([A-Z0-9]{2,6})',  # "Trade BTC with USDT"
        r'trade\s+([A-Z0-9]{2,6})\s+against\s+([A-Z0-9]{2,6})',  # "Trade BTC against USDT"
        r'([A-Z0-9]{2,6})\s+to\s+([A-Z0-9]{2,6})',  # "BTC to USDT"
        r'([A-Z0-9]{2,6})\s+versus\s+([A-Z0-9]{2,6})'  # "BTC versus USDT"
    ]
    
    # Check each pattern against transcript
    for pattern in patterns:
        match = re.search(pattern, transcript)
        if match:
            base = match.group(1)
            quote = match.group(2)
            result = f"{base}/{quote}"
            logger.info(f"Extracted trading pair: {result}")
            return result
    
    logger.info("No trading pair extracted")
    return None

def extract_number(transcript):
    """
    Extract numerical value from transcript.
    Supports both digits (e.g., "100") and words (e.g., "one hundred").
    Returns float or None if no number found.
    """
    # Remove currency symbols that might interfere with parsing
    clean_str = re.sub(r'[,$€£¥]', '', transcript)
    # Try to find numeric pattern
    match = re.search(r'\d+(?:\.\d+)?', clean_str)
    if match:
        return float(match.group(0))
    try:
        # Convert word phrases to numbers
        return w2n.word_to_num(clean_str)
    except ValueError:
        return None

def get_current_price(exchange_name, symbol, retries=3, delay=1):
    """
    Fetch current market price from exchange API.
    Implements retry logic with exponential backoff for reliability.
    Returns tuple (price, error_message)
    """
    # Normalize symbol for exchange API requirements
    normalized_symbol = normalize_symbol(exchange_name, symbol)
    # Retry loop for handling temporary failures
    for attempt in range(retries):
        try:
            # Dynamically get exchange class from ccxt
            exchange_class = getattr(ccxt, exchange_name)
            # Create exchange instance with rate limiting enabled
            exchange = exchange_class({'enableRateLimit': True})
            # Fetch ticker data
            ticker = exchange.fetch_ticker(normalized_symbol)
            return float(ticker['last']), None
        except Exception as e:
            logger.error(f"Error fetching price for {exchange_name} {normalized_symbol}: {e}", exc_info=True)
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2  # Exponential backoff
            else:
                return None, "failed to fetch price"

def normalize_symbol(exchange_name, symbol):
    """Normalize trading pair symbol format for exchange API"""
    return symbol.replace('/', '') if symbol else symbol

def cleanup_old_states():
    """Remove expired conversation states to prevent memory leaks"""
    now = time.time()
    with state_lock:
        # Iterate through all conversation states
        for call_id, state in list(conversation_state.items()):
            # Remove ended sessions older than 5 minutes
            if state.get('ended') and now - state.get('created', now) > 300:
                logger.info(f"Cleaning up old call state: {call_id}")
                del conversation_state[call_id]

# Web Routes ------------------------------------------------------------------

@app.route('/')
def home():
    """Serve the main web interface"""
    return render_template('index.html')

@app.route('/start-call', methods=['POST'])
def start_call():
    """Initialize a new trading session"""
    try:
        # Generate unique session ID
        call_id = str(uuid.uuid4())
        initial_message = "Hello! Welcome to Voice Trading Assistant. Please choose an exchange: OKX, Bybit, Deribit, or Binance."
        
        # Initialize conversation state
        with state_lock:
            conversation_state[call_id] = {
                'exchange': None,        # Selected exchange
                'trading_pair': None,    # Trading pair (e.g., BTC/USDT)
                'quantity': None,        # Trade amount
                'price': None,           # Target price
                'messages': [{           # Conversation history
                    'sender': 'bot',
                    'text': initial_message,
                    'timestamp': datetime.now(timezone.utc).isoformat()
                }],
                'ended': False,          # Session completion flag
                'created': time.time(),  # Creation timestamp
                'current_step': 'exchange',  # Next required parameter
                'retry_count': 0         # Consecutive error count
            }
        logger.info(f"Started new session with call_id: {call_id}")
        return jsonify({'call_id': call_id, 'initial_message': initial_message})
    except Exception as e:
        logger.error(f"Start session failed: {e}", exc_info=True)
        return jsonify({'error': 'Session initiation failed', 'details': str(e)}), 500

@app.route('/webhook', methods=['POST'])
def webhook():
    """Main endpoint for processing voice transcripts and managing conversation flow"""
    data = request.get_json(silent=True) or {}
    call_id = data.get('call_id')
    transcript = data.get('transcript', '').strip()

    # Validate call ID
    if not call_id or call_id not in conversation_state:
        logger.warning(f"Invalid or missing call_id: {call_id}")
        return jsonify({'error': 'Invalid or missing call_id'}), 400

    with state_lock:
        state = conversation_state[call_id]
        # Check if session has ended
        if state['ended']:
            logger.info(f"Session already ended for call_id: {call_id}")
            return jsonify({'error': 'Session has ended'}), 400

        # Add user message to conversation history
        state['messages'].append({
            'sender': 'user',
            'text': transcript,
            'timestamp': data.get('timestamp', datetime.now(timezone.utc).isoformat())
        })

        transcript_lower = transcript.lower()
        # Detect if this is a correction
        is_correction = any(keyword in transcript_lower for keyword in correction_keywords)
        updated_slots = []  # Track which parameters were updated

        # Extract parameters from transcript
        exch = extract_exchange(transcript)
        symbol = extract_trading_pair(transcript)
        # Find all numbers in the transcript
        numbers = [float(n) for n in re.findall(r'\d+(?:\.\d+)?', transcript_lower)] or [extract_number(transcript)]
        # Determine quantity and price based on context
        qty = numbers[0] if numbers and 'at' not in transcript_lower else None
        price = numbers[-1] if numbers and 'at' in transcript_lower else (numbers[0] if numbers and state['quantity'] else None)

        # Log extracted values for debugging
        logger.info(f"Extracted from transcript '{transcript}': exchange={exch}, trading_pair={symbol}, qty={qty}, price={price}")

        # Handle parameter updates
        if is_correction:
            # Process corrections for any parameter
            if exch:
                state['exchange'] = exch
                updated_slots.append(f"exchange to {exchange_display_names.get(exch, exch)}")
            if symbol:
                state['trading_pair'] = symbol
                updated_slots.append(f"trading pair to {symbol}")
            if qty is not None:
                state['quantity'] = qty
                updated_slots.append(f"quantity to {qty}")
            if price is not None:
                state['price'] = price
                updated_slots.append(f"price to {price}")
        else:
            # Only update missing parameters for regular inputs
            if state['exchange'] is None and exch:
                state['exchange'] = exch
                updated_slots.append(f"exchange to {exchange_display_names.get(exch, exch)}")
            if state['trading_pair'] is None and symbol:
                state['trading_pair'] = symbol
                updated_slots.append(f"trading pair to {symbol}")
            if state['quantity'] is None and qty is not None:
                state['quantity'] = qty
                updated_slots.append(f"quantity to {qty}")
            if state['price'] is None and price is not None:
                state['price'] = price
                updated_slots.append(f"price to {price}")

        # Log updated state for debugging
        logger.info(f"Updated state: {state}")

        # Check if all required parameters are collected
        missing_slots = [s for s in ['exchange', 'trading_pair', 'quantity', 'price'] if state[s] is None]
        if not missing_slots:
            # All parameters collected - confirm order
            current_price, error = get_current_price(state['exchange'], state['trading_pair'])
            exchange_display = exchange_display_names.get(state['exchange'], state['exchange'].capitalize())
            # Format price message
            price_msg = f"Current market price is ${current_price:.2f}." if current_price else f"⚠️ Couldn't fetch price: {error}."
            
            # Construct confirmation message
            bot_response = (
                f"✅ Order confirmed: Trading {state['quantity']} {state['trading_pair']} "
                f"on {exchange_display} at ${state['price']:.2f}. {price_msg} Goodbye!"
            )
            # Mark session as completed
            state['ended'] = True
            state['retry_count'] = 0
        else:
            # Still missing parameters - prompt for next
            next_step = missing_slots[0]
            state['current_step'] = next_step
            # Increment retry counter if no updates
            if not updated_slots:
                state['retry_count'] += 1

            # Acknowledge updates if any
            ack = f"Got it, updated {' and '.join(updated_slots)}." if updated_slots else ""
            
            # Contextual prompts based on current step
            prompts = {
                'exchange': [
                    "Please choose an exchange: OKX, Bybit, Deribit, or Binance.",
                    "Which exchange? Say OKX, Bybit, Deribit, or Binance.",
                    "Exchange name, please (e.g., OKX)."
                ],
                'trading_pair': [
                    "What trading pair would you like? (e.g., BTC/USDT)",
                    "Please specify the trading pair, like ETH/USDT.",
                    "Trading pair? (e.g., BTC/USDT)"
                ],
                'quantity': [
                    f"How many units of {state['trading_pair']}?",
                    f"Number of {state['trading_pair']} units, please.",
                    f"Quantity for {state['trading_pair']}?"
                ],
                'price': [
                    "At what price? Just say the number.",
                    "Please say the price (e.g., 2000).",
                    "Price?"
                ]
            }
            # Select prompt based on retry count
            prompt_idx = min(state['retry_count'], 2)
            bot_response = f"{ack} {prompts[next_step][prompt_idx]}".strip() if ack or prompt_idx > 0 else "Sorry, I didn’t catch that. Please try again."

        # Add bot response to conversation history
        state['messages'].append({
            'sender': 'bot',
            'text': bot_response,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })

        logger.info(f"Bot response for call_id {call_id}: {bot_response}")
        return jsonify({'response': bot_response})

@app.route('/poll-messages/<call_id>', methods=['GET'])
def poll_messages(call_id):
    """Endpoint for clients to fetch conversation updates"""
    cleanup_old_states()  # Perform cleanup of old sessions
    with state_lock:
        if call_id not in conversation_state:
            logger.warning(f"Session not found for call_id: {call_id}")
            return jsonify({'error': 'Session not found', 'messages': [], 'ended': True}), 404
        state = conversation_state[call_id]
        return jsonify({'messages': state['messages'], 'ended': state['ended']})

@app.route('/end-call', methods=['POST'])
def end_call():
    """Manually terminate a trading session"""
    data = request.get_json(silent=True) or {}
    call_id = data.get('call_id')
    with state_lock:
        if call_id in conversation_state:
            conversation_state[call_id]['ended'] = True  # Mark session as ended
            logger.info(f"Ended session with call_id: {call_id}")
            return jsonify({'status': 'Session ended'})
        logger.warning(f"Session not found for call_id: {call_id}")
        return jsonify({'error': 'Session not found'}), 404

# Main Execution -------------------------------------------------------------
if __name__ == '__main__':
    # Get configuration from environment variables
    debug_mode = os.getenv('DEBUG', 'false').lower() == 'true'
    port = int(os.getenv('PORT', 5000))
    logger.info(f"Starting server on port {port} (debug={debug_mode})")
    # Start Flask application
    app.run(host='0.0.0.0', port=port, debug=debug_mode, threaded=True)