import os


EXECUTOR_FAMILY_CONFIG = {
    "core": {
        "topic_env": "KAFKA_SUBMISSIONS_TOPIC_CORE",
        "topic_default": "code_submissions_core",
        "group_env": "KAFKA_EXECUTOR_GROUP_CORE",
        "group_default": "judge_vortex_executor_core",
        "service_name": "executor-core",
        "build_target": "executor-core",
        "languages": (
            "python",
            "javascript",
            "ruby",
            "php",
            "cpp",
            "c",
            "go",
            "rust",
            "typescript",
            "sql",
        ),
    },
    "java": {
        "topic_env": "KAFKA_SUBMISSIONS_TOPIC_JAVA",
        "topic_default": "code_submissions_java",
        "group_env": "KAFKA_EXECUTOR_GROUP_JAVA",
        "group_default": "judge_vortex_executor_java",
        "service_name": "executor-java",
        "build_target": "executor-java",
        "languages": ("java",),
    },
    "swift": {
        "topic_env": "KAFKA_SUBMISSIONS_TOPIC_SWIFT",
        "topic_default": "code_submissions_swift",
        "group_env": "KAFKA_EXECUTOR_GROUP_SWIFT",
        "group_default": "judge_vortex_executor_swift",
        "service_name": "executor-swift",
        "build_target": "executor-swift",
        "languages": ("swift",),
    },
    "haskell": {
        "topic_env": "KAFKA_SUBMISSIONS_TOPIC_HASKELL",
        "topic_default": "code_submissions_haskell",
        "group_env": "KAFKA_EXECUTOR_GROUP_HASKELL",
        "group_default": "judge_vortex_executor_haskell",
        "service_name": "executor-haskell",
        "build_target": "executor-haskell",
        "languages": ("haskell",),
    },
    "csharp": {
        "topic_env": "KAFKA_SUBMISSIONS_TOPIC_CSHARP",
        "topic_default": "code_submissions_csharp",
        "group_env": "KAFKA_EXECUTOR_GROUP_CSHARP",
        "group_default": "judge_vortex_executor_csharp",
        "service_name": "executor-csharp",
        "build_target": "executor-csharp",
        "languages": ("csharp",),
    },
}


def get_executor_routes():
    routes = {}
    for family, config in EXECUTOR_FAMILY_CONFIG.items():
        routes[family] = {
            **config,
            "topic": os.getenv(config["topic_env"], config["topic_default"]),
            "consumer_group": os.getenv(config["group_env"], config["group_default"]),
        }
    return routes


def get_route_for_language(language):
    normalized_language = (language or "").strip().lower()
    for family, route in get_executor_routes().items():
        if normalized_language in route["languages"]:
            return {"family": family, **route}
    raise ValueError(f"Unsupported submission language: {language}")


def get_submission_topic(language):
    return get_route_for_language(language)["topic"]


def get_topic_consumer_groups():
    return tuple(
        (route["topic"], route["consumer_group"])
        for route in get_executor_routes().values()
    )


def get_all_submission_topics():
    return tuple(route["topic"] for route in get_executor_routes().values())


def get_supported_languages():
    languages = set()
    for route in get_executor_routes().values():
        languages.update(route["languages"])
    return tuple(sorted(languages))
