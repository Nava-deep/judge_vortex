import logging
import requests
from docker_manager import run_code_in_sandbox

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ✅ Update this port to match your running Django server
CORE_API_URL = "http://localhost:53562/api"

def update_submission(submission_id, status, output, time_ms):
    """Update Django with the raw output of the run."""
    url = f"{CORE_API_URL}/submissions/{submission_id}/update/"
    payload = {
        "status": status,
        "output": output,
        "execution_time_ms": time_ms
    }
    try:
        # Added a timeout so the worker doesn't freeze if Django goes down
        requests.patch(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Failed to update Django: {e}")

def grade_submission(submission_data):
    """Runs the code in Docker and returns the result to Django."""
    sub_id = submission_data['submission_id']
    code = submission_data['code']
    language = submission_data['language']
    user_input = submission_data.get('user_input', "") 
    time_limit = submission_data['time_limit_ms']

    logger.info(f"Running Submission {sub_id}...")

    # Run in the Docker Sandbox
    result = run_code_in_sandbox(code, language, user_input, time_limit)

    # Determine status based on the sandbox result
    if result['status'] == 'TLE':
        update_submission(sub_id, 'TLE', "Time Limit Exceeded", time_limit)
    elif result['status'] == 'RUNTIME_ERROR':
        update_submission(sub_id, 'RUNTIME_ERROR', result['output'], result['time_ms'])
    else:
        # ✅ Changed 'ACCEPTED' to 'SUCCESS' to match the frontend terminal color logic
        update_submission(sub_id, 'SUCCESS', result['output'], result['time_ms'])
        logger.info(f"Submission {sub_id} finished successfully.")