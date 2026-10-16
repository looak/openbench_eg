import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
MANAGE = ROOT / "manage.py"
CONFIG_FILE = ROOT / "Config" / "config.json"


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_manage(args, env):
    cmd = [sys.executable, str(MANAGE)] + args
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def create_enabled_user(env, username, password):
    shell_cmd = (
        "from django.contrib.auth.models import User; "
        "from OpenBench.models import Profile; "
        f"u, _ = User.objects.get_or_create(username='{username}'); "
        f"u.set_password('{password}'); u.save(); "
        "p, _ = Profile.objects.get_or_create(user=u); "
        "p.enabled = True; p.save()"
    )
    run_manage(["shell", "-c", shell_cmd], env)

def create_active_workload(env, author, engine_name, book_name):
    shell_cmd = (
        "from OpenBench.models import Engine, Test; "
        f"dev = Engine.objects.create(name='{engine_name}-dev', source='https://example.invalid/dev', sha='d'*40, bench=1); "
        f"base = Engine.objects.create(name='{engine_name}-base', source='https://example.invalid/base', sha='b'*40, bench=1); "
        "Test.objects.create("
        f"author='{author}', "
        f"book_name='{book_name}', "
        "dev=dev, "
        "base=base, "
        "dev_repo='https://example.invalid/dev', "
        f"dev_engine='{engine_name}', "
        "dev_options='Threads=1 Hash=16', "
        "dev_network='', "
        "dev_netname='', "
        "dev_time_control='N=1000', "
        "base_repo='https://example.invalid/base', "
        f"base_engine='{engine_name}', "
        "base_options='Threads=1 Hash=16', "
        "base_network='', "
        "base_netname='', "
        "base_time_control='N=1000', "
        "workload_size=2, "
        "priority=1, "
        "throughput=100, "
        "approved=True, "
        "awaiting=False, "
        "finished=False, "
        "deleted=False"
        ")"
    )
    run_manage(["shell", "-c", shell_cmd], env)


def wait_for_server(base_url, username, password, timeout_seconds=30):
    deadline = time.time() + timeout_seconds
    payload = {"username": username, "password": password}
    url = f"{base_url}/clientVersionRef/"

    while time.time() < deadline:
        try:
            response = requests.post(url, data=payload, timeout=2)
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)

    raise RuntimeError("Server failed to start within timeout")


def assert_json(response, endpoint):
    response.raise_for_status()
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError(f"{endpoint} returned non-JSON body: {response.text[:300]}") from exc


def pick_engine_for_workload(build_info):
    for engine_name, info in build_info.items():
        if info.get("private"):
            continue
        systems = info.get("systems") or []
        compilers = info.get("compilers") or []
        if systems and compilers:
            return engine_name, info
    raise RuntimeError("No public engine with systems/compilers found in clientGetBuildInfo")


def run_smoke(base_url, env, username, password):
    auth_payload = {"username": username, "password": password}

    version_ref = assert_json(
        requests.post(f"{base_url}/clientVersionRef/", data=auth_payload, timeout=10),
        "clientVersionRef",
    )
    for key in ("client_version", "client_repo_url", "client_repo_ref"):
        if key not in version_ref:
            raise RuntimeError(f"clientVersionRef missing key '{key}'")

    runner_ref = assert_json(
        requests.post(f"{base_url}/clientMatchRunnerVersionRef/", data=auth_payload, timeout=10),
        "clientMatchRunnerVersionRef",
    )
    for key in ("fastchess_min_version", "fastchess_repo_url", "fastchess_repo_ref"):
        if key not in runner_ref:
            raise RuntimeError(f"clientMatchRunnerVersionRef missing key '{key}'")

    build_info = assert_json(
        requests.get(f"{base_url}/clientGetBuildInfo/", timeout=10),
        "clientGetBuildInfo",
    )
    engine_name, engine_build = pick_engine_for_workload(build_info)

    config_data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not config_data.get("books"):
        raise RuntimeError("config.json has no books configured")
    create_active_workload(env, username, engine_name, config_data["books"][0])

    system_info = {
        "compilers": {
            engine_name: [engine_build["compilers"][0], "smoke"],
        },
        "tokens": {},
        "cpu_flags": engine_build.get("cpuflags", []),
        "cpu_name": "smoke-cpu",
        "os_name": engine_build["systems"][0],
        "os_ver": "smoke",
        "python_ver": "3.x",
        "mac_address": "001122334455",
        "logical_cores": 8,
        "physical_cores": 4,
        "ram_total_mb": 8192,
        "machine_id": None,
        "machine_name": "smoke-worker",
        "concurrency": 1,
        "sockets": 1,
        "syzygy_max": 0,
        "noisy": False,
        "focus": [],
        "client_ver": version_ref["client_version"],
    }

    worker_info_payload = {
        "username": username,
        "password": password,
        "system_info": json.dumps(system_info),
    }
    worker_info = assert_json(
        requests.post(f"{base_url}/clientWorkerInfo/", data=worker_info_payload, timeout=10),
        "clientWorkerInfo",
    )

    machine_id = worker_info.get("machine_id")
    secret = worker_info.get("secret")
    if not machine_id or not secret:
        raise RuntimeError(f"clientWorkerInfo missing machine credentials: {worker_info}")

    workload_payload = {
        "machine_id": machine_id,
        "secret": secret,
        "blacklist": [],
    }
    workload_response = assert_json(
        requests.post(f"{base_url}/clientGetWorkload/", data=workload_payload, timeout=10),
        "clientGetWorkload",
    )
    if "error" in workload_response:
        raise RuntimeError(f"clientGetWorkload returned error: {workload_response['error']}")
    if "workload" not in workload_response:
        raise RuntimeError(f"clientGetWorkload returned no workload: {workload_response}")

    print("SMOKE PASS")
    print(f"  client_version={version_ref['client_version']}")
    print(f"  machine_id={machine_id}")
    print(f"  workload_id={workload_response['workload']['test']['id']}")


def main():
    parser = argparse.ArgumentParser(description="Run isolated OpenBench server/client endpoint smoke test")
    parser.add_argument("--username", default="smoke")
    parser.add_argument("--password", default="smoke-pass")
    parser.add_argument("--port", type=int, default=0, help="Port for local runserver, 0 chooses a free port")
    args = parser.parse_args()

    port = args.port or find_free_port()
    server_url = f"http://127.0.0.1:{port}"

    temp_root = tempfile.mkdtemp(prefix="openbench-smoke-")
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "OpenSite.settings_isolated"
    env["OPENBENCH_ISOLATED_ROOT"] = temp_root

    server_proc = None
    try:
        run_manage(["migrate", "--noinput"], env)
        create_enabled_user(env, args.username, args.password)

        cmd = [sys.executable, str(MANAGE), "runserver", f"127.0.0.1:{port}", "--noreload"]
        server_proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        wait_for_server(server_url, args.username, args.password)
        run_smoke(server_url, env, args.username, args.password)

    finally:
        if server_proc is not None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=10)
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    main()
