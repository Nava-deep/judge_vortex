import docker
import time
import os
import uuid
import shutil
import re

client = docker.from_env()

LANG_CONFIG = {
    "python": ("python:3.11-slim", 'start=$(date +%s%3N); python3 {file} < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err', "py"),
    "javascript": ("node:18-slim", 'start=$(date +%s%3N); node {file} < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err', "js"),
    "ruby": ("ruby:3.2-slim", 'start=$(date +%s%3N); ruby {file} < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err', "rb"),
    "php": ("php:8.2-cli", 'start=$(date +%s%3N); php {file} < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err', "php"),
    "cpp": ("gcc:latest", 'g++ -O3 {file} -o out 2>&1 && { start=$(date +%s%3N); ./out < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err; }', "cpp"),
    "c": ("gcc:latest", 'gcc -O3 {file} -o out 2>&1 && { start=$(date +%s%3N); ./out < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err; }', "c"),
    "java": ("vortex-java-warmed", 'javac Main.java 2>&1 && { start=$(date +%s%3N); java Main < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err; }', "java"),
    "typescript": ("node:18-slim", 'npx -y tsc {file} --outFile solution.js 2>&1 && { start=$(date +%s%3N); node solution.js < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err; }', "ts"),
    "go": ("golang:1.21-alpine", 'go build -o out {file} 2>&1 && { start=$(date +%s%3N); ./out < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err; }', "go"),
    "rust": ("vortex-rust-warmed", 'cargo build --release --offline 2>&1 && { start=$(date +%s%3N); cargo run --release --offline < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err; }', "rs"),
    "csharp": ("vortex-csharp-warmed", 'dotnet build -c Release -nologo -v q 2>&1 && { start=$(date +%s%3N); dotnet exec bin/Release/net7.0/Vortex.dll < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err; }', "cs"),
    "swift": ("swift:latest", 'swiftc {file} -o out 2>&1 && { start=$(date +%s%3N); ./out < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err; }', "swift"),
    "haskell": ("haskell:9.4-slim", 'ghc -O2 {file} -o out 2>&1 && { start=$(date +%s%3N); ./out < {infile} 2>&1; err=$?; echo ""; echo "[VORTEX_TIME:$(($(date +%s%3N)-start))]"; exit $err; }', "hs"),
}

def run_code_in_sandbox(code, language, input_data, time_limit_ms):
    if language not in LANG_CONFIG:
        return {"status": "ERROR", "output": f"Language {language} not supported", "time_ms": 0}

    image, cmd_template, ext = LANG_CONFIG[language]
    job_id = str(uuid.uuid4())
    host_path = f"/tmp/vortex_{job_id}"
    os.makedirs(host_path, exist_ok=True)
    
    # 1. Map to Pre-Warmed Internal Paths
    internal_workdir = '/code'
    if language == "java":
        filename = "Main.java"
    elif language == "csharp":
        filename = "Program.cs"
        internal_workdir = '/app/Vortex' 
    elif language == "rust":
        filename = "main.rs"
        internal_workdir = '/app/vortex_app/src'
    else:
        filename = f"solution.{ext}"

    # 2. Write Files
    file_path = os.path.join(host_path, filename)
    with open(file_path, "w") as f:
        f.write(code)

    input_file = os.path.join(host_path, "input.txt")
    with open(input_file, "w") as f:
        f.write(input_data if input_data else "")

    # 3. Construct the Command
    # 🟢 NEW: Pass the correct input path safely into the template
    infile_path = "/code/input.txt" if language in ["java", "csharp", "rust"] else "input.txt"
    final_command = f"sh -c '{cmd_template.format(file=filename, infile=infile_path)}'"

    start_time = time.time()
    
    try:
        # 4. Volume Mounting Logic
        volumes = {host_path: {'bind': '/code', 'mode': 'rw'}}
        if language in ["csharp", "rust", "java"]:
            volumes[file_path] = {'bind': f"{internal_workdir}/{filename}", 'mode': 'rw'}

        container = client.containers.run(
            image=image,
            command=final_command,
            volumes=volumes,
            working_dir='/code' if language not in ["csharp", "rust"] else internal_workdir,
            detach=True,
            mem_limit="512m",
            network_disabled=True,
            stdout=True,
            stderr=True,
        )

        try:
            result_status = container.wait(timeout=time_limit_ms / 1000.0)
            execution_time_ms = int((time.time() - start_time) * 1000)
            logs_raw = container.logs()
        except:
            container.kill()
            container.remove(force=True)
            # Instantly return TLE if the container times out
            return {"status": "TIME_LIMIT_EXCEEDED", "output": "Execution timed out.", "time_ms": time_limit_ms}

        container.remove(force=True)

        # --- 5. Result Analysis & Error Splitting ---
        logs = logs_raw.decode('utf-8')
        
        # Default to SUCCESS if exit code is 0, else assume RUNTIME_ERROR
        status = "SUCCESS" if result_status['StatusCode'] == 0 else "RUNTIME_ERROR"
        
        # 🔍 EXTRACT REAL RUNTIME
        match = re.search(r'\[VORTEX_TIME:(\d+)\]', logs)
        if match:
            # Overwrite the Docker time with the pure Code execution time!
            execution_time_ms = int(match.group(1))
            
            # Remove the tag and empty lines from the user's output
            logs = re.sub(r'\n?\[VORTEX_TIME:\d+\]\n?', '', logs)
        else:
            # 🚨 THE SMART CHECK: If there is no time tag AND an error occurred, 
            # it means the code failed to compile!
            if result_status['StatusCode'] != 0:
                status = "COMPILATION_ERROR"

        final_output = logs.strip()
        if not final_output:
            final_output = f"Process exited with {status} but no output."

        return {
            "status": status, 
            "output": final_output, 
            "time_ms": execution_time_ms
        }

    except Exception as e:
        return {"status": "SYSTEM_ERROR", "output": str(e), "time_ms": 0}