import os
import time
import asyncio
import tempfile
import resource

LANG_CONFIG = {
    # Interpreted Languages
    "python": ("main.py", None, ["python3", "main.py"]),
    "javascript": ("main.js", None, ["node", "main.js"]),
    "ruby": ("main.rb", None, ["ruby", "main.rb"]),
    "php": ("main.php", None, ["php", "main.php"]),
    
    # Compiled Languages (Binary output)
    "cpp": ("main.cpp", ["g++", "-O3", "main.cpp", "-o", "out"], ["./out"]),
    "c": ("main.c", ["gcc", "-O3", "main.c", "-o", "out"], ["./out"]),
    "go": ("main.go", ["go", "build", "-o", "out", "main.go"], ["./out"]),
    "rust": ("main.rs", ["rustc", "-O", "main.rs", "-o", "out"], ["./out"]),
    "swift": ("main.swift", ["swiftc", "-O", "main.swift", "-o", "out"], ["./out"]),
    "haskell": ("main.hs", ["ghc", "-O2", "main.hs", "-o", "out"], ["./out"]),
    
    # JVM / Bytecode Languages
    "java": ("Main.java", ["javac", "Main.java"], ["java", "Main"]),
    
    # Transpiled / Special Runtimes
    "typescript": ("main.ts", ["npx", "tsc", "main.ts", "--outFile", "solution.js", "--target", "es6", "--lib", "es6,dom"], ["node", "solution.js"]),
    "csharp": ("Program.cs", ["mcs", "Program.cs", "-out:out.exe"], ["mono", "out.exe"]),

    # DB (SQL) - Standard configuration
    "sql": ("query.sql", None, ["sqlite3", ":memory:", "-batch", "-init", "setup.sql"])
}

async def run_code_in_sandbox(code, language, input_data, time_limit_ms):
    # Set a 256MB Memory Limit (in bytes)
    MAX_MEMORY_BYTES = 256 * 1024 * 1024 

    def set_limits():
        """This function runs inside the subprocess right before the code starts"""
        # Set Memory Limit (Address Space)
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
        # Prevent the code from creating massive files (1MB limit)
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        # Disable core dumps to save disk space
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    if language not in LANG_CONFIG:
        return {"status": "SYSTEM_ERROR", "output": f"Language {language} not supported.", "time_ms": 0}

    filename, compile_cmd, run_cmd = LANG_CONFIG[language]
    time_limit_sec = time_limit_ms / 1000.0

    with tempfile.TemporaryDirectory() as temp_dir:
        source_path = os.path.join(temp_dir, filename)
        
        # Write the user's code to the source file
        with open(source_path, "w") as f:
            f.write(code)

        # SQL SPECIAL HANDLING:
        # We write the input_data (schema) to setup.sql so sqlite3 can initialize with it.
        if language == "sql":
            setup_path = os.path.join(temp_dir, "setup.sql")
            with open(setup_path, "w") as f:
                f.write(input_data if input_data else "")
            
            # We override the run_cmd to execute the user's query file and format the output
            # -header: shows column names, -box: makes it look like a professional table
            run_cmd = ["sqlite3", ":memory:", "-batch", "-init", "setup.sql", ".read query.sql"]

        # 1. Async Compilation
        if compile_cmd:
            try:
                comp_process = await asyncio.create_subprocess_exec(
                    *compile_cmd, cwd=temp_dir,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(comp_process.communicate(), timeout=15.0)
                if comp_process.returncode != 0:
                    return {"status": "COMPILATION_ERROR", "output": stderr.decode().strip(), "time_ms": 0}
            except asyncio.TimeoutError:
                return {"status": "COMPILATION_ERROR", "output": "Compilation timed out.", "time_ms": 0}

        # 2. Async Execution with Memory Limits
        start_time = time.perf_counter()
        try:
            # For SQL, we don't need stdin since we used -init and .read
            stdin_mode = asyncio.subprocess.PIPE if language != "sql" else None
            
            exec_process = await asyncio.create_subprocess_exec(
                *run_cmd, cwd=temp_dir,
                stdin=stdin_mode, 
                stdout=asyncio.subprocess.PIPE, 
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=set_limits
            )
            
            if language == "sql":
                stdout, stderr = await asyncio.wait_for(exec_process.communicate(), timeout=time_limit_sec)
            else:
                input_bytes = input_data.encode() if input_data else b""
                stdout, stderr = await asyncio.wait_for(exec_process.communicate(input=input_bytes), timeout=time_limit_sec)
            
            execution_time_ms = int((time.perf_counter() - start_time) * 1000)

            if exec_process.returncode != 0:
                status = "RUNTIME_ERROR"
                error_msg = stderr.decode().strip() or stdout.decode().strip()
                
                # SIGKILL (-9) usually means OOM on Linux/Mac
                if exec_process.returncode == -9 or "MemoryError" in error_msg:
                    status = "MEMORY_LIMIT_EXCEEDED"
                    error_msg = "Process exceeded 256MB memory limit."
                
                return {"status": status, "output": error_msg, "time_ms": execution_time_ms}

            # Filter out the SQLite "init" messages if any, though -batch usually hides them
            output = stdout.decode().strip()
            return {"status": "SUCCESS", "output": output, "time_ms": execution_time_ms}

        except asyncio.TimeoutError:
            try: exec_process.kill()
            except: pass
            return {"status": "TLE", "output": "Time Limit Exceeded.", "time_ms": time_limit_ms}
        except Exception as e:
            return {"status": "SYSTEM_ERROR", "output": str(e), "time_ms": 0}