from shared.judging import build_testcases, get_testcase_count, normalize_judge_output


def build_hidden_testcases(testcase_input, expected_output):
    return build_testcases(testcase_input, expected_output)


def build_visible_testcases(testcase_input, expected_output):
    return build_testcases(testcase_input, expected_output)
def get_hidden_testcase_count(testcase_input, expected_output):
    return get_testcase_count(testcase_input, expected_output)


def get_visible_testcase_count(testcase_input, expected_output):
    return get_testcase_count(testcase_input, expected_output)
