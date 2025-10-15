from flask import Flask, request, jsonify
import requests
import os 
import logging
import json
from datetime import datetime
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_FROM_NUMBER = os.getenv('TWILIO_FROM_NUMBER')
TO_NUMBER = os.getenv('TO_NUMBER')
TO_NUMBERS_ALL = os.getenv('TO_NUMBERS_ALL')  # Comma-separated list of phone numbers

# Track last POST time for dead man's switch
last_post_time = time.time()

# ----------------------
# Internal helper utils
# ----------------------
def find_first_firing_alert(alerts_json):
    """Return the first alert with status == 'firing', or None if not found."""
    for alert in alerts_json.get('alerts', []):
        if alert.get('status') == 'firing':
            return alert
    return None


def build_twiml_from_alert(alert):
    alertname = alert.get('labels', {}).get('alertname', 'Unknown Alert')
    return alertname, f'<Response><Say>Alert triggered: {alertname}</Say></Response>'


def initiate_twilio_call(to_number, twiml_response, logger_context=None):
    url = f'https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json'
    auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    payload = {
        'From': TWILIO_FROM_NUMBER,
        'To': to_number,
        'Twiml': twiml_response
    }

    if logger_context is None:
        logger_context = {}

    logger.info('Initiating Twilio call', extra={
        **logger_context,
        'from': TWILIO_FROM_NUMBER,
        'to': to_number
    })

    response = requests.post(url, auth=auth, data=payload)
    return response

@app.route('/dms', methods=['GET', 'POST'])
def deadmansswitch():
    global last_post_time
    
    if request.method == 'POST':
        last_post_time = time.time()
        logger.info('Dead man switch ping received', extra={
            'timestamp': datetime.fromtimestamp(last_post_time).isoformat()
        })
        return jsonify({'message': 'Ping received'}), 200
    
    # For GET requests, check if we're within the 2-minute window
    current_time = time.time()
    time_since_last_ping = current_time - last_post_time
    
    if time_since_last_ping > 120:  # 2 minutes = 120 seconds
        logger.error('Dead man switch triggered - no ping received for over 2 minutes', extra={
            'seconds_since_last_ping': time_since_last_ping,
            'last_ping_time': datetime.fromtimestamp(last_post_time).isoformat()
        })
        return jsonify({
            'status': 'error',
            'message': 'No ping received for over 2 minutes',
            'seconds_since_last_ping': time_since_last_ping
        }), 500
    
    logger.info('Dead man switch check passed', extra={
        'seconds_since_last_ping': time_since_last_ping,
        'last_ping_time': datetime.fromtimestamp(last_post_time).isoformat()
    })
    return jsonify({
        'status': 'ok',
        'seconds_since_last_ping': time_since_last_ping
    }), 200

@app.route('/twilio-call', methods=['POST'])
def twilio_call():
    try:
        data = request.json

        firing_alert = find_first_firing_alert(data)
        if not firing_alert:
            logger.info('No firing alerts found, skipping call', extra={
                'request_data': json.dumps(data)
            })
            return jsonify({'message': 'No firing alerts, skipping call'}), 200
        
        alertname, twiml_response = build_twiml_from_alert(firing_alert)

        logger.info('Received firing alert notification', extra={
            'alertname': alertname,
            'request_data': json.dumps(data)
        })

        response = initiate_twilio_call(TO_NUMBER, twiml_response, logger_context={'alertname': alertname})

        if response.status_code == 201:
            logger.info('Call initiated successfully', extra={
                'status_code': response.status_code,
                'response': response.json()
            })
            return jsonify({'message': 'Call initiated successfully'}), 200
        else:
            logger.error('Failed to initiate call', extra={
                'status_code': response.status_code,
                'error': response.text
            })
            return jsonify({'error': 'Failed to initiate call', 'details': response.text}), 500
    except Exception as e:
        logger.error('Unexpected error in twilio_call', extra={
            'error': str(e),
            'traceback': logging.traceback.format_exc()
        })
        return jsonify({'error': 'Internal server error', 'details': str(e)}), 500

@app.route('/twilio-call-all', methods=['POST'])
def twilio_call_all():
    try:
        if not TO_NUMBERS_ALL:
            logger.error('TO_NUMBERS_ALL env var not set')
            return jsonify({'error': 'TO_NUMBERS_ALL env var not set'}), 400

        data = request.json

        firing_alert = find_first_firing_alert(data)

        if not firing_alert:
            logger.info('No firing alerts found, skipping calls to all numbers', extra={
                'request_data': json.dumps(data)
            })
            return jsonify({'message': 'No firing alerts, skipping calls'}), 200

        alertname, twiml_response = build_twiml_from_alert(firing_alert)

        logger.info('Received firing alert notification for all numbers', extra={
            'alertname': alertname,
            'request_data': json.dumps(data)
        })

        to_numbers = [n.strip() for n in TO_NUMBERS_ALL.split(',') if n.strip()]
        results = []

        for to_number in to_numbers:
            response = initiate_twilio_call(to_number, twiml_response, logger_context={'alertname': alertname})

            if response.status_code == 201:
                logger.info('Call initiated successfully', extra={
                    'to': to_number,
                    'status_code': response.status_code,
                    'response': response.json()
                })
                results.append({'to': to_number, 'status': 'success'})
            else:
                logger.error('Failed to initiate call', extra={
                    'to': to_number,
                    'status_code': response.status_code,
                    'error': response.text
                })
                results.append({'to': to_number, 'status': 'failed', 'details': response.text})

        successes = sum(1 for r in results if r['status'] == 'success')
        failures = len(results) - successes

        return jsonify({
            'message': 'Calls processed',
            'successes': successes,
            'failures': failures,
            'results': results
        }), 200 if failures == 0 else 207
    except Exception as e:
        logger.error('Unexpected error in twilio_call_all', extra={
            'error': str(e)
        })
        return jsonify({'error': 'Internal server error', 'details': str(e)}), 500

@app.before_request
def log_request_info():
    logger.info('Incoming request', extra={
        'method': request.method,
        'path': request.path,
        'headers': dict(request.headers),
        'body': request.get_data().decode('utf-8') if request.is_json else None
    })

@app.after_request
def log_response_info(response):
    logger.info('Outgoing response', extra={
        'status_code': response.status_code,
        'headers': dict(response.headers)
    })
    return response

if __name__ == '__main__':
    logger.info('Starting Twilio adapter server', extra={
        'port': 5000,
        'environment': 'development' if app.debug else 'production'
    })
    app.run(host='0.0.0.0', port=5000, debug=True)

