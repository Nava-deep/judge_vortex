import os
import time
import asyncio
import tempfile
import resource
import platform

# 15 Supported Languages Configuration
LANG_CONFIG = {
    "python": ("main.py", None, ["python3", "main.py"]),
    "javascript": ("main.js", None, ["node", "main.js"]),
    "ruby": ("main.rb", None, ["ruby", "main.rb"]),
    "php": ("main.php", None, ["php", "main.php"]),
    "cpp": ("main.cpp", ["g++", "-O3", "main.cpp", "-o", "out"], ["./out"]),
    "c": ("main.c", ["gcc", "-O3", "main.c", "-o", "out"], ["./out"]),
    "go": ("main.go", ["go", "build", "-o", "out", "main.go"], ["./out"]),
    "rust": ("main.rs", ["rustc", "-O", "main.rs", "-o", "out"], ["./out"]),
    "swift": ("main.swift", ["swiftc", "-O", "main.swift", "-o", "out"], ["./out"]),
    "haskell": ("main.hs", ["ghc", "-O2", "main.hs", "-o", "out"], ["./out"]),
    "java": ("Main.java", ["javac", "Main.java"], ["java", "Main"]),
    "typescript": ("main.ts", ["npx", "tsc", "main.ts", "--outFile", "solution.js", "--target", "es6"], ["node", "solution.js"]),
    "csharp": ("Program.cs", ["mcs", "Program.cs", "-out:out.exe"], ["mono", "out.exe"]),
    "sql": ("query.sql", None, ["sqlite3", ":memory:", "-batch", "-init", "setup.sql", ".read query.sql"])
}

async def run_code_in_sandbox(code, language, input_data, time_limit_ms):
    MAX_MEMORY_BYTES = 256 * 1024 * 1024 
    is_mac = platform.system() == "Darwin"

    def set_limits():
        """Function to safely apply limits inside the child process"""
        try:
            # On Mac, RLIMIT_AS often causes instant failure. We use DATA/RSS or skip.
            if not is_mac:
                resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
            else:
                # Be more lenient on Mac local dev to prevent SYSTEM_ERROR
                resource.setrlimit(resource.RLIMIT_DATA, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
            
            # Universal limits
            resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except Exception:
            # If a specific limit fails, we'd rather the code run than crash with SYSTEM_ERROR
            pass

    if language not in LANG_CONFIG:
        return {"status": "SYSTEM_ERROR", "output": f"Language {language} not supported.", "time_ms": 0}

    filename, compile_cmd, run_cmd = LANG_CONFIG[language]
    time_limit_sec = time_limit_ms / 1000.0

    with tempfile.TemporaryDirectory() as temp_dir:
        source_path = os.path.join(temp_dir, filename)
        with open(source_path, "w") as f:
            f.write(code)

        if language == "sql":
            setup_path = os.path.join(temp_dir, "setup.sql")
            with open(setup_path, "w") as f:
                f.write(input_data if input_data else "")

        # 1. Async Compilation
        if compile_cmd:
            try:
                # Use a specific timeout for compilation (separate from execution)
                comp_process = await asyncio.create_subprocess_exec(
                    *compile_cmd, cwd=temp_dir,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(comp_process.communicate(), timeout=20.0)
                if comp_process.returncode != 0:
                    return {"status": "COMPILATION_ERROR", "output": stderr.decode(errors='replace').strip(), "time_ms": 0}
            except asyncio.TimeoutError:
                return {"status": "COMPILATION_ERROR", "output": "Compilation timed out.", "time_ms": 0}

        # 2. Async Execution
        start_time = time.perf_counter()
        exec_process = None
        try:
            # SQL doesn't use stdin; others do
            stdin_mode = asyncio.subprocess.PIPE if language != "sql" else None
            
            exec_process = await asyncio.create_subprocess_exec(
                *run_cmd, cwd=temp_dir,
                stdin=stdin_mode, 
                stdout=asyncio.subprocess.PIPE, 
                stderr=asyncio.subprocess.PIPE,
                # Use preexec_fn only on non-mac or with extreme care
                preexec_fn=set_limits if not is_mac else None 
            )
            
            if language == "sql":
                stdout, stderr = await asyncio.wait_for(exec_process.communicate(), timeout=time_limit_sec)
            else:
                input_bytes = input_data.encode() if input_data else b""
                stdout, stderr = await asyncio.wait_for(exec_process.communicate(input=input_bytes), timeout=time_limit_sec)
            
            execution_time_ms = int((time.perf_counter() - start_time) * 1000)
            
            if exec_process.returncode != 0:
                err_out = stderr.decode(errors='replace').strip() or stdout.decode(errors='replace').strip()
                # Detection for memory limits or crashes
                if exec_process.returncode == -9:
                    return {"status": "MEMORY_LIMIT_EXCEEDED", "output": "Process killed (Likely Memory Limit).", "time_ms": execution_time_ms}
                return {"status": "RUNTIME_ERROR", "output": err_out, "time_ms": execution_time_ms}

            return {"status": "SUCCESS", "output": stdout.decode(errors='replace').strip(), "time_ms": execution_time_ms}

        except asyncio.TimeoutError:
            if exec_process:
                try: exec_process.kill()
                except: pass
            return {"status": "TLE", "output": "Time Limit Exceeded.", "time_ms": time_limit_ms}
        except Exception as e:
            return {"status": "SYSTEM_ERROR", "output": str(e), "time_ms": 0}