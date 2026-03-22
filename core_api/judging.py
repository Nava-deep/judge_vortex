import re


def normalize_judge_output(value):
    if value is None:
        return ""

    normalized = str(value).replace('\r\n', '\n').replace('\r', '\n').strip()
    return '\n'.join(line.rstrip() for line in normalized.split('\n'))


def split_hidden_case_blocks(value):
    normalized = str(value or '').replace('\r\n', '\n').replace('\r', '\n').strip('\n')
    if not normalized:
        return []

    return [
        block.strip('\n')
        for block in re.split(r'\n\s*\n', normalized)
        if block.strip()
    ]


def build_hidden_testcases(testcase_input, expected_output):
    input_blocks = split_hidden_case_blocks(testcase_input)
    output_blocks = [
        normalize_judge_output(block)
        for block in split_hidden_case_blocks(expected_output)
    ]

    if len(output_blocks) > 1 and len(input_blocks) == len(output_blocks):
        return [
            {
                'input': input_blocks[index],
                'expected_output': output_blocks[index],
            }
            for index in range(len(output_blocks))
        ]

    return [{
        'input': str(testcase_input or ''),
        'expected_output': normalize_judge_output(expected_output),
    }]


def get_hidden_testcase_count(testcase_input, expected_output):
    return len(build_hidden_testcases(testcase_input, expected_output))
