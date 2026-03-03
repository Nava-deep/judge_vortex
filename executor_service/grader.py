import logging
import requests
import asyncio
from sandbox import run_code_in_sandbox

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CORE_API_URL = "http://localhost:53562/api"

def _patch_django(url, payload):
    """A helper function to run the blocking network request."""
    try:
        requests.patch(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Failed to update Django: {e}")

async def update_submission(submission_id, status, output, time_ms):
    """Asynchronously pushes the result back to Django."""
    url = f"{CORE_API_URL}/submissions/{submission_id}/update/"
    payload = {"status": status, "output": output, "execution_time_ms": time_ms}
    # Don't block the main event loop while waiting for Django to reply
    await asyncio.to_thread(_patch_django, url, payload)

async def grade_submission(submission_data):
    """The main async grading pipeline."""
    sub_id = submission_data['submission_id']
    code = submission_data['code']
    language = submission_data['language']
    user_input = submission_data.get('user_input', "") 
    time_limit = submission_data['time_limit_ms']

    logger.info(f"Running Submission {sub_id} natively (Pure Async)...")

    # 🟢 Await the new async sandbox!
    result = await run_code_in_sandbox(code, language, user_input, time_limit)

    if result['status'] == 'TLE':
        await update_submission(sub_id, 'TLE', "Time Limit Exceeded", time_limit)
    elif result['status'] in ['RUNTIME_ERROR', 'COMPILATION_ERROR', 'SYSTEM_ERROR']:
        await update_submission(sub_id, result['status'], result['output'], result['time_ms'])
    else:
        await update_submission(sub_id, 'SUCCESS', result['output'], result['time_ms'])
        logger.info(f"Submission {sub_id} finished successfully.")