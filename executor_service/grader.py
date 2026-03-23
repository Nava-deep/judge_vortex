import logging
import os
import requests
import asyncio
from sandbox import run_code_in_sandbox, prepare_execution, execute_prepared

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CORE_API_URL = os.getenv("CORE_API_URL", "http://localhost:53562/api").rstrip("/")


def normalize_judge_output(value):
    if value is None:
        return ""

    normalized = str(value).replace('\r\n', '\n').replace('\r', '\n').strip()
    return '\n'.join(line.rstrip() for line in normalized.split('\n'))

def _patch_django(url, payload):
    """A helper function to run the blocking network request."""
    try:
        requests.patch(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Failed to update Django: {e}")

async def update_submission(submission_id, status, output, time_ms, passed_testcases=None, total_testcases=None):
    """Asynchronously pushes the result back to Django."""
    url = f"{CORE_API_URL}/submissions/{submission_id}/update/"
    payload = {"status": status, "output": output, "execution_time_ms": time_ms}
    if passed_testcases is not None:
        payload["passed_testcases"] = passed_testcases
    if total_testcases is not None:
        payload["total_testcases"] = total_testcases
    # Don't block the main event loop while waiting for Django to reply
    await asyncio.to_thread(_patch_django, url, payload)

async def grade_submission(submission_data):
    """The main async grading pipeline."""
    sub_id = submission_data['submission_id']
    code = submission_data['code']
    language = submission_data['language']
    user_input = submission_data.get('user_input', "") 
    time_limit = submission_data['time_limit_ms']
    judge_cases = submission_data.get('judge_cases') or []
    files = submission_data.get('files') or []
    entry_file = submission_data.get('entry_file') or None

    logger.info(f"Running submission {sub_id} through the executor pipeline...")

    if judge_cases:
        total_testcases = len(judge_cases)
        passed_testcases = 0
        total_time_ms = 0
        last_output = ""
        prepared = await prepare_execution(
            code,
            language,
            judge_cases[0].get('input', ''),
            files=files,
            entry_file=entry_file,
        )

        if isinstance(prepared, dict):
            await update_submission(
                sub_id,
                prepared['status'],
                prepared['output'],
                prepared.get('time_ms', 0),
                passed_testcases,
                total_testcases
            )
            return

        try:
            for case in judge_cases:
                result = await execute_prepared(prepared, case.get('input', ""), time_limit)
                total_time_ms += int(result.get('time_ms') or 0)
                last_output = result.get('output', '') or ''

                if result['status'] == 'TLE':
                    await update_submission(sub_id, 'TLE', "Time Limit Exceeded", total_time_ms, passed_testcases, total_testcases)
                    return
                if result['status'] in ['MEMORY_LIMIT_EXCEEDED', 'MLE']:
                    await update_submission(sub_id, 'MLE', result['output'], total_time_ms, passed_testcases, total_testcases)
                    return
                if result['status'] in ['RUNTIME_ERROR', 'COMPILATION_ERROR', 'SYSTEM_ERROR']:
                    await update_submission(sub_id, result['status'], result['output'], total_time_ms, passed_testcases, total_testcases)
                    return

                actual_output = normalize_judge_output(result['output'])
                expected_output = normalize_judge_output(case.get('expected_output', ''))
                if actual_output == expected_output:
                    passed_testcases += 1
                    continue

                await update_submission(sub_id, 'SUCCESS', result['output'], total_time_ms, passed_testcases, total_testcases)
                return

            await update_submission(sub_id, 'SUCCESS', last_output, total_time_ms, passed_testcases, total_testcases)
            logger.info(f"Submission {sub_id} finished successfully.")
            return
        finally:
            prepared.cleanup()

    # Await the new async sandbox!
    result = await run_code_in_sandbox(
        code,
        language,
        user_input,
        time_limit,
        files=files,
        entry_file=entry_file,
    )

    if result['status'] == 'TLE':
        await update_submission(sub_id, 'TLE', "Time Limit Exceeded", time_limit)
    elif result['status'] in ['MEMORY_LIMIT_EXCEEDED', 'MLE']:
        await update_submission(sub_id, 'MLE', result['output'], result['time_ms'])
    elif result['status'] in ['RUNTIME_ERROR', 'COMPILATION_ERROR', 'SYSTEM_ERROR']:
        await update_submission(sub_id, result['status'], result['output'], result['time_ms'])
    else:
        await update_submission(sub_id, 'SUCCESS', result['output'], result['time_ms'])
        logger.info(f"Submission {sub_id} finished successfully.")
