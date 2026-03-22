import asyncio
import os
import platform
import resource
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass

LANG_CONFIG = {
    "python": {"filename": "main.py", "compile_cmd": None, "run_cmd": ["python3", "main.py"]},
    "javascript": {"filename": "main.js", "compile_cmd": None, "run_cmd": ["node", "main.js"]},
    "ruby": {"filename": "main.rb", "compile_cmd": None, "run_cmd": ["ruby", "main.rb"]},
    "php": {"filename": "main.php", "compile_cmd": None, "run_cmd": ["php", "main.php"]},
    "cpp": {"filename": "main.cpp", "compile_cmd": ["g++", "-O3", "main.cpp", "-o", "out"], "run_cmd": ["./out"]},
    "c": {"filename": "main.c", "compile_cmd": ["gcc", "-O3", "main.c", "-o", "out"], "run_cmd": ["./out"]},
    "go": {"filename": "main.go", "compile_cmd": ["go", "build", "-o", "out", "main.go"], "run_cmd": ["./out"]},
    "rust": {"filename": "main.rs", "compile_cmd": ["rustc", "-O", "main.rs", "-o", "out"], "run_cmd": ["./out"]},
    "java": {
        "filename": "Main.java",
        "compile_cmd": [
            "javac",
            "-J-Xms8m",
            "-J-Xmx96m",
            "-J-XX:ReservedCodeCacheSize=32m",
            "-J-XX:CompressedClassSpaceSize=16m",
            "-J-XX:+UseSerialGC",
            "Main.java",
        ],
        "run_cmd": [
            "java",
            "-Xms8m",
            "-Xmx64m",
            "-XX:ReservedCodeCacheSize=32m",
            "-XX:CompressedClassSpaceSize=16m",
            "-XX:+UseSerialGC",
            "-cp",
            ".",
            "Main",
        ],
    },
    "typescript": {
        "filename": "main.ts",
        "compile_cmd": ["npx", "tsc", "main.ts", "--outFile", "solution.js", "--target", "es6"],
        "run_cmd": ["node", "solution.js"],
    },
    "sql": {"filename": "query.sql", "compile_cmd": None, "run_cmd": ["sqlite3", ":memory:", "-batch", "-init", "setup.sql", ".read query.sql"]},
}

MAX_MEMORY_BYTES = int(os.getenv("EXECUTOR_MEMORY_BYTES", str(256 * 1024 * 1024)))
COMPILE_MEMORY_BYTES = int(os.getenv("EXECUTOR_COMPILE_MEMORY_BYTES", str(512 * 1024 * 1024)))
MAX_OUTPUT_KB = int(os.getenv("EXECUTOR_MAX_OUTPUT_KB", "1024"))
MAX_MEMORY_KB = max(1024, MAX_MEMORY_BYTES // 1024)
COMPILE_MEMORY_KB = max(1024, COMPILE_MEMORY_BYTES // 1024)
MAX_OUTPUT_BYTES = max(1024, MAX_OUTPUT_KB * 1024)
EXECUTOR_BACKEND = os.getenv("EXECUTOR_BACKEND", "native").strip().lower()
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"
ISOLATE_BINARY = shutil.which("isolate")
ISOLATE_AVAILABLE = IS_LINUX and ISOLATE_BINARY is not None
ISOLATE_BOXES = max(1, int(os.getenv("ISOLATE_BOXES", "64")))
ISOLATE_PATH = os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
DEFAULT_ISOLATE_ENV = {
    "PATH": ISOLATE_PATH,
    "HOME": "/box",
    "XDG_CACHE_HOME": "/box/.cache",
    "TMPDIR": "/box/tmp",
}
COMMON_ISOLATE_DIR_RULES = ["/etc/alternatives=/etc/alternatives:maybe"]


def _detect_java_home():
    javac_path = shutil.which("javac")
    if not javac_path:
        return None
    return os.path.dirname(os.path.dirname(os.path.realpath(javac_path)))


JAVA_HOME = _detect_java_home()
LANGUAGE_ISOLATE_CONFIG = {
    "go": {
        "env": {
            "GOCACHE": "/box/.cache/go-build",
        }
    },
    "java": {
        "dirs": [
            "/etc/java-17-openjdk=/etc/java-17-openjdk:maybe",
        ],
        "env": {
            "JAVA_HOME": JAVA_HOME or "",
        },
    },
}
NATIVE_FALLBACK_LANGUAGES = set()

_BOX_ID_QUEUE = asyncio.Queue()
for _box_id in range(ISOLATE_BOXES):
    _BOX_ID_QUEUE.put_nowait(_box_id)


@dataclass
class PreparedExecution:
    backend: str
    language: str
    run_cmd: list
    work_dir: str
    temp_dir_ctx: tempfile.TemporaryDirectory | None = None
    box_id: int | None = None

    def cleanup(self):
        if self.backend == "isolate" and self.box_id is not None and ISOLATE_BINARY:
            try:
                subprocess.run(
                    [ISOLATE_BINARY, f"--box-id={self.box_id}", "--cg", "--cleanup"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            finally:
                try:
                    _BOX_ID_QUEUE.put_nowait(self.box_id)
                except asyncio.QueueFull:
                    pass

        if self.temp_dir_ctx is not None:
            self.temp_dir_ctx.cleanup()


def set_limits():
    """Apply process limits for native fallback execution."""
    try:
        if not IS_MAC:
            resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
        else:
            resource.setrlimit(resource.RLIMIT_DATA, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))

        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass


def _unsupported_language(language):
    return {"status": "SYSTEM_ERROR", "output": f"Language {language} not supported.", "time_ms": 0}


def _use_isolate_backend():
    return EXECUTOR_BACKEND == "isolate" and ISOLATE_AVAILABLE


def _should_use_native_fallback(language):
    return language in NATIVE_FALLBACK_LANGUAGES


def _truncate_output(value):
    if len(value) <= MAX_OUTPUT_BYTES:
        return value
    return value[:MAX_OUTPUT_BYTES].rstrip() + "\n[output truncated]"


def _decode_output(stdout, stderr):
    out = _truncate_output(stdout.decode(errors="replace").strip())
    err = _truncate_output(stderr.decode(errors="replace").strip())
    return out, err


def _parse_isolate_meta(meta_path):
    data = {}
    if not os.path.exists(meta_path):
        return data

    with open(meta_path, "r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()
    return data


def _meta_time_ms(meta, fallback_ms=0):
    for key in ("time", "time-wall"):
        value = meta.get(key)
        if not value:
            continue
        try:
            return int(float(value) * 1000)
        except (TypeError, ValueError):
            continue
    return fallback_ms


def _shell_command(parts):
    return shlex.join([str(part) for part in parts])


def _validate_runtime_tools(language):
    config = LANG_CONFIG[language]
    filename = config["filename"]
    compile_cmd = config["compile_cmd"]
    run_cmd = config["run_cmd"]
    del filename

    tools = []
    if compile_cmd:
        tools.append(compile_cmd[0])
    if run_cmd:
        tools.append(run_cmd[0])

    missing = []
    for tool in tools:
        if tool.startswith(".") or tool.startswith("/"):
            continue
        if shutil.which(tool) is None:
            missing.append(tool)

    if missing:
        return {
            "status": "SYSTEM_ERROR",
            "output": "Missing runtime tooling: " + ", ".join(sorted(set(missing))),
            "time_ms": 0,
        }

    return None


def _resolve_command(parts):
    if not parts:
        return parts

    tool = parts[0]
    if tool.startswith(".") or tool.startswith("/"):
        if os.path.isabs(tool) and os.path.exists(tool):
            return [os.path.realpath(tool), *parts[1:]]
        return parts

    resolved = shutil.which(tool)
    if not resolved:
        return parts
    return [os.path.realpath(resolved), *parts[1:]]


def _get_isolate_dirs(language):
    language_dirs = LANGUAGE_ISOLATE_CONFIG.get(language, {}).get("dirs", [])
    return [*COMMON_ISOLATE_DIR_RULES, *language_dirs]


def _get_isolate_env(language):
    language_env = LANGUAGE_ISOLATE_CONFIG.get(language, {}).get("env", {})
    return {**DEFAULT_ISOLATE_ENV, **language_env}


async def _run_isolate(box_id, command_parts, time_limit_sec, wall_time_sec, process_limit, input_bytes=None, memory_kb=None, dir_rules=None, env_vars=None):
    with tempfile.NamedTemporaryFile(prefix="isolate-meta-", suffix=".txt", delete=False) as meta_handle:
        meta_path = meta_handle.name

    resolved_command = _resolve_command(command_parts)
    args = [
        ISOLATE_BINARY,
        f"--box-id={box_id}",
        "--cg",
        f"--meta={meta_path}",
        f"--time={time_limit_sec}",
        f"--wall-time={wall_time_sec}",
        "--extra-time=0.25",
        f"--cg-mem={memory_kb or MAX_MEMORY_KB}",
        f"--processes={process_limit}",
        "--open-files=64",
        "--chdir=/box",
        "--run",
        "--",
        "/bin/sh",
        "-lc",
        _shell_command(resolved_command),
    ]

    for rule in dir_rules or []:
        args.insert(-5, f"--dir={rule}")

    merged_env = DEFAULT_ISOLATE_ENV if env_vars is None else env_vars
    for name, value in merged_env.items():
        args.insert(-5, f"--env={name}={value}")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    timeout = max(wall_time_sec + 5, 10)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=input_bytes), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        stdout, stderr = await proc.communicate()
        raise

    meta = _parse_isolate_meta(meta_path)

    try:
        os.remove(meta_path)
    except FileNotFoundError:
        pass

    return proc.returncode, meta, stdout, stderr


async def _prepare_native_execution(code, language, input_data=""):
    if language not in LANG_CONFIG:
        return _unsupported_language(language)

    missing_runtime = _validate_runtime_tools(language)
    if missing_runtime is not None:
        return missing_runtime

    config = LANG_CONFIG[language]
    filename = config["filename"]
    compile_cmd = _resolve_command(config["compile_cmd"]) if config["compile_cmd"] else None
    run_cmd = _resolve_command(config["run_cmd"]) if config["run_cmd"] else None
    temp_dir_ctx = tempfile.TemporaryDirectory()
    temp_dir = temp_dir_ctx.name

    try:
        source_path = os.path.join(temp_dir, filename)
        with open(source_path, "w", encoding="utf-8") as handle:
            handle.write(code)

        if language == "sql":
            setup_path = os.path.join(temp_dir, "setup.sql")
            with open(setup_path, "w", encoding="utf-8") as handle:
                handle.write(input_data if input_data else "")

        if compile_cmd:
            try:
                comp_process = await asyncio.create_subprocess_exec(
                    *compile_cmd,
                    cwd=temp_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(comp_process.communicate(), timeout=20.0)
                if comp_process.returncode != 0:
                    temp_dir_ctx.cleanup()
                    return {
                        "status": "COMPILATION_ERROR",
                        "output": stderr.decode(errors="replace").strip(),
                        "time_ms": 0,
                    }
            except asyncio.TimeoutError:
                temp_dir_ctx.cleanup()
                return {"status": "COMPILATION_ERROR", "output": "Compilation timed out.", "time_ms": 0}

        return PreparedExecution(
            backend="native",
            language=language,
            run_cmd=run_cmd,
            work_dir=temp_dir,
            temp_dir_ctx=temp_dir_ctx,
        )
    except Exception as exc:
        temp_dir_ctx.cleanup()
        return {"status": "SYSTEM_ERROR", "output": str(exc), "time_ms": 0}


async def _prepare_isolate_execution(code, language, input_data=""):
    if language not in LANG_CONFIG:
        return _unsupported_language(language)

    missing_runtime = _validate_runtime_tools(language)
    if missing_runtime is not None:
        return missing_runtime

    config = LANG_CONFIG[language]
    filename = config["filename"]
    compile_cmd = _resolve_command(config["compile_cmd"]) if config["compile_cmd"] else None
    run_cmd = _resolve_command(config["run_cmd"]) if config["run_cmd"] else None
    isolate_dirs = _get_isolate_dirs(language)
    isolate_env = _get_isolate_env(language)
    box_id = await _BOX_ID_QUEUE.get()

    try:
        subprocess.run(
            [ISOLATE_BINARY, f"--box-id={box_id}", "--cg", "--cleanup"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        init_proc = await asyncio.create_subprocess_exec(
            ISOLATE_BINARY,
            f"--box-id={box_id}",
            "--cg",
            "--init",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(init_proc.communicate(), timeout=10.0)
        if init_proc.returncode != 0:
            _BOX_ID_QUEUE.put_nowait(box_id)
            return {
                "status": "SYSTEM_ERROR",
                "output": stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip() or "Failed to initialize isolate.",
                "time_ms": 0,
            }

        box_root = stdout.decode(errors="replace").strip() or f"/var/local/lib/isolate/{box_id}"
        work_dir = os.path.join(box_root, "box")
        os.makedirs(os.path.join(work_dir, ".cache", "go-build"), exist_ok=True)
        os.makedirs(os.path.join(work_dir, "tmp"), exist_ok=True)
        source_path = os.path.join(work_dir, filename)
        with open(source_path, "w", encoding="utf-8") as handle:
            handle.write(code)

        if language == "sql":
            setup_path = os.path.join(work_dir, "setup.sql")
            with open(setup_path, "w", encoding="utf-8") as handle:
                handle.write(input_data if input_data else "")

        if compile_cmd:
            returncode, meta, comp_stdout, comp_stderr = await _run_isolate(
                box_id=box_id,
                command_parts=compile_cmd,
                time_limit_sec=20,
                wall_time_sec=25,
                process_limit=128,
                memory_kb=COMPILE_MEMORY_KB,
                dir_rules=isolate_dirs,
                env_vars=isolate_env,
            )
            out, err = _decode_output(comp_stdout, comp_stderr)
            status_code = meta.get("status")

            if status_code in {"TO", "WT"}:
                subprocess.run([ISOLATE_BINARY, f"--box-id={box_id}", "--cg", "--cleanup"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                _BOX_ID_QUEUE.put_nowait(box_id)
                return {"status": "COMPILATION_ERROR", "output": "Compilation timed out.", "time_ms": _meta_time_ms(meta)}

            if returncode != 0:
                subprocess.run([ISOLATE_BINARY, f"--box-id={box_id}", "--cg", "--cleanup"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                _BOX_ID_QUEUE.put_nowait(box_id)
                return {
                    "status": "COMPILATION_ERROR",
                    "output": err or out or "Compilation failed.",
                    "time_ms": _meta_time_ms(meta),
                }

        return PreparedExecution(
            backend="isolate",
            language=language,
            run_cmd=run_cmd,
            work_dir=work_dir,
            box_id=box_id,
        )
    except Exception as exc:
        try:
            subprocess.run([ISOLATE_BINARY, f"--box-id={box_id}", "--cg", "--cleanup"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        finally:
            _BOX_ID_QUEUE.put_nowait(box_id)
        return {"status": "SYSTEM_ERROR", "output": str(exc), "time_ms": 0}


async def prepare_execution(code, language, input_data=""):
    if _use_isolate_backend() and not _should_use_native_fallback(language):
        return await _prepare_isolate_execution(code, language, input_data)
    return await _prepare_native_execution(code, language, input_data)


async def _execute_native(prepared, input_data, time_limit_ms):
    time_limit_sec = time_limit_ms / 1000.0
    exec_process = None

    try:
        start_time = time.perf_counter()
        stdin_mode = asyncio.subprocess.PIPE if prepared.language != "sql" else None
        exec_process = await asyncio.create_subprocess_exec(
            *prepared.run_cmd,
            cwd=prepared.work_dir,
            stdin=stdin_mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=set_limits if not IS_MAC else None,
        )

        if prepared.language == "sql":
            stdout, stderr = await asyncio.wait_for(exec_process.communicate(), timeout=time_limit_sec)
        else:
            input_bytes = input_data.encode() if input_data else b""
            stdout, stderr = await asyncio.wait_for(exec_process.communicate(input=input_bytes), timeout=time_limit_sec)

        execution_time_ms = int((time.perf_counter() - start_time) * 1000)
        out, err = _decode_output(stdout, stderr)

        if exec_process.returncode != 0:
            if exec_process.returncode == -9:
                return {"status": "MEMORY_LIMIT_EXCEEDED", "output": "Process killed (Likely Memory Limit).", "time_ms": execution_time_ms}
            return {"status": "RUNTIME_ERROR", "output": err or out, "time_ms": execution_time_ms}

        return {"status": "SUCCESS", "output": out, "time_ms": execution_time_ms}
    except asyncio.TimeoutError:
        if exec_process:
            try:
                exec_process.kill()
            except Exception:
                pass
        return {"status": "TLE", "output": "Time Limit Exceeded.", "time_ms": time_limit_ms}
    except Exception as exc:
        return {"status": "SYSTEM_ERROR", "output": str(exc), "time_ms": 0}


async def _execute_isolate(prepared, input_data, time_limit_ms):
    time_limit_sec = max(time_limit_ms / 1000.0, 0.1)
    wall_time_sec = max(time_limit_sec + 1.0, 2.0)
    input_bytes = None if prepared.language == "sql" else (input_data.encode() if input_data else b"")

    if prepared.language == "sql":
        setup_path = os.path.join(prepared.work_dir, "setup.sql")
        with open(setup_path, "w", encoding="utf-8") as handle:
            handle.write(input_data if input_data else "")

    returncode, meta, stdout, stderr = await _run_isolate(
        box_id=prepared.box_id,
        command_parts=prepared.run_cmd,
        time_limit_sec=time_limit_sec,
        wall_time_sec=wall_time_sec,
        process_limit=64,
        input_bytes=input_bytes,
        dir_rules=_get_isolate_dirs(prepared.language),
        env_vars=_get_isolate_env(prepared.language),
    )

    execution_time_ms = _meta_time_ms(meta, time_limit_ms if meta.get("status") in {"TO", "WT"} else 0)
    out, err = _decode_output(stdout, stderr)
    status_code = meta.get("status")
    exit_signal = meta.get("exitsig")

    if status_code in {"TO", "WT"}:
        return {"status": "TLE", "output": "Time Limit Exceeded.", "time_ms": execution_time_ms or time_limit_ms}

    if status_code == "XX":
        return {"status": "SYSTEM_ERROR", "output": err or out or "Isolate internal error.", "time_ms": execution_time_ms}

    if meta.get("cg-oom-killed") is not None:
        return {"status": "MEMORY_LIMIT_EXCEEDED", "output": err or out or "Memory Limit Exceeded.", "time_ms": execution_time_ms}

    if status_code == "SG" and exit_signal in {"9", "11"}:
        return {"status": "MEMORY_LIMIT_EXCEEDED", "output": err or out or "Process killed by sandbox.", "time_ms": execution_time_ms}

    if returncode != 0:
        return {"status": "RUNTIME_ERROR", "output": err or out or "Runtime error.", "time_ms": execution_time_ms}

    return {"status": "SUCCESS", "output": out, "time_ms": execution_time_ms}


async def execute_prepared(prepared, input_data, time_limit_ms):
    if prepared.backend == "isolate":
        return await _execute_isolate(prepared, input_data, time_limit_ms)
    return await _execute_native(prepared, input_data, time_limit_ms)


async def run_code_in_sandbox(code, language, input_data, time_limit_ms):
    prepared = await prepare_execution(code, language, input_data)
    if isinstance(prepared, dict):
        return prepared

    try:
        return await execute_prepared(prepared, input_data, time_limit_ms)
    finally:
        prepared.cleanup()
